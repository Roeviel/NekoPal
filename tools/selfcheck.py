"""端到端自检：一条命令看清哪些环节通了、哪些没通。

用法：
    .venv\\Scripts\\python.exe tools\\selfcheck.py
    .venv\\Scripts\\python.exe tools\\selfcheck.py --quick   # 跳过联网的慢项目
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

RESULTS: list[tuple[str, str, str]] = []  # (环节, 状态, 说明)


def record(name: str, ok: bool | None, detail: str = "") -> None:
    state = "通过" if ok else ("失败" if ok is False else "跳过")
    RESULTS.append((name, state, detail))
    mark = {"通过": "[OK]  ", "失败": "[FAIL]", "跳过": "[SKIP]"}[state]
    print(f"{mark} {name}  {detail}")


def step(name: str, fn, *, skip: bool = False, skip_reason: str = ""):
    if skip:
        record(name, None, skip_reason)
        return None
    t0 = time.time()
    try:
        detail = fn()
        cost = f"({time.time() - t0:.1f}s)"
        record(name, True, f"{detail} {cost}".strip())
        return detail
    except Exception as exc:  # noqa: BLE001
        record(name, False, f"{type(exc).__name__}: {exc}")
        if "--verbose" in sys.argv:
            traceback.print_exc()
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="跳过联网慢项目")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print("=" * 68)
    print("  NekoPal 自检")
    print(f"  Python: {sys.version.split()[0]}   目录: {ROOT}")
    print("=" * 68)

    # ---- 1. 依赖 ----
    def check_deps() -> str:
        missing = []
        for mod in ("httpx", "numpy", "fastapi", "uvicorn"):
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        optional = []
        for mod in ("edge_tts", "soundfile"):
            try:
                __import__(mod)
            except ImportError:
                optional.append(mod)
        if missing:
            raise RuntimeError(f"缺少必需依赖: {', '.join(missing)}")
        note = f"必需依赖齐全" + (f"；缺少可选: {', '.join(optional)}" if optional else "")
        return note

    # ---- 1.5 服务模块能不能导入 ----
    # 为什么单独一项：`py_compile` 只看语法，**注解求值错误和路由注册错误只在
    # import 时才炸**。踩过一次 —— 把 pydantic 模型字段命名成 `list`，
    # 遮蔽了内置类型，注解 `list[dict] | None` 求值成 `None[dict]`，
    # 整个服务启动即崩，而 `py_compile` 全绿、自检其它项也全过。
    def check_import() -> str:
        import importlib

        srv = importlib.import_module("neko.server")
        routes = len(getattr(srv, "app").routes)
        for mod in ("neko.brain", "neko.devices", "neko.saves", "neko.asr",
                    "neko.emotes", "neko.prosody", "neko.persona"):
            importlib.import_module(mod)
        return f"服务模块可导入，路由 {routes} 条"

    step("依赖导入", check_deps)
    step("服务模块可导入", check_import)

    # ---- 2. 配置 ----
    cfg = None

    def check_config():
        from neko.config import load_config, missing_key_hint

        c = load_config()
        hint = missing_key_hint(c) or "已配置 API Key"
        return f"模型 {c['llm']['model']}；{hint}"

    step("配置加载", check_config)

    from neko.config import load_config

    cfg = load_config()

    # ---- 自检必须在**隔离的临时库**上跑 ----
    #
    # 踩过的坑，代价是用户的真实聊天被刷屏：
    # check_memory 直接 `Memory(cfg)`、check_brain 直接 `Brain(cfg)`，两者都落在
    # 用户的 data/neko.db 上。而 check_brain 里的 `b.reply("你好呀")` 走的是
    # **channel="local"（正常聊天通道）**，于是每跑一次自检，用户的聊天里就多一条
    # "你好呀"和她的回复，记忆里还多一条"自检用的记忆条目"。
    # 跑了 14 次之后，她开始数："第八次了喵""第十二次了喵""你是不是手滑给自己
    # 设了个定时器" —— 而用户完全不知道那些消息是自己冒出来的。
    #
    # 结论：**自检工具绝不能写生产数据。**
    import json as _json
    import shutil as _shutil
    import tempfile as _tempfile

    # 注意：本机 TEMP 指向项目上层目录，mkdtemp() 会**在项目里**留下
    # neko_selfcheck_xxxx/ 目录。所以显式收在 data/ 下，并在结束时删掉。
    _tmp_dir = Path(_tempfile.mkdtemp(prefix="selfcheck-", dir=str(ROOT / "data")))

    def isolated(extra: dict | None = None) -> dict:
        """返回一份指向临时库的配置副本。不改动传入的对象。"""
        c = _json.loads(_json.dumps(cfg, ensure_ascii=False))
        c.setdefault("memory", {})["db_path"] = str(_tmp_dir / "selfcheck.db")
        # 自检用独立存档目录，别往用户的 data/saves 里丢测试存档
        c["memory"]["saves_dir"] = str(_tmp_dir / "saves")
        if extra:
            for k, v in extra.items():
                c.setdefault(k, {}).update(v)
        return c

    # ---- 3. 记忆库 ----
    def check_memory() -> str:
        from neko.memory import Memory

        m = Memory(isolated())
        m.add_message("user", "自检消息", channel="selfcheck")
        m.remember("自检用的记忆条目", kind="fact", key="selfcheck")
        hits = m.search_memories("自检记忆")
        st = m.stats()
        if not hits:
            raise RuntimeError("写入后检索不到，检索逻辑有问题")
        return f"{st['messages']} 条对话 / {st['memories']} 条记忆 / {st['notes']} 个笔记"

    step("SQLite 记忆库", check_memory)

    # ---- 4. 人设 ----
    def check_persona() -> str:
        from neko.persona import describe, system_prompt

        prompt = system_prompt(cfg, memories=[{"kind": "fact", "key": "测试", "value": "值"}])
        if len(prompt) < 200:
            raise RuntimeError("提示词过短，可能拼装失败")
        return f"{describe(cfg)}；提示词 {len(prompt)} 字"

    step("人设提示词", check_persona)

    # ---- 5. 大脑（离线应答） ----
    def check_brain() -> str:
        from neko.brain import Brain

        b = Brain(isolated())
        out = b.reply("你好呀")
        text = (out.get("text") or "").strip()
        if not text:
            raise RuntimeError("回复为空")
        ready = "在线" if b.llm.available() else "离线兜底"
        return f"{ready}；回复: {text[:36]}…"

    step("大脑编排", check_brain)

    # ---- 6. B站 ----
    def check_bilibili() -> str:
        from neko.bilibili import BiliClient

        with BiliClient(cfg) as bili:
            results = bili.search("Python 入门", limit=5)
            if not results:
                raise RuntimeError("搜索返回 0 条")
            first = results[0]
            info = bili.video(first["bvid"])
            subs = bili.subtitles(first["bvid"])
            return (
                f"搜到 {len(results)} 条；示例《{first['title'][:24]}》；"
                f"字幕 {len(subs)} 条"
            )

    step("B站免登录接口", check_bilibili, skip=args.quick, skip_reason="--quick")

    # ---- 6b. 观众视角内容源 ----
    def check_audience() -> str:
        from neko.bili_extra import comments, danmaku
        from neko.bilibili import BiliClient

        with BiliClient(cfg) as bili:
            info = bili.video("BV1Sz4y1U77N")
        dm = danmaku(info["cid"])
        cm = comments(info["aid"])
        if not dm and not cm:
            raise RuntimeError("弹幕和评论都拿不到，免登录学习会退化成「只读标题」")
        return f"弹幕 {len(dm)} 条 / 评论 {len(cm)} 条（无字幕时的主力内容源）"

    step("观众视角内容源（弹幕+评论）", check_audience, skip=args.quick, skip_reason="--quick")

    # ---- 6c. 定时自学调度器（用假大脑做确定性验证，不依赖真实时间）----
    def check_scheduler() -> str:
        import time as _time

        from neko.scheduler import StudyScheduler

        calls: list[dict] = []

        class FakeBrain:
            def proactive_share(self) -> dict:
                return {"text": "测试分享", "audio": None, "note": {"bvid": "BVtest"}, "kind": "share"}

        now = _time.time()
        hhmm = _time.strftime("%H:%M", _time.localtime(now))
        fired: list[dict] = []
        sched = StudyScheduler(
            {"schedule": {"auto_study": True, "times": [hhmm]}},
            FakeBrain(),
            on_event=fired.append,
        )
        first = sched.tick(now=now)
        if not first:
            raise RuntimeError("命中设定时间却没有触发")
        if not fired:
            raise RuntimeError("触发了但没有把事件推出去")
        if sched.tick(now=now) is not None:
            raise RuntimeError("同一时刻重复触发了（去重失效）")

        # 没开启时应完全不动作
        off = StudyScheduler({"schedule": {"auto_study": False, "times": [hhmm]}}, FakeBrain())
        if off.tick(now=now) is not None:
            raise RuntimeError("auto_study=false 时仍然触发了")
        return f"命中即触发 + 同刻去重 + 关闭时不动作，全部正确（测试时刻 {hhmm}）"

    step("定时自学调度器", check_scheduler)

    # ---- 7. 语音 ----
    def check_voice() -> str:
        from neko.tts import NekoTTS

        tts = NekoTTS(cfg)
        if not tts.available():
            raise RuntimeError("edge-tts 不可用（未安装或无网络）")
        path = tts.synth_to_cache("喵～自检一下，我的声音好不好听呀？")
        p = Path(path)
        if not p.exists() or p.stat().st_size < 1000:
            raise RuntimeError(f"合成文件异常: {path}")
        return f"{p.name} ({p.stat().st_size // 1024} KB)"

    step("语音合成 + 猫娘音色", check_voice, skip=args.quick, skip_reason="--quick")

    # ---- 8. 面板 ----
    def check_server() -> str:
        import httpx

        url = f"http://{cfg['server']['host']}:{cfg['server']['port']}"
        r = httpx.get(f"{url}/api/status", timeout=5.0)
        r.raise_for_status()
        st = r.json()["status"]
        return f"在线；大脑 {'就绪' if st['llm_ready'] else '未配置'}；笔记 {st['notes']}"

    step("本地面板服务", check_server)

    # ---- 汇总 ----
    print("-" * 68)
    passed = sum(1 for _, s, _ in RESULTS if s == "通过")
    failed = sum(1 for _, s, _ in RESULTS if s == "失败")
    skipped = sum(1 for _, s, _ in RESULTS if s == "跳过")
    print(f"  通过 {passed} | 失败 {failed} | 跳过 {skipped}")
    if failed:
        print("  未通过的项目：")
        for name, state, detail in RESULTS:
            if state == "失败":
                print(f"    · {name}: {detail}")
    print("=" * 68)
    _shutil.rmtree(_tmp_dir, ignore_errors=True)   # 临时库用完就删，别留垃圾
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
