"""蕴 · 桌面应用入口（把面板包成一个真正的软件窗口）。

为什么这样设计
--------------
原生窗口用 pywebview（Windows 上走 Edge WebView2），但它依赖 pythonnet，
而 **pythonnet 目前不支持 Python 3.14**（实测 `Python.Runtime.dll` ABI 解析失败）。
本机又只有 3.14，所以这里做**能力探测 + 优雅降级**，而不是赌一条路：

    1. 优先 pywebview  -> 我们自己完全掌控的原生窗口（无浏览器痕迹）
    2. 退化为 Edge/Chrome 的 `--app=URL` 模式
       -> 无地址栏、无标签页、独立任务栏图标、独立用户配置目录，观感接近原生应用
    3. 再退化为默认浏览器打开（最坏情况，至少能用）

用法：
    pythonw -m neko.app            # 双击「启动蕴.bat」走的就是这条，无控制台窗口
    python  -m neko.app --browser  # 强制用 Edge/Chrome 应用窗口
    python  -m neko.app --web      # 只启服务，不自动开窗口
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT, ensure_config_file, load_config

WINDOW_TITLE = "蕴 · 猫娘伴友"
WINDOW_SIZE = (1200, 840)
MIN_SIZE = (900, 620)


# ------------------------------------------------------------------ 服务

class ServerThread:
    """在后台线程里跑 uvicorn，主线程留给窗口。"""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.thread: threading.Thread | None = None
        self.server: Any = None
        self.error: Exception | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        import uvicorn

        from .server import app

        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)

        def run() -> None:
            try:
                self.server.run()
            except Exception as exc:  # noqa: BLE001
                self.error = exc

        self.thread = threading.Thread(target=run, name="nekopal-server", daemon=True)
        self.thread.start()

    def wait_ready(self, timeout: float = 30.0) -> bool:
        """等内置服务**真的能用 HTTP 应答**，而不是只等端口被占用。

        早先只做 socket connect 会踩一个真实的竞态：uvicorn 先绑定端口、
        随后才开始处理请求。窗口若在"端口已开但服务未就绪"的那一瞬间打开，
        页面加载会失败，而且失败后不会自动恢复 ——
        结果就是桌面上一个标题为 URL、内容空白的窗口。
        所以这里必须真的请求一次 /api/status 并要求 200。
        """
        deadline = time.time() + timeout
        last = "未开始"
        while time.time() < deadline:
            if self.error is not None:
                raise RuntimeError(f"服务启动失败：{self.error}")
            try:
                resp = httpx.get(f"{self.url}/api/status", timeout=2.0)
                if resp.status_code == 200:
                    return True
                last = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            time.sleep(0.2)
        print(f"[app] 等待服务就绪超时（最后状态：{last}）")
        return False

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=5.0)


# ------------------------------------------------------------------ 窗口后端

def pywebview_ready() -> tuple[bool, str]:
    """探测 pywebview 在当前解释器上是否真的能用（pythonnet 是常见坑）。"""
    try:
        import webview  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"未安装 pywebview（{exc}）"
    try:
        # Windows 上真正干活的是 winforms/edgechromium 后端，它依赖 pythonnet
        from webview.platforms import winforms  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"pywebview 后端不可用（{type(exc).__name__}: {exc}）"
    return True, "可用"


def find_browser() -> tuple[str, str] | None:
    """找一个支持 --app 模式的 Chromium 浏览器。返回 (路径, 名字)。"""
    candidates = [
        (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "Edge"),
        (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "Edge"),
        (os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"), "Edge"),
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "Chrome"),
        (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "Chrome"),
        (os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"), "Chrome"),
    ]
    for path, name in candidates:
        if path and Path(path).is_file():
            return path, name
    # 注册表兜底
    try:
        import winreg

        for hive, key, exe in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe", "Edge"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", "Chrome"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    path = winreg.QueryValue(k, None)
                    if path and Path(path).is_file():
                        return path, exe
            except OSError:
                continue
    except ImportError:
        pass
    return None


def run_app_window(
    url: str, profile_dir: Path, *, watch: Any = None, disable_sandbox: bool = True
) -> bool:
    """用 Edge/Chrome 的 --app 模式开一个无边框应用窗口。窗口关掉才返回。

    watch 是一个无参可调用对象，返回 False 表示"内置服务已经没了"。
    这时必须**主动关掉窗口** —— 否则桌面上会留一个显示死页面的僵尸窗口，
    用户会以为软件还在跑。

    disable_sandbox 会加 --no-sandbox。这不是随手加的：
    实测在受限的 Windows 会话里，Chromium 的渲染进程沙箱初始化失败
    （日志里能看到 EdgeUpdate 注册表访问被拒），结果是**窗口一片空白**。
    加了这个开关页面才正常渲染。因为本窗口只加载 127.0.0.1 上我们自己
    的页面、不加载任何外部内容，所以这个取舍是可接受的；
    如果你的环境不需要，把 config.json 的 window.disable_sandbox 设成 false。
    """
    found = find_browser()
    if not found:
        return False
    exe, name = found
    profile_dir.mkdir(parents=True, exist_ok=True)
    debug_port = (os.environ.get("NEKOPAL_DEBUG_PORT") or "").strip()
    args = [
        exe,
        f"--app={url}",
        f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
        f"--user-data-dir={profile_dir}",   # 独立配置，不碰你平时的浏览器会话
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeSidebarV2,msEdgeCollections",
        "--disable-sync",
    ]
    if debug_port:
        # 排障用：设了 NEKOPAL_DEBUG_PORT 就能用 CDP 查看这个窗口的真实页面状态
        args.insert(1, f"--remote-debugging-port={debug_port}")
        print(f"[app] 已开启调试端口 {debug_port}")
    if disable_sandbox:
        args.append("--no-sandbox")
        # 不加 --test-type 时，Edge 会在窗口顶部弹一条
        # "你使用的是不受支持的命令行标志: --no-sandbox" 的警告条。
        # 本窗口只加载 127.0.0.1 上我们自己的页面，不需要拿这条警告吓用户。
        args.append("--test-type")
        print("[app] 已关闭 Chromium 沙箱（受限会话下不加这个会渲染空白页）")
    print(f"[app] 使用 {name} 应用窗口打开：{url}")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while True:
            if proc.poll() is not None:
                return True                      # 用户自己关了窗口
            if watch is not None and not watch():
                print("[app] 内置服务已停止，关闭窗口以免留下死页面")
                proc.terminate()
                return True
            time.sleep(0.5)
    except KeyboardInterrupt:
        proc.terminate()
    return True


def run_native_window(url: str) -> bool:
    """pywebview 原生窗口。失败返回 False 交给上层降级。"""
    import webview

    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
        text_select=True,
        confirm_close=False,
    )
    print("[app] 使用 pywebview 原生窗口")
    webview.start(private_mode=False, storage_path=str(ROOT / "data" / "webview"))
    del window
    return True


# ------------------------------------------------------------------ 主流程

def main() -> int:
    parser = argparse.ArgumentParser(description="蕴 · 猫娘伴友（桌面应用）")
    parser.add_argument("--browser", action="store_true", help="强制用浏览器应用窗口，不用 pywebview")
    parser.add_argument("--web", action="store_true", help="只启服务，不自动开窗口")
    parser.add_argument("--port", type=int, default=None, help="覆盖配置里的端口")
    args = parser.parse_args()

    # 不管从哪里、用什么工作目录启动，都统一切到项目根目录，
    # 这样 web/、data/、config.json 这些相对路径一定找得到。
    try:
        os.chdir(ROOT)
    except OSError:
        pass

    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    ensure_config_file()
    cfg = load_config()
    host = cfg["server"]["host"]
    port = int(args.port or cfg["server"]["port"])

    server = ServerThread(host, port)
    server.start()
    if not server.wait_ready():
        print("[app] 服务启动超时，看看是不是端口被占了：", server.url)
        return 1

    url = server.url
    # 排障/截图用：NEKOPAL_START_VIEW=settings 可以让窗口直接打开某个页面；
    # 带后缀（如 settings-bili）会再滚到对应小节。
    start_view = (os.environ.get("NEKOPAL_START_VIEW") or "").strip()
    if start_view.split("-")[0] in ("chat", "notes", "memory", "music", "settings"):
        url = f"{url}/#{start_view}"
    persona = cfg.get("persona", {})
    print("=" * 58)
    print(f"  {persona.get('name', '蕴')} · 猫娘伴友 已启动")
    print(f"  地址： {url}")
    if not cfg["llm"]["api_key"]:
        print("  [注意] 还没配置 DeepSeek API Key，现在只能离线陪你说话")
        print(f"         请编辑 {ROOT / 'config.json'} 的 llm.api_key")
    print("=" * 58)

    if args.web:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        server.stop()
        return 0

    opened = False
    server_alive = lambda: server.thread is not None and server.thread.is_alive()  # noqa: E731
    try:
        if not args.browser:
            ready, reason = pywebview_ready()
            if ready:
                try:
                    opened = run_native_window(url)
                except Exception as exc:  # noqa: BLE001
                    print(f"[app] 原生窗口失败（{exc}），改用浏览器应用窗口")
                    opened = False
            else:
                print(f"[app] 跳过原生窗口：{reason}")
        if not opened:
            # 沙箱开关：环境变量优先于配置，方便临时排障
            env_flag = (os.environ.get("NEKOPAL_DISABLE_SANDBOX") or "").strip().lower()
            if env_flag in ("0", "false", "no"):
                sandbox_off = False
            elif env_flag in ("1", "true", "yes"):
                sandbox_off = True
            else:
                sandbox_off = bool((cfg.get("window") or {}).get("disable_sandbox", True))
            opened = run_app_window(
                url,
                ROOT / "data" / "browser_profile",
                watch=server_alive,
                disable_sandbox=sandbox_off,
            )
        if not opened:
            print("[app] 没找到 Edge/Chrome，用默认浏览器打开")
            webbrowser.open(url)
            try:
                while server_alive():
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    finally:
        server.stop()
        print("[app] 已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
