#!/usr/bin/env python3
"""护栏回放：拉最近 N 天频道消息，统计 canonical 命中 / 护栏拦截（只读，不写事件）"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import PIPE_ROOT, STATE_DIR  # noqa: E402
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone

BASE = PIPE_ROOT
CONFIG = os.path.join(BASE, "monitor_config.json")
CANONICAL = STATE_DIR + "/canonical.json"
QUARK_URL_RE = re.compile(r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+")
EXCLUDE = ("短剧", "微短剧", "更新目录", "目录合集", "合集目录",
           "预告", "花絮", "采访", "解说", "盘点", "混剪", "片段")


def resolve(title, canon):
    best = None
    for name, meta in canon.items():
        for alias in meta.get("aliases", []) + [name]:
            if alias and alias in title:
                if best is None or len(alias) > best[1]:
                    best = (name, len(alias), alias)
    return best


async def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
    from telethon import TelegramClient
    conf = json.load(open(CONFIG))
    canon = json.load(open(CANONICAL))
    client = TelegramClient(os.path.join(BASE, conf.get("session", "quark_monitor")),
                            int(conf["api_id"]), str(conf["api_hash"]))
    await client.connect()
    if not await client.is_user_authorized():
        print("session 未授权"); return 2
    ent = await client.get_entity((conf.get("channels") or ["ExampleChannel"])[0])
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    stats = {"msgs": 0, "with_link": 0, "canonical_hit": 0, "guarded": 0,
             "guarded_samples": [], "hit_shows": {}}
    async for msg in client.iter_messages(ent, limit=limit):
        if msg.date.timestamp() < cutoff:
            break
        stats["msgs"] += 1
        text = msg.text or ""
        title = text.strip().split("\n")[0][:160]
        links = QUARK_URL_RE.findall(text)
        if not links:
            continue
        stats["with_link"] += 1
        r = resolve(title, canon)
        if not r:
            continue
        stats["canonical_hit"] += 1
        stats["hit_shows"][r[0]] = stats["hit_shows"].get(r[0], 0) + 1
        reason = next((w for w in EXCLUDE if w in title), None)
        if reason:
            stats["guarded"] += 1
            if len(stats["guarded_samples"]) < 10:
                stats["guarded_samples"].append({"title": title[:90], "reason": reason})
    await client.disconnect()
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
