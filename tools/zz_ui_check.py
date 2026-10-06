"""真点一下的界面自检：折叠键、试听按钮、声音总开关联动「朗读」键。

为什么要专门一个脚本
--------------------
静态截图只能证明"长什么样"，证明不了"点了有没有反应"。
而这次改动最核心的正是**点击行为**：

1. 设置/音乐的小节默认收起，点标题才展开；
2. 展开后里面的「试听」按钮必须还能用 —— 折叠是把节点**搬**进 `.fold-body`，
   如果哪天有人改成重写 innerHTML，监听器会全丢，按钮就变成点不动的摆设
   （这种坏法截图上完全看不出来）；
3. 声音总开关关掉后，对话页顶部的「朗读」键必须消失。

做法：本机 Edge 的 DevTools 协议（CDP）。项目里没有 websocket 库，
所以这里**手写了一个最小 WebSocket 客户端**（握手 + 掩码文本帧 + 收帧），
够用就好，不引依赖。

用法：
    python tools\\zz_ui_check.py [--port 9333] [--url http://127.0.0.1:8790/#settings]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]


# --------------------------------------------------------------------------- WebSocket

class WS:
    """只实现用得上的那点 RFC6455：客户端掩码文本帧、收帧、忽略 ping。"""

    def __init__(self, url: str) -> None:
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {u.path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("CDP 握手时连接被关闭")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("CDP 握手失败：" + head[:200].decode("latin1"))
        self.buf = rest

    def send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        n = len(data)
        header = bytearray([0x81])                    # FIN + text
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("CDP 连接断了")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> dict:
        while True:
            b0, b1 = self._read(2)
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length) if length else b""
            if opcode == 0x8:
                raise RuntimeError("CDP 主动关闭了连接")
            if opcode in (0x9, 0xA):          # ping/pong 忽略
                continue
            return json.loads(payload.decode("utf-8", "replace"))

    def call(self, method: str, params: dict | None = None, timeout: float = 180.0) -> dict:
        self._id = getattr(self, "_id", 0) + 1
        rid = self._id
        self.send({"id": rid, "method": method, "params": params or {}})
        self.sock.settimeout(timeout)
        while True:
            msg = self.recv()
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method} 出错：{msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str, timeout: float = 180.0):
        res = self.call("Runtime.evaluate", {
            "expression": expr, "awaitPromise": True, "returnByValue": True,
        }, timeout=timeout)
        if res.get("exceptionDetails"):
            raise RuntimeError("页面里报错：" +
                               json.dumps(res["exceptionDetails"], ensure_ascii=False)[:400])
        return res.get("result", {}).get("value")


# --------------------------------------------------------------------------- 检查项

CHECKS: list[tuple[str, str, str]] = [
    # (名字, 期望, JS 表达式)
    ("折叠默认收起",
     "0",
     "document.querySelectorAll('#view-settings .fold-body.open').length"),

    ("点标题能展开、再点能收起",
     "true,true",
     """(() => {
       const h = document.querySelector('#view-settings h3.sec.fold-head');
       const b = h.nextElementSibling;
       h.click(); const opened = b.classList.contains('open');
       h.click(); const closed = !b.classList.contains('open');
       h.click();                       // 留着展开，后面的检查要用
       return opened + ',' + closed;
     })()"""),

    ("「声音」小节里同时装着音色试听和 RVC",
     "true,true,true,true",
     """(() => {
       const heads = [...document.querySelectorAll('#view-settings h3.sec')];
       const h = heads.find(x => x.textContent.includes('声音'));
       h.click();
       const b = h.nextElementSibling;
       return [b.classList.contains('open'), !!b.querySelector('#voice-list'),
               !!b.querySelector('#rvc-list'), !!b.querySelector('#voice-on')].join(',');
     })()"""),

    ("音乐页也折叠",
     "true,0",
     """(() => {
       const n = document.querySelectorAll('#view-music h3.sec.fold-head').length;
       const open = document.querySelectorAll('#view-music .fold-body.open').length;
       return (n > 0) + ',' + open;
     })()"""),
]


def poll(ws: WS, expr: str, until, timeout: float, step: float = 0.8):
    """反复求值直到 until(值) 成立或超时。**不用 CDP 的 awaitPromise** ——
    实测它在这个 Edge 上不生效（Promise 会被当对象序列化成 `{}`），
    所以等待逻辑放在 Python 这边，反而更好调试。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = ws.eval(expr)
        if until(last):
            return last
        time.sleep(step)
    return last


