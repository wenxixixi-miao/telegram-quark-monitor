#!/usr/bin/env python3
"""跨路径入库/通知账本（Review6）

官方追更与 P1 intake 是两条独立链路：一边刚写入的文件，另一边用网盘列表有时看不到
（列表/索引延迟），于是同一集会各存一份。本账本在本地记录"某剧某集已入库 / 今天已通知"，
两条链路保存前都查、成功都记，保证：

  1. 同一部剧的同一集只存一份（无论哪个源先到）；
  2. 同一部剧每天只通知一次（当天后续入库静默，但照常入库）。

状态文件：$TQM_STATE/ep_ledger.json（只读兼容：不存在时视为空）
  {"saved": {"示例剧": {"1|165": 1793...}}, "notified": {"示例剧": "2026-11-01"}}
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import STATE_DIR  # noqa: E402
import json
import os
import time

STATE = os.environ.get("EP_LEDGER", STATE_DIR + "/ep_ledger.json")
SAVED_TTL = 7 * 86400      # 入库记录保留 7 天（跨天重复也能防）


def _now():
    return int(time.time())


def _today():
    return time.strftime("%Y-%m-%d")


def _norm(pair):
    """(season, ep) -> (int|None, int)；进不了 int 的返回 None"""
    if not pair:
        return None
    try:
        season = pair[0]
        ep = pair[1]
    except (TypeError, IndexError):
        return None
    try:
        ep = int(ep)
    except (TypeError, ValueError):
        return None
    if season is not None:
        try:
            season = int(season)
        except (TypeError, ValueError):
            season = None
    return (season, ep)


def _key(pair):
    pair = _norm(pair)
    if not pair:
        return None
    return "%s|%s" % ("?" if pair[0] is None else pair[0], pair[1])


def load():
    try:
        with open(STATE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(d):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE)
    except OSError:
        pass


def saved_pairs(canonical, d=None):
    """该剧 7 天内已入库的 (season, ep) 集合"""
    d = d if d is not None else load()
    rec = (d.get("saved") or {}).get(canonical) or {}
    out = set()
    for k in rec:
        part = k.split("|")
        if len(part) != 2:
            continue
        s = None if part[0] == "?" else part[0]
        try:
            out.add((None if s is None else int(s), int(part[1])))
        except (TypeError, ValueError):
            continue
    return out


def record_saved(canonical, pairs):
    """记录已入库的集；返回实际新增的 pair 数"""
    d = load()
    saved = d.setdefault("saved", {})
    rec = saved.setdefault(canonical, {})
    cutoff = _now() - SAVED_TTL
    for k in list(rec):
        if int(rec.get(k) or 0) < cutoff:
            rec.pop(k, None)
    n = 0
    for pair in pairs or []:
        k = _key(pair)
        if not k:
            continue
        if k not in rec:
            n += 1
        rec[k] = _now()
    save(d)
    return n


def notified_today(canonical):
    d = load()
    return (d.get("notified") or {}).get(canonical) == _today()


def record_notified(canonical):
    d = load()
    d.setdefault("notified", {})[canonical] = _today()
    # 清掉非今天的旧通知记录，避免无限增长
    today = _today()
    d["notified"] = {k: v for k, v in d["notified"].items() if v == today}
    save(d)


def filter_new(canonical, pairs):
    """pairs 里去掉已入库的，返回 (保留列表, 重复列表)"""
    done = saved_pairs(canonical)
    keep, dup = [], []
    for p in pairs or []:
        np = _norm(p)
        if np and np in done:
            dup.append(p)
        else:
            keep.append(p)
    return keep, dup
