"""模拟设备网关：在没有真硬件的情况下验证「蕴 → 网关 → 设备」整条链路。

真网关要做的是协议适配（MQTT / HTTP / 串口 / HomeAssistant）。
这里把那些换成打印，接口形状保持一致：

    POST /action  {"device": "desk_light", "action": "light.on", "name": "台灯"}
    -> {"ok": true, "state": "on"}
    GET  /state   -> 所有设备的当前状态

用法：
    .venv\\Scripts\\python.exe tools\\mock_gateway.py          # 监听 127.0.0.1:8791
    .venv\\Scripts\\python.exe tools\\mock_gateway.py 9000     # 换端口
"""

from __future__ import annotations

import io
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

#: 模拟设备。真网关里这些来自 MQTT/HA 的实体注册表。
STATE: dict[str, str] = {"desk_light": "off", "door_lock": "locked"}

#: 命令 -> 新状态。真网关里就是往 MQTT topic 发消息 / 调 HA service。
EFFECT = {
    "light.on": ("desk_light", "on"),
    "light.off": ("desk_light", "off"),
    "lock.unlock": ("door_lock", "unlocked"),
    "lock.lock": ("door_lock", "locked"),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/state"):
            self._send(200, {"ok": True, "state": dict(STATE)})
        else:
            self._send(404, {"ok": False, "reason": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/action"):
            self._send(404, {"ok": False, "reason": "not found"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._send(400, {"ok": False, "reason": "bad json"})
            return

        device = str(payload.get("device") or "")
        action = str(payload.get("action") or "")
        name = str(payload.get("name") or device)
        auth = self.headers.get("Authorization") or ""
        print(f"  [网关] 收到: {name}({device}) <- {action}   鉴权头={auth[:22] or '(无)'}")

        if action not in EFFECT:
            print(f"  [网关] 拒绝：不认识的命令 {action}")
            self._send(200, {"ok": False, "reason": f"网关不认识的命令：{action}"})
            return
        target, new_state = EFFECT[action]
        if target != device:
            self._send(200, {"ok": False, "reason": f"命令 {action} 不属于设备 {device}"})
            return
        STATE[target] = new_state
        print(f"  [网关] 执行成功：{name} -> {new_state}")
        self._send(200, {"ok": True, "state": new_state, "device": device})

    def log_message(self, *args) -> None:
        """别把默认的访问日志打到控制台，太吵。"""


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"  模拟网关已启动: http://127.0.0.1:{port}")
    print(f"  初始状态: {STATE}")
    print("  POST /action  {\"device\":\"desk_light\",\"action\":\"light.on\"}")
    print("  GET  /state")
    print("  Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
