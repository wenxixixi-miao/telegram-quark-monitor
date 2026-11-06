#!/usr/bin/env python3
"""LLM 标题兜底：消费 tg_events 里的 unmatched 事件，用 DeepSeek 认剧名

- 高置信度（≥0.9）：写 new_share 事件（交给 intake 正常入库）+ 把标题写进 canonical 别名
- 低置信度：进 $TQM_STATE/title_pending.json（等人工确认），不静默
- 默认只读；--apply 才写事件/别名；--dry 打印判定
用法：
  python3 llm_title_resolve.py --dry
  python3 llm_title_resolve.py --apply
  python3 llm_title_resolve.py --dry --events-file /tmp/test.jsonl   # 测试用
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import LIB_DIR, SCRIPT_DIR, STATE_DIR  # noqa: E402
import argparse
import json
import os
import sys
import time

sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, LIB_DIR)

EVENTS = STATE_DIR + "/tg_events.jsonl"
CANONICAL = STATE_DIR + "/canonical.json"
STATE = STATE_DIR + "/llm_title_state.json"
PENDING = STATE_DIR + "/title_pending.json"
CANDIDATES = STATE_DIR + "/new_show_candidates.json"
RESOLVED = STATE_DIR + "/title_resolved.json"
HIGH = 0.9


def load(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default


def save(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def append_event(ev):
    with open(EVENTS, "a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--events-file")
    ap.add_argument("--max", type=int, default=30)
    args = ap.parse_args()

    events_file = args.events_file or EVENTS
    canon = load(CANONICAL, {})
    if not canon:
        print("canonical.json 不存在")
        return 2
    try:
        import llm_recognize
    except Exception as e:  # noqa: BLE001
        print("⚠️ llm_recognize 不可用：%s" % e)
        return 0
    if not llm_recognize.available():
        print("⚠️ 未配置 $TQM_ENV/llm.env")
        return 0

    try:
        with open(events_file) as f:
            offset = 0 if args.events_file else int(load(STATE, {}).get("offset") or 0)
            f.seek(offset)
            lines = f.readlines()
            new_offset = f.tell()
    except FileNotFoundError:
        print("没有事件文件")
        return 0

    pending = load(PENDING, {})
    candidates = load(CANDIDATES, {"items": {}})
    cand_added = 0
    resolved = load(RESOLVED, {})
    applied, low = [], []
    seen_urls = set(resolved) | set(pending)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") != "unmatched":
            continue
        url = ev.get("share_url")
        title = (ev.get("title") or "").strip()
        if not url or not title or url in seen_urls:
            continue
        seen_urls.add(url)
        if len(applied) + len(low) >= args.max:
            break
        res = llm_recognize.classify_title(title, [], list(canon.keys()))
        name = res.get("canonical")
        conf = res.get("confidence", 0)
        rec = {"title": title, "url": url, "canonical": name, "confidence": conf,
               "reason": res.get("reason", ""), "ts": int(time.time()),
               "msg_id": ev.get("msg_id")}
        if name and conf >= HIGH:
            applied.append(rec)
            resolved[url] = rec
            if args.apply:
                # 1) 写 new_share 事件 → intake 下一轮正常按缺集入库
                append_event({"ts": int(time.time()), "type": "new_share",
                              "canonical": name, "alias": "llm",
                              "share_url": url, "channel": ev.get("channel"),
                              "msg_id": ev.get("msg_id"), "title": title,
                              "msg_date": ev.get("msg_date"),
                              "llm_confidence": conf})
                # 2) 标题加入 canonical 别名（以后同类标题规则直接命中）
                alias = title[:24]
                try:
                    meta = canon[name]
                    aliases = set(meta.get("aliases") or [])
                    if alias and alias not in aliases:
                        aliases.add(alias)
                        meta["aliases"] = sorted(aliases)
                        save(CANONICAL, canon)
                except Exception:
                    pass
        elif name:
            # 可能是我们追的剧，但没把握 → 进待确认（会提示）
            low.append(rec)
            pending[url] = rec
        else:
            # 模型判断是"完全没在追的剧" → 只记候选清单，不推微信
            items = candidates.setdefault("items", {})
            import re as _re
            base = _re.sub(r"[（(].*?[)）]|[\s\d.\-_/]+", "", title)[:24] or title[:24]
            if base not in items:
                items[base] = {"title": title, "url": url, "msg_id": ev.get("msg_id"),
                               "ts": int(time.time()), "reason": rec.get("reason", "")}
                cand_added += 1
    if args.apply:
        save(RESOLVED, resolved)
        save(PENDING, pending)
        save(CANDIDATES, candidates)
        save(STATE, {"offset": new_offset, "ts": int(time.time())})

    for r in applied:
        print("✅ %s → %s (%.2f) %s" % (r["title"][:40], r["canonical"], r["confidence"], r["reason"][:50]))
    for r in low:
        print("⏳ %s → 待确认 (候选 %s %.2f) %s" % (r["title"][:40], r["canonical"], r["confidence"], r["reason"][:40]))
    if not applied and not low:
        if not (args.apply and cand_added):
            print("（无新的 unmatched 事件）")
        else:
            print("")
    if not args.apply and not args.events_file:
        print("\n[dry] 加 --apply 才写事件/别名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
