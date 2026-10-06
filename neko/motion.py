"""轮式机器人的运动控制。

## 为什么单独一层，不塞进设备白名单

设备那层（`devices.py`）管的是"开灯/关锁"这种**离散、可逆**的动作。
轮子不一样：**移动的机器出事代价大得多**，而且它带参数（速度、时长、距离）。
所以单独一层，把三条安全规则钉死。

## 三条安全规则（都是"物理世界没有读档"的延伸）

**1. 所有移动都必须有界。**
不存在"一直往前开"这种指令。每条指令都带时长，而被截断到 `max_duration`
（默认 3 秒）以内。想开更远就多发几条 —— 累加出来的距离是可审计的，
而一条"开到撞墙为止"不是。

**2. 不需要心跳也能自己停。**
因为指令全都有界，机器人执行完就停。这比"靠心跳维持"安全得多：
WiFi 断了、PC 崩了、程序被杀了，机器人不会还在往前冲。
心跳（`ping`）只是给连续运动场景留的第二道保险。

**3. 参数越界就截断，并且如实告诉用户截断了。**
静默截断会让人误以为"我设了 100% 它就跑 100%"。

## 和机器人的接口

机器人（或它的网关）实现一个端点：

    POST /action
    {
      "device": "wheel",
      "action": "forward",     # forward / backward / left / right / stop
      "speed": 60,             # 0-100
      "duration": 1.5,         # 秒，必有且 <= max_duration
      "distance": null         # 米，可选；有编码器的机器人可以按距离走
    }
    -> {"ok": true, "state": {"x": 1.2, "y": 0.0, "heading": 0.0}}
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

#: 动作白名单。她只能用这几个词，别的会被拒。
ACTIONS: dict[str, str] = {
    "前进": "forward",
    "后退": "backward",
    "左转": "left",
    "右转": "right",
    "停": "stop",
    "停下": "stop",
    "刹车": "stop",
}
#: 反向：英文名 -> 中文，用于回话
ACTION_CN = {"forward": "前进", "backward": "后退", "left": "左转",
             "right": "右转", "stop": "停"}

MARK_RE = re.compile(r"\[移动:\s*([^=\]]+?)\s*\]")


def _cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
    return ((cfg or {}).get("motion") or {})


def enabled(cfg: dict[str, Any] | None) -> bool:
    c = _cfg(cfg)
    return bool(c.get("enabled", False)) and bool(c.get("gateway") or
                                                 ((cfg or {}).get("devices") or {}).get("gateway"))


def _gateway(cfg: dict[str, Any] | None) -> str:
    c = _cfg(cfg)
    return str(c.get("gateway") or ((cfg or {}).get("devices") or {}).get("gateway") or "").rstrip("/")


def _token(cfg: dict[str, Any] | None) -> str:
    c = _cfg(cfg)
    return str(c.get("token") or ((cfg or {}).get("devices") or {}).get("token") or "")


def resolve(action: str) -> tuple[str, str]:
    """把中文动作解析成协议里的英文名。返回 (英文名, 错误说明)。"""
    key = re.sub(r"\s+", "", str(action or ""))
    if key in ACTIONS:
        return ACTIONS[key], ""
    if key in ACTION_CN.values():
        return key, ""
    return "", f"轮子不会「{action}」，它只会：前进、后退、左转、右转、停"


def plan(action: str, cfg: dict[str, Any] | None,
         speed: float | None = None, duration: float | None = None,
         distance: float | None = None) -> dict[str, Any]:
    """把一条移动意图变成**有界的**指令。返回 {"cmd": {...}, "clamped": [...]}。"""
    c = _cfg(cfg)
    code, err = resolve(action)
    if err:
        return {"ok": False, "reason": err}

    max_speed = float(c.get("max_speed", 70) or 70)
    max_duration = float(c.get("max_duration", 3.0) or 3.0)
    max_distance = float(c.get("max_distance", 3.0) or 3.0)
    default_speed = float(c.get("default_speed", 50) or 50)
    default_duration = float(c.get("default_duration", 1.0) or 1.0)

    clamped: list[str] = []

    def bound(value: float, lo: float, hi: float, label: str) -> float:
        v = max(lo, min(hi, value))
        if abs(v - value) > 1e-6:
            clamped.append(f"{label} {value:g} → {v:g}")
        return v

    if code == "stop":
        # 停车不带参数，也不该被任何东西限制
        return {"ok": True, "cmd": {"device": "wheel", "action": "stop",
                                    "speed": 0, "duration": 0.0},
                "clamped": []}

    sp = bound(float(speed if speed is not None else default_speed), 0.0, max_speed, "速度")
    du = bound(float(duration if duration is not None else default_duration),
               0.1, max_duration, "时长")
    dist = None
    if distance is not None:
        dist = bound(float(distance), 0.05, max_distance, "距离")

    cmd: dict[str, Any] = {"device": "wheel", "action": code,
                           "speed": round(sp, 1), "duration": round(du, 2)}
    if dist is not None:
        cmd["distance"] = round(dist, 2)
    return {"ok": True, "cmd": cmd, "clamped": clamped}


def execute(cmd: dict[str, Any], cfg: dict[str, Any] | None,
            timeout: float = 8.0) -> dict[str, Any]:
    """把指令发给机器人。失败如实返回，绝不假装成功。"""
    gw = _gateway(cfg)
    if not gw:
        return {"ok": False, "reason": "还没配置机器人地址（motion.gateway）"}
    headers = {"Content-Type": "application/json"}
    tok = _token(cfg)
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"{gw}/action", json=cmd, headers=headers)
        if r.status_code != 200:
            return {"ok": False, "reason": f"机器人返回 HTTP {r.status_code}"}
        data = r.json()
    except httpx.HTTPError as exc:
        return {"ok": False, "reason": f"连不上机器人：{type(exc).__name__}"}
    except ValueError:
        return {"ok": False, "reason": "机器人返回的不是 JSON"}
    if not isinstance(data, dict):
        return {"ok": False, "reason": "机器人返回结构异常"}
    data.setdefault("ok", False)
    return data


def run(action: str, cfg: dict[str, Any] | None, **kw: Any) -> dict[str, Any]:
    """计划 + 执行。返回里带 cmd / clamped / 机器人回的状态。"""
    p = plan(action, cfg, **kw)
    if not p.get("ok"):
        return p
    out = execute(p["cmd"], cfg)
    out["cmd"] = p["cmd"]
    out["clamped"] = p["clamped"]
    return out


def describe(cfg: dict[str, Any] | None) -> str:
    """给提示词用的说明。"""
    if not enabled(cfg):
        return ""
    c = _cfg(cfg)
    return (
        f"  轮子（机器人底盘）：{'、'.join(ACTION_CN.values())}\n"
        f"    写 `[移动:前进]` 这样就能让它动。**所有移动都有时长上限"
        f"（{float(c.get('max_duration', 3.0) or 3.0):g} 秒）**，系统会自动截断 —— "
        "想让它走更远就分几次发，不要写「一直往前」。\n"
        "    移动是物理动作：**不确定前面有什么就别让它动**。"
    )


def heartbeat(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """给连续运动场景留的第二道保险。"""
    return execute({"device": "wheel", "action": "ping"}, cfg)


if __name__ == "__main__":  # 自检：python -m neko.motion
    import io as _io
    import sys

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    from .config import load_config

    cfg = load_config()
    print("运动控制:", "已启用" if enabled(cfg) else "未启用")
    print("机器人地址:", _gateway(cfg) or "（未配置）")
    print()
    print("解析与限幅：")
    for act, kw in [("前进", {}), ("前进", {"speed": 100, "duration": 30}),
                    ("左转", {"duration": 0.5}), ("停", {}), ("起飞", {})]:
        p = plan(act, cfg, **kw)
        if p.get("ok"):
            print(f"  {act}{kw or ''} -> {p['cmd']}  截断={p['clamped'] or '无'}")
        else:
            print(f"  {act} -> 拒绝：{p['reason']}")
    print()
    print("提示词片段：")
    print(describe(cfg) or "  （未启用）")
