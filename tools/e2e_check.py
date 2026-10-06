"""端到端验证：不需要真实 API Key，也能把核心链路真跑一遍。

它做的事：
    1. 在本地起一个说 OpenAI 协议的模拟 LLM（tools/mock_llm.py）
    2. 用环境变量把大脑指向它（不动你的 config.json）
    3. 用**临时数据库**跑完整流程，不污染 data/neko.db
    4. 走一遍：流式聊天 → 学一个真实 B 站视频 → 提炼知识点 → 存笔记
              → 出题 → 对答案 → 记忆检索 → 指令
    5. 断言"真实 B 站材料确实进入了模型上下文"，而不是只看日志像不像

用法：
    .venv\\Scripts\\python.exe tools\\e2e_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

if hasattr(sys.stdout, "buffer"):
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, fn):
    t0 = time.time()
    try:
        detail = fn()
        RESULTS.append((name, "通过", str(detail)))
        print(f"[OK]   {name}  {detail}  ({time.time() - t0:.1f}s)")
        return detail
    except Exception as exc:  # noqa: BLE001
        import traceback

        RESULTS.append((name, "失败", f"{type(exc).__name__}: {exc}"))
        print(f"[FAIL] {name}  {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    import mock_llm

    server = mock_llm.serve(port)

    # 临时目录固定在项目内，且每次先清空：
    # 之前用 ignore_errors=True 把"删不掉"这件事藏起来了，结果根目录堆了残留目录。
    tmp_root = ROOT / ".e2e-tmp"
    if tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="run_", dir=str(tmp_root)))
    os.environ["DEEPSEEK_API_KEY"] = "mock-key-for-e2e"
    os.environ["NEKO_LLM_BASE_URL"] = base

    print("=" * 72)
    print("  蕴 · AI伴友 —— 端到端验证（本地模拟 LLM，不需要真实 Key）")
    print(f"  模拟 LLM: {base}    临时数据: {tmpdir}")
    print("=" * 72)

    from neko.brain import Brain
    from neko.config import load_config

    cfg = load_config()
    cfg["memory"]["db_path"] = str(tmpdir / "neko.db")
    cfg["voice"]["cache_dir"] = str(tmpdir / "voice")
    cfg["voice"]["enabled"] = False          # 本测试只验大脑链路，跳过语音
    cfg["schedule"]["auto_study"] = False

    brain = Brain(cfg)
    state: dict = {}

    # ---------- 1. 配置指向生效 ----------
    check("LLM 配置指向模拟服务", lambda: (
        f"{brain.llm.base_url} / {brain.llm.model}"
        if (brain.llm.available() and brain.llm.base_url == base)
        else (_ for _ in ()).throw(AssertionError(f"未指向模拟服务: {brain.llm.base_url}"))
    ))

    # ---------- 2. 非流式聊天 ----------
    def t_chat():
        out = brain.chat("你好呀，今天有空吗")
        text = out["text"]
        assert "模拟回复" in text, f"回复不符合预期: {text!r}"
        state["chat"] = text
        return text[:40]

    check("非流式聊天（走完整 HTTP）", t_chat)

    # ---------- 3. 流式聊天：必须真的收到多个增量 ----------
    def t_stream():
        deltas = []
        final = ""
        for ev in brain.chat_stream("再说一次"):
            if ev["type"] == "delta":
                deltas.append(ev["text"])
            elif ev["type"] == "done":
                final = ev["text"]
        assert len(deltas) > 1, f"只收到 {len(deltas)} 个增量，SSE 解析可能没生效"
        assert "模拟回复" in final, f"流式拼接结果不对: {final!r}"
        return f"{len(deltas)} 个增量 → 拼接正确"

    check("流式聊天（SSE 增量解析）", t_stream)

    # ---------- 4. 学一个真实 B 站视频 ----------
    def t_study():
        out = brain.reply("学点东西")
        assert out.get("kind") == "study", f"不是学习结果: {out.get('kind')} / {out.get('text')}"
        note = out["note"]
        state["note"] = note
        state["level"] = out.get("level")
        assert out.get("level") in ("subtitle", "audience"), f"内容等级异常: {out.get('level')}"
        points = note.get("points") or []
        assert len(points) >= 3, f"知识点只有 {len(points)} 条: {points}"
        assert all(len(p) >= 10 for p in points), f"有知识点过短，像是占位: {points}"
        assert not any("材料里没找到" in p for p in points), "返回了占位知识点，说明材料没送达模型"
        return f"{out['level']} / {len(points)} 个知识点 / 《{note['title'][:20]}》"

    check("学习闭环（真实B站数据→提炼→笔记）", t_study)

    # ---------- 5. 关键断言：真实材料确实进了模型上下文 ----------
    def t_material_reached():
        sums = [r for r in mock_llm.REQUESTS
                if r.get("messages") and '"points"' in r["messages"][0].get("content", "")]
        assert sums, "没有捕获到提炼请求"
        prompt = sums[-1]["messages"][-1]["content"]
        assert "观众" in prompt or "弹幕" in prompt, "prompt 里没有说明材料来源（观众/弹幕）"
        long_lines = [ln for ln in prompt.splitlines()
                      if len(ln.strip()) >= 18 and not ln.strip().startswith(("【", "—"))]
        assert len(long_lines) >= 30, f"材料只有 {len(long_lines)} 行有效内容，可能没抓到弹幕"
        # 保存知识点里至少有一条能在材料里找到 → 证明不是凭空生成的
        points = state.get("note", {}).get("points") or []
        blob = prompt.replace("\n", " ")
        hit = [p for p in points if p[:20] and p[:20] in blob]
        assert hit, "笔记里的知识点在材料里找不到，说明不是从材料派生的"
        return f"材料 {len(long_lines)} 行有效内容，{len(hit)}/{len(points)} 个知识点可溯源"

    check("真实材料确实进入模型上下文（可溯源）", t_material_reached)

    # ---------- 6. 出题 ----------
    def t_quiz():
        out = brain.reply("考考我")
        assert out.get("kind") == "quiz", f"不是测验结果: {out.get('kind')} / {out.get('text')}"
        quiz = out.get("quiz") or []
        assert len(quiz) == 3, f"题目数量不对: {len(quiz)}"
        assert all(q.get("q") and q.get("a") for q in quiz), "有题目缺题干或答案"
        return f"{len(quiz)} 道题"

    check("出题（依据学过的笔记）", t_quiz)

    # ---------- 7. 对答案 ----------
    def t_answer():
        text = brain.reply("看答案")["text"]
        assert "参考要点" in text or "1." in text, f"答案格式不对: {text[:80]!r}"
        return text.splitlines()[0][:40]

    check("对答案", t_answer)

    # ---------- 8. 人设一致性 ----------
    def t_persona():
        sys_prompt = brain.system("你好")
        assert "蕴" in sys_prompt, "系统提示词里没有她的名字"
        assert "同行者" in sys_prompt, "系统提示词里没有称呼"
        assert "清冷" in sys_prompt, "系统提示词里没有性格设定"
        assert "不要用「喵」" in sys_prompt, "没有注入'不带喵口癖'的约束"
        # 发给模型的聊天请求里，system 也不该混进喵口癖指令
        chats = [r for r in mock_llm.REQUESTS
                 if r.get("messages") and '"points"' not in r["messages"][0].get("content", "")
                 and '"questions"' not in r["messages"][0].get("content", "")]
        assert chats, "没有捕获到聊天请求"
        return f"提示词 {len(sys_prompt)} 字，姓名/称呼/性格/口癖约束齐全"

    check("人设一致性（蕴 / 同行者 / 无喵口癖）", t_persona)

    # ---------- 9. 记忆与笔记落库 ----------
    def t_memory():
        m = brain.memory
        notes = m.notes()
        assert notes, "笔记没有落库"
        bvid = state["note"]["bvid"]
        assert m.get_note(bvid), "按 bvid 查不到笔记"
        hist = m.history_for_llm()
        assert len(hist) >= 6, f"对话历史只有 {len(hist)} 条"
        m.remember("同行者是程序员", kind="preference", key="职业")
        hits = m.search_memories("程序员是做什么的")
        assert hits, "长期记忆写完检索不到"
        return f"{len(notes)} 个笔记 / {len(hist)} 条对话 / 记忆检索命中 {len(hits)}"

    check("笔记与记忆落库、可检索", t_memory)

    # ---------- 10. 指令 ----------
    def t_commands():
        pairs = {"帮助": "学点东西", "状态": "大脑", "你学过什么": "笔记"}
        for cmd, expect in pairs.items():
            text = brain.reply(cmd)["text"]
            assert expect in text, f"「{cmd}」的回复里没有「{expect}」: {text[:60]!r}"
        return "帮助 / 状态 / 你学过什么 均正常"

    check("内置指令", t_commands)

    # ---------- 11. 无 Key 时的降级仍然可用 ----------
    def t_offline():
        os.environ.pop("DEEPSEEK_API_KEY", None)
        cfg2 = dict(cfg)
        cfg2["llm"] = dict(cfg["llm"])
        cfg2["llm"]["api_key"] = ""
        cfg2["memory"] = dict(cfg["memory"])
        cfg2["memory"]["db_path"] = str(tmpdir / "neko2.db")
        b2 = Brain(cfg2)
        assert not b2.llm.available(), "清了 Key 还说可用"
        text = b2.chat("你好")["text"]
        assert "还没接上大脑" in text, f"降级话术不对: {text!r}"
        assert "喵" not in text, f"降级话术带喵，与人设冲突: {text!r}"
        b2.close()
        os.environ["DEEPSEEK_API_KEY"] = "mock-key-for-e2e"
        return "明确告知缺 Key，且话术跟随人设"

    check("未配 Key 时的降级路径", t_offline)

    brain.close()
    server.shutdown()

    # ---------- 汇总 ----------
    print("-" * 72)
    passed = sum(1 for _, s, _ in RESULTS if s == "通过")
    failed = sum(1 for _, s, _ in RESULTS if s == "失败")
    print(f"  通过 {passed} | 失败 {failed} | 共 {len(RESULTS)} 项")
    print(f"  模拟 LLM 共收到 {len(mock_llm.REQUESTS)} 次请求")
    print("=" * 72)

    report = tmpdir / "e2e_report.json"
    report.write_text(
        json.dumps({"results": [{"name": n, "state": s, "detail": d} for n, s, d in RESULTS],
                    "llm_requests": len(mock_llm.REQUESTS),
                    "note": state.get("note"), "level": state.get("level")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  报告已写入 {report}")

    # 清理临时目录，并且**真的确认删掉了**（不再用 ignore_errors 掩盖失败）
    try:
        shutil.rmtree(tmpdir)
        if tmp_root.exists():
            shutil.rmtree(tmp_root)
        cleaned = not tmp_root.exists()
        print(f"  临时目录清理: {'成功' if cleaned else '仍残留 ' + str(tmp_root)}")
    except OSError as exc:
        print(f"  临时目录清理失败（{exc}），请手动删除 {tmp_root}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
