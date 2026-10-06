"""NekoPal —— 虚拟猫娘伴友。

模块划分：
    config    配置加载
    persona   人设与提示词
    llm       大模型适配（DeepSeek / OpenAI 兼容）
    memory    SQLite 记忆与笔记
    bilibili  B站免登录客户端（WBI 签名）
    bili_extra 弹幕与评论内容源
    learner   视频 → 知识点提炼 → 笔记
    scheduler 定时自学与主动分享
    tts       语音合成 + 猫娘音色
    brain     编排：一条用户消息 → 一条猫娘回复
    server    内置 HTTP 服务
    app       桌面应用入口（窗口 + 服务 + 看门狗）

这个包在导入时先做一件事：**保证 sys.stdout / sys.stderr 可用**。
原因见 `_ensure_stdio`。
"""

from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.3.0"
__all__ = ["__version__"]


def _ensure_stdio() -> None:
    """保证标准输出/错误可用。

    用 `pythonw.exe`（无控制台）启动时，`sys.stdout` 和 `sys.stderr` **都是 None**。
    任何往它们写东西的代码都会炸 —— 包括 uvicorn 的日志。
    结果就是最迷惑人的那种故障：**双击了，什么都没发生，也没有任何报错**。

    实测：同一个应用，`.venv\\Scripts\\python.exe -m neko.app` 正常，
    换成 `pythonw.exe` 立刻以退出码 1 崩掉。

    所以这里在包导入时就把它们接到 `data/app.log`；
    顺带解决了无控制台模式下"出了错完全看不到"的问题。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = None
    try:
        log_dir = Path(__file__).resolve().parent.parent / "data"
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "app.log", "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        pass
    if stream is None:
        import io

        stream = io.StringIO()
    if sys.stdout is None:
        sys.stdout = stream  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = stream  # type: ignore[assignment]


_ensure_stdio()
