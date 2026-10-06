"""定时自学：让她在指定的时间自己去 B 站学东西，学完主动分享。

为什么单独一个模块
------------------
"自己会学习"是这个软件和普通聊天壳的分界线。没有调度器，她就只能在
你点按钮时才动，那是工具不是伴友。

实现要点：
- 一个守护线程，每 30 秒醒来一次，看当前时间是否命中 `schedule.times`
- 用 `已触发过的 (日期, 时:分)` 去重，避免同一天同一时刻触发多次
- 学习本身交给 `Brain.proactive_share()`，它内部会：
    优先分享"学过但还没发过"的笔记，否则去学一个新的
  并且 `Learner.daily_study()` 已经带了每日上限（`bilibili.daily_limit`）
- 学到的笔记**先落库**，再尝试推给界面。所以就算应用当时没开，
  下次打开也能在对话里看到（前端会拉 channel=proactive 的历史）
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


class StudyScheduler:
    def __init__(
        self,
        cfg: dict[str, Any],
        brain: Any,
        *,
        on_event: Callable[[dict], None] | None = None,
        poll_seconds: float = 30.0,
    ) -> None:
        self.cfg = cfg
        self.brain = brain
        self.on_event = on_event
        self.poll_seconds = poll_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._fired: set[str] = set()
        #: 进程启动时刻：用于"启动后补学"（今天还没学就先补一次）
        self._started_at = time.time()
        #: 上次真正去学的时刻：用于"每隔 N 小时学一次"模式
        self._last_study = 0.0
        self._catch_up_done = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        cfg_sched = self.cfg.get("schedule", {})
        if not cfg_sched.get("auto_study"):
            print("[scheduler] 定时自学未开启（schedule.auto_study = false）")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nekopal-scheduler", daemon=True)
        self._thread.start()
        times = ", ".join(str(t) for t in (cfg_sched.get("times") or [])) or "未设置"
        every = float(cfg_sched.get("every_hours", 0) or 0)
        extra = f"；运行期间每 {every:g} 小时一次" if every > 0 else ""
        catch = "；启动后若今天没学过会补一次" if cfg_sched.get("catch_up", True) else ""
        print(f"[scheduler] 定时自学已启动，每天 {times}{extra}{catch}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # ---------- 主循环 ----------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 —— 定时任务出错不能弄死进程
                print(f"[scheduler] 本轮出错：{exc}")
            # 用 Event.wait 代替 sleep，这样 stop() 能立刻唤醒
            self._stop.wait(self.poll_seconds)

    def _has_studied_today(self, now: float) -> bool:
        """今天是否已经学过了。

        必须查数据库而不只是看进程内存：内存里那份重启就没了，
        会导致"每次开软件她都补学一次"，白白吃掉每日上限。
        """
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if self._last_study and time.strftime("%Y-%m-%d", time.localtime(self._last_study)) == day:
            return True
        if any(str(k).startswith(day) for k in self._fired):
            return True
        try:
            return str(self.brain.memory.get_kv("last_study_date") or "") == day
        except Exception:  # noqa: BLE001 —— 查不到就当没学过
            return False

    def _remember_studied(self, now: float) -> None:
        try:
            self.brain.memory.set_kv("last_study_date", time.strftime("%Y-%m-%d", time.localtime(now)))
        except Exception:  # noqa: BLE001
            pass

    def tick(self, now: float | None = None) -> dict | None:
        """检查一次。该学就学一个，返回分享事件或 None。

        三种触发方式：
        1. 命中 ``schedule.times`` 里的固定时刻（带同刻去重）
        2. **启动补学**：应用启动后，如果今天还没学过，延迟一小会儿补一次。
           没有这条，"每天 12:30/21:00" 只要那个点没开着软件就永远不会学——
           用户感受到的正是"她从来不自己学"。
        3. **间隔模式**：``schedule.every_hours`` > 0 时，距上次学习超过 N 小时就再学一次。

        注意：2 和 3 只在**不带 now 参数**（也就是真实循环调用）时生效，
        这样自检里传固定时刻进来仍然是确定性的。
        """
        cfg_sched = self.cfg.get("schedule", {})
        if not cfg_sched.get("auto_study"):
            return None

        live = now is None
        ts = time.time() if now is None else float(now)
        stamp = time.localtime(ts)
        key: str | None = None

        # --- 1) 固定时刻 ---
        times = [str(t) for t in (cfg_sched.get("times") or [])]
        current = time.strftime("%H:%M", stamp)
        if times and current in times:
            candidate = f"{time.strftime('%Y-%m-%d', stamp)} {current}"
            if candidate in self._fired:
                return None                      # 同一天同一时刻只触发一次
            self._fired.add(candidate)
            if len(self._fired) > 50:
                self._fired = set(sorted(self._fired)[-50:])
            key = candidate
            print(f"[scheduler] {candidate} 到点了，让她去学点东西…")

        # --- 2) 启动补学 ---
        if key is None and live and cfg_sched.get("catch_up", True) and not self._catch_up_done:
            delay = max(0.0, float(cfg_sched.get("startup_delay", 90) or 0))
            if ts - self._started_at >= delay:
                self._catch_up_done = True
                if not self._has_studied_today(ts):
                    key = f"startup {time.strftime('%Y-%m-%d %H:%M', stamp)}"
                    print("[scheduler] 今天还没学过，启动后补一次…")

        # --- 3) 间隔模式 ---
        if key is None and live:
            every = float(cfg_sched.get("every_hours", 0) or 0)
            if every > 0 and self._last_study and (ts - self._last_study) >= every * 3600:
                key = f"interval {int(ts)}"
                print(f"[scheduler] 距上次自学超过 {every:g} 小时，再学一次…")

        if key is None:
            return None

        self._last_study = ts
        result = self.brain.proactive_share()
        self._remember_studied(ts)
        if not result:
            print("[scheduler] 这次没有可分享的内容（可能今日已达上限或搜索失败）")
            return None

        event = {
            "type": "share",
            "text": result.get("text", ""),
            "audio": result.get("audio"),
            "note": result.get("note"),
            "kind": result.get("kind", "share"),
        }
        if self.on_event is not None:
            self.on_event(event)
        return event
