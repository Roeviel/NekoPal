"""本地面板：FastAPI，浏览器打开 http://127.0.0.1:8790

启动：
    python -m neko.server          # 或双击 run.bat
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import queue
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel

from .brain import Brain
from .config import ROOT, ensure_config_file, load_config
from . import saves as saves_mod
from .persona import PRESETS as PERSONA_PRESETS

# Windows 控制台默认是 GBK，某些符号（如 ⚠）会导致 print 直接抛异常。
# 这里把编不出来的字符替换掉，保证程序永远不因为一句日志崩掉。
# （pythonw 启动时 sys.stdout/stderr 是 None，那种情况已由 neko/__init__.py 兜住）
for _stream in (sys.stdout, sys.stderr):
    if _stream is None:
        continue
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

WEB_DIR = ROOT / "web"

# 主动分享事件队列：定时自学学完东西后推给界面
EVENTS: "queue.Queue[dict]" = queue.Queue(maxsize=100)

# 界面（SSE）连接计数。这是"窗口到底有没有真的连上后端"的唯一可靠信号：
# 页面渲染不出来时，这条长连接不会建立。排障时非常有用。
_ui_lock = threading.Lock()
_ui_clients = 0


def ui_clients() -> int:
    with _ui_lock:
        return _ui_clients


def push_event(event: dict) -> None:
    try:
        EVENTS.put_nowait(event)
    except queue.Full:
        pass


#: 调度器实例。过去它是 lifespan 里的局部变量，导致"自主学习"的接口
#: 根本拿不到它（NameError）；改成模块级，接口才能查状态、改配置。
_scheduler: Any = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动定时自学调度器；应用退出时停掉。"""
    global _scheduler
    from .scheduler import StudyScheduler

    _scheduler = StudyScheduler(load_config(), brain(), on_event=push_event)
    _scheduler.start()
    # **启动时续接上次进度**（游戏存档语义）：关掉再打开，应该在原来的进度上继续。
    # 这个函数自己会比较"实时库"和"当前进度槽"谁更新，谁新用谁 ——
    # 上次若是崩溃退出（没写成退出存档），实时库反而更新，不会被旧槽位覆盖回去。
    # 而且它**不新增任何存档条目**。
    try:
        out = saves_mod.resume(brain().cfg)
        print(f"[saves] 续接进度：{out.get('action')} "
              f"（实时 {out.get('from', out.get('messages'))} 条 / 槽位 "
              f"{out.get('slot', out.get('messages'))} 条）")
    except Exception as exc:  # noqa: BLE001 —— 存档失败不该挡住启动
        print(f"[saves] 续接进度失败：{exc}")

    # **RVC 变声要在后台先热起来**：worker 启动要十几秒（RVC 自带的 Python 3.9
    # 里 import torch + 加载模型），如果等第一句话才起，用户会觉得软件卡住了。
    # 失败也不影响说话 —— 变声不可用时 tts 会自动退回 edge-tts 的原声。
    try:
        from . import rvc as rvc_mod

        if rvc_mod.enabled(brain().cfg):
            threading.Thread(target=rvc_mod.warmup, args=(brain().cfg,),
                             name="rvc-warmup", daemon=True).start()
            print("[rvc] 已在后台预热变声 worker（首次约 10~20 秒，期间照常说话）")
    except Exception as exc:  # noqa: BLE001
        print(f"[rvc] 预热失败（不影响说话）：{exc}")

    try:
        yield
    finally:
        if _scheduler is not None:
            _scheduler.stop()
        # 退出时把常驻的 RVC worker 收掉，别留一个吃内存的 python 进程
        try:
            from . import rvc as rvc_mod

            rvc_mod.shutdown()
        except Exception:  # noqa: BLE001
            pass
        # 退出时把当前进度写进固定槽（覆盖，不会堆积）
        try:
            snap = saves_mod.auto(brain().cfg, "quit")
            print(f"[saves] 退出存档：{snap.get('name')} {snap.get('counts')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[saves] 退出存档失败：{exc}")


def scheduler():
    """取调度器实例。"""
    if _scheduler is None:
        raise HTTPException(503, "调度器尚未启动")
    return _scheduler


app = FastAPI(title="蕴 · AI伴友", version="0.2.0", lifespan=lifespan)
if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
_brain: Brain | None = None


