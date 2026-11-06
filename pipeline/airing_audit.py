#!/usr/bin/env python3
"""播出时标核对（U1 基础）：TMDB 已播集 vs 本地已有集

判定口径（重要）：
  - 不用「总集数差」当漏更依据（历史删除会造成天天空报）
  - 只看「尾部」：TMDB 最近一集已播 ≥ LAG_HOURS，但本地系列目录没有这一集 → 报「尾部落后」
  - 同时输出总已播/本地集数（仅作参考，不当告警）
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import ENV_DIR, STATE_DIR  # noqa: E402
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TMDB_ENV = ENV_DIR + "/tmdb.env"
CANON = STATE_DIR + "/canonical.json"
MEDIA = "/srv/media/剧集"
LAG_HOURS = 48
VID = (".strm",)


def tmdb_key():
    for line in open(TMDB_ENV):
        if "=" in line and "KEY" in line.split("=")[0]:
            v = line.split("=", 1)[1].strip().strip('"').strip("'")
            if v:
                return v
    raise SystemExit("缺少 TMDB API KEY")


KEY = tmdb_key()


def tmdb(path, **params):
    params["api_key"] = KEY
    params.setdefault("language", "zh-CN")
    url = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(params)
    try:
        return json.load(urllib.request.urlopen(url, timeout=25))
    except Exception:
        return {}


def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def local_eps(series_dir):
    """返回 ({(season,ep)}, max_ep_by_season)"""
    eps = set()
    if not os.path.isdir(series_dir):
        return eps
    for root, _d, files in os.walk(series_dir):
        for fn in files:
            if not fn.endswith(".strm"):
                continue
            m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", fn)
            if m:
                eps.add((int(m.group(1)), int(m.group(2))))
    return eps


def find_series_dir(title, year):
    cand = os.path.join(MEDIA, f"{title} ({year})")
    if os.path.isdir(cand):
        return cand
    for d in os.listdir(MEDIA):
        if d.startswith(title):
            return os.path.join(MEDIA, d)
    return cand


def main():
    canon = json.load(open(CANON))
    rows = []
    now = time.time()
    for name, meta in canon.items():
        if meta.get("kind") != "tv":
            continue
        tid = meta.get("tmdb_id")
        if not tid:
            continue
        det = tmdb("/tv/%d" % int(tid))
        seasons = [s for s in (det.get("seasons") or []) if s.get("season_number", 0) > 0]
        latest = None      # (season, ep, air_ts, air_date)
        released = 0
        season_map = {}
        for s in seasons:
            sn = s["season_number"]
            sd = tmdb("/tv/%d/season/%d" % (int(tid), sn))
            eps = sd.get("episodes") or []
            season_map[sn] = len(eps)
            for e in eps:
                ts = parse_date(e.get("air_date") or "")
                if ts is None:
                    continue
                if ts <= now:
                    released += 1
                    if latest is None or ts > latest[2]:
                        latest = (sn, e.get("episode_number"), ts, e.get("air_date"))
        sdir = find_series_dir(meta.get("title"), meta.get("year"))
        leps = local_eps(sdir)
        local_max = max([ep for (_s, ep) in leps], default=0)
        if latest:
            sn, ep, ts, adate = latest
            present = (sn, ep) in leps
            age_days = round((now - ts) / 86400, 1)
            tail_lag = (not present) and ((now - ts) >= LAG_HOURS * 3600)
            rows.append({"canonical": name, "title": meta.get("title"),
                         "tmdb_id": tid, "latest_aired": f"S{sn:02d}E{ep:02d}" if ep else f"S{sn:02d}E?",
                         "air_date": adate, "air_age_days": age_days,
                         "local_has_latest": present, "tail_lag": tail_lag,
                         "released_eps": released, "local_eps": len(leps),
                         "local_max_ep": local_max, "local_dir": os.path.basename(sdir)})
        else:
            rows.append({"canonical": name, "title": meta.get("title"), "tmdb_id": tid,
                         "latest_aired": None, "tail_lag": False,
                         "released_eps": 0, "local_eps": len(leps), "local_max_ep": local_max})
        time.sleep(0.15)

    lags = [r for r in rows if r.get("tail_lag")]
    print("=" * 96)
    print("播出时标核对（LAG≥%dh 判尾部落后；总集数差仅参考，不作漏更告警）" % LAG_HOURS)
    print("=" * 96)
    print(f"{'剧':18s} {'最近已播':10s} {'播出日期':11s} {'本地有?':5s} {'尾部落后':6s} {'已播/本地':10s}")
    for r in rows:
        print(f"{r['canonical'][:16]:18s} {str(r.get('latest_aired')):10s} "
              f"{str(r.get('air_date')):11s} {str(r.get('local_has_latest')):5s} "
              f"{'⚠️' if r.get('tail_lag') else '':6s} {r['released_eps']}/{r['local_eps']}")
    print()
    if lags:
        print("尾部落后清单（需要补的）：")
        for r in lags:
            print("  - %s：最近已播 %s（%s，%.1f 天前）本地没有" %
                  (r["canonical"], r["latest_aired"], r["air_date"], r["air_age_days"]))
    else:
        print("✅ 无尾部落后（所有剧最近一集本地都有）")
    out = STATE_DIR + "/airing_audit.json"
    json.dump({"ts": int(now), "lag_hours": LAG_HOURS, "rows": rows}, open(out, "w"),
              ensure_ascii=False, indent=1)
    print("\n明细已存:", out)


if __name__ == "__main__":
    main()
