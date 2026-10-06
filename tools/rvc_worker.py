"""常驻的 RVC 变声进程：模型只加载一次，之后按 JSON 行收任务。

为什么要常驻：实测模型加载要 **3.9 秒**，而推理只要 **0.8 倍音频时长**。
每句话都重启进程的话，光加载就吃掉 4 秒；常驻之后每次只付推理时间。

协议（stdin/stdout 各一行 JSON）：

    收： {"id":1, "model":"x.pth", "index":"y.index", "in":"a.wav", "out":"b.wav", "pitch":0}
    发： {"id":1, "ok":true, "infer_sec":1.2, "sr":40000, "cached_voice":"x"}

换模型时才重新 load（同一模型连续转只付推理时间）。

**这个脚本必须用 RVC 整合包自带的 Python 跑**，见 tools/rvc_convert.py 的说明。
"""

from __future__ import annotations

import json
import os
import sys
import time

#: RVC 整合包目录（含 runtime\python.exe）。由 neko/rvc.py 通过环境变量传入；
#: 开源版不写死任何私人路径。
RVC_DIR = os.environ.get("RVC_DIR") or ""
# 工作目录（cwd）。**不能是 RVC 安装目录**：Config() 会改写
# `configs/inuse/*.json`（相对 cwd），而安装目录不在工作区里，
# 受限沙箱（低完整性级别）下写不进去，RVC 会在启动阶段就 PermissionError。
# 所以由 neko/rvc.py 传一个工作区里的目录过来，这里的 configs/ 是它的副本。
RVC_HOME = os.environ.get("RVC_HOME") or RVC_DIR


def jout(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    if not RVC_DIR or not os.path.isdir(RVC_DIR):
        jout({"ready": False,
              "error": "RVC_DIR 没设置或不是目录（需要 RVC 整合包根目录）"})
        return 2
    os.makedirs(os.path.join(RVC_HOME, "configs", "inuse", "v1"), exist_ok=True)
    os.makedirs(os.path.join(RVC_HOME, "configs", "inuse", "v2"), exist_ok=True)
    os.chdir(RVC_HOME)
    sys.path.insert(0, RVC_DIR)

    # 这三个环境变量 RVC 是硬依赖（值取自包根的 .env）
    os.environ.setdefault("rmvpe_root", os.path.join(RVC_DIR, "assets", "rmvpe"))
    os.environ.setdefault("outside_index_root", os.path.join(RVC_DIR, "assets", "indices"))
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ["index_root"] = os.environ.get("index_root") or os.path.join(RVC_DIR, "logs")
    os.environ["weight_root"] = os.environ.get("weight_root") or os.path.join(RVC_DIR, "assets", "weights")

    # RVC 的 Config() 自己会 argparse；我们的参数走环境变量，所以这里清空 argv
    sys.argv = [sys.argv[0]]

    from configs.config import Config
    from infer.modules.vc.modules import VC

    config = Config()
    # **构造完 Config 就切回 RVC 根目录**：这两步对 cwd 的要求正好相反 ——
    #   Config()  要往 `configs/inuse/*.json` **写**，只能在工作区（可写）里做；
    #   推理      要按相对路径**读** `assets/hubert/hubert_base.pt`，
    #             只有站在 RVC 根目录下才找得到（181MB，没必要复制一份）。
    # 推理过程只读不写，所以切过去是安全的。
    os.chdir(RVC_DIR)
    vc = VC(config)
    jout({"ready": True, "device": str(config.device)})

    loaded = ""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        if req.get("cmd") == "quit":
            jout({"id": rid, "ok": True, "bye": True})
            return 0
        try:
            model = req["model"]
            model_dir = os.path.dirname(os.path.abspath(model))
            sid = os.path.basename(model)
            os.environ["weight_root"] = model_dir
            idx = req.get("index") or ""
            os.environ["index_root"] = (os.path.dirname(os.path.abspath(idx)) if idx
                                        else model_dir)

            t0 = time.time()
            if sid != loaded:                       # 换模型才重新加载
                vc.get_vc(sid)
                loaded = sid
            t_load = time.time() - t0

            t1 = time.time()
            info, opt = vc.vc_single(
                0, req["in"], int(req.get("pitch", 0)), None,
                req.get("method", "rmvpe"), idx, None,
                float(req.get("index_rate", 0.66)), 3, 0,
                float(req.get("rms_mix_rate", 0.25)), float(req.get("protect", 0.33)),
            )
            # **失败时 RVC 返回的是 `(info, (None, None))` 而不是 `None`**：
            # 只判断 `opt is None` 会漏掉它，然后在 sf.write 里以
            # `IndexError: tuple index out of range` 的形式炸掉 ——
            # 真正的原因（比如找不到 ffmpeg）就被这句莫名其妙的报错盖掉了。
            if not opt or opt[0] is None or opt[1] is None:
                jout({"id": rid, "ok": False, "error": f"没有产出音频：{info}"})
                continue
            import soundfile as sf

            sr, data = opt
            sf.write(req["out"], data, sr)
            jout({"id": rid, "ok": True, "sr": sr,
                  "bytes": os.path.getsize(req["out"]),
                  "load_sec": round(t_load, 2),
                  "infer_sec": round(time.time() - t1, 2)})
        except Exception as exc:  # noqa: BLE001
            import traceback

            jout({"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "trace": traceback.format_exc()[-600:]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
