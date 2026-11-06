#!/usr/bin/env python3
"""Telegram 频道历史搜索（只读，给 Hermes/人工找片用）

- 默认用**独立 session** `quark_monitor_new`，与常驻监听器 `quark_tg_discover` 的
  `quark_monitor` session 互不干扰，**不需要停 daemon**。
- 只读：不发言、不退频道、不改任何东西。
- 输出：人类可读列表 + 可选 JSON；结果同时存 $TQM_STATE/tg_search_last.json

用法：
  python3 tg_search.py --query "示例剧"
  python3 tg_search.py --query "凡人修仙传" --days 60 --limit 100
  python3 tg_search.py --query "示例剧" --json
  python3 tg_search.py --query "示例剧" --canonical 示例剧   # 只看能匹配到该剧的帖子
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import PIPE_ROOT, STATE_DIR  # noqa: E402
import argparse
import asyncio
import json
import os
import re
import sys
import time

BASE = PIPE_ROOT
CONFIG = os.path.join(BASE, "monitor_config.json")
CANONICAL = STATE_DIR + "/canonical.json"
OUT = STATE_DIR + "/tg_search_last.json"
QUARK_RE = re.compile(r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+")
JUNK = re.compile(r"(短剧更新目录|更新目录|目录合集|合集目录)")
SESSION_SEARCH = "quark_monitor_new"      # 独立只读 session
SESSION_MAIN = "quark_monitor"            # daemon 用的 session（回退时才用）


def load(p, d=None):
    try:
        return json.load(open(p))
    except Exception:
        return d if d is not None else {}


def resolve_canon(title, canon):
    best = None
    for name, meta in canon.items():
        for a in list(meta.get("aliases") or []) + [name]:
            if a and a in title and (best is None or len(a) > best[1]):
                best = (name, len(a))
    return best[0] if best else None


async def _search(session, conf, query, days, limit):
    from telethon import TelegramClient
    client = TelegramClient(os.path.join(BASE, session), int(conf["api_id"]), str(conf["api_hash"]))
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        return None
    cutoff = time.time() - days * 86400
    channel = (conf.get("channels") or ["ExampleChannel"])[0]
    entity = await client.get_entity(channel)
    rows = []
    async for msg in client.iter_messages(entity, search=query, limit=limit):
        if msg.date.timestamp() < cutoff:
            break
        text = msg.text or ""
        title = text.strip().split("\n")[0].strip()[:160]
        links = list(dict.fromkeys(QUARK_RE.findall(text)))
        rows.append({"id": msg.id,
                     "date": msg.date.astimezone().isoformat(timespec="minutes"),
                     "title": title,
                     "junk": bool(JUNK.search(title)),
                     "links": links})
    await client.disconnect()
    return rows


async def _run(args):
    conf = load(CONFIG)
    if not conf.get("api_id"):
        print("❌ 缺少 monitor_config.json")
        return 2
    canon = load(CANONICAL)
    rows = await _search(SESSION_SEARCH, conf, args.query, args.days, args.limit)
    if rows is None:
        # 独立 session 失效 → 回退：短暂停 daemon，用主 session 只读搜，再拉起
        import subprocess
        print("⚠️ 独立 session 已失效，临时停 daemon 用主 session 搜索（几秒）", file=sys.stderr)
        subprocess.run(["systemctl", "stop", "quark-tg-discover"], check=False)
        try:
            rows = await _search(SESSION_MAIN, conf, args.query, args.days, args.limit)
        finally:
            subprocess.run(["systemctl", "start", "quark-tg-discover"], check=False)
        if rows is None:
            print("❌ 两个 session 都不可用，需要重新登录（找 DSH）")
            return 3

    for r in rows:
        r["canonical"] = resolve_canon(r["title"], canon)
    if args.canonical:
        rows = [r for r in rows if r.get("canonical") == args.canonical]
    if not args.show_junk:
        rows = [r for r in rows if not r["junk"]]
    if args.limit_seen:
        rows = rows[:args.limit_seen]

    result = {"query": args.query, "days": args.days, "ts": int(time.time()),
              "count": len(rows), "rows": rows}
    try:
        tmp = OUT + ".tmp"
        json.dump(result, open(tmp, "w"), ensure_ascii=False, indent=1)
        os.replace(tmp, OUT)
    except OSError:
        pass

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("没有找到相关帖子。" + ("（可试试 --days 更大 / 换关键词）" if args.days <= 60 else ""))
        return 0
    print("找到 %d 条（只读，未做任何改动）：" % len(rows))
    for r in rows:
        tag = r.get("canonical") or "?"
        print("  [%s] %s  %s  → %s" % (r["date"][5:16], tag, r["title"][:70],
                                        (r["links"][0] if r["links"] else "无链接")))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--canonical", help="只保留能匹配到该剧的帖子")
    ap.add_argument("--show-junk", action="store_true", help="保留短剧目录帖")
    ap.add_argument("--limit-seen", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