@app.middleware("http")
async def _revalidate_ui_assets(request, call_next):
    """界面文件（index.html / static / favicon）一律**要求重新验证**。

    为什么必须有这一层：这些响应只有 ETag / Last-Modified、**没有 Cache-Control**，
    于是浏览器按"启发式缓存"自己算一个保鲜期，期间**根本不问服务器**，
    直接拿旧文件渲染。真实踩到的后果是：改了 web/ 之后重启软件，
    窗口里还是旧界面 —— 用户看到的是"你没给我改"，而服务器端一切正常，
    查起来极其费劲（这次就是靠翻浏览器缓存目录里那份 38KB 的旧 index.html 才定位到的）。

    `no-cache` 不是"不缓存"，而是"每次用之前先问一句"：
    文件没变就 304（很快），变了就拿到新的。这样改界面**必定生效**，
    也不需要用户去清缓存。
    """
    resp = await call_next(request)
    path = request.url.path
    if request.method == "GET" and (path == "/" or path.startswith("/static/")
                                    or path == "/favicon.ico"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def brain() -> Brain:
    """全局单例：保证面板和微信桥共享同一套记忆。"""
    global _brain
    if _brain is None:
        ensure_config_file()
        _brain = Brain(load_config())
    return _brain


class ChatIn(BaseModel):
    text: str
    channel: str = "local"


class StudyIn(BaseModel):
    topic: str | None = None
    bvid: str | None = None


class MemoryIn(BaseModel):
    value: str
    kind: str = "fact"
    key: str = ""


# ---------------- 页面 ----------------

@app.get("/")
def index() -> FileResponse:
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(500, "web/index.html 缺失")
    return FileResponse(index_file)


@app.get("/favicon.ico")
def favicon():
    icon = WEB_DIR / "favicon.ico"
    if icon.is_file():
        return FileResponse(icon, media_type="image/x-icon")
    return JSONResponse({}, status_code=204)


# ---------------- 接口 ----------------

@app.get("/api/status")
def api_status() -> dict:
    status = brain().status()
    # 让状态里能看到"界面连上没连上"，排障时一眼就知道窗口是不是真的在跑
    status["ui_clients"] = ui_clients()
    status["ui_connected"] = ui_clients() > 0
    # 朗读开关的真实状态由后端配置决定 —— 之前它只写在 HTML 的 `checked` 里，
    # 没有任何持久化，所以每次加载都会变回"开"。
    status["auto_read"] = bool((brain().cfg.get("voice") or {}).get("auto_read", True))
    # 声音总开关：关掉之后她只出文字。对话页要据此**把「朗读」键收起来** ——
    # 声音都关了还能点"朗读"，那是在骗人。
    status["voice_enabled"] = bool((brain().cfg.get("voice") or {}).get("enabled", True))
    return {"ok": True, "status": status, "persona": brain().cfg.get("persona", {})}


class VoiceOnIn(BaseModel):
    on: bool = True


@app.post("/api/voice/enabled")
def api_voice_enabled(payload: VoiceOnIn) -> dict:
    """声音总开关（设置页「声音」小节里那一个）。

    关掉之后：不合成语音（`Brain.tts` 直接返回 None）、不朗读，
    对话页顶部的「朗读」键也跟着收起来。
    """
    _patch_config({"voice": {"enabled": bool(payload.on)}})
    api_reload()
    on = bool((brain().cfg.get("voice") or {}).get("enabled", True))
    print(f"[voice] 声音总开关 -> {'开' if on else '关'}")
    return {"ok": True, "enabled": on}


class AutoReadIn(BaseModel):
    on: bool = True


@app.post("/api/voice/auto-read")
def api_voice_auto_read(payload: AutoReadIn) -> dict:
    """记住「朗读」开关。以后每次加载都按这个值恢复。"""
    _patch_config({"voice": {"auto_read": bool(payload.on)}})
    api_reload()
    print(f"[voice] 自动朗读 -> {'开' if payload.on else '关'}")
    return {"ok": True, "auto_read": bool((brain().cfg.get("voice") or {})
                                          .get("auto_read", True))}


@app.post("/api/reload")
def api_reload() -> dict:
    """重新读取 config.json（改了 API Key / 人设 / 外观后不必重启应用）。

    为什么需要它：应用已经在跑的时候再双击一次启动脚本，第二个实例会因为
    端口被占用而失败。所以"改完配置怎么生效"必须有一条不重启的路。
    """
    from .llm import LLM

    b = brain()
    fresh = load_config()
    # 就地更新同一个 dict：Memory / Learner 等都持有这个引用，改内容即可同步
    b.cfg.clear()
    b.cfg.update(fresh)
    # LLM 是独立对象，Key 可能变了，需要重建
    try:
        b.llm.close()
    except Exception:  # noqa: BLE001
        pass
    b.llm = LLM(fresh)
    # 用 b.cfg（同一个 dict），不要用 fresh —— 否则各处持有的引用就分家了
    b.learner.cfg = b.cfg
    b.learner.llm = b.llm
    # B 站客户端把 Cookie 缓存在实例里（Learner.bili 是懒加载属性）。
    # 扫码登录后如果不重建，字幕依旧按"未登录"去取 —— 必须重启应用才生效。
    # 这就是"登录了但笔记还是观众视角"的原因。
    old_bili = getattr(b.learner, "_bili", None)
    if old_bili is not None:
        try:
            old_bili.close()
        except Exception:  # noqa: BLE001
            pass
        b.learner._bili = None
    # 语音模块也重建，让新的音色参数生效
    b._tts = None
    b._tts_tried = False

    status = b.status()
    status["ui_clients"] = ui_clients()
    status["ui_connected"] = ui_clients() > 0
    print(f"[reload] 已重新读取 config.json，大脑可用 = {b.llm.available()}")
    return {"ok": True, "status": status, "persona": b.cfg.get("persona", {})}


class LLMSettingsIn(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


#: 常见服务商预设：选一个就把接口地址填好，省得用户去翻文档。
#: 全部是 OpenAI 兼容接口，所以本项目这一套适配层直接就能用。
#: hint 只写"怎么用"，**不写具体模型名** —— 模型名会随服务商上下线变化，
#: 写死会过期（实测本机 DeepSeek 账号可用的模型就不是文档里那几个）。
#: 真实可用列表一律以「获取可用模型」从接口拉回来的为准。
LLM_PROVIDERS: list[dict] = [
    {"key": "deepseek", "label": "DeepSeek 官方",
     "base_url": "https://api.deepseek.com", "hint": "点「获取可用模型」看这把 Key 能用哪些"},
    {"key": "openai", "label": "OpenAI",
     "base_url": "https://api.openai.com/v1", "hint": "需要 OpenAI 的 Key"},
    {"key": "moonshot", "label": "月之暗面 Kimi",
     "base_url": "https://api.moonshot.cn/v1", "hint": "需要月之暗面的 Key"},
    {"key": "zhipu", "label": "智谱 GLM",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "hint": "需要智谱的 Key"},
    {"key": "qwen", "label": "通义千问（兼容模式）",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "hint": "需要阿里云百炼的 Key"},
    {"key": "siliconflow", "label": "硅基流动",
     "base_url": "https://api.siliconflow.cn/v1", "hint": "聚合了多家开源模型"},
    {"key": "ollama", "label": "本地 Ollama",
     "base_url": "http://127.0.0.1:11434/v1", "hint": "本地跑，不需要 Key"},
]


@app.get("/api/llm/providers")
def api_llm_providers() -> dict:
    conf = brain().cfg.get("llm") or {}
    return {"ok": True, "providers": LLM_PROVIDERS,
            "current": {"base_url": conf.get("base_url"), "model": conf.get("model")}}


@app.get("/api/llm/models")
def api_llm_models() -> dict:
    """向当前接口问它有哪些模型（OpenAI 兼容的 ``GET /models``）。

    比让用户手打模型名靠谱得多：打错了只会得到"模型不存在"的报错，
    而这里列出来的都是这个 Key 真能用的。
    """
    conf = brain().cfg.get("llm") or {}
    base = str(conf.get("base_url") or "").rstrip("/")
    key = str(conf.get("api_key") or "")
    if not base:
        raise HTTPException(400, "还没填接口地址")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    last_err = ""
    for path in ("/models", "/v1/models"):
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as c:
                resp = c.get(base + path, headers=headers)
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                continue
            data = resp.json().get("data") or []
            models = sorted({str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")})
            if not models:
                last_err = "接口没返回任何模型"
                continue
            return {"ok": True, "models": models, "current": conf.get("model")}
        except (httpx.HTTPError, ValueError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
    raise HTTPException(502, f"拿不到模型列表（{last_err}）。"
                             f"有些服务商不提供这个接口，手动填模型名也可以")


@app.post("/api/llm/test")
def api_llm_test() -> dict:
    """真发一句话过去，确认这个模型确实能用。

    只把模型名写进配置是不够的：名字写错了 `available()` 依然返回 True，
    用户会以为切好了，实际下一句聊天就报错。
    """
    b = brain()
    if not b.llm.available():
        raise HTTPException(400, "还没填 API Key")
    t0 = time.time()
    try:
        # max_tokens 不能给小：有些模型（推理型）会先吐思考内容，
        # 给 16 的话正式回复还没开始预算就用完了，回来看起来是空的。
        out = b.llm.ask("你是一个测试探针。", "只回复两个字：可用", max_tokens=64)
    except Exception as exc:  # noqa: BLE001 —— 任何失败都如实回报给界面
        return {"ok": False, "model": b.llm.model, "error": str(exc)[:300]}
    reply = (out or "").strip()
    result: dict[str, Any] = {"ok": True, "model": b.llm.model,
                              "reply": reply[:60], "ms": int((time.time() - t0) * 1000)}
    if not reply:
        # 接口通了但没内容，这是个真实隐患：用户会以为切好了，聊天却是空的
        result["warn"] = "接口通了，但没返回内容（可能是推理型模型吃掉了输出预算）"
    return result



# 前端错误上报缓冲。应用窗口没有控制台，前端一旦报错就是"界面莫名其妙不对"，
# 必须有个地方能看到真实堆栈，否则只能靠猜。
CLIENT_LOGS: list[dict] = []


class ClientLogIn(BaseModel):
    kind: str = "error"
    message: str = ""
    source: str = ""
    stack: str = ""


@app.post("/api/clientlog")
def api_clientlog(payload: ClientLogIn) -> dict:
    entry = {
        "ts": time.strftime("%H:%M:%S"),
        "kind": payload.kind,
        "message": payload.message[:500],
        "source": payload.source[:200],
        "stack": payload.stack[:4000],
    }
    CLIENT_LOGS.append(entry)
    del CLIENT_LOGS[:-40]
    print(f"[client:{entry['kind']}] {entry['message']}  @{entry['source']}")
    try:
        log = ROOT / "data" / "client.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return {"ok": True}


@app.get("/api/clientlog")
def api_clientlog_get() -> dict:
    return {"ok": True, "entries": CLIENT_LOGS}


@app.post("/api/llm")
def api_llm_settings(payload: LLMSettingsIn) -> dict:
    """在界面里填 API Key（写进 config.json 并立刻生效）。

    加这个是因为"让用户自己去编辑器改 JSON 再保存"太容易失败：
    实测用户输入了内容但没保存，磁盘上的文件根本没变。
    界面上填、点保存，路径短得多，也不容易漏。

    注意：返回值里**不会回显 Key**，只回状态里的是否可用。
    """
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "api_key" in patch:
        patch["api_key"] = patch["api_key"].strip()
    if not any(str(v).strip() for v in patch.values()):
        raise HTTPException(400, "没有要修改的内容")
    _patch_config({"llm": patch})
    print("[llm] 已更新配置：" + ", ".join(f"{k}={'<已设置>' if k == 'api_key' else v}" for k, v in patch.items()))
    return api_reload()


@app.get("/api/greet")
def api_greet() -> dict:
    return {"ok": True, **brain().greet()}


@app.post("/api/chat")
def api_chat(payload: ChatIn) -> dict:
    # 预算闸放在**接口层**：成本控制本来就属于这里，而且能挡住所有外部调用者
    # （聊天界面、脚本、以后接入的机器人）。拦下来时不调模型，直接回话。
    from . import meter as meter_mod

    ok, why = meter_mod.guard(brain().cfg, payload.text, payload.channel)
    if not ok:
        return {"ok": True, "text": why, "kind": "budget", "audio": None}
    try:
        out = brain().reply(payload.text, channel=payload.channel)
    except Exception as exc:  # noqa: BLE001 —— 面板不该因为一个异常白屏
        return {"ok": False, "text": f"我这边出错了：{exc}", "kind": "error", "audio": None}
    return {"ok": True, **out}


@app.get("/api/meter")
def api_meter(refresh: bool = False) -> dict:
    """余额 / 今日花费 / 用量 / 被拦了几次。界面上一行小字就是拿它渲染的。"""
    from . import meter as meter_mod

    return {"ok": True, **meter_mod.status(brain().cfg, refresh=refresh),
            "llm_enabled": bool(brain().llm.available())}


class BudgetIn(BaseModel):
    show: bool | None = None
    enabled: bool | None = None
    daily_cny: float | None = None
    duplicate_max: int | None = None
    max_per_minute: int | None = None
    #: 大模型总开关（写在 llm.enabled 上）
    llm_enabled: bool | None = None


@app.post("/api/budget/config")
def api_budget_config(payload: BudgetIn) -> dict:
    """改预算设置 / 大模型总开关。"""
    from . import meter as meter_mod

    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    llm_flag = patch.pop("llm_enabled", None)
    if patch:
        _patch_config({"budget": patch})
    if llm_flag is not None:
        _patch_config({"llm": {"enabled": bool(llm_flag)}})
        print(f"[llm] 大模型总开关 -> {'开' if llm_flag else '关'}"
              + ("" if llm_flag else "（她将不再发起任何 API 请求）"))
    if patch or llm_flag is not None:
        api_reload()
    return {"ok": True, **meter_mod.status(brain().cfg),
            "llm_enabled": bool(brain().llm.available())}


@app.get("/api/chat/stream")
def api_chat_stream(text: str, channel: str = "local") -> StreamingResponse:
    """SSE 流式聊天。前端用 EventSource 接。"""
    b = brain()

    # 同上的预算闸。拦下来时也走 SSE，前端不用区分两套路径。
    from . import meter as meter_mod

    ok, why = meter_mod.guard(b.cfg, text, channel)
    if not ok:
        def denied() -> Iterator[str]:
            yield f"data: {json.dumps({'type': 'delta', 'text': why}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'text': why}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(denied(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    def gen() -> Iterator[str]:
        t0 = time.time()
        try:
            for event in b.chat_stream(text, channel=channel):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'done', 'text': f'我这边出错了：{exc}'}, ensure_ascii=False)}\n\n"
        # 耗时可见性：之前排查"界面卡住"时完全看不到时间花在哪 ——
        # 大模型的流式部分很快，真正慢的是标记后处理（[学习:] 会真的去学一遍，
        # 那里有 ASR 和网络请求）。超过 8 秒就打一行出来，下次不用再猜。
        cost = time.time() - t0
        if cost > 8:
            print(f"[chat] 这轮回复耗时 {cost:.1f}s（慢的多半是标记后处理，比如 [学习:]）")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/study")
def api_study(payload: StudyIn) -> dict:
    b = brain()
    result = b.learner.study(bvid=payload.bvid, topic=payload.topic)
    if not result.get("ok"):
        return {"ok": False, "text": result.get("reason", "学习失败")}
    note = result["note"]
    b.memory.mark_shared(note["bvid"])
    b.memory.add_message("assistant", result["share"], channel="local")
    return {
        "ok": True,
        "text": result["share"],
        "kind": "study",
        "note": note,
        "level": result.get("level"),
        "source": result.get("source"),
        "confidence": result.get("confidence"),
        "audio": b.voice(result["share"]),
    }


@app.post("/api/proactive")
def api_proactive() -> dict:
    b = brain()
    out = b.proactive_share()
    if not out:
        meow = "喵" if b.cfg.get("persona", {}).get("meow", True) else ""
        return {"ok": False, "text": f"现在没有可分享的内容{meow}"}
    return {"ok": True, **out}


@app.get("/api/notes")
def api_notes(limit: int = 50) -> dict:
    return {"ok": True, "notes": brain().memory.notes(limit=limit)}


@app.delete("/api/notes/{bvid}")
def api_note_delete(bvid: str) -> dict:
    """删掉单条笔记。

    只删这条笔记，**不动对话记录、不动记忆** —— 用户清掉一条学歪了的笔记时，
    不该顺带把聊天也删了（那是「数据清理」里另一个按钮的事）。
    """
    m = brain().memory
    if not m.delete_note(bvid):
        raise HTTPException(404, "没有这条笔记（可能已经被删了）")
    print(f"[notes] 已删除单条笔记 {bvid}")
    return {"ok": True, "deleted": bvid, "notes": m.notes(limit=200)}


@app.get("/api/events")
def api_events() -> StreamingResponse:
    """SSE：把"她自己学完主动分享"这类事件实时推给界面。

    没有事件时每 20 秒发一个心跳注释帧，避免中间层掐断空闲连接。
    这个连接的有无同时也是"界面是否真的渲染并运行了"的判据。
    """

    def gen() -> Iterator[str]:
        global _ui_clients
        with _ui_lock:
            _ui_clients += 1
        try:
            yield f"data: {json.dumps({'type': 'hello', 'text': ''}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event = EVENTS.get(timeout=20)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            with _ui_lock:
                _ui_clients = max(0, _ui_clients - 1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/search")
def api_search(q: str) -> dict:
    m = brain().memory
    return {"ok": True, "notes": m.search_notes(q, k=5), "memories": m.search_memories(q, k=8)}


@app.get("/api/memories")
def api_memories() -> dict:
    rows = brain().memory.all_memories()
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r.get("kind") or "fact", []).append(r)
    return {"ok": True, "memories": rows, "grouped": grouped}


@app.post("/api/memory")
def api_memory(payload: MemoryIn) -> dict:
    brain().memory.remember(payload.value, kind=payload.kind, key=payload.key)
    return {"ok": True, "message": f"记住了：{payload.value}"}


@app.delete("/api/memory")
def api_memory_clear() -> dict:
    m = brain().memory
    rows = m.all_memories()
    for r in rows:
        m.forget(r["value"])
    return {"ok": True, "deleted": len(rows)}


@app.get("/api/history")
def api_history(limit: int = 50, channel: str | None = None) -> dict:
    # 过滤掉自检/测试写进来的消息：tools/selfcheck.py 会往真实库里塞一条
    # "自检消息"（channel=selfcheck），不过滤的话每跑一次自检就在用户聊天里
    # 多一句莫名其妙的话。
    rows = [
        m for m in brain().memory.recent(limit, channel=channel)
        if str(m.get("channel") or "") not in ("selfcheck", "test")
    ]
    return {"ok": True, "messages": rows}


@app.delete("/api/history")
def api_history_clear() -> dict:
    n = brain().memory.clear_history()
    return {"ok": True, "deleted": n}


class MsgDeleteIn(BaseModel):
    #: 要删的消息 id（一条或多条）
    ids: list = []


@app.post("/api/messages/delete")
def api_messages_delete(payload: MsgDeleteIn) -> dict:
    """按条删除聊天记录（支持一条或多条）。

    和「清除聊天记录」的区别：这个只删你选中的，其余原样保留。
    删了就是删了 —— 不产生存档（自动存档只保留"关闭程序时"那一种）。
    """
    ids = [int(i) for i in (payload.ids or []) if str(i).strip().lstrip("-").isdigit()]
    if not ids:
        raise HTTPException(400, "没有指定要删除的消息")
    n = brain().memory.delete_messages(ids)
    if not n:
        raise HTTPException(404, "这些消息不存在（可能已经被删过了）")
    print(f"[data] 按条删除 {n} 条聊天记录（请求 {len(ids)} 条）")
    return {"ok": True, "deleted": n, "status": brain().status()}


@app.get("/api/quiz")
def api_quiz(topic: str | None = None) -> dict:
    out = brain().learner.quiz(topic)
    if out.get("ok"):
        brain().memory.set_kv("pending_quiz", out["questions"])
    return out


# ---------------- 音色试听与切换 ----------------

# 几个可直接试听的音色方案。选哪个由用户听决定，不靠猜。
VOICE_PRESETS: list[dict] = [
    {"key": "sweet", "label": "偏甜少女（推荐）", "desc": "Xiaoxiao + 2 半音，F0≈264Hz，自然但年轻",
     "azure_voice": "zh-CN-XiaoxiaoNeural", "pitch_semitones": 2.0, "formant_shift": 0.12},
    {"key": "natural", "label": "自然原声", "desc": "Xiaoxiao 不做变调，最稳最不像机器",
     "azure_voice": "zh-CN-XiaoxiaoNeural", "pitch_semitones": 0.0, "formant_shift": 0.0},
    {"key": "higher", "label": "更高更亮", "desc": "Xiaoxiao + 4 半音，F0≈296Hz，更嗲但接近童声",
     "azure_voice": "zh-CN-XiaoxiaoNeural", "pitch_semitones": 4.0, "formant_shift": 0.18},
    {"key": "lively", "label": "活泼小艺", "desc": "换 Lively 底声，语调更跳",
     "azure_voice": "zh-CN-XiaoyiNeural", "pitch_semitones": 2.0, "formant_shift": 0.10},
    {"key": "raw", "label": "完全不处理", "desc": "edge-tts 直出，不加任何音色处理",
     "azure_voice": "zh-CN-XiaoxiaoNeural", "enabled": False},
]

VOICE_PREVIEW_TEXT = "同行者，这样听起来还行吗？我觉得这个音色比较自然。"


class VoiceApplyIn(BaseModel):
    key: str


def _voice_preset(key: str) -> dict:
    for p in VOICE_PRESETS:
        if p["key"] == key:
            return p
    raise HTTPException(400, f"未知的音色方案：{key}")


@app.get("/api/stickers")
def api_stickers() -> dict:
    """用户自己导入的表情包列表。

    早期版本内置了 12 个程序生成的表情包，用户要求删掉，改为完全由用户导入：
    表情包应该是你自己挑的，而不是程序画给你的。
    """
    from . import stickers as st

    return {"ok": True, "stickers": st.list_user(brain().cfg)}


class StickerImportIn(BaseModel):
    items: list[dict]  # [{"name": "开心.png", "data_url": "data:image/png;base64,..."}]


@app.post("/api/stickers/import")
def api_stickers_import(payload: StickerImportIn) -> dict:
    """导入表情包（一次可多张）。走 base64 JSON，不引入 multipart 依赖。"""
    from . import stickers as st

    if not payload.items:
        raise HTTPException(400, "没有收到图片")
    if len(payload.items) > 60:
        raise HTTPException(400, "一次最多导入 60 张")
    cfg = brain().cfg
    added, failed = [], []
    for it in payload.items:
        name = str((it or {}).get("name") or "sticker")
        try:
            raw, ext = _decode_data_url(str((it or {}).get("data_url") or ""))
            added.append(st.save_user(cfg, raw, ext, name))
        except HTTPException as exc:
            failed.append({"name": name, "reason": exc.detail})
        except OSError as exc:
            failed.append({"name": name, "reason": f"写入失败：{exc}"})
    print(f"[stickers] 导入成功 {len(added)} 张，失败 {len(failed)} 张")
    return {"ok": True, "added": added, "failed": failed,
            "stickers": st.list_user(cfg)}


@app.get("/api/stickers/local/{name}")
def api_stickers_local(name: str) -> FileResponse:
    from . import stickers as st

    p = st.find_user_path(brain().cfg, name)
    if not p:
        raise HTTPException(404, "表情不存在")
    return FileResponse(p)


@app.delete("/api/stickers/local/{name}")
def api_stickers_delete(name: str) -> dict:
    from . import stickers as st

    if not st.delete_user(brain().cfg, name):
        raise HTTPException(404, "表情不存在")
    return {"ok": True, "stickers": st.list_user(brain().cfg)}


@app.get("/api/stickers/search")
def api_stickers_search(q: str, limit: int | None = None) -> dict:
    """联网搜表情包（Bing 图片）。失败返回空列表，不报错。"""
    from . import stickers as st

    conf = brain().cfg.get("stickers", {})
    if not conf.get("search", True):
        return {"ok": True, "stickers": [], "note": "网络表情包搜索已在配置里关闭"}
    n = int(limit or conf.get("count", 12))
    hits = st.search(q, limit=max(1, min(n, 30)), cfg=brain().cfg)
    items = [{"url": h["url"], "thumb": f"/api/stickers/img?u={quote(h['url'], safe='')}"} for h in hits]
    return {"ok": True, "keyword": q, "query": st.build_query(q, brain().cfg), "stickers": items}


@app.get("/api/stickers/img")
def api_stickers_img(u: str) -> FileResponse:
    """把外链图片代理下来（很多图床禁止外链，直连会碎图）。"""
    from . import stickers as st

    conf = brain().cfg.get("stickers", {})
    p = st.fetch_image(u, conf.get("cache_dir", "data/stickers_cache"))
    if not p:
        raise HTTPException(404, "图片下载失败")
    return FileResponse(p)


class StickerPickIn(BaseModel):
    url: str


@app.post("/api/stickers/pick")
def api_stickers_pick(payload: StickerPickIn) -> dict:
    """选中某张网络图 -> 下载缓存 -> 返回本地短链接，供消息里引用。"""
    from . import stickers as st

    conf = brain().cfg.get("stickers", {})
    p = st.fetch_image(payload.url, conf.get("cache_dir", "data/stickers_cache"))
    if not p:
        raise HTTPException(400, "这张图下载不下来，换一张试试")
    return {"ok": True, "marker": f"[图片:/api/stickers/got/{p.name}]",
            "local": f"/api/stickers/got/{p.name}"}


@app.get("/api/stickers/got/{name}")
def api_stickers_got(name: str) -> FileResponse:
    from . import stickers as st

    conf = brain().cfg.get("stickers", {})
    cache = Path(conf.get("cache_dir", "data/stickers_cache")).resolve()
    target = (cache / name).resolve()
    if cache not in target.parents or not target.is_file():
        raise HTTPException(404, "表情不存在")
    return FileResponse(target)


class ClearDataIn(BaseModel):
    what: str  # chat | memory | notes | all
    confirm: str = ""


@app.post("/api/data/clear")
def api_data_clear(payload: ClearDataIn) -> dict:
    """清除数据。必须带上 confirm="确认" 才真的删——误点代价太大。"""
    what = (payload.what or "").strip()
    if what not in ("chat", "memory", "notes", "all"):
        raise HTTPException(400, f"未知的清除目标：{what}")
    if payload.confirm.strip() != "确认":
        raise HTTPException(400, "需要在 confirm 里回「确认」才会真的删除")
    cfg = brain().cfg
    # 清除前的自动备份**默认已关闭**（用户要求：只保留"关闭程序时"的自动存档）。
    # 仍然调用 auto()，因为它按策略决定做不做 —— 想重新打开这个保险，
    # 把 config.json 里 memory.auto_save.before_clear 设成 true 即可。
    # ⚠ 关着的时候清除是**没有退路**的，所以界面上会明确提示。
    try:
        backup = saves_mod.auto(cfg, "clear")
        if backup.get("skipped"):
            print(f"[data] 清除 {what}（清除前自动存档已关闭，本次没有备份）")
        else:
            print(f"[data] 清除前已存档：{backup.get('name')} {backup.get('counts')}")
    except Exception as exc:  # noqa: BLE001 —— 存档失败也不能挡住用户主动清除
        backup = {"ok": False, "reason": str(exc)}
        print(f"[data] 清除前存档失败：{exc}")
    m = brain().memory
    done: dict[str, int] = {}
    if what in ("chat", "all"):
        done["chat"] = m.clear_history()
    if what in ("memory", "all"):
        done["memory"] = m.clear_memories()
    if what in ("notes", "all"):
        done["notes"] = m.clear_notes()
    print(f"[data] 已清除 {what}：{done}")
    return {"ok": True, "cleared": done, "backup": backup.get("name"),
            "status": brain().status()}


# ---------------- 存档（类似游戏存档：保存 / 读取 / 删除） ----------------

class SaveIn(BaseModel):
    name: str = ""


@app.get("/api/saves")
def api_saves() -> dict:
    cfg = brain().cfg
    return {"ok": True, "saves": saves_mod.list_saves(cfg),
            "active": saves_mod.active_save(cfg),
            "dir": str(saves_mod.saves_dir(cfg))}


@app.post("/api/saves")
def api_save_create(payload: SaveIn) -> dict:
    """把当前进度存成一个具名存档。"""
    cfg = brain().cfg
    try:
        out = saves_mod.snapshot(cfg, payload.name or None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"存档失败：{exc}") from exc
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason") or "存档失败")
    # 新建的存档自动成为"当前槽位"：之后往里跳来跳去就有了归属
    saves_mod.set_active(cfg, out["name"])
    return {"ok": True, "save": out, "saves": saves_mod.list_saves(cfg),
            "active": saves_mod.active_save(cfg)}


@app.post("/api/saves/load")
def api_save_load(payload: SaveIn) -> dict:
    """读取存档（覆盖当前对话/记忆/笔记）。读之前会自动备份现状。

    界面上的按钮走的是 `/api/saves/switch`（跳转，不销毁当前进度）；
    这个接口留着给脚本用，语义就是"覆盖"。
    """
    try:
        out = saves_mod.load_save(brain().cfg, payload.name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"读档失败：{exc}") from exc
    if not out.get("ok"):
        raise HTTPException(404, out.get("reason") or "没有这个存档")
    return {"ok": True, **out, "status": brain().status(),
            "saves": saves_mod.list_saves(brain().cfg)}


@app.post("/api/saves/switch")
def api_save_switch(payload: SaveIn) -> dict:
    """**跳转**到某个存档槽：先把当前进度写回它自己的槽位，再切过去。

    用户的原话是「不要覆盖存档，改为跳转存档」—— 原来的"读取"会直接把当前
    对话/记忆/笔记换成存档内容，当前那段就没了。跳转则是来回切换存档槽，
    两边都留着。
    """
    try:
        out = saves_mod.switch_to(brain().cfg, payload.name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"跳转失败：{exc}") from exc
    if not out.get("ok"):
        raise HTTPException(404, out.get("reason") or "没有这个存档")
    return {"ok": True, **out, "status": brain().status(),
            "saves": saves_mod.list_saves(brain().cfg)}


@app.delete("/api/saves/{name}")
def api_save_delete(name: str) -> dict:
    if not saves_mod.delete_save(brain().cfg, name):
        raise HTTPException(404, f"没有这个存档：{name}")
    return {"ok": True, "saves": saves_mod.list_saves(brain().cfg)}


# ---------------- 一起听音乐（网易云扫码登录 + 记录分享） ----------------

@app.get("/api/music/state")
def api_music_state() -> dict:
    from . import music as mus

    return {"ok": True, **mus.state(brain().cfg), "stats": mus.stats(brain().cfg)}


class MusicProviderIn(BaseModel):
    #: "netease" 或 "qq"
    provider: str = "netease"


@app.post("/api/music/provider")
def api_music_provider(payload: MusicProviderIn) -> dict:
    """选哪家：网易云还是 QQ音乐。搜歌两家都免登录，这个只影响"最近在听"。"""
    from . import music as mus

    p = "qq" if payload.provider.lower() in ("qq", "qqmusic") else "netease"
    _patch_config({"music": {"provider": p}})
    api_reload()
    print(f"[music] 当前音乐服务商 -> {p}")
    return {"ok": True, **mus.state(brain().cfg)}


@app.post("/api/music/qr")
def api_music_qr(provider: str = "") -> dict:
    """生成登录二维码。手机扫一下即可，**不需要把密码给这个程序**。"""
    from . import music as mus

    cfg = brain().cfg
    if (provider or mus.provider(cfg)).lower() in ("qq", "qqmusic"):
        from . import music_qq

        return music_qq.qr_start(cfg)
    return mus.qr_start(cfg)


@app.get("/api/music/qr/poll")
def api_music_qr_poll(key: str, provider: str = "") -> dict:
    from . import music as mus

    cfg = brain().cfg
    if (provider or mus.provider(cfg)).lower() in ("qq", "qqmusic"):
        from . import music_qq

        return music_qq.qr_poll(cfg, key)
    return mus.qr_poll(cfg, key)


@app.get("/api/music/qr/trace")
def api_music_qr_trace(provider: str = "netease") -> dict:
    """扫码过程的状态轨迹。扫不动的时候看这个 —— 能看出卡在哪一步。"""
    from . import music as mus

    p = "qq" if provider.lower() in ("qq", "qqmusic") else "netease"
    return {"ok": True, "provider": p, "trace": mus.qr_trace(p)}


@app.post("/api/music/logout")
def api_music_logout(provider: str = "") -> dict:
    from . import music as mus

    return mus.logout(brain().cfg, provider)


class MusicCookieIn(BaseModel):
    #: 用户从浏览器里复制过来的 Cookie（整段都行，会自己抠）
    text: str = ""


@app.post("/api/music/cookie")
def api_music_cookie(payload: MusicCookieIn, provider: str = "") -> dict:
    """手动导入 cookie —— 扫码走不通时的兜底。

    扫码依赖逆向出来的签名，网易云随时可能让第三方二维码在自家 App 里失效；
    QQ 那边更直接：``ptqrshow`` 给的是 QQ互联登录码，**只有手机QQ App 的
    内置扫码器**认它，用别的 App 扫会跳到 QQ 下载页。cookie 这条路不依赖扫码。
    """
    from . import music as mus

    cfg = brain().cfg
    if (provider or mus.provider(cfg)).lower() in ("qq", "qqmusic"):
        from . import music_qq

        out = music_qq.import_cookie(cfg, payload.text)
        if out.get("ok"):
            return {"ok": True, **out, **mus.state(cfg)}
        return out
    out = mus.import_cookie(cfg, payload.text)
    if out.get("ok"):
        return {"ok": True, **out, **mus.state(cfg)}
    return out


@app.get("/api/music/recent")
def api_music_recent(limit: int = 20) -> dict:
    """最近在听什么（要登录）。"""
    from . import music as mus

    cfg = brain().cfg
    st = mus.state(cfg)
    if not st.get("logged_in"):
        return {"ok": False, "reason": st.get("reason") or "还没登录网易云", "songs": []}
    return {"ok": True, "songs": mus.recent(cfg, limit=limit)}


@app.get("/api/music/search")
def api_music_search(q: str, limit: int = 6) -> dict:
    from . import music as mus

    return {"ok": True, "songs": mus.search(brain().cfg, q, limit=limit)}


@app.get("/api/music/url")
def api_music_url(id: str, provider: str = "") -> dict:
    """**点播那一刻现取一次直链。**

    网易云的直链是带时间戳的（``.../20261005092125/...``），过一会儿就失效。
    卡片是存进历史里的，如果存死 URL，昨天那张卡片今天点就播不了。
    所以卡片里只存 song id，真正播放前再问一次。
    """
    from . import music as mus

    cfg = brain().cfg
    p = "qq" if provider.lower() in ("qq", "qqmusic") else "netease"
    song = {"id": id, "provider": p}
    u = mus.song_url(cfg, song)
    return {"ok": bool(u["url"]), "url": u["url"], "web": u["web"]}


class MusicTogetherIn(BaseModel):
    #: 想听什么（留空 = 从"最近在听"里挑）
    keyword: str = ""


@app.post("/api/music/together")
def api_music_together(payload: MusicTogetherIn) -> dict:
    """一起听一首：她挑歌、读了歌词、说点什么，并且**记下来**。

    用同步 def：FastAPI 会自动把它丢到线程池跑，
    不会阻塞 SSE 那条长连接（她评论要调一次模型，可能几秒）。
    """
    return brain().music_together(payload.keyword)


@app.get("/api/music/sessions")
def api_music_sessions(limit: int = 30) -> dict:
    from . import music as mus

    return {"ok": True, "sessions": mus.sessions(brain().cfg, limit=limit),
            "stats": mus.stats(brain().cfg)}


class MusicTasteIn(BaseModel):
    likes: list | None = None
    dislikes: list | None = None
    prefer_history: bool | None = None
    #: 想先试试效果：给个关键词，返回她会挑哪首（不真的记一条）
    preview: str = ""


@app.get("/api/music/taste")
def api_music_taste() -> dict:
    from . import music as mus

    return {"ok": True, **mus.taste(brain().cfg)}


@app.post("/api/music/taste")
def api_music_taste_set(payload: MusicTasteIn) -> dict:
    """改听歌倾向。``likes``/``dislikes`` 存歌手或风格关键词。"""
    from . import music as mus

    out = mus.set_taste(brain().cfg, likes=payload.likes, dislikes=payload.dislikes,
                        prefer_history=payload.prefer_history)
    print(f"[music] 听歌倾向已更新：喜欢 {out['likes']} / 不喜欢 {out['dislikes']}")
    r: dict = {"ok": True, **out}
    if payload.preview.strip():
        got = mus.preferred(brain().cfg, payload.preview)
        r["preview"] = {"song": got.get("song"), "why": got.get("why"),
                        "candidates": [{"title": s.get("title"), "artist": s.get("artist"),
                                        "score": round(mus.score_song(s, payload.preview, out), 1)}
                                       for s in got.get("candidates", [])[:6]]}
    return r


class MusicFeedbackIn(BaseModel):
    #: 歌的 id + provider，或者直接给 artist
    id: str = ""
    provider: str = ""
    artist: str = ""
    title: str = ""
    #: true=喜欢 false=不喜欢
    like: bool = True


@app.post("/api/music/feedback")
def api_music_feedback(payload: MusicFeedbackIn) -> dict:
    """对一首歌表态 -> 学进听歌倾向（把歌手加进喜欢/不喜欢）。"""
    from . import music as mus

    song = {"id": payload.id, "artist": payload.artist,
            "title": payload.title, "provider": payload.provider}
    if not song["artist"] and payload.id:
        song.update(mus.detail(brain().cfg, payload.id, prov=payload.provider))
    return mus.feedback(brain().cfg, song, payload.like)


class MusicSessionDelIn(BaseModel):
    ids: list = []


@app.post("/api/music/sessions/delete")
def api_music_sessions_delete(payload: MusicSessionDelIn) -> dict:
    from . import music as mus

    n = 0
    for i in payload.ids or []:
        n += mus.delete_session(brain().cfg, int(i))
    if not n:
        raise HTTPException(404, "这些记录不存在")
    return {"ok": True, "deleted": n, "sessions": mus.sessions(brain().cfg)}


# ---------------- 设备控制（桌面机器人 / 智能家居） ----------------

class ActionIn(BaseModel):
    device: str
    action: str
    #: 危险动作必须显式带上这个才执行（前端/机器人确认后传）
    confirm: str = ""


@app.get("/api/devices")
def api_devices() -> dict:
    """设备白名单。机器人/网关启动时可以拉一次，知道有哪些可控对象。"""
    from . import devices as dv

    cfg = brain().cfg
    reg = dv.DeviceRegistry(cfg)
    return {
        "ok": True,
        "enabled": reg.enabled,
        "gateway": reg.gateway,
        "devices": [
            {"id": d["id"], "name": d["name"], "kind": d["kind"],
             "dangerous": d["dangerous"], "actions": list(d["actions"])}
            for d in reg.devices.values()
        ],
        "pending": dv.parse_pending(brain().memory.get_kv(dv.pending_key())),
    }


@app.post("/api/action")
def api_action(payload: ActionIn) -> dict:
    """直接下发一个设备动作（给机器人/网关/脚本用）。

    **同样过白名单**：没登记过的设备和动作一律拒绝。
    危险动作要么带 ``confirm: "确认"``，要么先走聊天让她挂起等你确认。
    """
    from . import devices as dv

    reg = dv.DeviceRegistry(brain().cfg)
    if not reg.enabled:
        raise HTTPException(400, "设备控制没启用（config.json 的 devices.enabled）")
    dev, err = reg.resolve(payload.device, payload.action)
    if dev is None:
        dv.audit(brain().memory, None, f"{payload.device}={payload.action}",
                 {"ok": False, "reason": err}, source="api")
        raise HTTPException(400, err)
    if dev["dangerous"] and payload.confirm.strip() != "确认":
        brain().memory.set_kv(dv.pending_key(), json.dumps(
            {"device": dev["name"], "id": dev["id"], "action": payload.action},
            ensure_ascii=False))
        return {"ok": False, "need_confirm": True,
                "reason": f"{dev['name']}是危险操作，需要在 confirm 里回「确认」"}
    result = reg.execute(dev, payload.action)
    dv.audit(brain().memory, dev, payload.action, result, source="api")
    return {"ok": bool(result.get("ok")), "device": dev["name"],
            "action": payload.action, **result}


@app.get("/api/actions")
def api_actions(limit: int = 50) -> dict:
    """设备动作审计日志。物理世界的操作没有存档，这张表是唯一能查的东西。"""
    rows = brain().memory.recent_actions(max(1, min(limit, 500)))
    return {"ok": True, "actions": rows}


class DeviceConfigIn(BaseModel):
    enabled: bool | None = None
    gateway: str | None = None
    token: str | None = None
    #: 整份白名单（前端编辑后整体提交，简单可靠）。
    #: **字段名不能叫 `list`** —— 那样会在类命名空间里遮蔽内置的 list，
    #: pydantic 求值注解 `list[dict] | None` 时就会变成 `None[dict]`，
    #: 报 "Unable to evaluate type annotation"，而且是**启动时**才炸。
    devices: "list[dict] | None" = None


@app.post("/api/devices/config")
def api_devices_config(payload: DeviceConfigIn) -> dict:
    """保存设备设置（开关 / 网关地址 / token / 白名单）。"""
    patch: dict = {}
    if payload.enabled is not None:
        patch["enabled"] = bool(payload.enabled)
    if payload.gateway is not None:
        patch["gateway"] = payload.gateway.strip()
    if payload.token is not None:
        patch["token"] = payload.token.strip()
    if payload.devices is not None:
        clean: list[dict] = []
        for d in payload.devices[:40]:
            if not isinstance(d, dict):
                continue
            name = str(d.get("name") or "").strip()
            if not name:
                continue
            raw = d.get("actions")
            if isinstance(raw, dict):
                actions = {str(k): str(v) for k, v in raw.items()}
            elif isinstance(raw, list):
                actions = {str(a): str(a) for a in raw}
            else:
                actions = {}
            clean.append({
                "id": str(d.get("id") or name),
                "name": name,
                "kind": str(d.get("kind") or ""),
                "dangerous": bool(d.get("dangerous", False)),
                "actions": actions,
            })
        patch["list"] = clean
    if patch:
        _patch_config({"devices": patch})
    api_reload()
    from . import devices as dv

    reg = dv.DeviceRegistry(brain().cfg)
    return {"ok": True, "enabled": reg.enabled, "gateway": reg.gateway,
            "devices": [{"id": d["id"], "name": d["name"], "kind": d["kind"],
                         "dangerous": d["dangerous"], "actions": list(d["actions"])}
                        for d in reg.devices.values()]}


@app.post("/api/devices/test")
def api_devices_test() -> dict:
    """测试网关连不连得上 —— 界面上那个「测试连接」按钮。"""
    import httpx as _httpx

    from . import devices as dv

    reg = dv.DeviceRegistry(brain().cfg)
    if not reg.gateway:
        return {"ok": False, "reason": "还没填网关地址"}
    headers = {}
    if reg.token:
        headers["Authorization"] = f"Bearer {reg.token}"
    try:
        with _httpx.Client(timeout=reg.timeout) as c:
            # 先试 /state（真网关大多会提供）；没有再退回用根路径探活
            r = c.get(f"{reg.gateway}/state", headers=headers)
            if r.status_code == 200:
                data = r.json()
                return {"ok": True, "gateway": reg.gateway,
                        "state": data.get("state") if isinstance(data, dict) else None}
            return {"ok": False, "reason": f"网关返回 HTTP {r.status_code}"}
    except _httpx.HTTPError as exc:
        return {"ok": False, "reason": f"连不上 {reg.gateway}：{type(exc).__name__}",
                "hint": "网关没启动？或者地址/端口填错了？"}


def _lan_ip() -> str:
    """取本机在局域网里的地址，好让机器人知道连哪儿。

    用 UDP connect 探路由（**不会真的发包**），比遍历网卡稳，也不依赖外网可达。
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------- 局域网接入的鉴权 ----------------
#
# 只绑 127.0.0.1 时谁都连不进来，但硬件（桌面机器人）在局域网里就连不上。
# 一旦改成 0.0.0.0 而没有鉴权，同一个 Wi-Fi 下任何人都能让她开锁。
# 所以这里做一条**只针对非本机来源**的 token 校验：
#   - 本机（127.0.0.1）不受限 —— 界面自己就是这么连的，不然界面先挂了
#   - 局域网来的请求必须带 `Authorization: Bearer <token>`（或 ?token=）
# token 为空时不校验（默认状态，也就是只绑回环地址时的状态）。

_LOOPBACK = ("127.0.0.1", "::1", "localhost", "testclient")


@app.middleware("http")
async def _lan_token_guard(request, call_next):
    host = ""
    if request.client is not None:
        host = request.client.host or ""
    if host not in _LOOPBACK:
        token = str((brain().cfg.get("devices") or {}).get("token") or "")
        if token:
            given = request.headers.get("authorization") or ""
            if given.lower().startswith("bearer "):
                given = given[7:].strip()
            given = given or request.query_params.get("token") or ""
            if given != token:
                return JSONResponse(
                    {"detail": "局域网访问需要 token（Authorization: Bearer <token>）"},
                    status_code=401,
                )
    return await call_next(request)


class BindIn(BaseModel):
    #: true = 允许局域网里的硬件连进来（host 改成 0.0.0.0）
    lan: bool


@app.post("/api/connect/bind")
def api_connect_bind(payload: BindIn) -> dict:
    """开关"允许局域网接入"。

    **开的时候会自动生成 token**（如果还没有）—— 局域网上没有任何认证地
    暴露一个能开锁的接口是不能接受的。
    改 host 要重启才生效（端口在启动时就绑好了）。
    """
    import secrets

    cfg = brain().cfg
    host = "0.0.0.0" if payload.lan else "127.0.0.1"
    patch: dict = {"server": {"host": host}}
    token = str((cfg.get("devices") or {}).get("token") or "")
    if payload.lan and not token:
        token = secrets.token_urlsafe(24)
        patch["devices"] = {"token": token}
    _patch_config(patch)
    api_reload()
    return {"ok": True, "host": host, "lan": bool(payload.lan), "token": token,
            "need_restart": True,
            "note": ("host 改成了 " + host + "，重启软件后生效。"
                     + ("" if payload.lan else "（局域网接入已关闭）"))}


class TokenIn(BaseModel):
    regenerate: bool = False


@app.post("/api/connect/token")
def api_connect_token(payload: TokenIn) -> dict:
    """重新生成接入 token（旧的立刻失效）。"""
    import secrets

    token = secrets.token_urlsafe(24) if payload.regenerate else str(
        (brain().cfg.get("devices") or {}).get("token") or "")
    if payload.regenerate:
        _patch_config({"devices": {"token": token}})
        api_reload()
    return {"ok": True, "token": token}


@app.get("/api/connect-info")
def api_connect_info() -> dict:
    """给硬件/机器人看的接入信息：连哪儿、调什么、怎么带 token。

    这个接口只读，不改任何东西；界面上的「复制接入信息」按钮就是拿它渲染的。
    """
    cfg = brain().cfg
    port = int((cfg.get("server") or {}).get("port") or 8790)
    host = str((cfg.get("server") or {}).get("host") or "127.0.0.1")
    lan = _lan_ip()
    token = str((cfg.get("devices") or {}).get("token") or "")
    base_lan = f"http://{lan}:{port}"
    return {
        "ok": True,
        "bind": f"{host}:{port}",
        # 只绑 127.0.0.1 时局域网是连不上的，这点必须如实说，不能让用户白折腾
        "lan_reachable": host not in ("127.0.0.1", "localhost"),
        "base_local": f"http://127.0.0.1:{port}",
        "base_lan": base_lan,
        "token": token,
        "endpoints": [
            {"method": "POST", "path": "/api/chat",
             "body": {"text": "你好"}, "note": "发一句话，返回 text 和语音 audio"},
            {"method": "GET", "path": "/api/chat/stream?text=你好",
             "note": "流式（SSE），逐段返回，机器人做即时反应更自然"},
            {"method": "GET", "path": "/api/events",
             "note": "SSE 长连接，她会主动推消息（学完东西想分享时）"},
            {"method": "POST", "path": "/api/action",
             "body": {"device": "desk_light", "action": "开"}, "note": "控制设备"},
            {"method": "GET", "path": "/api/status", "note": "状态探活，固件里做心跳用"},
        ],
    }


# ---------------- B 站账号（扫码登录，为的是拿字幕） ----------------

#: 待确认的扫码会话。只保留最近一个，够用。
_PENDING_QR: dict[str, str] = {}
#: nav 结果缓存，避免每次刷新状态都去打 B 站（也别因此触发风控）
_NAV_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


@app.post("/api/bili/qr")
def api_bili_qr() -> dict:
    """申请登录二维码，返回内联 SVG 供界面显示。"""
    from . import bili_auth

    q = bili_auth.qr_generate()
    _PENDING_QR.clear()
    _PENDING_QR["key"] = q["qrcode_key"]
    return {"ok": True, "svg": bili_auth.qr_svg(q["url"])}


@app.post("/api/bili/qr/poll")
def api_bili_qr_poll() -> dict:
    """轮询扫码结果；成功则把 Cookie 写进 config.json 并立即生效。"""
    from . import bili_auth

    key = _PENDING_QR.get("key")
    if not key:
        raise HTTPException(400, "还没有申请二维码")
    result = bili_auth.qr_poll(key)
    if result.get("state") == "ok":
        _patch_config({"bilibili": {"cookie": result.pop("cookie")}})
        _PENDING_QR.clear()
        _NAV_CACHE["ts"] = 0.0          # 逼下一次重新查
        reloaded = api_reload()
        result["status"] = reloaded.get("status")
        print("[bili] 扫码登录成功，已保存 Cookie 并重新加载配置")
    return {"ok": True, **result}


@app.get("/api/bili/status")
def api_bili_status(force: bool = False) -> dict:
    """B 站账号状态。结果缓存 60 秒。"""
    from . import bili_auth

    cookie = (brain().cfg.get("bilibili") or {}).get("cookie") or ""
    configured = bool(cookie.strip())
    if not configured:
        return {"ok": True, "configured": False, "logged_in": False, "uname": ""}
    now = time.time()
    if not force and _NAV_CACHE["data"] and now - float(_NAV_CACHE["ts"]) < 60:
        return {"ok": True, "configured": True, **_NAV_CACHE["data"]}
    info = bili_auth.nav(cookie)
    _NAV_CACHE.update(ts=now, data=info)
    return {"ok": True, "configured": True, **info}


@app.post("/api/bili/logout")
def api_bili_logout() -> dict:
    """退出登录：清掉本地 Cookie。不会影响你手机/浏览器上的登录状态。"""
    _patch_config({"bilibili": {"cookie": ""}})
    _NAV_CACHE.update(ts=0.0, data=None)
    _PENDING_QR.clear()
    api_reload()
    print("[bili] 已清除本地 Cookie")
    return {"ok": True, "configured": False, "logged_in": False}


# ---------------- 自主学习（定时 + 间隔 + 启动补学） ----------------

class ScheduleIn(BaseModel):
    auto_study: bool | None = None
    times: list[str] | None = None
    every_hours: float | None = None
    catch_up: bool | None = None
    startup_delay: float | None = None


@app.get("/api/schedule")
def api_schedule_get() -> dict:
    return {
        "ok": True,
        "schedule": dict(brain().cfg.get("schedule") or {}),
        "running": bool(scheduler()._thread and scheduler()._thread.is_alive()),
        "last_study": scheduler()._last_study,
    }


@app.post("/api/schedule")
def api_schedule_set(payload: ScheduleIn) -> dict:
    """改自学计划。改完立刻生效，不用重启。"""
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(400, "没有要修改的内容")

    if "times" in patch:
        clean: list[str] = []
        for raw in patch["times"]:
            t = str(raw).strip()
            if not t:
                continue
            try:
                hh, mm = t.split(":")
                h, m = int(hh), int(mm)
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
            except ValueError as exc:
                raise HTTPException(400, f"时间格式不对：{t}（应为 HH:MM）") from exc
            clean.append(f"{h:02d}:{m:02d}")
        patch["times"] = clean
    if "every_hours" in patch:
        patch["every_hours"] = max(0.0, min(float(patch["every_hours"]), 48.0))

    _patch_config({"schedule": patch})
    result = api_reload()

    # 调度器持有一份**独立的** cfg 引用，不同步过来的话开关不会生效
    sch = scheduler()
    sch.cfg.clear()
    sch.cfg.update(brain().cfg)
    if brain().cfg.get("schedule", {}).get("auto_study"):
        sch.start()          # 幂等：已经在跑就直接返回
    else:
        sch.stop()

    running = bool(scheduler()._thread and scheduler()._thread.is_alive())
    print(f"[schedule] 已更新：{patch} | 调度器运行中={running}")
    return {**result, "schedule": dict(brain().cfg.get("schedule") or {}), "running": running}


# ---------------- 学习方向（轮换列表） ----------------

class TopicsIn(BaseModel):
    topics: list[str]


@app.get("/api/topics")
def api_topics_get() -> dict:
    bili = brain().cfg.get("bilibili") or {}
    return {"ok": True, "topics": list(bili.get("topics") or [])}


@app.post("/api/topics")
def api_topics_set(payload: TopicsIn) -> dict:
    """改自动轮换的学习方向。改完立刻生效。"""
    clean: list[str] = []
    seen: set[str] = set()
    for raw in payload.topics:
        t = str(raw).strip().replace("\n", " ")[:40]
        if not t or t in seen:
            continue
        seen.add(t)
        clean.append(t)
    if not clean:
        raise HTTPException(400, "至少要留一个学习方向，否则她就没得轮换了")
    if len(clean) > 40:
        raise HTTPException(400, "最多 40 个方向")
    _patch_config({"bilibili": {"topics": clean}})
    api_reload()
    print(f"[topics] 已更新为 {len(clean)} 个方向：{'、'.join(clean[:6])}…")
    return {"ok": True, "topics": clean}


@app.get("/api/voice/presets")
def api_voice_presets() -> dict:
    cfg = brain().cfg
    cur = cfg.get("voice", {})
    return {
        "ok": True,
        "presets": [{k: v for k, v in p.items()} for p in VOICE_PRESETS],
        "current": {
            "azure_voice": cur.get("azure_voice"),
            "pitch_semitones": cur.get("pitch_semitones"),
            "formant_shift": cur.get("formant_shift"),
            "enabled": cur.get("enabled", True),
        },
    }


@app.post("/api/voice/preview")
def api_voice_preview(payload: VoiceApplyIn) -> dict:
    """用指定方案合成一句话，返回可播放的音频路径。"""
    from .tts import NekoTTS

    preset = _voice_preset(payload.key)
    cfg = dict(brain().cfg)
    cfg["voice"] = {**cfg["voice"], **{k: v for k, v in preset.items() if k in
                                       ("azure_voice", "pitch_semitones", "formant_shift", "enabled")}}
    try:
        tts = NekoTTS(cfg)
        path = tts.synth_to_cache(VOICE_PREVIEW_TEXT)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"试听合成失败：{exc}") from exc
    return {"ok": True, "key": payload.key, "audio": path, "text": VOICE_PREVIEW_TEXT}


@app.post("/api/voice/apply")
def api_voice_apply(payload: VoiceApplyIn) -> dict:
    """把选中的音色方案写进 config.json 并立刻生效。"""
    preset = _voice_preset(payload.key)
    patch = {k: v for k, v in preset.items() if k in
             ("azure_voice", "pitch_semitones", "formant_shift", "enabled", "rate", "reverb")}
    _patch_config({"voice": patch})
    result = api_reload()
    print(f"[voice] 已切换到「{preset['label']}」")
    return {**result, "applied": payload.key, "label": preset["label"]}


# ---------------- RVC 变声（把底声换成真人音色） ----------------
#
# 和上面的「音色试听」是两层：上面那层改的是 edge-tts 的**底声 + DSP**，
# 这一层是在成品音频上再跑一次 RVC 推理，换成训练出来的真人音色。
# 三层串起来是：edge-tts 出声 -> 猫娘 DSP -> RVC 换音色。

RVC_PREVIEW_TEXT = "同行者，这就是我现在的音色，你觉得怎么样？"


class RvcVoiceIn(BaseModel):
    name: str


class RvcToggleIn(BaseModel):
    on: bool = True


@app.get("/api/rvc/status")
def api_rvc_status() -> dict:
    """变声状态：整合包在不在、三个模型在不在、worker 起没起、当前/生效音色是谁。"""
    from . import rvc as rvc_mod

    cfg = brain().cfg
    st = rvc_mod.status(cfg)
    # 「配置里选的」和「tts 真正在用的」可能不一致（比如配置坏了），都报出来
    st["voice_name"] = (rvc_mod.current(cfg) or {}).get("name", "")
    tts = brain().tts
    st["effective"] = tts.rvc_voice_name if tts is not None else ""
    return {"ok": True, "rvc": st}


@app.post("/api/rvc/warmup")
def api_rvc_warmup() -> dict:
    """把常驻 worker 拉起来。**后台做**，接口立刻返回（启动要十几秒）。"""
    from . import rvc as rvc_mod

    if not rvc_mod.enabled(brain().cfg):
        raise HTTPException(400, "RVC 没启用，或整合包路径不对（缺 runtime\\python.exe）")
    threading.Thread(target=rvc_mod.warmup, args=(brain().cfg,),
                     name="rvc-warmup", daemon=True).start()
    return {"ok": True, "started": True}


@app.post("/api/rvc/preview")
def api_rvc_preview(payload: RvcVoiceIn) -> dict:
    """用指定模型试听一句话 —— 走**完整链路**（edge-tts → DSP → RVC）。

    为什么不在别处合成再转：试听必须和真实回复同一条路径，
    否则"试听好听、说话难听"这种偏差根本查不出来。
    """
    from . import rvc as rvc_mod
    from .tts import NekoTTS

    cfg = brain().cfg
    voices = rvc_mod.voices(cfg)
    hit = next((v for v in voices if v["name"] == payload.name), None)
    if hit is None:
        raise HTTPException(400, f"没有这个音色：{payload.name}")
    if not hit["ok"]:
        raise HTTPException(400, f"模型文件不存在：{hit['model']}")

    trial = copy.deepcopy(cfg)
    trial["voice"]["rvc"]["voice"] = payload.name
    trial["voice"]["rvc"]["enabled"] = True
    try:
        tts = NekoTTS(trial)
        path = tts.synth_to_cache(RVC_PREVIEW_TEXT)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"试听合成失败：{exc}") from exc
    return {"ok": True, "name": payload.name, "audio": path, "text": RVC_PREVIEW_TEXT}


@app.post("/api/rvc/apply")
def api_rvc_apply(payload: RvcVoiceIn) -> dict:
    """选中一个变声音色：写进 config.json 并立刻生效（之后所有回复都用它）。"""
    from . import rvc as rvc_mod

    names = [v["name"] for v in rvc_mod.voices(brain().cfg)]
    if payload.name not in names:
        raise HTTPException(400, f"没有这个音色：{payload.name}")
    _patch_config_deep({"voice": {"rvc": {"voice": payload.name, "enabled": True}}})
    result = api_reload()
    print(f"[rvc] 已切换到「{payload.name}」")
    return {**result, "applied": payload.name}


@app.post("/api/rvc/toggle")
def api_rvc_toggle(payload: RvcToggleIn) -> dict:
    """变声总开关。关掉就回到「edge-tts + DSP」的声音（每句话快几秒）。"""
    _patch_config_deep({"voice": {"rvc": {"enabled": bool(payload.on)}}})
    result = api_reload()
    print(f"[rvc] 变声 -> {'开' if payload.on else '关'}")
    return {**result, "enabled": bool(payload.on)}


# ---------------- 外观：头像与聊天背景 ----------------

# 允许的图片类型 -> (扩展名, 魔数前缀)
_IMAGE_TYPES: dict[str, tuple[str, tuple[bytes, ...]]] = {
    "image/png": (".png", (b"\x89PNG\r\n\x1a\n",)),
    "image/jpeg": (".jpg", (b"\xff\xd8\xff",)),
    "image/jpg": (".jpg", (b"\xff\xd8\xff",)),
    "image/webp": (".webp", (b"RIFF",)),
    "image/gif": (".gif", (b"GIF87a", b"GIF89a")),
    "image/bmp": (".bmp", (b"BM",)),
}
_APPEARANCE_KINDS = ("neko_avatar", "user_avatar", "chat_background")
_MAX_IMAGE_BYTES = 8 * 1024 * 1024      # 单张图最大 8MB


class AppearanceIn(BaseModel):
    kind: str
    data_url: str
    opacity: float | None = None


class AppearanceResetIn(BaseModel):
    kind: str


class OpacityIn(BaseModel):
    opacity: float


class ThemeIn(BaseModel):
    theme: str


class MemoryForgetIn(BaseModel):
    value: str


class PersonaIn(BaseModel):
    name: str | None = None
    call_user: str | None = None
    style: str | None = None
    meow: bool | None = None
    keywords: list[str] | None = None
    custom_prompt: str | None = None


def _patch_config(patch: dict) -> None:
    """只改动 config.json 里涉及的键，保留用户文件里的其他内容。"""
    path = ROOT / "config.json"
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key].update(value)
        else:
            raw[key] = value
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _patch_config_deep(patch: dict) -> None:
    """**递归**合并写回 config.json。

    为什么不能直接用 ``_patch_config``：它只合并一层，
    ``{"voice": {"rvc": {"voice": "x"}}}`` 会把整段 ``voice.rvc`` 覆盖掉 ——
    三个模型的列表会被一条"换音色"的操作顺手删掉。这个坑踩过一次。
    """
    path = ROOT / "config.json"
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}

    def merge(dst: dict, src: dict) -> None:
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                merge(dst[key], value)
            else:
                dst[key] = value

    merge(raw, patch)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _assets_dir() -> Path:
    d = Path(brain().cfg["assets_dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    """把 data:image/...;base64,xxx 解成 (字节, 扩展名)，并校验类型/魔数/大小。

    走 base64 JSON 而不是 multipart，是为了不引入 python-multipart 依赖。
    """
    if not data_url.startswith("data:"):
        raise HTTPException(400, "不是合法的 data URL")
    head, _, payload = data_url.partition(",")
    if not payload:
        raise HTTPException(400, "图片数据为空")
    mime = head[5:].split(";")[0].strip().lower()
    if mime not in _IMAGE_TYPES:
        raise HTTPException(400, f"不支持的图片格式：{mime}（支持 png / jpg / webp / gif / bmp）")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, f"图片数据解码失败：{exc}") from exc
    if not raw:
        raise HTTPException(400, "图片数据为空")
    if len(raw) > _MAX_IMAGE_BYTES:
        raise HTTPException(413, f"图片太大（{len(raw) // 1024 // 1024}MB），请控制在 8MB 以内")
    ext, magics = _IMAGE_TYPES[mime]
    if not any(raw.startswith(m) for m in magics):
        raise HTTPException(400, "文件内容与声明的图片格式不符")
    return raw, ext


def _appearance_payload() -> dict:
    cfg = brain().cfg
    appearance = dict(cfg.get("appearance") or {})
    assets = _assets_dir()
    # 以目录 mtime 做缓存版本号，换图后浏览器一定会重新拉取
    stamp = int(assets.stat().st_mtime) if assets.exists() else 0
    urls = {}
    for kind in _APPEARANCE_KINDS:
        name = appearance.get(kind) or ""
        urls[kind] = f"/api/assets/{name}?v={stamp}" if name else ""
    return {
        "ok": True,
        "appearance": appearance,
        "urls": urls,
        "persona": cfg.get("persona", {}),
        "styles": list(PERSONA_PRESETS),
    }


@app.get("/api/appearance")
def api_appearance() -> dict:
    return _appearance_payload()


@app.post("/api/appearance")
def api_appearance_set(payload: AppearanceIn) -> dict:
    if payload.kind not in _APPEARANCE_KINDS:
        raise HTTPException(400, f"未知的外观项：{payload.kind}")
    raw, ext = _decode_data_url(payload.data_url)
    assets = _assets_dir()
    # 清掉同一项的旧文件（扩展名可能不同，避免残留）
    for old in assets.glob(f"{payload.kind}.*"):
        old.unlink(missing_ok=True)
    filename = f"{payload.kind}{ext}"
    (assets / filename).write_bytes(raw)
    patch: dict = {payload.kind: filename}
    if payload.kind == "chat_background" and payload.opacity is not None:
        patch["background_opacity"] = max(0.05, min(1.0, float(payload.opacity)))
    _patch_config({"appearance": patch})
    # 同步内存里的配置，让后续读取立刻生效
    brain().cfg.setdefault("appearance", {}).update(patch)
    print(f"[appearance] 已更新 {payload.kind} -> {filename}（{len(raw) // 1024} KB）")
    return _appearance_payload()


@app.post("/api/appearance/reset")
def api_appearance_reset(payload: AppearanceResetIn) -> dict:
    if payload.kind not in _APPEARANCE_KINDS:
        raise HTTPException(400, f"未知的外观项：{payload.kind}")
    assets = _assets_dir()
    for old in assets.glob(f"{payload.kind}.*"):
        old.unlink(missing_ok=True)
    _patch_config({"appearance": {payload.kind: ""}})
    brain().cfg.setdefault("appearance", {})[payload.kind] = ""
    return _appearance_payload()


@app.post("/api/persona")
def api_persona(payload: PersonaIn) -> dict:
    """改名字 / 称呼 / 性格，写回 config.json 并即时生效。"""
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(400, "没有要修改的内容")
    if "style" in patch and patch["style"] not in PERSONA_PRESETS:
        raise HTTPException(400, f"未知的人设预设：{patch['style']}")
    _patch_config({"persona": patch})
    brain().cfg.setdefault("persona", {}).update(patch)
    print(f"[persona] 已更新：{patch}")
    return {"ok": True, "persona": brain().cfg.get("persona", {})}


@app.post("/api/appearance/opacity")
def api_appearance_opacity(payload: OpacityIn) -> dict:
    """只改聊天背景浓度（拖滑块用）。"""
    opacity = max(0.05, min(1.0, float(payload.opacity)))
    _patch_config({"appearance": {"background_opacity": opacity}})
    brain().cfg.setdefault("appearance", {})["background_opacity"] = opacity
    return {"ok": True, "appearance": brain().cfg.get("appearance", {})}


@app.post("/api/appearance/theme")
def api_appearance_theme(payload: ThemeIn) -> dict:
    theme = payload.theme if payload.theme in ("wechat", "dark") else "wechat"
    _patch_config({"appearance": {"theme": theme}})
    brain().cfg.setdefault("appearance", {})["theme"] = theme
    return {"ok": True, "appearance": brain().cfg.get("appearance", {})}


@app.post("/api/memory/forget")
def api_memory_forget(payload: MemoryForgetIn) -> dict:
    brain().memory.forget(payload.value)
    return {"ok": True, "message": f"忘掉了：{payload.value}"}


@app.get("/api/assets/{name}")
def api_asset(name: str) -> FileResponse:
    assets = _assets_dir().resolve()
    target = (assets / name).resolve()
    # 防目录穿越
    if assets not in target.parents or not target.is_file():
        raise HTTPException(404, "资源不存在")
    return FileResponse(target)


# ---------------- 语音文件 ----------------

@app.get("/audio/{name}")
def audio(name: str) -> FileResponse:
    cache = Path(brain().cfg["voice"]["cache_dir"])
    target = (cache / name).resolve()
    # 防目录穿越
    if cache.resolve() not in target.parents or not target.is_file():
        raise HTTPException(404, "音频不存在")
    return FileResponse(target, media_type="audio/wav")


# ---------------- 启动 ----------------

def main() -> None:
    import uvicorn

    ensure_config_file()
    cfg = load_config()
    host = cfg["server"]["host"]
    port = int(cfg["server"]["port"])
    print("=" * 58)
    print("  NekoPal AI伴友 已启动")
    print(f"  面板地址： http://{host}:{port}")
    print(f"  配置文件： {ROOT / 'config.json'}")
    if not cfg["llm"]["api_key"]:
        print("  [注意] 还没配置 DeepSeek API Key，现在只能离线陪你说话")
        print(f"    请编辑 {ROOT / 'config.json'} 的 llm.api_key")
    print("  按 Ctrl+C 停止")
    print("=" * 58)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
