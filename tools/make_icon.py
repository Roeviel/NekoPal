"""生成应用图标（猫娘头像一个 .ico）。

只用来生成本地图标资源，运行时不需要它。
需要 Pillow；用 DSH 自带 Python 跑即可：
    python tools/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

BG = (34, 26, 46, 255)        # 深紫底
PINK = (255, 143, 199, 255)
FACE = (255, 217, 238, 255)
DARK = (138, 74, 117, 255)
BLUSH = (255, 168, 212, 150)


def make(size: int) -> Image.Image:
    s = size * 8  # 超采样后缩小，边缘更干净
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = int(s * 0.06)
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=int(s * 0.22), fill=BG)

    # 猫耳
    d.polygon(
        [(int(s * 0.24), int(s * 0.40)), (int(s * 0.34), int(s * 0.16)), (int(s * 0.46), int(s * 0.36))],
        fill=FACE,
    )
    d.polygon(
        [(int(s * 0.76), int(s * 0.40)), (int(s * 0.66), int(s * 0.16)), (int(s * 0.54), int(s * 0.36))],
        fill=FACE,
    )

    # 脸
    m = int(s * 0.20)
    d.ellipse([m, int(s * 0.30), s - m, s - int(s * 0.16)], fill=FACE)

    # 眼睛
    eye_r = int(s * 0.045)
    for cx in (s * 0.38, s * 0.62):
        d.ellipse([cx - eye_r, s * 0.52 - eye_r, cx + eye_r, s * 0.52 + eye_r], fill=DARK)

    # 嘴（一个小小的粉色弧）
    d.arc(
        [s * 0.44, s * 0.60, s * 0.56, s * 0.70],
        start=20,
        end=160,
        fill=PINK,
        width=max(2, int(s * 0.018)),
    )

    # 腮红
    for cx in (s * 0.31, s * 0.69):
        d.ellipse([cx - s * 0.045, s * 0.62, cx + s * 0.045, s * 0.67], fill=BLUSH)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    WEB.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [make(sz) for sz in sizes]
    ico = WEB / "favicon.ico"
    images[-1].save(ico, format="ICO", sizes=[(sz, sz) for sz in sizes])
    png = WEB / "icon.png"
    make(256).save(png)
    print(f"已生成 {ico}（{ico.stat().st_size} 字节）")
    print(f"已生成 {png}（{png.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
