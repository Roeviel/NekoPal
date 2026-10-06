"""B站客户端自测 CLI（免登录公开数据）。

用途：验证 ``neko.bilibili`` 是否真的能拿到 JSON（而不是风控 HTML）。

用法::

    .venv\\Scripts\\python.exe tools\\bili_probe.py                      # 默认关键词
    .venv\\Scripts\\python.exe tools\\bili_probe.py "机器学习入门" 5      # 关键词 + 条数
    .venv\\Scripts\\python.exe tools\\bili_probe.py --bv BV1GJ411x7h7     # 只测指定视频
    .venv\\Scripts\\python.exe tools\\bili_probe.py --json               # 输出原始 JSON 摘要

退出码：0 成功；1 失败（搜索/详情拿不到 JSON，通常是签名或风控问题）。
只访问 api.bilibili.com / www.bilibili.com / *.hdslb.com，仅用于本地学习。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许直接 `python tools/bili_probe.py`（把项目根目录加进 sys.path）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from neko.bilibili import BiliClient, BiliError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NekoPal B站客户端自测")
    parser.add_argument("keyword", nargs="?", default="Python 入门", help="搜索关键词")
    parser.add_argument("limit", nargs="?", type=int, default=5, help="返回条数上限")
    parser.add_argument("--bv", default=None, help="跳过搜索，直接测这个 bvid")
    parser.add_argument("--json", action="store_true", help="额外打印 JSON 摘要")
    args = parser.parse_args(argv)

    failures = 0

    with BiliClient() as bili:
        print(f"[probe] client = {bili!r}")
        print(f"[probe] subtitle_priority = {bili.subtitle_priority}")

        target = args.bv

        print(f"\n=== search({args.keyword!r}, limit={args.limit}) ===")
        if target:
            print("(已指定 --bv，跳过搜索)")
        else:
            try:
                items = bili.search(args.keyword, limit=args.limit)
            except BiliError as exc:
                print(f"!! 搜索失败：{exc}")
                return 1
            if not items:
                print("!! 搜索返回 0 条（可能是签名降级或关键词无结果）")
                failures += 1
            else:
                print(f"OK 搜到 {len(items)} 条：")
                for idx, item in enumerate(items, 1):
                    print(
                        f"  {idx}. {item['title'][:70]}\n"
                        f"     up={item['author']} mid={item['mid']} "
                        f"时长={item['duration']}s 播放={item['play']} "
                        f"弹幕={item['danmaku']} pubdate={item['pubdate']}\n"
                        f"     {item['url']}"
                    )
            if args.json and items:
                print(json.dumps(items[:2], ensure_ascii=False, indent=2))
            target = target or items[0]["bvid"]

        if not target:
            return 1

        print(f"\n=== video({target}) ===")
        try:
            info = bili.video(target)
        except BiliError as exc:
            print(f"!! 详情失败：{exc}")
            return 1
        print(
            f"OK 标题：{info['title'][:80]}\n"
            f"   bvid={info['bvid']} aid={info['aid']} cid={info['cid']}\n"
            f"   up={info['owner']}({info['owner_mid']}) 时长={info['duration']}s "
            f"播放={info['view']} 点赞={info['like']} 分P={len(info['pages'])}\n"
            f"   desc={info['desc'][:100]!r}"
        )
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))

        print(f"\n=== subtitles({target}) ===")
        status = bili.subtitle_status(target)
        print(
            f"  探测：need_login={status['need_login']} 可用={status['available']} "
            f"(cc={status['cc']} ai={status['ai']}) asr_language={status['asr_language']!r}"
        )
        subs = bili.subtitles(target)
        if not subs:
            if status["need_login"]:
                print(
                    "无可用字幕。注意：B站在未登录时会置 need_login_subtitle=True，"
                    "即「字幕是登录门槛功能」，未登录下实测可得率约为 0（见 bilibili.py 已知坑 7）。"
                )
            else:
                print("无可用字幕（该视频确实没有 CC / AI 字幕）")
        else:
            print(f"OK 拿到 {len(subs)} 条字幕：")
            for sub in subs:
                print(
                    f"  · [{sub['lan']}] {sub['lan_doc']} source={sub['source']} "
                    f"文本 {len(sub['text'])} 字\n    前 80 字：{sub['text'][:80]!r}"
                )

        print("\n=== hot(limit=3) ===")
        try:
            ranking = bili.hot(limit=3)
            print(f"OK 排行榜 {len(ranking)} 条：")
            for item in ranking:
                print(f"  · {item['title'][:60]} | {item['url']}")
        except BiliError as exc:
            print(f"!! 排行榜失败：{exc}")
            failures += 1

    print(f"\n[probe] 完成，失败项 {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
