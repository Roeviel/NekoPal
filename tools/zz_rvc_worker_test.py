"""测常驻 worker：同一模型连续转两次，看第二次省掉多少。"""

import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from neko import rvc  # noqa: E402
from neko.config import ROOT, load_config  # noqa: E402

cfg = load_config()
cfg["voice"]["rvc"]["enabled"] = True          # 测试期间临时打开

vc = ROOT / "data" / "voice_cache"
src = sorted(vc.glob("*.wav"), key=lambda p: p.stat().st_mtime)[-1]
import soundfile as sf

d, sr = sf.read(src)
print(f"  输入: {src.name}  {len(d)/sr:.2f}s @ {sr}Hz")

print("\n  ① 预热（起 worker + 加载模型）")
t0 = time.time()
w = rvc.warmup(cfg)
print(f"     {w['ok']}  耗时 {time.time()-t0:.1f}s")
for line in rvc.log():
    print("      ", line)

vs = rvc.voices(cfg)
for i, v in enumerate(vs):
    out = str(ROOT / "data" / f"_rvc_t{i}.wav")
    print(f"\n  ② 转到「{v['name']}」")
    t1 = time.time()
    r = rvc.convert(cfg, str(src), out, v)
    el = time.time() - t1
    if r["ok"]:
        print(f"     ok  {el:.1f}s  推理 {r['infer_sec']}s  加载 {r['load_sec']}s  "
              f"输出 {r['bytes']//1024}KB  sr={r['sr']}")
    else:
        print(f"     ✗ {r['reason']}")

# 同一模型再转一次 —— 应该明显更快（不重新加载）
print("\n  ③ 同一模型立刻再转一次（验证模型没有重复加载）")
t2 = time.time()
r = rvc.convert(cfg, str(src), str(ROOT / "data" / "_rvc_again.wav"), vs[0])
print(f"     耗时 {time.time()-t2:.1f}s  推理 {r.get('infer_sec')}s  加载 {r.get('load_sec')}s")

rvc.shutdown()
print("\n  已关闭 worker")
