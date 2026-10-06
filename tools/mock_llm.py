"""本地模拟大模型服务（OpenAI 兼容协议），用于**不依赖真实 API Key** 的端到端验证。

为什么需要它
------------
没有 Key 时，聊天、知识点提炼、出题这三条最核心的链路一次都没被真实跑过。
但这个缺口不需要真 Key 就能补：起一个说 OpenAI 协议的本地服务，
让 `llm.py` 真的走一遍 HTTP、真的解析 SSE 流、真的让 `learner.py` 去抽 JSON。

**关键设计**：这个模拟服务不是返回死数据，而是**从收到的 prompt 里派生回答** ——
它会去材料里挑出真正有信息量的行当作知识点。
所以只要材料没送达模型，返回的 points 就会是空/垃圾，测试立刻失败。
这就把"材料确实进入了模型上下文"这件事变成了可断言的事实，而不是靠人眼看日志。

它同时记录每一次请求，方便上层断言 prompt 内容。

用法：
    python tools/mock_llm.py                # 前台跑，默认 127.0.0.1:8799
    python tools/mock_llm.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# 已收到的请求（供测试断言），线程安全地追加
REQUESTS: list[dict[str, Any]] = []
_LOCK = threading.Lock()

# 用来判断一行是不是"有信息量的知识点"
_HEADER_PREFIXES = ("【", "—", "-", "─", "=====")
_MIN_POINT_CHARS = 18


def _extract_points(prompt: str, limit: int = 4) -> list[str]:
    """从 prompt 里挑出像知识点的行。

    这正是这个 mock 的价值所在：如果 learner 没把真实材料放进 prompt，
    这里就会挑不出东西，测试会失败 —— 而不是悄悄返回一个假的正确答案。
    """
    points: list[str] = []
    for raw in prompt.splitlines():
        line = raw.strip()
        if not line or line.startswith(_HEADER_PREFIXES):
            continue
        # 去掉评论的 "[1234赞] " 前缀
        line = re.sub(r"^\[\d+赞\]\s*", "", line)
        if len(line) < _MIN_POINT_CHARS:
            continue
        if not re.search(r"[\u4e00-\u9fff]", line):
            continue
        if line not in points:
            points.append(line)
        if len(points) >= limit:
            break
    return [p[:120] for p in points]


def _title_from(prompt: str) -> str:
    for pattern in (r"视频标题：\s*(.+)", r"【视频标题】\s*(.+)"):
        hit = re.search(pattern, prompt)
        if hit:
            return hit.group(1).strip()[:80]
    return "未知视频"


def _level_from(prompt: str) -> str:
    hit = re.search(r"内容来源等级：(\w+)", prompt)
    return hit.group(1) if hit else "unknown"


def build_reply(messages: list[dict[str, str]]) -> str:
    """根据 system prompt 判断这次调用想干什么，并派生回答。"""
    system = messages[0].get("content", "") if messages else ""
    user = "\n".join(m.get("content", "") for m in messages[1:]) if len(messages) > 1 else ""

    # 1) 出题
    if '"questions"' in system or "出题" in system:
        return json.dumps(
            {
                "questions": [
                    {"q": f"请说明：{_title_from(user) or '这个视频'}里最核心的一个结论是什么？",
                     "a": "参考要点：" + ("；".join(_extract_points(user, 2)) or "（材料不足）")},
                    {"q": "range() 返回的是什么类型？为什么说它省内存？",
                     "a": "返回惰性序列；不一次性生成全部元素，所以占用恒定。"},
                    {"q": "Python 字典的键为什么不能用列表？",
                     "a": "键必须是可哈希的不可变类型，列表可变，所以不行。"},
                ]
            },
            ensure_ascii=False,
        )

    # 2) 学习笔记提炼
    if '"points"' in system:
        points = _extract_points(user, 4)
        level = _level_from(user)
        note = (
            "这是观众讨论中提炼的要点。" if level == "audience"
            else "这是根据视频字幕整理的要点。" if level == "subtitle"
            else "材料有限，只能抓大意。"
        )
        return json.dumps(
            {
                "summary": f"《{_title_from(user)}》主要讲：{note}",
                "points": points or ["（材料里没找到足够的知识点）"],
                "tags": ["python", "基础"],
                "confidence": "medium" if level in ("audience", "desc") else "high",
            },
            ensure_ascii=False,
        )

    # 3) 普通聊天：回一句能验证往返的短句，并带上收到的字数
    last_user = messages[-1].get("content", "") if messages else ""
    return f"（模拟回复）我收到了 {len(last_user)} 个字的输入。"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # 静音访问日志
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "bad json")
            return

        with _LOCK:
            REQUESTS.append(payload)

        content = build_reply(payload.get("messages") or [])

        if payload.get("stream"):
            # 分片吐，模拟真实的流式行为；顺带验证 llm.py 的增量拼接
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            step = max(1, len(content) // 4)
            for i in range(0, len(content), step):
                chunk = {
                    "choices": [{"delta": {"content": content[i : i + step]}, "index": 0}],
                    "object": "chat.completion.chunk",
                }
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return

        body = json.dumps(
            {
                "id": "mock",
                "object": "chat.completion",
                "model": payload.get("model", "mock"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 8799, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="mock-llm", daemon=True)
    thread.start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="本地模拟 LLM（OpenAI 兼容）")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = serve(args.port, args.host)
    print(f"模拟 LLM 已启动： http://{args.host}:{args.port}/chat/completions")
    print("按 Ctrl+C 停止")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
