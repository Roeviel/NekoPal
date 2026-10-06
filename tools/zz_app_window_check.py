"""直接检查**正在运行的应用窗口**（而不是另开一个浏览器）里的新版界面是否生效。

在此之前踩过的坑：`data/browser_profile` 里那份过期缓存让窗口一直渲染旧界面，
重启也没用（浏览器根本没去问服务器）。所以这个脚本读的就是应用自己那个窗口，
并且会把它切到设置页，方便顺手抓图留证。

用法（应用需要带 NEKOPAL_DEBUG_PORT=9222 启动）：
    python tools\\zz_app_window_check.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from zz_ui_check import WS  # noqa: E402  （复用那个最小 WebSocket 客户端）

PORT = 9222


def main() -> int:
    pages = []
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2) as r:
                targets = json.loads(r.read().decode("utf-8", "replace"))
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    if not pages:
        print("连不上应用窗口的调试端口")
        return 2

    ws = WS(pages[0]["webSocketDebuggerUrl"])
    ws.call("Runtime.enable")
    time.sleep(1)
    print(f"窗口 URL : {pages[0].get('url')}")

    def check(name: str, expr: str, expect) -> bool:
        got = ws.eval(expr)
        ok = str(got) == str(expect)
        print(f"[{'OK' if ok else 'FAIL'}]  {name}：期望 {expect!r} / 实际 {got!r}")
        return ok

    ok = True
    ok &= check("页面里已是新版 app.js（有折叠逻辑）",
                "typeof buildFolds", "function")
    ok &= check("设置页小节全部折起来（默认收起）",
                "document.querySelectorAll('#view-settings .fold-body.open').length", 0)
    ok &= check("设置页小节数（≥14）",
                "document.querySelectorAll('#view-settings h3.sec.fold-head').length >= 14", "True")
    ok &= check("音乐页小节也折起来",
                "document.querySelectorAll('#view-music h3.sec.fold-head').length > 0", "True")
    ok &= check("声音总开关存在（新增）",
                "!!document.querySelector('#voice-on')", "True")
    ok &= check("音色试听与 RVC 在同一个折叠块里",
                """(() => { const h = document.querySelector('#sec-voice');
                   const b = h.nextElementSibling;
                   return b.classList.contains('fold-body') && !!b.querySelector('#voice-list')
                          && !!b.querySelector('#rvc-list'); })()""", "True")

    # 缓存策略用 Python 侧查响应头（页面里那个是异步的，而这个 Edge 的
    # CDP 不兑现 awaitPromise —— 之前踩过，返回的是被序列化成 {} 的 Promise）
    import httpx

    cc = httpx.get("http://127.0.0.1:8790/static/app.js", timeout=10).headers.get("cache-control")
    print(f"[{'OK' if cc == 'no-cache' else 'FAIL'}]  静态资源带 Cache-Control: no-cache —— 实际 {cc!r}")
    ok &= cc == "no-cache"

    # 把真实窗口切到设置页，方便抓图
    ws.eval("document.querySelector('.rail-btn[data-view=settings]').click(); 'ok'")
    time.sleep(1.5)
    view = ws.eval("document.querySelector('.view.active').id")
    print(f"切到设置页后：hash={ws.eval('location.hash')}  活动视图={view}")
    print("PASS" if ok else "有检查未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
