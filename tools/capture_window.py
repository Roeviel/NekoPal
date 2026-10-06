"""抓取"蕴"的应用窗口截图，用于人工确认界面观感。

两种抓法，各有取舍：
- 默认 **PrintWindow(PW_RENDERFULLCONTENT)**：只抓目标窗口自身内容，
  不受窗口层级影响，也不会把你桌面上别的东西拍进来。
  早期在 Chromium 沙箱启用时它只能抓到纯白；关掉沙箱后可以正常工作。
- `--screen`：抓屏幕上该窗口所在的矩形。窗口被别的程序挡住时会拍到遮挡物，
  所以只在确实需要"所见即所得"时用。

**重要：抓之前会把窗口置前并等 2.5 秒。**
窗口在后台时 Chromium 合成器不重绘，PrintWindow 会返回上一次绘制的画面
（过期帧）。这个坑真实发生过：页面已经切到设置页，抓到的却还是聊天页，
差点让我以为视图切换坏了。要跳过置前用 `--no-foreground`。

用法：python tools/capture_window.py <输出png> [--pid N] [--title 子串] [--screen]
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

from PIL import Image, ImageGrab

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
try:
    user32.SetProcessDPIAware()
except Exception:  # noqa: BLE001
    pass


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


def print_window(hwnd: int, rect: tuple[int, int, int, int]) -> Image.Image | None:
    """用 PrintWindow 抓窗口自身内容（与层级无关）。"""
    l, t, r, b = rect
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None
    hwnd_dc = user32.GetWindowDC(hwnd)
    mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    gdi32.SelectObject(mfc_dc, bmp)
    user32.PrintWindow(hwnd, mfc_dc, 2)     # 2 = PW_RENDERFULLCONTENT
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(bi)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(mfc_dc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mfc_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)
    if not got:
        return None
    return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")


def is_blank(img: Image.Image) -> bool:
    colors = img.getcolors(maxcolors=1_000_000) or []
    if not colors:
        return True
    colors.sort(reverse=True)
    return colors[0][0] > img.width * img.height * 0.985


def find_app_window(
    pid: int | None = None, title_filter: str | None = None
) -> tuple[int, str, tuple[int, int, int, int]] | None:
    found: list[tuple[int, str, tuple[int, int, int, int]]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        if pid is not None:
            owner = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(n, 1) + 1)
        if n:
            user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if title_filter:
            if title_filter.lower() not in title.lower():
                return True
        elif title in ("Program Manager", "Windows 默认锁屏界面"):
            return True          # 桌面本身不是应用窗口，别把它当目标
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        found.append((hwnd, title, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        return None
    # 取面积最大的那个（应用窗口通常最大）
    found.sort(key=lambda x: (x[2][2] - x[2][0]) * (x[2][3] - x[2][1]), reverse=True)
    return found[0]


def main() -> int:
    pid = None
    if "--pid" in sys.argv:
        pid = int(sys.argv[sys.argv.index("--pid") + 1])
    title_filter = None
    if "--title" in sys.argv:
        title_filter = sys.argv[sys.argv.index("--title") + 1]
    rest = [a for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()]
    if title_filter in rest:
        rest.remove(title_filter)
    out = Path(rest[0]) if rest else Path("data/ui_screen.png")
    foreground = "--no-foreground" not in sys.argv

    target = find_app_window(pid, title_filter)
    if not target:
        print(f"!! 没找到窗口（pid={pid} title={title_filter}）")
        return 1
    hwnd, title, rect = target
    l, t, r, b = rect
    print(f"目标窗口 hwnd={hwnd} title={title!r} rect=({l},{t},{r},{b})")

    want_screen = "--screen" in sys.argv

    # 必须**先置前再抓**：窗口在后台时 Chromium 的合成器不重绘，
    # PrintWindow 会返回上一次绘制的画面 —— 也就是一张过期帧。
    # 这个坑把我骗过一次：页面明明已经切到设置页，抓到的还是聊天页。
    if "--no-foreground" not in sys.argv:
        try:
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            time.sleep(2.5)                     # 留时间让合成器重绘一帧
            print("已把窗口置前（逼它重绘）")
        except Exception as exc:  # noqa: BLE001
            print(f"置前失败（继续抓取）: {exc}")

    img = None
    if not want_screen:
        img = print_window(hwnd, rect)
        if img is None or is_blank(img):
            print("PrintWindow 抓到空白（Chromium 的 GPU 合成会这样），改用屏幕抓取")
            img = None

    if img is None:
        img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True).convert("RGB")

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)

    colors = img.getcolors(maxcolors=4_000_000) or []
    colors.sort(reverse=True)
    total = img.width * img.height
    print(f"抓取 {img.width}x{img.height} 颜色数 {len(colors)} -> {out}")
    print("占比最高的 6 种颜色：")
    for cnt, col in colors[:6]:
        print(f"   RGB{col}  {cnt * 100 / total:5.1f}%")
    if is_blank(img):
        print("!! 抓到的几乎是纯色，窗口很可能没渲染内容")
        return 1
    print("=> 画面有内容")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
