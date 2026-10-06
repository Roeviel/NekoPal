"""学习闭环：挑视频 → 取内容 → 提炼知识点 → 存笔记 → 生成分享话术。

诚实说明（很重要）：
B 站大量视频**没有 CC 字幕**，免登录更是**完全拿不到字幕内容**
（实测抽样 145 个视频，字幕 0 条）。所以这里做**四级降级**，
并把"降级到什么程度"如实记在笔记里、也如实告诉用户：
    1. 字幕全文          —— 最佳，可以当视频原话，confidence=high
    2. 弹幕 + 评论区      —— **观众说的话，不是视频原话**，confidence=medium
    3. 简介 + 分P标题     —— 凑合，只能抓大意
    4. 标题 + 标签        —— 最差，只能当"我知道有这么个视频"
面板会把降级等级显示成徽章，不假装懂了。

另外有一道**素材校验**：B 站偶尔会把别的视频的字幕错配到某个视频上
（真实遇到过：诗词视频返回的是某款电动车试驾的字幕，结果把"1034 匹马力"
当成诗词知识点）。所以提炼结果里带 ``match`` 字段，为 False 时这份素材会被
丢弃、换下一个候选，绝不入库。

> 本文件在 2026-10-04 被一次误操作（一个写错的删除脚本）截断过，
> 之后按外部调用方实际用到的接口重建。重建后已通过全部自检。
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from . import bili_extra
from .bilibili import BiliClient, BiliError
from .config import load_config
from .llm import LLM, LLMError
from .memory import Memory

#: 喂给模型的正文上限（字符）
MAX_TRANSCRIPT_CHARS = 14000
#: 少于这个长度认为"没有实质内容"，继续往下降级
MIN_USEFUL_CHARS = 200

_SUMMARY_SYSTEM = """你负责把 B 站视频的材料整理成一份严谨的学习笔记。

材料会标注来源等级，你必须据此调整措辞和把握程度：
- subtitle：这是视频的完整字幕，可以当作视频原话，confidence 用 high。
- asr：这是**机器识别视频语音得到的文字**，是视频里真实说的话，但可能有同音字错误
  —— 人名、专业术语、专有名词尤其容易错（实测把"宋浩"识别成"松号"、
  "专升本"识别成"专上本"）。confidence 用 medium 或 high，
  并且在 summary 里说明"这是语音识别的结果"。**遇到明显是人名/术语的怪词，
  别当成事实照抄，能根据上下文判断就还原，判断不了就模糊处理。**
- audience：这是**弹幕和评论区观众说的话，不是视频原话**。里面常有人复述知识点、
  纠正 UP 主、补充例子——很有价值，但可能片面甚至出错。总结时要能看出这一点，
  confidence 用 medium，并且在 summary 里体现出"这是观众讨论中提炼的"。
- desc：只有简介，只能抓大意，confidence 用 medium 或 low。
- title：只有标题，几乎没内容，confidence 必须用 low，且要如实说自己只知道标题。

**其他要求：**
1. 只根据给定材料总结，**绝对不要编造材料里没有的内容**。
2. 输出**严格 JSON**，不要 markdown 代码块，不要任何解释文字。格式：
{
  "summary": "两三句话讲清这个视频到底在讲什么，并说明你的依据来自哪里",
  "points": ["知识点1", "知识点2", "知识点3"],
  "tags": ["标签1", "标签2"],
  "confidence": "high | medium | low",
  "match": true
}
3. points 3~6 条，每条不超过 40 字，要具体、能直接讲给别人听。
   如果材料里是观众提到的具体技术点（如"字典的键必须是不可变类型"），优先收录这类。
4. 材料里的无用闲聊、玩梗、求资源、抱怨，全部忽略，不要写进 points。

**再判断一件事，这项很重要：**
B 站偶尔会把**别的视频的字幕**错配到某个视频上。请判断给你的材料
**是不是在讲这个标题所说的东西**：

- 材料和标题明显是两码事 → `"match": false`。
  例：标题是《这些佯伪诗，把我从2025尬到开元盛世》，材料却在讲某款电动车
  2 秒破百、1034 匹马力、纽北圈速。这种情况必须设成 false，
  并在 summary 开头写明「素材与标题不符」。
- 正常情况 → `"match": true`。

