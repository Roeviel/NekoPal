"""把一段 wav 用 RVC 转成目标音色。

**这个脚本要用 RVC 整合包自带的 Python 跑**，不是项目的 venv：

    <RVC整合包>\\runtime\\python.exe rvc_convert.py \\
        --model <模型.pth> --index <索引.index> --in a.wav --out b.wav --pitch 0

（`<RVC整合包>` 由环境变量 `RVC_DIR` 指定；脚本自己不猜路径。）

为什么要单独一个脚本、用 subprocess 调：
- RVC 依赖内嵌的 Python 3.9 + fairseq + torch 2.0，和项目 venv（3.14）**完全不兼容**，
  是两套独立环境，只能进程隔离。
- 模型和 5.7 GB 的运行时都在项目外面，不该搬进来（也不该进版本库）。

**注意参数是手动从 sys.argv 里摘出来的**：RVC 的 `Config()` 自己会
`argparse.parse_args()`，遇到不认识的参数直接 SystemExit。所以先把自己的参数
读走，再把 sys.argv 收拾成只剩 `--dml`，然后才构造 Config。
"""

from __future__ import annotations

import json
import os
import sys
import time

#: RVC 整合包目录（含 runtime\python.exe）。由 neko/rvc.py 通过环境变量传入；
#: 手工调用时自己设 `RVC_DIR`。开源版不写死任何私人路径。
RVC_DIR = os.environ.get("RVC_DIR") or ""


def main() -> int:
    if not RVC_DIR or not os.path.isdir(RVC_DIR):
        print(json.dumps({"ok": False,
                          "error": "请用 RVC_DIR 环境变量指定 RVC 整合包目录"
                                   "（里面要有 runtime\\python.exe）"},
                         ensure_ascii=False))
        return 2
    argv = sys.argv[1:]
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key = a[2:]
            val = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else "1"
            opts[key] = val
            i += 2 if i + 1 < len(argv) and not argv[i + 1].startswith("--") else 1
        else:
            i += 1

    in_wav = opts.get("in")
    out_wav = opts.get("out")
    model = opts.get("model")
    index = opts.get("index", "")
    if not (in_wav and out_wav and model):
        print(json.dumps({"ok": False, "error": "缺少 --in / --out / --model"}))
        return 2

    pitch = int(float(opts.get("pitch", "0")))
    index_rate = float(opts.get("index_rate", "0.66"))
    protect = float(opts.get("protect", "0.33"))
    method = opts.get("method", "rmvpe")
    dml = opts.get("dml", "1") not in ("0", "false", "no")

    os.chdir(RVC_DIR)
    sys.path.insert(0, RVC_DIR)

    # **RVC 是按 `f'{os.getenv("weight_root")}/{sid}'` 找模型的**（见
    # infer/modules/vc/modules.py:100），不是直接吃绝对路径。
    # 所以把模型所在目录设进 weight_root，sid 只传文件名。
    model_dir = os.path.dirname(os.path.abspath(model))
    os.environ["weight_root"] = model_dir
    sid = os.path.basename(model)

    # `index_root` 也必须设：get_vc() 里无条件调 get_index_path_from_model()，
    # 它会 os.walk(os.getenv("index_root")) —— 不给就是 None，直接 TypeError。
    # 索引是**按模型名前缀**匹配的（`sid.split('.')[0] in 路径`），
    # 所以指向模型自己的目录最稳；没有索引也只是相似度略降，不影响能跑。
    if index:
        os.environ["index_root"] = os.path.dirname(os.path.abspath(index))
    else:
        os.environ["index_root"] = model_dir

    # `rmvpe_root` 是第 3 个必需的：f0 提取（rmvpe 方法）会读
    # `"%s/rmvpe.pt" % os.environ["rmvpe_root"]`（见 pipeline.py:147）。
    # 不给就是 KeyError。这些值本来写在包根的 .env 里，但我们的脚本不经过
    # infer-web.py，所以要自己补上。
    os.environ.setdefault("rmvpe_root", os.path.join(RVC_DIR, "assets", "rmvpe"))
    os.environ.setdefault("outside_index_root", os.path.join(RVC_DIR, "assets", "indices"))
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    if not os.path.exists(os.path.join(os.environ["rmvpe_root"], "rmvpe.pt")):
        print(json.dumps({"ok": False,
                          "error": f"找不到 rmvpe.pt：{os.environ['rmvpe_root']}"},
                         ensure_ascii=False))
        return 3

    # **只留 --dml 给 RVC 自己的 arg_parse**，否则它碰到未知参数会直接退出。
    sys.argv = [sys.argv[0]] + (["--dml"] if dml else [])

    t0 = time.time()
    try:
        from configs.config import Config
        from infer.modules.vc.modules import VC

        config = Config()
        vc = VC(config)
        vc.get_vc(sid)                        # 加载模型（第一次慢，主要是读权重）
        t_load = time.time() - t0

        t1 = time.time()
        info, opt = vc.vc_single(
            0,                                 # sid
            in_wav,
            pitch,                             # f0_up_key，升降调
            None,                              # f0_file
            method,                            # f0 提取方法
            index or "",                       # file_index
            None,                              # file_index2
            index_rate,
            3,                                 # filter_radius
            0,                                 # resample_sr，0=不重采样
            0.25,                              # rms_mix_rate
            protect,
        )
        t_infer = time.time() - t1
    except Exception as exc:  # noqa: BLE001
        import traceback

        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                          "trace": traceback.format_exc()[-800:]}, ensure_ascii=False))
        return 1

    if opt is None:
        print(json.dumps({"ok": False, "error": f"转换没产出音频：{info}"}, ensure_ascii=False))
        return 1

    import soundfile as sf

    sr, data = opt
    sf.write(out_wav, data, sr)
    size = os.path.getsize(out_wav)
    print(json.dumps({
        "ok": True, "out": out_wav, "sr": sr, "bytes": size,
        "load_sec": round(t_load, 2), "infer_sec": round(t_infer, 2),
        "total_sec": round(time.time() - t0, 2), "device": str(config.device),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
