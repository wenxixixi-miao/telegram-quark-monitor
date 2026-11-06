#!/usr/bin/env python3
"""待确认集数处理（P1 交互闭环）

intake 遇到"文件名认不出、LLM 也不敢定"的集时，会写进
  $TQM_STATE/episode_pending.json
本脚本负责：列出、通知、人工确认后写回并重新入队。

用法：
  python3 pending_apply.py --list
  python3 pending_apply.py --notify            # cron 用：只输出"新的"待确认
  python3 pending_apply.py --set 1 --episode 164   # 第 1 条 = 第 164 集
  python3 pending_apply.py --set 1 --episode 3 --season 2
  python3 pending_apply.py --skip 1

确认后会：写 $TQM_STATE/episode_resolutions.json，并往 tg_events 追加一条 new_share 事件，
下一轮 intake（≤5 分钟）会按"人工确认的集号"正常入库。
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import STATE_DIR  # noqa: E402
import argparse
import json
import os
import sys
import time

PENDING = STATE_DIR + "/episode_pending.json"
RESOLUTIONS = STATE_DIR + "/episode_resolutions.json"
SKIPPED = STATE_DIR + "/episode_skipped.json"
EVENTS = STATE_DIR + "/tg_events.jsonl"
HIGH = 0


def load(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default


def save(p, obj, mode=0o600):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    try:
        os.chmod(p, mode)
    except OSError:
        pass


def items_sorted():
    d = load(PENDING, {})
    items = list((d.get("items") or {}).values())
    items.sort(key=lambda x: x.get("ts") or 0)
    return items


def pick(items, key):
    """key 可以是序号（1 起）或 id"""
    key = str(key).strip()
    if key.isdigit():
        idx = int(key) - 1
        if 0 <= idx < len(items):
            return items[idx]
        raise SystemExit("序号超出范围（共 %d 条）" % len(items))
    for it in items:
        if it.get("id") == key or it.get("id", "").startswith(key):
            return it
    raise SystemExit("找不到待确认项：%s" % key)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--set")
    ap.add_argument("--episode", type=int)
    ap.add_argument("--season", type=int)
    ap.add_argument("--skip")
    args = ap.parse_args()

    items = items_sorted()

    if args.list:
        if not items:
            print("没有待确认项 ✅")
            return 0
        for i, it in enumerate(items, 1):
            print("%d) %s / %s  （分享：%s）" %
                  (i, it.get("canonical"), it.get("file"), it.get("share_url")))
        return 0

    if args.notify:
        d = load(PENDING, {})
        new = [it for it in (d.get("items") or {}).values() if not it.get("notified")]
        if not new:
            return 0
        print("❓ 有 %d 个文件认不出是哪一集，需要你确认：" % len(new))
        for i, it in enumerate(new, 1):
            print("  %d) %s / %s" % (i, it.get("canonical"), it.get("file")))
        print("回复方式：例如「第1条是第164集」，或让助手执行：")
        print("  python3 pipeline/pending_apply.py --set 1 --episode 164")
        print("（认不出就回「第1条跳过」）")
        for it in new:
            it["notified"] = True
        save(PENDING, d)
        return 0

    if args.set:
        if not args.episode:
            raise SystemExit("--set 需要同时给 --episode")
        it = pick(items, args.set)
        canon, url, fname = it.get("canonical"), it.get("share_url"), it.get("file")
        fixes = load(RESOLUTIONS, {})
        fixes["%s|%s|%s" % (canon, url, fname)] = {
            "episode": int(args.episode), "season": args.season,
            "ts": int(time.time()), "by": "manual"}
        save(RESOLUTIONS, fixes)
        # 从待确认里移除
        d = load(PENDING, {})
        d.get("items", {}).pop(it.get("id"), None)
        save(PENDING, d)
        # 重新入队，让下一轮 intake 处理
        ev = {"ts": int(time.time()), "type": "new_share", "canonical": canon,
              "alias": "manual", "share_url": url, "channel": "ExampleChannel",
              "msg_id": it.get("msg_id"), "title": it.get("title") or "",
              "msg_date": "", "manual_episode": int(args.episode)}
        with open(EVENTS, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        print("✅ 已记录：%s / %s = 第 %s 集；下一轮自动入库（≤5 分钟）" %
              (canon, fname, args.episode))
        return 0

    if args.skip:
        it = pick(items, args.skip)
        d = load(PENDING, {})
        d.get("items", {}).pop(it.get("id"), None)
        save(PENDING, d)
        sk = load(SKIPPED, {"items": {}})
        sk.setdefault("items", {})[it.get("id")] = dict(it, skipped_ts=int(time.time()))
        save(SKIPPED, sk)
        print("已跳过：%s / %s" % (it.get("canonical"), it.get("file")))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
