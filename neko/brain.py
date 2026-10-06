"""编排层：一条用户消息 → 一条猫娘回复。

这是**唯一**的对外入口，面板和微信桥都调 `Brain.reply()`，
保证两种通道下猫娘是同一个人、同一套记忆。

支持的自然语言指令（中文为主，也接受 / 前缀）：
    学习 / 学点东西 / 看个视频 [主题]    → 去 B 站学一个视频
    <B站链接或BV号>                    → 学你指定的这个视频
    考考我 / 出题                       → 根据笔记出题
    笔记 / 你学过什么                   → 列出学过的视频
    记住 xxx                           → 写入长期记忆
    状态                               → 打印系统状态
    帮助                               → 指令列表
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator

from .config import load_config, missing_key_hint
from .learner import Learner
from .llm import LLM, LLMError
from .memory import Memory
from .persona import greeting, system_prompt

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_URL_RE = re.compile(r"https?://(?:www\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10})")
#: 她在回复里写 [学习:主题] 就真的去学（见 _resolve_study_marker）
_STUDY_MARK_RE = re.compile(r"\[学习:([^\]]+)\]")

# 被动记忆抽取：只说"我喜欢X"这种，猫娘也悄悄记住
_PASSIVE_MEMORY = [
    (re.compile(r"我(?:最)?喜欢(.{1,30})"), "preference", "喜欢"),
    (re.compile(r"我叫(.{1,20})"), "fact", "名字"),
    (re.compile(r"我是(.{1,30}?)(?:，|。|,|$)"), "fact", "身份"),
    (re.compile(r"我(?:在|住在)(.{1,25}?)(?:，|。|,|$)"), "fact", "所在"),
    (re.compile(r"我(?:想|要|准备)学(.{1,30})"), "goal", "学习目标"),
]

def help_text(meow: bool = True) -> str:
    """指令列表。带不带「喵」跟随人设，免得跟冷漠人设打架。"""
    return (
        f"我能做的事{'喵' if meow else ''}：\n"
        "· 直接跟我聊天 —— 想到什么说什么\n"
        "· 「学点东西」或「学点 <主题>」—— 我自己去 B 站看视频\n"
        "· 丢一个 B 站链接或 BV 号给我 —— 我学这个\n"
        "· 「考考我」—— 我根据学过的内容出题\n"
        "· 「你学过什么」—— 看我攒的笔记\n"
        "· 「记住 ……」—— 我写进长期记忆\n"
        "· 「状态」—— 看看我的运行情况"
    )


class Brain:
    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or load_config()
        self.memory = Memory(self.cfg)
        self.llm = LLM(self.cfg)
        self.learner = Learner(self.cfg, memory=self.memory, llm=self.llm)
        self._tts = None
        self._tts_tried = False

    # ---------- 语音（延迟加载，缺依赖也不影响聊天） ----------

    @property
    def tts(self):
        if not self._tts_tried:
            self._tts_tried = True
            if self.cfg.get("voice", {}).get("enabled", True):
                try:
                    from .tts import NekoTTS

                    self._tts = NekoTTS(self.cfg)
                except Exception as exc:  # noqa: BLE001 —— 语音坏了不能拖垮聊天
                    print(f"[brain] 语音模块不可用，仅文字模式：{exc}")
                    self._tts = None
        return self._tts

    def voice(self, text: str) -> str | None:
        """把回复合成成语音文件，失败返回 None（绝不让语音错误影响回复）。

        语气：先从**原文**（含 `[表情:x]` 标记）判定情绪，再把标记剥掉送去合成。
        顺序不能反 —— 标记剥掉之后就没法判情绪了，而那正是最可靠的信号。
        """
        from . import prosody as pr

        tts = self.tts
        if tts is None or not text:
            return None
        emotion = pr.detect_emotion(text)
        # 语音里不该把标记和颜文字念出来
        spoken = pr.strip_markers(text)
        spoken = re.sub(r"[（(][^）)]{0,12}[゜ω・∀´｀∇≧≦▽皿д][^）)]{0,12}[）)]", "", spoken)
        spoken = spoken.strip()
        if not spoken:
            return None
        try:
            if not tts.available():
                return None
            return tts.synth_to_cache(spoken, emotion=emotion)
        except Exception as exc:  # noqa: BLE001
            print(f"[brain] 语音合成失败：{exc}")
            return None

    # ---------- 记忆抽取 ----------

    def _absorb(self, text: str) -> list[str]:
        """从用户话里悄悄记住一些信息，返回记住的条目。"""
        absorbed: list[str] = []
        m = re.match(r"^记住[，,：:\s]*(.+)$", text.strip())
        if m:
            value = m.group(1).strip()
            if value:
                whoever = self.cfg.get("persona", {}).get("call_user", "主人")
                self.memory.remember(value, kind="fact", key=f"{whoever}说的")
                absorbed.append(value)
            return absorbed
        for pattern, kind, key in _PASSIVE_MEMORY:
            hit = pattern.search(text)
            if hit:
                value = hit.group(1).strip(" 的了吧呢")
                if 1 < len(value) <= 30 and not value.startswith("不"):
                    self.memory.remember(value, kind=kind, key=key)
                    absorbed.append(f"{key}：{value}")
        return absorbed

    # ---------- 对话 ----------

    def system(self, user_text: str = "") -> str:
        memories = self.memory.search_memories(user_text, k=8) if user_text else self.memory.all_memories()[:8]
        notes = self.memory.notes(limit=8)
        return system_prompt(self.cfg, memories=memories, notes=notes)

    def _resolve_study_marker(self, text: str) -> str:
        """她回复里写 ``[学习:主题]`` 就**真的去学**，并把结果接在后面。

        为什么需要这个：用户说「从古今诗词开始吧」这类自然说法时，匹配不上
        旧的指令正则，于是走了普通聊天 —— 模型就顺着演一段"我要去学习"的戏，
        一集都没看。用户看到的正是"命令她去学习没用"。
        现在承诺和行动被绑在一起：写了标记就一定会学。
        """
        hit = _STUDY_MARK_RE.search(text or "")
        if not hit:
            return text
        topic = hit.group(1).strip()
        head = _STUDY_MARK_RE.sub("", text).strip()
        result = self._do_study(topic=topic or None)
        body = str(result.get("text") or "").strip()
        if not body:
            body = self._m("我没找到合适的视频喵…换个说法，或者直接丢个链接给我？",
                           "没找到合适的视频。换个说法，或者直接给我链接。")
        print(f"[brain] 她主动去学了：{topic or '(轮换主题)'}")
        return f"{head}\n\n{body}".strip() if head else body

    def _resolve_device_marks(self, text: str) -> str:
        """她回复里写 ``[设备:台灯=开]`` 就**真的去执行**，并把结果接在后面。

        沿用 `[学习:主题]` 的同一套范式：标记 = 承诺，写了就一定执行。

        **危险动作不直接执行**：标了 dangerous 的（开锁、断电这类）先把待办挂进 kv，
        回复里问她一句"确定吗"，等用户明确确认再真正下发。
        软件里删错文件能读存档，物理世界里开错锁没有存档。
        """
        from . import devices as dv

        text = str(text or "")
        if "[设备:" not in text:
            return text
        reg = dv.DeviceRegistry(self.cfg)
        if not reg.enabled:
            # 没开这个功能就把标记删掉，别让用户看到 [设备:xxx] 这种原始文本
            return dv.MARK_RE.sub("", text).strip()

        def repl(m: re.Match[str]) -> str:
            name, action = m.group(1).strip(), m.group(2).strip()
            dev, err = reg.resolve(name, action)
            if dev is None:
                dv.audit(self.memory, None, f"{name}={action}",
                         {"ok": False, "reason": err}, source="chat")
                print(f"[devices] 拒绝执行：{err}")
                return f"（{err}）"

            if dev["dangerous"]:
                # 挂起，等确认。只保留最近一个待办，够用。
                self.memory.set_kv(dv.pending_key(), json.dumps(
                    {"device": dev["name"], "id": dev["id"], "action": action},
                    ensure_ascii=False))
                print(f"[devices] 危险动作待确认：{dev['name']}={action}")
                return f"（{dev['name']}属于危险操作，我先不动手 —— 你确认了我再执行）"

            result = reg.execute(dev, action)
            dv.audit(self.memory, dev, action, result, source="chat")
            if result.get("ok"):
                state = result.get("state")
                print(f"[devices] 已执行 {dev['name']}={action}" + (f" -> {state}" if state else ""))
                return f"（已执行：{dev['name']} {action}）"
            why = result.get("reason") or "设备没响应"
            print(f"[devices] 执行失败 {dev['name']}={action}：{why}")
            return f"（{dev['name']}没执行成功：{why}）"

        return dv.MARK_RE.sub(repl, text).strip()

    def confirm_pending_action(self, text: str) -> str | None:
        """处理挂起的危险动作。返回 None 表示当前没有待确认的事。"""
        from . import devices as dv

        pending = dv.parse_pending(self.memory.get_kv(dv.pending_key()))
        if not pending:
            return None
        if dv.is_cancel(text):
            self.memory.set_kv(dv.pending_key(), "")
            dv.audit(self.memory, {"id": pending.get("id"), "name": pending.get("device")},
                     str(pending.get("action")), {"ok": False, "reason": "用户取消"},
                     source="confirm")
            return self._m("好，那我不动它喵。", "好，不动了。")
        if not dv.is_confirm(text):
            return None     # 用户在说别的，待办继续挂着

        self.memory.set_kv(dv.pending_key(), "")
        reg = dv.DeviceRegistry(self.cfg)
        dev, err = reg.resolve(str(pending.get("device") or ""), str(pending.get("action") or ""))
        if dev is None:
            return f"（{err}）"
        result = reg.execute(dev, str(pending.get("action")))
        dv.audit(self.memory, dev, str(pending.get("action")), result, source="confirm")
        if result.get("ok"):
            return self._m(f"嗯，{dev['name']}已经按你说的执行了喵。",
                           f"{dev['name']}已经执行了。")
        return f"（{dev['name']}没执行成功：{result.get('reason') or '设备没响应'}）"

    def _resolve_emote_marks(self, text: str) -> str:
        """把 ``[表情:key]`` 落成具体的图。

        **只用用户自己那套表情包，不再往 B 站官方表情兜底。**

        踩过的坑（两次）：
        - 最早走 Bing 搜图：图和当下情绪**没有任何语义关联**（程序只按哈希错开取，
          根本不知道图里是什么），用户收到过一张"××眼"配在正经问题后面。
        - 后来改成映射到 B 站官方表情：标签是`滑稽`/`辣眼睛`/`抠鼻`/`阴险`这类
          **弹幕梗图**，跟"可爱猫娘"完全不是一回事，人设直接崩了。

        现在：key 必须在本地表情包里，否则**直接删掉标记**（不猜、不兜底）。
        猜错比不发更糟 —— 表情包图上有字，发出去等于她也说了那句话。
        """
        if "[表情:" not in text:
            return text
        from . import stickers as st

        local_keys = {s["key"] for s in st.list_user(self.cfg)}
        dropped: list[str] = []

        def repl(m: re.Match[str]) -> str:
            key = m.group(1).strip()
            if key in local_keys:
                return m.group(0)          # 本地有，交给前端渲染
            dropped.append(key)
            return ""

        out = re.sub(r"\[表情:([^\]]+)\]", repl, text)
        if dropped:
            print(f"[emotes] 丢掉不存在的表情键：{dropped}（可用：{sorted(local_keys)}）")
        return out

    def _resolve_media(self, text: str) -> str:
        """把回复里的表情包标记落成可用链接。

        - [表情:key]      -> 本地表情包，前端直接渲染，这里不动
        - [图片:关键词]    -> 联网搜一张，下载缓存，换成短的本地链接
        - [图片:/api/...] -> 已经是本地链接，不动

        搜不到/下载失败就把标记删掉：宁可少一张图，也不能让她回一句
        带着 "[图片:xxx]" 这种原始标记的怪话。
        """
        if "[图片:" not in text:
            return text
        from . import stickers as st

        cache = self.cfg.get("stickers", {}).get("cache_dir", "data/stickers_cache")

        def repl(m: re.Match[str]) -> str:
            token = m.group(1).strip()
            if token.startswith("/api/"):
                return m.group(0)
            url = token if token.startswith(("http://", "https://")) else ""
            if not url:
                # 传 cfg 进去，让检索式补上"动漫猫娘 表情包 q版"这类限定词，
                # 否则搜到的是相机拍的真猫照片，和猫娘形象不搭。
                hits = st.search(token, limit=3, cfg=self.cfg)
                if not hits:
                    return ""
                url = hits[0]["url"]
            local = st.fetch_image(url, cache)
            return f"[图片:/api/stickers/got/{local.name}]" if local else ""

        out = re.sub(r"\[图片:([^\]]+)\]", repl, text)
        return re.sub(r"[ \t]{2,}", " ", out).strip()

    def chat(self, text: str, channel: str = "local") -> dict:
        """普通聊天（非流式）。"""
        self.memory.add_message("user", text, channel=channel)
        self._absorb(text)

        # 有挂起的危险动作、且这句是在确认/取消 -> 直接处理，不走 LLM。
        # 走 LLM 的话她可能把"确认"理解成别的意思，而这是要开锁的场合。
        confirmed = self.confirm_pending_action(text)
        if confirmed is not None:
            self.memory.add_message("assistant", confirmed, channel=channel)
            return {"text": confirmed, "kind": "device", "audio": self.voice(confirmed)}

        # 上一条是"一起听"、这句是点头 -> 现在才展开讲（不走普通闲聊）
        explained = self.music_explain(text)
        if explained is not None:
            self.memory.add_message("assistant", explained, channel=channel)
            return {"text": explained, "kind": "music", "audio": self.voice(explained)}

        messages = [{"role": "system", "content": self.system(text)}]
        messages += self.memory.history_for_llm()
        # history_for_llm 已含刚写入的这条 user 消息

        if not self.llm.available():
            reply = self._offline_reply(text)
        else:
            try:
                reply = self.llm.complete(messages)
            except LLMError as exc:
                whoever = self.cfg["persona"].get("call_user", "主人")
                reply = self._m(
                    f"呜…我的脑子刚刚卡住了（{exc}）。{whoever}再说一次好不好？",
                    f"我这边断了一下（{exc}）。你再说一次。",
                )

        reply = self._resolve_music_marks(self._resolve_media(
            self._resolve_emote_marks(
                self._resolve_device_marks(self._resolve_study_marker((reply or "").strip() or self._nudge()))
            )
        ))
        self.memory.add_message("assistant", reply, channel=channel)
        return {"text": reply, "kind": "chat", "audio": self.voice(reply)}

    def chat_stream(self, text: str, channel: str = "local") -> Iterator[dict]:
        """流式聊天，逐段产出事件：{"type":"delta"|"done","text":...}。"""
        # 记住这次问答两条消息的 id，末尾一起带回去 —— 前端靠它做"按条删除"
        user_id = self.memory.add_message("user", text, channel=channel)
        self._absorb(text)

        confirmed = self.confirm_pending_action(text)
        if confirmed is not None:
            aid = self.memory.add_message("assistant", confirmed, channel=channel)
            yield {"type": "delta", "text": confirmed}
            yield {"type": "done", "text": confirmed, "audio": self.voice(confirmed),
                   "user_id": user_id, "assistant_id": aid}
            return

        # 上一条是"一起听"、这句是点头 -> 现在才展开讲
        explained = self.music_explain(text)
        if explained is not None:
            aid = self.memory.add_message("assistant", explained, channel=channel)
            yield {"type": "delta", "text": explained}
            yield {"type": "done", "text": explained, "audio": self.voice(explained),
                   "user_id": user_id, "assistant_id": aid}
            return

        if not self.llm.available():
            reply = self._resolve_media(
                self._resolve_emote_marks(self._resolve_study_marker(self._offline_reply(text)))
            )
            yield {"type": "delta", "text": reply}
            aid = self.memory.add_message("assistant", reply, channel=channel)
            yield {"type": "done", "text": reply, "audio": self.voice(reply),
                   "user_id": user_id, "assistant_id": aid}
            return

        messages = [{"role": "system", "content": self.system(text)}]
        messages += self.memory.history_for_llm()
        buffer: list[str] = []
        try:
            for piece in self.llm.chat_stream(messages):
                buffer.append(piece)
                yield {"type": "delta", "text": piece}
        except LLMError as exc:
            note = f"（我这边断线了：{exc}）"
            buffer.append(note)
            yield {"type": "delta", "text": note}

        reply = self._resolve_music_marks(self._resolve_media(
            self._resolve_emote_marks(
                self._resolve_device_marks(
                    self._resolve_study_marker("".join(buffer).strip() or self._nudge())
                )
            )
        ))
        aid = self.memory.add_message("assistant", reply, channel=channel)
        yield {"type": "done", "text": reply, "audio": self.voice(reply),
               "user_id": user_id, "assistant_id": aid}

    def _offline_reply(self, text: str) -> str:
        """没配 Key 时的兜底，保证程序可用、且明确告诉用户缺什么。"""
        hint = missing_key_hint(self.cfg)
        p = self.cfg.get("persona", {})
        if p.get("meow", True):
            base = "喵…我现在还没接上大脑，只能陪你说话，答不了实际问题。"
        else:
            base = "我还没接上大脑。现在只能应答，答不了实际问题。"
        return f"{base}（{hint}）" if hint else base

    def _m(self, with_meow: str, without: str) -> str:
        """按人设选措辞：带喵口癖的用前者，不带用后者。"""
        return with_meow if self.cfg.get("persona", {}).get("meow", True) else without

    def _nudge(self) -> str:
        """空输入时的一句话，跟随人设。"""
        return self._m("喵？", "嗯？")

    # ---------- 指令 ----------

    def _handle_command(self, text: str) -> dict | None:
        raw = text.strip()
        lowered = raw.lower().lstrip("/ ")

        if re.match(r"^(帮助|help|你能做什么|指令)$", lowered):
            return {"text": help_text(self.cfg.get("persona", {}).get("meow", True)), "kind": "system"}

        if re.match(r"^(状态|status)$", lowered):
            st = self.status()
            return {"text": self._format_status(st), "kind": "system", "status": st}

        if re.match(r"^(你学过什么|笔记|我的笔记|学过什么)$", lowered):
            notes = self.memory.notes(limit=10)
            if not notes:
                return {
                    "text": self._m("我还没学过东西呢喵…要不要让我去看一个？",
                                    "我还没学过东西。要我去看一个吗？"),
                    "kind": "system",
                }
            lines = [self._m(f"我攒了 {len(notes)} 个视频的笔记喵：", f"我攒了 {len(notes)} 个视频的笔记：")]
            for n in notes[:10]:
                lines.append(f"· 《{n['title']}》（{n.get('author') or '未知UP'}）{n.get('url', '')}")
            return {"text": "\n".join(lines), "kind": "system", "notes": notes}

        if re.match(r"^(考考我|出题|小测验|测验)$", lowered):
            out = self.learner.quiz()
            if not out.get("ok"):
                return {"text": out.get("reason", "出不了题"), "kind": "system"}
            qs = out["questions"]
            lines = [self._m("来，我考考你喵～", "来。我考考你。")]
            for i, q in enumerate(qs, 1):
                lines.append(f"{i}. {q.get('q', '')}")
            lines.append("（想对答案就说「看答案」）")
            self.memory.set_kv("pending_quiz", qs)
            return {"text": "\n".join(lines), "kind": "quiz", "quiz": qs}

        if re.match(r"^(看答案|答案)$", lowered):
            qs = self.memory.get_kv("pending_quiz") or []
            if not qs:
                return {
                    "text": self._m("没有待对答案的题喵，先说「考考我」",
                                    "没有待对答案的题。先说「考考我」。"),
                    "kind": "system",
                }
            lines = [self._m("答案是喵：", "答案是：")]
            for i, q in enumerate(qs, 1):
                lines.append(f"{i}. {q.get('a', '')}")
            return {"text": "\n".join(lines), "kind": "system"}

        url_hit = _URL_RE.search(raw)
        bv_hit = _BV_RE.search(raw)
        if url_hit or bv_hit:
            bvid = (url_hit or bv_hit).group(1)
            return self._do_study(bvid=bvid)

        m = re.match(r"^/?\s*(?:学点东西|学点|学习|看个视频|看视频|刷视频)\s*(.*)$", raw)
        if m:
            topic = m.group(1).strip(" ：:，,") or None
            return self._do_study(topic=topic)

        return None

    def _do_study(self, bvid: str | None = None, topic: str | None = None) -> dict:
        result = self.learner.study(bvid=bvid, topic=topic)
        if not result.get("ok"):
            return {
                "text": self._m(f"呜，我没看成…{result.get('reason', '')}",
                                f"没看成。{result.get('reason', '')}"),
                "kind": "system",
            }
        note = result["note"]
        self.memory.mark_shared(note["bvid"])
        text = result["share"]
        return {
            "text": text,
            "kind": "study",
            "note": note,
            "level": result.get("level"),
            "source": result.get("source"),
            "audio": self.voice(text),
        }

    # ---------- 对外统一入口 ----------

    def reply(self, text: str, channel: str = "local") -> dict:
        """处理一条消息，返回 {text, kind, audio, ...}。"""
        text = (text or "").strip()
        if not text:
            return {"text": self._nudge(), "kind": "system", "audio": None}

        command = self._handle_command(text)
        if command is not None:
            if command.get("audio") is None:
                command["audio"] = self.voice(command["text"])
            self.memory.add_message("user", text, channel=channel)
            self.memory.add_message("assistant", command["text"], channel=channel)
            return command

        return self.chat(text, channel=channel)

    def proactive_share(self) -> dict | None:
        """主动分享：优先分享已学但没发过的笔记，否则去学一个新的。"""
        unshared = self.memory.unshared_notes()
        if unshared:
            note = unshared[0]
            level = "desc" if note.get("confidence") in ("low", "medium") else "subtitle"
            text = self.learner.share_text(note, level=level)
            self.memory.mark_shared(note["bvid"])
            self.memory.add_message("assistant", text, channel="proactive")
            return {"text": text, "kind": "share", "note": note, "audio": self.voice(text)}

        result = self.learner.daily_study()
        if not result.get("ok"):
            return None
        note = result["note"]
        self.memory.mark_shared(note["bvid"])
        self.memory.add_message("assistant", result["share"], channel="proactive")
        return {
            "text": result["share"],
            "kind": "share",
            "note": note,
            "audio": self.voice(result["share"]),
        }

    # ---------- 状态 ----------

    def status(self) -> dict:
        st = self.memory.stats()
        st.update(
            {
                "llm_ready": self.llm.available(),
                "llm_model": self.llm.model,
                "llm_base": self.llm.base_url,
                "voice_ready": bool(self.tts is not None and self.tts.available()),
                "persona": self.cfg.get("persona", {}).get("name", "小喵"),
                "topics": self.cfg.get("bilibili", {}).get("topics", []),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        return st

    def _format_status(self, st: dict) -> str:
        def mark(ok: bool) -> str:
            return "正常" if ok else "未配置"

        return "\n".join(
            [
                self._m(f"我是 {st['persona']}，现在状态是这样喵：",
                        f"我是 {st['persona']}。当前状态："),
                f"· 大脑（{st['llm_model']}）：{mark(st['llm_ready'])}",
                f"· 声音：{mark(st['voice_ready'])}",
                f"· 记忆库：{st['messages']} 条对话 / {st['memories']} 条记忆 / {st['notes']} 个笔记",
                f"· 学习主题：{'、'.join(st['topics']) or '未设置'}",
                f"· 时间：{st['time']}",
            ]
        )

    def greet(self) -> dict:
        text = greeting(self.cfg)
        # 把问候也存进历史。不存的话界面上那条问候不在数据库里，
        # 一刷新就会换一句新的，而且"清除后看到的那句话"下次进来就消失了。
        try:
            self.memory.add_message("assistant", text, channel="greet")
        except Exception as exc:  # noqa: BLE001 —— 存不下也不能不打招呼
            print(f"[brain] 问候存库失败：{exc}")
        return {"text": text, "kind": "greet", "audio": self.voice(text)}

    # ---------- 音乐 ----------

    def _resolve_music_marks(self, text: str) -> str:
        """把 ``[音乐:歌名]`` 落成一张可点击播放的音乐卡，**并记进"一起听过"**。

        她能主动分享音乐（就像微信发一张音乐卡片），而不只是在"一起听"里出现。

        踩过的坑：这里一开始只发卡、**不记录**，于是"她在聊天里推的歌"在
        「一起听过」列表里是空的 —— 用户反馈"对话中分享的音乐没有记录在一起听中"。
        现在两条路径（音乐页的「一起听」+ 聊天里的主动分享）都会落地成一条记录。

        **解析失败就把标记删掉**，别让用户看到 ``[音乐:xxx]`` 这种原始文本。
        """
        if "[音乐:" not in text:
            return text
        from . import music as mus

        # 她这次说的正文（去掉各种标记）—— 记进列表当这条的"评论"，
        # 否则「一起听过」里那一条的评论栏是空的，看不出当时聊了什么。
        plain = re.sub(r"\[(?:表情|图片|音乐|学习|设备|移动):[^\]]*\]", "", text).strip()

        def repl(m: re.Match[str]) -> str:
            keyword = m.group(1).strip()
            if not keyword:
                return ""
            try:
                song = mus.pick(self.cfg, keyword)
                if not song:
                    print(f"[music] 分享失败，没找到：{keyword}")
                    return ""
                card = mus.card(self.cfg, song)
                # **记进"一起听过"** —— 两条路径（音乐页的「一起听」+
                # 聊天里的主动分享）都要落地成记录。
                mus.record_session(self.cfg, song, plain[:200], source="share")
                print(f"[music] 已记录分享：《{song['title']}》")
                return mus.card_marker(card)
            except Exception as exc:  # noqa: BLE001
                print(f"[music] 分享失败：{type(exc).__name__}: {exc}")
                return ""

        return re.sub(r"\[音乐:([^\]]+)\]", repl, text)

    # ---------- 一起听音乐 ----------

    def music_together(self, keyword: str = "") -> dict:
        """一起听一首歌：挑歌 -> 读歌词 -> 她说点什么 -> **记下来**。

        为什么让她读歌词再说：只报歌名的话她只能讲场面话。
        有了歌词她才能真的聊"这首歌在讲什么"，也才对得起"记录分享"。
        """
        from . import music as mus

        cfg = self.cfg
        if not mus.enabled(cfg):
            return {"ok": False, "reason": "音乐功能已在配置里关闭"}

        song = mus.pick(cfg, keyword)
        if not song:
            st = mus.state(cfg)
            if not st.get("logged_in") and not keyword.strip():
                return {"ok": False, "reason": "还没登录网易云，或者先告诉我听什么",
                        "need_login": True}
            return {"ok": False, "reason": f"没找到「{keyword}」这首歌"}

        lyric = mus.lyric(cfg, song["id"], prov=song.get("provider", ""))
        comment = ""
        who = cfg["persona"].get("call_user", "主人")
        # **开场只说一句，然后问要不要讲。**
        # 之前这里直接让她输出 2~3 句评论，用户反馈"附加评论过多"——
        # 还没听就先被灌输一堆解读，很吵。现在把讲解挪到他点头之后。
        if self.llm.available():
            try:
                prompt = (
                    f"你刚给{who}挑了这首歌，准备一起听：\n"
                    f"《{song['title']}》— {song['artist']}\n"
                    "\n只做两件事，一共不超过 30 个字：\n"
                    "1. 说一句邀请他一起听的话（可以有你的态度，但**别夸歌、别解读歌词**）\n"
                    "2. 问他一句要不要你讲讲这首歌\n"
                    "直接给这句话，不要引号，不要解释你在做什么。"
                )
                comment = self.llm.ask(
                    "你在邀请同行者一起听歌。只说一句邀请 + 一句询问。", prompt,
                    max_tokens=int((cfg.get("music") or {}).get("invite_tokens", 80)),
                ).strip()
            except LLMError as exc:
                print(f"[music] 邀请语生成失败：{exc}")
                comment = ""
        if not comment:
            comment = f"同行者，一起听《{song['title']}》吧～ 要我讲讲吗？"
        comment = comment[:60]

        sid = mus.record_session(cfg, song, comment, source="together")
        # 也写进对话历史：这样"一起听歌"是真实发生过的一轮对话，不是弹个卡片。
        # 消息里带的卡片标记会被前端渲染成可点击播放的音乐卡（仿微信）。
        head = comment or f"同行者，我们一起听《{song['title']}》吧～"
        card = mus.card(cfg, song)
        line = f"{head}\n{mus.card_marker(card)}"
        # 记下"还没讲"，等她点头再展开 —— 别一上来就长篇大论
        mus.set_pending(cfg, song, lyric)
        mid = 0
        try:
            mid = self.memory.add_message("assistant", line, channel="music")
        except Exception as exc:  # noqa: BLE001
            print(f"[brain] 一起听写库失败：{exc}")

        return {"ok": True, "kind": "music", "song": song, "comment": comment,
                "card": card, "lyric": lyric[:200], "session_id": sid,
                "message_id": mid, "text": line, "audio": self.voice(head)}

    #: 用户点头要听讲解时的说法。**必须短**，避免把"我在说别的事"当成同意。
    _MUSIC_YES = re.compile(
        r"^\s*(好|好啊|好呀|好的|行|可以|要|嗯|讲|讲讲|讲讲吧|讲吧|说说|说说看|展开|"
        r"展开说说|听听|听|来吧|想听|那你讲|你讲)[啊呀吧呢么了的。！!~～\s]*$"
    )

    def music_explain(self, text: str) -> str | None:
        """用户点头后，才展开讲这首歌。

        用户反馈"附加评论过多"，所以流程改成：**先发卡片 + 问一句，等他点头再讲**。
        只有很短、很像应答的话才算同意 —— "好"算，"好的我明天再说"不算。
        """
        from . import music as mus

        pending = mus.get_pending(self.cfg)
        if not pending:
            return None
        if not self._MUSIC_YES.match((text or "").strip()):
            return None
        song = pending["song"]
        lyric = pending.get("lyric") or ""
        mus.clear_pending(self.cfg)
        if not self.llm.available():
            return f"《{song['title']}》——{song['artist']}。我脑子没接上，讲不了喵。"
        try:
            prompt = (
                f"同行者想听你讲讲《{song['title']}》（{song['artist']}）。\n"
                + (f"歌词：\n{lyric}\n" if lyric else "（没拿到歌词，只能凭歌名说）\n")
                + "\n用你自己的口吻说 3~4 句：这首歌在讲什么、你注意到哪一句、"
                  "为什么觉得它值得听。**不要复述歌词**，不要客套，不要报歌名。"
            )
            return self.llm.ask(
                "你在给同行者讲一首刚一起听的歌。说点真的，别客套。", prompt,
                max_tokens=int((self.cfg.get("music") or {}).get("explain_tokens", 400)),
            ).strip()
        except LLMError as exc:
            print(f"[music] 讲解失败：{exc}")
            return f"想讲来着，但我这边断了一下（{exc}）。再问一次？"

    def close(self) -> None:
        self.learner.close()
        self.llm.close()


if __name__ == "__main__":  # 自检：python -m neko.brain
    import json

    b = Brain()
    print("人设:", b.cfg["persona"]["name"], b.cfg["persona"]["style"])
    print("状态:", json.dumps(b.status(), ensure_ascii=False, indent=2))
    print("--- 离线/在线回复测试 ---")
    print(b.reply("你好呀，今天有空吗")["text"])
    b.close()