def check_preview_button(ws: WS) -> tuple[bool, str]:
    """展开后点一次「试听」：要真的合成出音频（监听器没被折叠弄丢）。"""
    hint = "document.querySelector('#voice-hint').textContent || ''"
    ws.eval("document.querySelector('#voice-list [data-preview]').click(); 'clicked'")
    got = poll(ws, hint,
               lambda t: isinstance(t, str) and (t.startswith("试听：") or t.startswith("试听失败")),
               timeout=180)
    if isinstance(got, str) and got.startswith("试听："):
        src = ws.eval("(() => { const a = document.querySelector('audio');"
                      " return a ? a.src.split('/').pop() : ''; })()")
        return True, f"{got}｜音频 {src}"
    return False, f"试听没成功：{got!r}"


def check_voice_switch_hides_auto_read(ws: WS) -> tuple[bool, str]:
    """点「声音」总开关：关掉后对话页的「朗读」键要藏起来，开回来要恢复。"""
    hidden = ("(() => { const av = document.querySelector('#auto-voice');"
              " return av ? av.closest('.switch').classList.contains('hidden') : null; })()")
    checked = "document.querySelector('#voice-on').checked"
    if ws.eval(checked) is not True:
        ws.eval("document.querySelector('#voice-on').click()")   # 先保证是开着的
        poll(ws, checked, lambda v: v is True, timeout=20)
    was_hidden = ws.eval(hidden)

    ws.eval("document.querySelector('#voice-on').click()")       # 关
    off_hidden = poll(ws, hidden, lambda v: v is True, timeout=30)
    off_state = ws.eval(checked)

    ws.eval("document.querySelector('#voice-on').click()")       # 再开回来
    on_hidden = poll(ws, hidden, lambda v: v is False, timeout=30)

    ok = (was_hidden is False) and (off_state is False) and (off_hidden is True) and (on_hidden is False)
    return ok, (f"初始朗读键隐藏={was_hidden}｜关后开关={off_state} 朗读键隐藏={off_hidden}"
                f"｜开回后朗读键隐藏={on_hidden}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--url", default="http://127.0.0.1:8790/#settings")
    ap.add_argument("--keep", action="store_true", help="留着浏览器窗口，方便人肉看")
    args = ap.parse_args()

    edge = next((p for p in EDGE_CANDIDATES if p and Path(p).is_file()), None)
    if not edge:
        print("找不到 Edge")
        return 2
    profile = ROOT / "data" / "_ui_check_profile"
    profile.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen([
        edge, f"--app={args.url}", "--window-size=1280,1000",
        f"--user-data-dir={profile}", f"--remote-debugging-port={args.port}",
        "--no-first-run", "--no-default-browser-check", "--disable-sync",
        "--no-sandbox", "--test-type",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = None
    for _ in range(60):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json", timeout=2) as r:
                targets = json.loads(r.read().decode("utf-8", "replace"))
            pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                ws_url = pages[0]["webSocketDebuggerUrl"]
                break
        except Exception:  # noqa: BLE001
            continue
    if not ws_url:
        proc.kill()
        print("连不上 CDP 调试端口")
        return 2

    ws = WS(ws_url)
    ws.call("Runtime.enable")
    time.sleep(3)                      # 等页面 boot 完（折叠是在 boot 里做的）

    failed = 0
    total = len(CHECKS) + 2
    try:
        for name, expect, expr in CHECKS:
            try:
                got = ws.eval(expr)
            except Exception as exc:  # noqa: BLE001
                got = f"异常 {exc}"
            ok = str(got) == expect
            failed += 0 if ok else 1
            print(f"[{'OK' if ok else 'FAIL'}]  {name}")
            print(f"        期望 {expect!r} / 实际 {got!r}")

        for name, fn in (("展开后「试听」按钮仍然可用（监听器没被折叠弄丢）", check_preview_button),
                         ("对话页「朗读」键跟着声音总开关走", check_voice_switch_hides_auto_read)):
            try:
                ok, detail = fn(ws)
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"异常 {type(exc).__name__}: {exc}"
            failed += 0 if ok else 1
            print(f"[{'OK' if ok else 'FAIL'}]  {name}")
            print(f"        {detail}")
    finally:
        if not args.keep:
            proc.kill()
            time.sleep(1)
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(profile)],
                           capture_output=True)

    print("-" * 60)
    print(f"通过 {total - failed} / {total}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
