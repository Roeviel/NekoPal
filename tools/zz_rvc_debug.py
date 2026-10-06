"""调试用：手工跑一次 RVC 转换，把 worker 的 **完整回包 + stderr 日志** 都打出来。

neko/rvc.py 里为了不让界面上出现长 traceback，只传回 error 一行；
但排查时那一行往往不够（loguru 的日志才是真正指路的东西）。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from neko import rvc  # noqa: E402
from neko.config import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    d = rvc.rvc_dir(cfg)
    home = rvc.work_dir(d)
    env = rvc.spawn_env(cfg)          # 和真正启动 worker 时完全同一套环境

    err = open(ROOT / "data" / "_rvc_dbg_stderr.log", "w", encoding="utf-8",
               errors="replace")
    p = subprocess.Popen([str(d / "runtime" / "python.exe"), str(ROOT / "tools" / "rvc_worker.py")],
                         cwd=str(home), env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=err, text=True,
                         encoding="utf-8", bufsize=1, creationflags=0x08000000)
    box: "queue.Queue[str | None]" = queue.Queue()
    threading.Thread(target=lambda: ([box.put(l) for l in p.stdout] if p.stdout else None,
                                     box.put(None)), daemon=True).start()

    t0 = time.time()
    while time.time() - t0 < 120:
        try:
            line = box.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            print("stdout 关闭"); break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            print("  [非 JSON]", line[:160]); continue
        if msg.get("ready"):
            print(f"ready device={msg.get('device')} 等待 {time.time()-t0:.1f}s")
            break

    src = sorted((ROOT / "data" / "voice_cache").glob("*.wav"),
                 key=lambda q: q.stat().st_mtime)[-1]
    out = ROOT / "data" / "_dbg_out.wav"
    v = rvc.voices(cfg)[0]
    req = {"id": 1, "model": v["model"], "index": v.get("index", ""),
           "in": str(src), "out": str(out), "pitch": 0, "method": "rmvpe"}
    print(f"输入 {src.name} -> 模型 {v['name']}")
    p.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
    p.stdin.flush()
    t1 = time.time()
    while time.time() - t1 < 180:
        try:
            line = box.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            print("  [非 JSON]", line[:200]); continue
        if msg.get("id") == 1:
            print("\n=== worker 回包 ===")
            print(json.dumps(msg, ensure_ascii=False, indent=2))
            break
    try:
        p.stdin.write('{"cmd":"quit"}\n'); p.stdin.flush(); p.wait(timeout=5)
    except Exception:  # noqa: BLE001
        p.kill()
    err.close()

    text = (ROOT / "data" / "_rvc_dbg_stderr.log").read_text(encoding="utf-8",
                                                              errors="replace")
    print("\n=== worker stderr（末尾 3000 字符）===")
    print(text[-3000:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
