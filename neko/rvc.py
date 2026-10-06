"""RVC 变声：把 edge-tts 合成出来的人声换成目标音色。

架构上为什么这么绕：

- RVC 依赖 **内嵌的 Python 3.9 + torch 2.0 + fairseq**（在整合包里），
  和本项目 venv（3.14）**完全不兼容**，只能靠子进程隔离。
- 模型加载实测要 **3.9 秒**，而推理只要 **0.8 倍音频时长**。
  所以起一个**常驻 worker**，模型只加载一次，之后每句只付推理时间。
- 这台机器上 DirectML 会撞 `PrivateUse1` 调度错误（三个 f0 方法都试过），
  所以只能走 CPU —— 0.8 倍速对话够用，但**它不便宜**：一句 5 秒的话要多等约 4 秒。

**失败绝不影响说话**：任何异常都返回原音频，宁可声音没变，也不能没声音。

沙箱相关的三个坑（实测，别再踩）
--------------------------------
RVC 会在**自己的安装目录**里写文件，这在受限环境里会直接翻车：

1. `import librosa` → numba 要在包的 `__pycache__` 里建临时锁文件。
   如果目标程序跑在**低完整性级别**（DSH 的沙箱会把工作区里的 python 降到 Low），
   写 Medium 级别目录会被拒绝（`PermissionError`），而 `os.access()` **谎报可写**，
   于是 numba 的重试循环**占满一个核永不返回**。→ 用 `NUMBA_CACHE_DIR` 把缓存挪进工作区。
2. `tempfile` 选不到可用临时目录（`%TEMP%` 同样写不进去）→ 显式把
   `TEMP/TMP/TMPDIR` 指到工作区的 `data/tmp`。
3. RVC 自己的 `Config()` 会**改写** `configs/inuse/*.json`（相对当前工作目录）。
   → 让 worker 在**工作区里的一个"家目录"**（`data/rvc_home`）里跑，
   那里有一份 `configs/` 的副本，写进去的就落在工作区，不受沙箱限制。

**读取必须带真超时**：早期版本用 `p.stdout.readline()` 等 worker 回话 ——
这是**永久阻塞**的，超时判断根本没机会执行。worker 一旦卡住，
整个聊天都会跟着卡死（用户看不到任何报错）。现在改成"读线程 + 队列 + 超时"，
超时就杀掉 worker 并退回原声。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

_LOCK = threading.Lock()
_PROC: subprocess.Popen | None = None
_LINES: "queue.Queue[str | None] | None" = None
_SEQ = 0
_LOG: list[str] = []
_LAST_ERROR = ""


def conf(cfg: dict[str, Any] | None) -> dict[str, Any]:
    return ((cfg or {}).get("voice") or {}).get("rvc") or {}


def enabled(cfg: dict[str, Any] | None) -> bool:
    c = conf(cfg)
    if not c.get("enabled"):
        return False
    return rvc_dir(cfg) is not None


def rvc_dir(cfg: dict[str, Any] | None) -> Path | None:
    """RVC 整合包目录（含 runtime/python.exe 的那一层）。"""
    d = Path(str(conf(cfg).get("dir") or ""))
    if d.is_dir() and (d / "runtime" / "python.exe").is_file():
        return d
    return None


def work_dir(src: Path | None = None) -> Path:
    """worker 的工作目录（= 它的 cwd），并在里面准备好一份 `configs/` 副本。

    为什么不用 RVC 安装目录：`Config()` 会往 `configs/inuse/` 写文件，
    而安装目录**不在工作区里**，受限沙箱下写不进去（见模块开头第 3 条）。
    放到工作区里，同样的写入就合法了。

    `configs/` 只在缺失时复制，所以用户改过 RVC 的配置后副本会偏旧；
    但 `inuse/*.json` 本来就会被 RVC 自己覆盖，模板也几乎不会变，够用。
    """
    # 拿不到调用方给的目录时回落到环境变量；两个都没有就只建空目录，
    # 后面 `s.is_dir()` 判断会自然跳过复制（开源版不写死任何私人路径）。
    root = src or Path(os.environ.get("RVC_DIR") or "")
    d = ROOT / "data" / "rvc_home"
    for ver in ("v1", "v2"):
        for rel in (Path("configs") / ver, Path("configs") / "inuse" / ver):
            (d / rel).mkdir(parents=True, exist_ok=True)
            s = root / rel
            if not s.is_dir():
                continue
            for f in s.glob("*.json"):
                dst = d / rel / f.name
                if not dst.is_file():
                    try:
                        shutil.copy2(f, dst)
                    except OSError:
                        pass
    return d


def data_tmp() -> Path:
    d = ROOT / "data" / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def voices(cfg: dict[str, Any] | None) -> list[dict[str, Any]]:
    """可选的音色。config 里配的是绝对路径（模型在项目外面）。"""
    out = []
    for v in conf(cfg).get("voices") or []:
        model = Path(str(v.get("model") or ""))
        out.append({
            "name": v.get("name") or model.stem,
            "model": str(model),
            "index": str(v.get("index") or ""),
            "pitch": int(v.get("pitch", 0) or 0),
            "ok": model.is_file(),
        })
    return out


def current(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    want = str(conf(cfg).get("voice") or "")
    for v in voices(cfg):
        if v["name"] == want:
            return v
    return None


def note(msg: str) -> None:
    """留一小段运行日志，界面上能看到（不然 RVC 出问题完全是个黑盒）。"""
    global _LAST_ERROR
    _LOG.append(f"{time.strftime('%H:%M:%S')} {msg}")
    del _LOG[:-20]
    if any(w in msg for w in ("失败", "起不来", "超时", "退出", "找不到", "没生效")):
        _LAST_ERROR = msg
    print(f"[rvc] {msg}")


def log() -> list[str]:
    return list(_LOG)


# ---------- 常驻进程 ----------

def _pump(proc: subprocess.Popen, box: "queue.Queue[str | None]") -> None:
    """把子进程 stdout 搬进队列。

    必须是独立线程：`readline()` 在主线程里是**永久阻塞**的，
    "超时"根本轮不到检查（这就是早期版本一卡就卡死整个聊天的原因）。
    """
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                box.put(line)
    except Exception:  # noqa: BLE001 —— 管道断开是正常退出路径
        pass
    finally:
        box.put(None)          # EOF 标记


def _wait_json(box: "queue.Queue[str | None]", timeout: float,
               want_id: int | None = None) -> dict[str, Any] | None:
    """在 ``timeout`` 秒内取一条 JSON 消息；超时/EOF 返回 None。"""
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            return None
        try:
            line = box.get(timeout=remain)
        except queue.Empty:
            return None
        if line is None:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue                       # RVC 自己的 print 会混进来，跳过
        if want_id is not None and msg.get("id") != want_id:
            continue
        return msg


def spawn_env(cfg: dict[str, Any] | None = None) -> dict[str, str]:
    """worker 需要的环境变量。**集中在这里**，因为每一项都是为了绕开一个具体的坑：

    - ``RVC_HOME``：worker 的 cwd，里面有一份 `configs/` 副本，让 RVC 的
      配置改写落在工作区里（安装目录在受限环境下不可写）。
    - ``TEMP/TMP/TMPDIR``：`%TEMP%` 在受限环境下同样写不进去，
      而 numba 建缓存要用临时文件。
    - ``NUMBA_CACHE_DIR``：不然 numba 会往 site-packages 的 `__pycache__` 里写。
    - ``PATH``：RVC 根目录里有 `ffmpeg.exe`，而它加载音频时只叫 `ffmpeg`。
      以前靠"cwd 恰好是 RVC 根目录"找到它；cwd 换掉之后必须显式进 PATH。

    这些是 debug 脚本也要用的，所以单独暴露出来，避免两处各写一份然后走样。
    """
    d = rvc_dir(cfg) or Path(os.environ.get("RVC_DIR") or "")
    env = dict(os.environ)
    env["RVC_DIR"] = str(d)
    env["PYTHONIOENCODING"] = "utf-8"
    env["RVC_HOME"] = str(work_dir(d))
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(data_tmp())
    env["NUMBA_CACHE_DIR"] = str(ROOT / "data" / "numba_cache")
    (ROOT / "data" / "numba_cache").mkdir(parents=True, exist_ok=True)
    env["PATH"] = os.pathsep.join(
        [str(d), str(d / "runtime"), env.get("PATH") or ""])
    return env


def _spawn(cfg: dict[str, Any]) -> subprocess.Popen | None:
    global _LINES
    d = rvc_dir(cfg)
    if d is None:
        note("找不到 RVC 整合包（config 里的 voice.rvc.dir 不对？）")
        return None
    py = d / "runtime" / "python.exe"
    script = ROOT / "tools" / "rvc_worker.py"
    if not script.is_file():
        note(f"缺少 worker 脚本：{script}")
        return None

    home = work_dir(d)
    env = spawn_env(cfg)

    flags = 0x08000000 if os.name == "nt" else 0
    try:
        p = subprocess.Popen(
            [str(py), str(script)], cwd=str(home), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, creationflags=flags)
    except Exception as exc:  # noqa: BLE001
        note(f"启动 RVC worker 失败：{type(exc).__name__}: {exc}")
        return None

    box: "queue.Queue[str | None]" = queue.Queue()
    threading.Thread(target=_pump, args=(p, box), daemon=True,
                     name="rvc-reader").start()
    _LINES = box

    # 等它报到（首次要 import torch + librosa，实测十几秒，冷启动可能更久）
    limit = float(conf(cfg).get("startup_timeout_sec", 300))
    t0 = time.time()
    while True:
        msg = _wait_json(box, max(1.0, limit - (time.time() - t0)))
        if msg is None:
            if p.poll() is not None:
                note(f"worker 提前退出（code={p.returncode}），"
                     f"多半是 RVC 环境问题，看 data/rvc_home 是否可写")
            else:
                note(f"worker 启动超时（{limit:.0f}s 内没说 ready），已放弃这次变声")
            try:
                p.kill()
            except OSError:
                pass
            _LINES = None
            return None
        if msg.get("ready"):
            note(f"worker 就绪，device={msg.get('device')}（等待 {time.time()-t0:.1f}s）")
            return p


def _ensure(cfg: dict[str, Any]) -> subprocess.Popen | None:
    global _PROC
    if _PROC is not None and _PROC.poll() is None:
        return _PROC
    _PROC = _spawn(cfg)
    return _PROC


def _drop(reason: str) -> None:
    """把坏掉的 worker 清掉，下次调用会重开一个。"""
    global _PROC, _LINES
    note(reason)
    if _PROC is not None and _PROC.poll() is None:
        try:
            _PROC.kill()
        except OSError:
            pass
    _PROC = None
    _LINES = None


def warmup(cfg: dict[str, Any]) -> dict[str, Any]:
    """提前把 worker 拉起来（要花十几秒），别让第一句话等太久。"""
    if not enabled(cfg):
        return {"ok": False, "reason": "RVC 未启用或路径不对"}
    with _LOCK:
        p = _ensure(cfg)
    return {"ok": p is not None, "error": _LAST_ERROR, "log": log()}


def convert(cfg: dict[str, Any], in_wav: str, out_wav: str,
            voice: dict[str, Any] | None = None) -> dict[str, Any]:
    """把 ``in_wav`` 换成目标音色写到 ``out_wav``。

    **失败就返回 ok=False，调用方继续用原音频** —— 变声坏了不能连累说话。
    """
    global _SEQ
    if not enabled(cfg):
        return {"ok": False, "reason": "RVC 未启用"}
    v = voice or current(cfg)
    if not v:
        return {"ok": False, "reason": "没有选中音色"}
    if not Path(v["model"]).is_file():
        return {"ok": False, "reason": f"模型文件不存在：{v['model']}"}

    with _LOCK:
        p = _ensure(cfg)
        box = _LINES
        if p is None or box is None:
            return {"ok": False, "reason": _LAST_ERROR or "RVC worker 起不来（看日志）"}
        _SEQ += 1
        req = {"id": _SEQ, "model": v["model"], "index": v.get("index") or "",
               "in": str(in_wav), "out": str(out_wav),
               "pitch": v.get("pitch", 0), "method": conf(cfg).get("f0_method", "rmvpe")}
        try:
            assert p.stdin is not None
            p.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            p.stdin.flush()
        except Exception as exc:  # noqa: BLE001
            _drop(f"发任务失败：{exc}")
            return {"ok": False, "reason": "worker 断了"}

        limit = float(conf(cfg).get("timeout_sec", 120))
        t0 = time.time()
        msg = _wait_json(box, limit, want_id=req["id"])
        if msg is None:
            if p.poll() is not None:
                _drop("worker 退出了（变声这次跳过，声音保持原样）")
                return {"ok": False, "reason": "worker 退出"}
            _drop(f"转换超时（>{limit:.0f}s），已杀掉 worker，这次保持原声")
            return {"ok": False, "reason": "转换超时"}
        if not msg.get("ok"):
            note(f"转换失败：{msg.get('error')}")
            return {"ok": False, "reason": msg.get("error") or "转换失败"}
        return {"ok": True, "sr": msg.get("sr"), "bytes": msg.get("bytes"),
                "infer_sec": msg.get("infer_sec"), "load_sec": msg.get("load_sec")}


def shutdown() -> None:
    global _PROC, _LINES
    with _LOCK:
        if _PROC is not None and _PROC.poll() is None:
            try:
                if _PROC.stdin is not None:
                    _PROC.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                    _PROC.stdin.flush()
                _PROC.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    _PROC.kill()
                except OSError:
                    pass
        _PROC = None
        _LINES = None


def status(cfg: dict[str, Any]) -> dict[str, Any]:
    d = rvc_dir(cfg)
    return {
        "enabled": bool(conf(cfg).get("enabled")),
        "dir": str(d) if d else str(conf(cfg).get("dir") or ""),
        "dir_ok": d is not None,
        "voice": (current(cfg) or {}).get("name", ""),
        "voices": voices(cfg),
        "running": _PROC is not None and _PROC.poll() is None,
        "device": str(conf(cfg).get("device") or "cpu"),
        "work_dir": str(ROOT / "data" / "rvc_home"),
        "last_error": _LAST_ERROR,
        "log": log(),
    }