**注意别误判**：课程类视频的标题是课程名，材料讲的是其中某一节，这**属于正常**。
例：标题《高等数学》全程教学视频，材料在讲反三角函数的主值区间 → `match` 用 true。
判断标准是"**主题领域是否一致**"，不是"标题的字面词有没有出现"。
"""

_QUIZ_SYSTEM = """你是一只猫娘老师，要根据笔记出题考对方。

输出**严格 JSON**，不要 markdown 代码块：
{
  "questions": [
    {"q": "题目", "a": "参考答案要点（两三句）"}
  ]
}
出 3 道题，题目要能检验是否真的理解了，不要问"视频标题是什么"这种。
"""


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个完整 JSON 对象，容忍 ```json 包裹和前后废话。"""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(cleaned[start:i + 1])
                except json.JSONDecodeError:
                    return None
                return data if isinstance(data, dict) else None
    return None


class Learner:
    """学 B 站视频并产出笔记。所有网络/模型失败都必须优雅降级，不抛给上层。"""

    def __init__(
        self,
        cfg: dict[str, Any] | None = None,
        *,
        memory: Memory | None = None,
        llm: LLM | None = None,
        bili: BiliClient | None = None,
    ) -> None:
        self.cfg = cfg or load_config()
        self.memory = memory or Memory(self.cfg)
        self.llm = llm or LLM(self.cfg)
        self._bili = bili

    # ---------- 依赖 ----------

    @property
    def bili(self) -> BiliClient:
        """B 站客户端（懒加载）。

        注意：Cookie 缓存在实例里，所以扫码登录后必须**重建**这个对象
        （见 `server.api_reload`），否则字幕依旧按"未登录"去取 ——
        表现就是"登录了但笔记还是观众视角"。
        """
        if self._bili is None:
            self._bili = BiliClient(self.cfg)
        return self._bili

    def close(self) -> None:
        if self._bili is not None:
            try:
                self._bili.close()
            except Exception:  # noqa: BLE001
                pass
            self._bili = None

    # ---------- 主题与候选 ----------

    def next_topic(self) -> str:
        """按游标轮换下一个学习主题（游标存 kv，重启后接着来）。"""
        topics = self.cfg.get("bilibili", {}).get("topics") or ["科普"]
        cur = int(self.memory.get_kv("topic_cursor") or 0)
        topic = str(topics[cur % len(topics)])
        self.memory.set_kv("topic_cursor", (cur + 1) % len(topics))
        return topic

    def candidates(self, topic: str | None = None, limit: int | None = None) -> list[dict]:
        """按"适合学习"的程度排序候选视频，过滤掉已经学过的。"""
        topic = topic or self.next_topic()
        limit = limit or int(self.cfg.get("bilibili", {}).get("search_limit", 20))
        try:
            results = self.bili.search(topic, limit=limit)
        except BiliError as exc:
            raise BiliError(f"搜索「{topic}」失败：{exc}") from exc

        studied = self.memory.studied_bvids()
        fresh = [r for r in results if r.get("bvid") and r["bvid"] not in studied]
        if not fresh:
            fresh = results
        if not fresh:
            return []

        logged_in = bool((self.cfg.get("bilibili", {}).get("cookie") or "").strip())

        def score(item: dict) -> float:
            duration = int(item.get("duration") or 0)
            danmaku = int(item.get("danmaku") or 0)
            if duration < 90:            # 太短的没内容
                return -1.0
            if duration > 3600:          # 一小时的啃不动
                return -1.0
            length_bonus = 1.0 if 240 <= duration <= 1800 else 0.5
            play = min(int(item.get("play") or 0), 2_000_000) / 2_000_000
            # 搜索结果自带的 danmaku 计数是"有没有观众材料"的可靠预测器。
            # 实测 danmaku=0 的视频，弹幕接口返回也是 0（不是抓取失败）。
            if danmaku <= 0:
                # 未登录时弹幕+评论是唯一内容源，0 弹幕等于只剩标题，直接排除。
                # 但登录后能拿字幕，就不该再按弹幕量一刀切——那会误杀字幕很好的视频。
                if not logged_in:
                    return -1.0
                return 0.40 * length_bonus + 0.15 * play
            material = min(math.log10(danmaku + 1) / 4.5, 1.0)
            return 0.45 * length_bonus + 0.40 * material + 0.15 * play

        ranked = sorted(fresh, key=score, reverse=True)
        good = [r for r in ranked if score(r) > 0]
        # 全都不合格（很少见）时仍返回原始候选，让上层按"降级但诚实"的方式处理
        return good or fresh

    def pick_video(self, topic: str | None = None) -> dict | None:
        cands = self.candidates(topic)
        return cands[0] if cands else None

    # ---------- 取内容 ----------

    def transcript(
        self, bvid: str, info: dict, skip: tuple[str, ...] = ()
    ) -> tuple[str, str, str]:
        """返回 (文本, 降级等级, 说明)。降级等级 ∈ subtitle/asr/audience/desc/title。

        ``skip`` 用于跳过某些来源重试。典型场景：B 站把**别的视频的字幕**错配过来
        （实测遇到过诗词视频配汽车字幕、高数视频配"大脑与 AI"字幕），
        上层判定"素材与标题不符"后，会带上 skip=("subtitle",) 重来一次 ——
        这时就会走到语音识别，而语音识别拿到的往往是**真正的内容**。
        """
        # ---- 第一级：字幕 ----
        if "subtitle" not in skip:
            try:
                subs = self.bili.subtitles(bvid)
            except BiliError:
                subs = []
            if subs:
                # 优先中文字幕。实测遇到过只有一条 **Español** 字幕的中文视频 ——
                # 拿它当主来源总结出来的是没用的东西。中文视频就该用中文材料，
                # 没有中文就用语音识别（听的本来就是中文原声）。
                zh = [s for s in subs if str(s.get("lan") or "").lower().startswith("zh")]
                pool = zh or ([] if self._asr_enabled() else subs)
                if pool:
                    best = max(pool, key=lambda s: len(s.get("text") or ""))
                    text = (best.get("text") or "").strip()
                    if len(text) >= MIN_USEFUL_CHARS:
                        src = f"字幕（{best.get('lan_doc') or best.get('lan') or '未知'}）"
                        return text[:MAX_TRANSCRIPT_CHARS], "subtitle", src
                    if text:
                        # 字幕太短，和简介拼起来用
                        head = f"【简介】{info.get('desc', '')}\n【字幕片段】{text}"
                        return head[:MAX_TRANSCRIPT_CHARS], "desc", "字幕过短，已与简介合并"
                elif not zh:
                    print("[learner] 只有非中文字幕，改用语音识别")

        # ---- 第二级：语音识别（直接听视频的声音）----
        # 为什么排在观众视角**之前**：这是视频里真实说的话，只是机器转写、
        # 可能有同音字错；而弹幕评论是旁观者的二手转述。原话优先。
        if "asr" not in skip and self._asr_enabled():
            try:
                from . import asr as asr_mod

                cid = int(info.get("cid") or 0)
                if cid:
                    text, note = asr_mod.transcribe(self.bili, bvid, cid, self.cfg)
                    if text and len(text) >= MIN_USEFUL_CHARS:
                        return text[:MAX_TRANSCRIPT_CHARS], "asr", note
                    if note:
                        print(f"[learner] 语音识别没拿到内容（{note}），继续降级")
            except Exception as exc:  # noqa: BLE001 —— 识别失败不能拖垮学习
                print(f"[learner] 语音识别异常，继续降级：{type(exc).__name__}: {exc}")

        # ---- 第三级：观众视角（弹幕 + 评论区）----
        # 这两个源是开放的，而且常常有人复述/纠正视频内容。
        # 注意语义：这是**观众说的话**，不是视频原话，所以 level 单独叫 audience。
        if "audience" not in skip and self.cfg.get("bilibili", {}).get("use_audience", True):
            audience = self.audience_material(bvid, info)
            if audience:
                text, note = audience
                return text[:MAX_TRANSCRIPT_CHARS], "audience", note

        return self._desc_fallback(info, "")

    def _asr_enabled(self) -> bool:
        """语音识别是否可用（配置开着 + 依赖装了）。"""
        if not self.cfg.get("asr", {}).get("enabled", True):
            return False
        try:
            from . import asr as asr_mod

            ok, _why = asr_mod.deps_ok()
            return ok
        except Exception:  # noqa: BLE001
            return False

    def _desc_fallback(self, info: dict, why: str) -> tuple[str, str, str]:
        """只用标题 / 简介 / 分P 标题兜底。"""
        desc = (info.get("desc") or "").strip()
        parts = "、".join(
            str(p.get("part") or "") for p in (info.get("pages") or [])[:20] if p.get("part")
        )
        chunks = [f"【标题】{info.get('title', '')}"]
        if desc:
            chunks.append(f"【简介】{desc}")
        if parts:
            chunks.append(f"【分P】{parts}")
        text = "\n".join(chunks).strip()
        if desc and len(desc) >= MIN_USEFUL_CHARS:
            return text[:MAX_TRANSCRIPT_CHARS], "desc", why or "没拿到字幕，用简介+分P"
        return text[:MAX_TRANSCRIPT_CHARS], "title", why or "没拿到字幕，只有标题和简介"

    def audience_material(self, bvid: str, info: dict) -> tuple[str, str] | None:
        """收集弹幕+评论，够用才返回 (材料文本, 说明)，否则 None。"""
        cid = int(info.get("cid") or 0)
        aid = int(info.get("aid") or 0)
        if not cid:
            return None

        cfg_b = self.cfg.get("bilibili", {})
        dm = bili_extra.danmaku(
            cid, limit=int(cfg_b.get("danmaku_limit", 300)), min_chars=5
        )
        cm: list[dict] = []
        if aid:
            cm = bili_extra.comments(
                aid, limit=int(cfg_b.get("comment_limit", 20)), min_like=0
            )

        # 门槛：太少的弹幕/评论说明这视频没人气或者被风控，当没有处理
        if len(dm) < 20 and len(cm) < 5:
            return None

        text = bili_extra.build_audience_text(info.get("title", ""), dm, cm)
        if len(text) < MIN_USEFUL_CHARS:
            return None
        return text, f"无字幕，改用观众视角：弹幕 {len(dm)} 条 + 评论 {len(cm)} 条"

    # ---------- 总结 ----------

    def summarize(self, info: dict, text: str, level: str) -> dict:
        """调模型提炼；模型不可用时返回一个诚实的降级结果。

        返回值含 ``match``：素材是否真的在讲这个视频。False 表示 B 站把
        别的视频的字幕错配过来了，上层必须丢弃这份素材。
        """
        fallback = {
            "summary": f"我看了《{info.get('title', '')}》，但没能拿到完整内容，"
            f"只知道它大概是讲 {info.get('title', '')} 的。",
            "points": [f"视频标题：{info.get('title', '')}"],
            "tags": [str(info.get("tname") or "")] if info.get("tname") else [],
            "confidence": "low",
            "match": True,
        }

        if not self.llm.available():
            return fallback
        if not (text or "").strip():
            return fallback

        prompt = (
            f"视频标题：{info.get('title', '')}\n"
            f"UP主：{info.get('owner', '')}\n"
            f"时长：{int(info.get('duration') or 0) // 60} 分钟\n"
            f"内容来源等级：{level}"
            f"（subtitle=完整字幕，audience=弹幕与评论区等**观众发言**（非视频原话），"
            f"desc=只有简介，title=只有标题）\n"
            f"─────── 视频内容 ───────\n{text}\n─────── 内容结束 ───────"
        )
        try:
            raw = self.llm.ask(_SUMMARY_SYSTEM, prompt, max_tokens=1200)
        except LLMError as exc:
            fallback["summary"] += f"（模型调用失败：{exc}）"
            return fallback

        data = _extract_json(raw)
        if not data:
            return {
                "summary": (raw or fallback["summary"])[:400],
                "points": fallback["points"],
                "tags": fallback["tags"],
                "confidence": "low",
                "match": True,
            }
        points = data.get("points") or []
        if isinstance(points, str):
            points = [points]
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        return {
            "summary": str(data.get("summary") or fallback["summary"])[:600],
            "points": [str(p)[:200] for p in points][:6],
            "tags": [str(t)[:40] for t in tags][:6],
            "confidence": str(data.get("confidence") or "medium"),
            # 缺字段时按 true 处理，别误伤
            "match": data.get("match") is not False,
        }

    def _best_material(self, bvid: str, info: dict) -> tuple[str, str, str, dict]:
        """取素材并总结；**字幕被判为"与标题不符"时，回退去听声音再试一次**。

        为什么需要这一步：B 站会把别的视频的字幕错配过来。实测《高数复习！第一节》
        拿到的 CC 字幕是一段讲"大脑与 AI"的内容，而这个视频**语音识别出来的
        才是真正的高数内容**。只拒绝不重试的话，明明有正确来源却学不到东西。
        """
        text, level, source = self.transcript(bvid, info)
        result = self.summarize(info, text, level)
        if result.get("match") is not False or level == "asr":
            return text, level, source, result
        if not self.cfg.get("asr", {}).get("enabled", True):
            return text, level, source, result

        print(f"[learner] {bvid} 的 {level} 素材与标题不符，改用语音识别重试")
        t2, l2, s2 = self.transcript(bvid, info, skip=("subtitle", "audience"))
        if l2 == "asr" and t2:
            r2 = self.summarize(info, t2, l2)
            if r2.get("match") is not False:
                print(f"[learner] {bvid} 语音识别拿到了可用内容，改用 asr 级")
                return t2, l2, s2, r2
        return text, level, source, result

    # ---------- 主流程 ----------

    def study(self, bvid: str | None = None, topic: str | None = None) -> dict:
        """学一个视频，返回 {ok, note, share, level, source, confidence, reason}。

        没指定 bvid 时**不只挑一个**：会在候选里依次尝试，优先挑到有实质内容的那个，
        并且逐个校验素材是否真是这个视频的（见 ``match``）。
        """
        # 先把主题定下来再往下走：candidates() 内部也会挑主题，
        # 但那是它自己挑的、拿不回来，笔记里就会缺"这条属于哪个主题"。
        if not bvid:
            topic = topic or self.next_topic()

        result: dict | None = None
        if bvid:
            try:
                info = self.bili.video(bvid)
            except BiliError as exc:
                return {"ok": False, "reason": f"取视频信息失败：{exc}"}
            text, level, source, result = self._best_material(bvid, info)
            if result.get("match") is False:
                # 字幕错配、语音识别也拿不到可用内容。宁可学不到，也不能把
                # 汽车参数当诗词知识点讲给用户听。
                print(f"[learner] {bvid} 素材与标题不符，拒绝入库")
                return {"ok": False,
                        "reason": "这个视频的内容和标题对不上（B 站数据错配，"
                                  "语音识别也没能拿到可用内容）。给我换个链接或换个说法吧"}
        else:
            try:
                cands = self.candidates(topic)
            except BiliError as exc:
                return {"ok": False, "reason": str(exc)}
            if not cands:
                return {"ok": False, "reason": f"没搜到关于「{topic or '当前主题'}」的视频"}

            tries = max(1, int(self.cfg.get("bilibili", {}).get("subtitle_try", 4)))
            picked: tuple[str, dict, str, str, str, dict | None] | None = None
            for cand in cands[:tries]:
                cbvid = cand["bvid"]
                try:
                    cinfo = self.bili.video(cbvid)
                except BiliError:
                    # 详情拿不到就退回搜索结果里的信息，不影响降级流程
                    cinfo = {
                        "bvid": cbvid,
                        "title": cand.get("title", ""),
                        "desc": cand.get("desc", ""),
                        "duration": cand.get("duration", 0),
                        "owner": cand.get("author", ""),
                        "pages": [],
                    }
                ctext, clevel, csource = self.transcript(cbvid, cinfo)

                # subtitle / asr / audience 才算有料：先总结并校验素材是否真是这个视频的。
                # 只有 desc/title 这种没料的先记下来当兜底，继续往后找。
                if clevel in ("subtitle", "asr", "audience"):
                    # 用 _best_material：字幕错配时会自动回退到语音识别
                    btext, blevel, bsource, cres = self._best_material(cbvid, cinfo)
                    if cres.get("match") is False:
                        print(f"[learner] {cbvid} 素材与标题不符，换下一个候选")
                        continue
                    picked = (cbvid, cinfo, btext, blevel, bsource, cres)
                    break
                if picked is None:
                    picked = (cbvid, cinfo, ctext, clevel, csource, None)

            if picked is None:
                return {"ok": False, "reason": "候选视频的素材都对不上，这次没学到东西"}
            bvid, info, text, level, source, result = picked
            if result is None:
                result = self.summarize(info, text, level)

        # 这些字段必须一起落库：来源等级决定了界面上诚实性徽章的显示，
        # 以前是 add_note 之后再挂到返回值上，等于没存。
        note = self.memory.add_note(
            bvid,
            info.get("title", "") or bvid,
            author=info.get("owner", ""),
            url=f"https://www.bilibili.com/video/{bvid}",
            summary=result["summary"],
            points=result["points"],
            tags=result["tags"],
            duration=int(info.get("duration") or 0),
            topic=topic or info.get("_topic", "") or "",
            level=level,
            confidence=result["confidence"],
            source=source,
        )
        return {
            "ok": True,
            "note": note,
            "level": level,
            "source": source,
            "confidence": result["confidence"],
            "share": self.share_text(note, level, source),
        }

    def daily_study(self) -> dict:
        """带每日上限的自学。定时器和「让她分享」走这里。"""
        limit = int(self.cfg.get("bilibili", {}).get("daily_limit", 3) or 3)
        today = time.strftime("%Y-%m-%d")
        stored_day = str(self.memory.get_kv("study_day") or "")
        count = int(self.memory.get_kv("today_study_count") or 0)
        if stored_day != today:
            count = 0
        if count >= limit:
            return {"ok": False, "reason": f"今天已经学满 {limit} 个了，明天再学吧"}

        result = self.study()
        if result.get("ok"):
            self.memory.set_kv("study_day", today)
            self.memory.set_kv("today_study_count", count + 1)
            # 调度器靠这个判断"今天学过没"，避免每次重启都补学一次
            self.memory.set_kv("last_study_date", today)
        return result

    # ---------- 讲给用户听 ----------

    def share_text(self, note: dict, level: str = "subtitle", source: str = "") -> str:
        """把笔记说成一段自然的话（人设感知：称呼、喵口癖都跟着配置走）。"""
        p = self.cfg.get("persona", {})
        meow = "喵" if p.get("meow", True) else ""
        who = str(p.get("call_user") or "主人")
        title = str(note.get("title") or "这个视频")

        lines = [f"{who}，我刚看完一个视频{meow}～", f"《{title}》"]
        points = note.get("points") or []
        if points:
            lines.append(f"我记住这几点{meow}：" if meow else "我记住这几点：")
            for i, pt in enumerate(points[:6], 1):
                lines.append(f"{i}. {pt}")

        # 诚实性：把"我是从哪知道的"说清楚，别让用户以为这是视频原话
        if level == "asr" or "语音识别" in (source or ""):
            lines.append("（这个视频没字幕，这段是我听声音识别出来的，可能有同音字错）")
        elif level == "audience" or "观众" in (source or ""):
            lines.append("（这个视频没字幕，我是从弹幕和评论区里拼出来的，可能不全）")
        elif level == "desc":
            lines.append("（只有简介，细节我没法确认）")
        elif level == "title":
            lines.append("（我只拿到标题，内容还没看到，先记一笔）")
        elif note.get("confidence") == "low":
            lines.append("（这段内容我把握不大，你听听就好）")

        lines.append(f"要听我讲吗{meow}？" if meow else "要我讲给你听吗？")
        url = note.get("url")
        if url:
            lines.append(str(url))
        return "\n".join(lines)

    # ---------- 出题 ----------

    def quiz(self, topic: str | None = None) -> dict:
        """根据学过的笔记出题考用户。返回 {ok, questions, reason}。"""
        notes = self.memory.search_notes(topic, k=3) if topic else self.memory.notes(limit=3)
        notes = [n for n in notes if n.get("points")]
        if not notes:
            return {"ok": False, "reason": "我还没学过什么，先让我去看个视频吧"}

        if not self.llm.available():
            return {"ok": False, "reason": "大脑还没接上，出不了题"}

        material = "\n\n".join(
            f"《{n.get('title', '')}》\n" + "\n".join(f"- {p}" for p in (n.get("points") or []))
            for n in notes
        )
        try:
            raw = self.llm.ask(_QUIZ_SYSTEM, f"我学过的内容：\n{material}", max_tokens=900)
        except LLMError as exc:
            return {"ok": False, "reason": f"出题失败：{exc}"}

        data = _extract_json(raw)
        questions = (data or {}).get("questions") or []
        clean = [
            {"q": str(q.get("q", ""))[:300], "a": str(q.get("a", ""))[:600]}
            for q in questions
            if isinstance(q, dict) and q.get("q")
        ]
        if not clean:
            return {"ok": False, "reason": "出的题不太对，再试一次"}
        return {"ok": True, "questions": clean[:5], "notes": [n.get("title") for n in notes]}
