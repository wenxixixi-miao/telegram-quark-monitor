#!/usr/bin/env python3
"""Check quark notify_queue.json and output unsent notifications.
If nothing to report, exit silently (empty stdout = no delivery).
After outputting, marks items as sent.
"""
import json, sys, os

QUEUE_FILE = "/root/quark-auto-save/notify_queue.json"

if not os.path.exists(QUEUE_FILE):
    sys.exit(0)

try:
    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
        items = json.load(f)
except (json.JSONDecodeError, IOError):
    sys.exit(0)

unsent = [item for item in items if not item.get("sent", True)]

if not unsent:
    sys.exit(0)

# Format message
lines = ["📦 **夸克转存通知**\n"]
for item in unsent:
    lines.append(f"[{item.get('time', '?')}] {item.get('title', '')}")
    detail = item.get("detail", "")
    if detail:
        lines.append(f"  {detail}")
    lines.append("")

print("\n".join(lines).strip())

# Mark as sent
for item in items:
    if not item.get("sent", True):
        item["sent"] = True

with open(QUEUE_FILE, "w", encoding="utf-8") as f:
    json.dump(items, f, ensure_ascii=False, indent=2)
