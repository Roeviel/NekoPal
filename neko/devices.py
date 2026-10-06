"""设备控制：把"嘴里说出来的动作"变成真的动作。

## 为什么沿用"标记"这套

`brain.py` 里 `[学习:主题]` 已经是**标记触发副作用**的成熟范式（她写下标记，
后端真的去学习）。控制设备用的是同一件事，所以加一个 `[设备:台灯=开]` 就够了，
不需要另造一套函数调用协议。

## 三道安全闸（物理世界没有"读档"）

1. **白名单**：只执行 `config.json` 里登记过的设备和动作。
   LLM 一定会**幻觉出并不存在的设备**（"打开客厅的加湿器" —— 你根本没有加湿器），
   没登记过的一律拒绝，并且如实告诉她"没有这个设备"。
2. **危险动作二次确认**：标了 `dangerous` 的动作（开锁、断电、开火）**不直接执行**，
   先把待办挂起来，等用户明确确认。软件里删错文件能读存档，
   **物理世界里开错锁没有存档**。
3. **全量审计**：谁在什么时候对哪个设备做了什么、成功没有，全部落库。
   出问题时这是唯一能查的东西。

## 和网关的分工

蕴只管"决定做什么"，**不直接碰设备**。真正的协议适配（MQTT / HTTP / 串口）
在独立网关里，容器是配置里的 `devices.gateway`。这样换设备/换协议不用动大脑。
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

#: 她写的标记：``[设备:台灯=开]``。设备名和动作都不含 ``]`` `=`。
MARK_RE = re.compile(r"\[设备:\s*([^=\]]+?)\s*=\s*([^=\]]+?)\s*\]")

#: 用户确认危险动作的说法
CONFIRM_WORDS = ("确认", "确定", "是的", "yes", "ok", "开吧", "动手", "执行")
CANCEL_WORDS = ("算了", "取消", "不用", "别", "不要", "no", "停")


def _clean(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


class DeviceRegistry:
    """设备清单 + 动作白名单。由 ``config.json`` 的 ``devices`` 段驱动。"""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        conf = ((cfg or {}).get("devices") or {})
        self.cfg = cfg or {}
        self.enabled = bool(conf.get("enabled", False))
        self.gateway = str(conf.get("gateway") or "").rstrip("/")
        self.token = str(conf.get("token") or "")
        self.timeout = float(conf.get("timeout", 6.0) or 6.0)
        self.devices: dict[str, dict[str, Any]] = {}
        for d in conf.get("list") or []:
            if not isinstance(d, dict):
                continue
            name = _clean(d.get("name") or "")
            if not name:
                continue
            # actions 支持两种写法：["开","关"] 或 {"开":"light.on","关":"light.off"}
            raw = d.get("actions") or {}
            if isinstance(raw, dict):
                actions = {_clean(k): str(v) for k, v in raw.items()}
            else:
                actions = {_clean(a): _clean(a) for a in raw}
            self.devices[name] = {
                "id": str(d.get("id") or name),
                "name": name,
                "kind": str(d.get("kind") or ""),
                "dangerous": bool(d.get("dangerous", False)),
                "actions": actions,
            }

    # ---------- 校验 ----------

    def find(self, name: str) -> dict[str, Any] | None:
        n = _clean(name)
        if n in self.devices:
            return self.devices[n]
        # 允许简称：她说"台灯"，登记的是"书桌台灯"
        for key, dev in self.devices.items():
            if n and (n in key or key in n):
                return dev
        return None

    def resolve(self, name: str, action: str) -> tuple[dict[str, Any] | None, str]:
        """把 (设备名, 动作) 解析成设备字典。返回 (设备, 错误说明)。"""
        dev = self.find(name)
        if dev is None:
            known = "、".join(self.devices) or "（一个都没登记）"
            return None, f"没有登记叫「{name}」的设备。现在有的：{known}"
        act = _clean(action)
        if act not in dev["actions"]:
            can = "、".join(dev["actions"]) or "（没有可用动作）"
            return None, f"「{dev['name']}」不支持「{action}」，它能做：{can}"
        return dev, ""

    # ---------- 执行 ----------

    def execute(self, dev: dict[str, Any], action: str) -> dict[str, Any]:
        """把动作交给网关。失败一律如实返回，绝不假装成功。"""
        if not self.gateway:
            return {"ok": False, "reason": "没有配置 devices.gateway，动作发不出去"}
        command = dev["actions"].get(_clean(action), _clean(action))
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = {"device": dev["id"], "action": command, "name": dev["name"]}
        try:
            with httpx.Client(timeout=self.timeout) as c:
                r = c.post(f"{self.gateway}/action", json=payload, headers=headers)
            if r.status_code != 200:
                return {"ok": False, "reason": f"网关返回 HTTP {r.status_code}"}
            data = r.json()
        except httpx.HTTPError as exc:
            return {"ok": False, "reason": f"连不上网关：{type(exc).__name__}"}
        except ValueError:
            return {"ok": False, "reason": "网关返回的不是 JSON"}
        if not isinstance(data, dict):
            return {"ok": False, "reason": "网关返回结构异常"}
        data.setdefault("ok", False)
        return data


# ---------- 待确认的危险动作 ----------

def pending_key() -> str:
    return "pending_action"


def parse_pending(raw: Any) -> dict[str, Any] | None:
    """从 kv 里取出挂起的危险动作。"""
    if not raw:
        return None
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict) or "device" not in raw:
        return None
    return raw


def is_confirm(text: str) -> bool:
    t = _clean(text).lower()
    return any(t == w or t.startswith(w) for w in CONFIRM_WORDS) and not is_cancel(t)


def is_cancel(text: str) -> bool:
    t = _clean(text).lower()
    return any(w in t for w in CANCEL_WORDS)


# ---------- 审计 ----------

def audit(memory: Any, dev: dict[str, Any] | None, action: str,
          result: dict[str, Any], source: str = "chat") -> None:
    """记一笔。审计写失败不能影响动作本身。"""
    if memory is None:
        return
    try:
        memory.add_action(
            device=(dev or {}).get("id") or "",
            name=(dev or {}).get("name") or "",
            action=action,
            ok=bool(result.get("ok")),
            detail=str(result.get("reason") or result.get("state") or "")[:200],
            source=source,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[devices] 审计写入失败：{exc}")


def describe(cfg: dict[str, Any] | None = None) -> str:
    """给提示词用的设备清单（她得知道自己能碰什么）。"""
    reg = DeviceRegistry(cfg)
    if not reg.enabled or not reg.devices:
        return ""
    lines = []
    for dev in reg.devices.values():
        acts = "、".join(dev["actions"])
        flag = "（危险，需要用户确认）" if dev["dangerous"] else ""
        lines.append(f"  {dev['name']}：{acts}{flag}")
    return "\n".join(lines)


if __name__ == "__main__":  # 自检：python -m neko.devices
    import io as _io
    import json
    import sys

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    from .config import load_config

    cfg = load_config()
    reg = DeviceRegistry(cfg)
    print("设备控制:", "已启用" if reg.enabled else "未启用")
    print("网关:", reg.gateway or "（未配置）")
    print("设备清单:")
    print(describe(cfg) or "  （空）")
    print("\n解析测试:")
    for name, act in [("台灯", "开"), ("不存在的设备", "开"), ("台灯", "起飞")]:
        dev, err = reg.resolve(name, act)
        print(f"  [设备:{name}={act}] -> " + (err if err else f"命中 {dev['name']}"))  # type: ignore[index]
    print("\n标记解析测试:")
    for t in ["[设备:台灯=开] 好了", "随便说点 [设备:门锁=开] 呢", "没有标记"]:
        print(f"  {t!r} -> {MARK_RE.findall(t)}")
