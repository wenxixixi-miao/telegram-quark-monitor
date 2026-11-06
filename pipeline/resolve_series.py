#!/usr/bin/env python3
"""新剧自动解析（P1）：目录名 → TMDB 候选 + 置信度

用途：把 /自动转存 里新出现、还没配置的剧目录，自动对到 TMDB（拿季容量/标题/年份），
      高置信度可自动写 tmdb_final.json + canonical.json；低置信度进 pending_mapping.json 等人工确认。
原则：只给候选+置信度，不猜测；低置信度绝不自动入库。

用法：
  python3 resolve_series.py --name "示例剧2"
  python3 resolve_series.py --scan-unknown        # 读 $TQM_STATE/unknown_dirs.json
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import ENV_DIR, LIB_DIR, STATE_DIR  # noqa: E402
import argparse
import difflib
import json
import os
import re
import sys
import urllib.parse
import urllib.request

TMDB_ENV = ENV_DIR + "/tmdb.env"
PENDING = STATE_DIR + "/pending_mapping.json"
VID = (".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".rmvb", ".flv", ".wmv", ".mov", ".mpg")

QUALITY = re.compile(
    r"(?i)(4k\w*|8k\w*|2160p\w*|1080p\w*|720p\w*|hdr\d*\w*|sdr|10bit|8bit|x26[45]|h\.?26[45]|hevc|avc|av01|"
    r"web[-\s]?dl|webrip|bluray|blu-ray|remux|bdrip|hdtv|aac|dts|ddp?\d?(?:\.\d)?|flac|hq|高码率|超清|"
    r"官方中字|中字|内封字幕|简繁英字幕|杜比|立体环绕声|臻彩|纯净版|典藏版|收藏版)")
NOISE = re.compile(
    r"(?i)(合集|全集|系列|部合集|\d+\s*部|全\d+季|更新至\s*\d+\s*集|S\d{1,2}E\d{1,4}(\s*-\s*E?\d{1,4})?|"
    r"\d+\s*集全|第[一二三四五六七八九十\d]+季|年番|4K|4k)")
CHANNEL = re.compile(r"(?i)^(食贫道|b站|哔哩哔哩|东瀛大宝荐|南洋大宝荐|北极大宝荐)\s*[-_\s]*")


def tmdb_key():
    for line in open(TMDB_ENV):
        if "=" in line and "KEY" in line.split("=")[0]:
            v = line.split("=", 1)[1].strip().strip('"').strip("'")
            if v:
                return v
    raise SystemExit("缺少 TMDB API KEY")


def tmdb(path, **params):
    params["api_key"] = tmdb_key()
    params.setdefault("language", "zh-CN")
    url = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(params)
    try:
        return json.load(urllib.request.urlopen(url, timeout=25))
    except Exception:
        return {}


def norm(s):
    s = (s or "").lower()
    return re.sub(r"[\[\]【】()（）〈〉《》·・,，、。:：!！?？'\"“”‘’\s_\-—~|/\\]+", "", s)


def clean_title(raw):
    s = os.path.splitext(raw)[0]
    s = QUALITY.sub(" ", s)
    s = NOISE.sub(" ", s)
    s = re.sub(r"[\[\]【】()（）]", " ", s)
    s = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ", s)     # 去年份
    s = re.sub(r"\s+", " ", s).strip(" -_.·")
    return s


def title_variants(raw):
    out = []
    base = clean_title(raw)
    cur = base
    for _ in range(3):
        m = CHANNEL.match(cur)
        if not m:
            break
        cur = cur[m.end():].strip(" -_.·")
    for s in (base, cur):
        if s and s not in out:
            out.append(s)
    # 尾部数字（"示例剧2"）→ 去掉尾巴再试
    s2 = re.sub(r"(?<=[\u4e00-\u9fff])\s*\d{1,2}$", "", cur).strip()
    if s2 and s2 not in out:
        out.append(s2)
    segs = [x for x in re.split(r"[\s\-_·]+", cur) if x]
    if len(segs) > 1:
        for i in range(len(segs)):
            for j in range(len(segs), i, -1):
                cand = (" ".join(segs[i:j]) if j - i > 1 else segs[i]).strip()
                if len(cand) >= 2 and cand not in out:
                    out.append(cand)
    return out


def year_of(raw):
    m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", raw)
    return int(m.group(1)) if m else None


def ep_stats(names):
    """粗估集号（复用 episode_parse 的 ep_key 更好，但这里保持零依赖）"""
    pats = [r"(?i)s\d{1,2}\s*[._ ]?e(?:p)?\s*0*(\d{1,4})",
            r"(?i)\bEP?\.?\s*0*(\d{1,4})(?!\d)",
            r"第\s*0*(\d{1,4})\s*[集话話]"]
    eps = []
    for n in names:
        for p in pats:
            m = re.search(p, n)
            if m:
                eps.append(int(m.group(1)))
                break
    return eps


FINAL = STATE_DIR + "/tmdb_final.json"
PACK = re.compile(r"(?i)(合集|全集|系列|部合集|\d+\s*部|全\d+季)")


def existing_match(name):
    """目录名与已核准 tmdb_final 的 key/title 匹配 → 直接复用，避免歧义"""
    try:
        final = json.load(open(FINAL))
    except Exception:
        return None
    n = norm(clean_title(name))
    for key, meta in final.items():
        title = meta.get("title") or key
        for cand in (key, title):
            c = norm(clean_title(cand))
            # 只允许"已有标题是新目录名的子串"（防 生化危机 → 示例剧 这类反向误配）
            if c and c in n and len(n) - len(c) <= 8:
                return {"kind": meta.get("kind"), "id": meta.get("id"), "title": title,
                        "original": "", "year": int(meta.get("year") or 0) or None,
                        "seasons": meta.get("seasons") or [], "total_eps": meta.get("total_eps"),
                        "confidence": 0.97,
                        "evidence": {"matched": key, "exact": True, "fuzzy": 1.0,
                                     "local_eps": 0, "max_ep": None, "source": "tmdb_final"}}
    return None


def resolve(name, names=None, kind_hint=None):
    """返回 {'candidates': [...]}；每个候选含 kind/id/title/year/seasons/confidence/evidence"""
    names = names or []
    ex = existing_match(name)
    if ex:
        return [ex]
    eps = ep_stats(names)
    is_tv = (len(eps) >= max(2, len(names) * 0.6)) if names else None
    kinds = [kind_hint] if kind_hint else (["tv", "movie"] if is_tv is None else
                                           (["tv"] if is_tv else ["movie"]))
    results = []
    for kind in kinds:
        for t in title_variants(name):
            q = tmdb("/search/" + ("tv" if kind == "tv" else "movie"),
                     query=t, include_adult="false")
            for r in (q.get("results") or [])[:8]:
                if kind == "tv":
                    title = r.get("name") or ""
                    otitle = r.get("original_name") or ""
                    date = r.get("first_air_date") or ""
                else:
                    title = r.get("title") or ""
                    otitle = r.get("original_title") or ""
                    date = r.get("release_date") or ""
                exact = norm(t) in (norm(title), norm(otitle))
                fuzzy = max(difflib.SequenceMatcher(None, norm(t), norm(title)).ratio(),
                            difflib.SequenceMatcher(None, norm(t), norm(otitle)).ratio())
                if not exact and fuzzy < 0.86:
                    continue
                conf = 0.9 if exact else 0.8
                y = year_of(date)
                ny = year_of(name)
                if y and ny:
                    if abs(y - ny) <= 1:
                        conf += 0.08
                    else:
                        conf -= 0.15
                det = tmdb("/%s/%d" % (kind, r["id"]))
                seasons, total = [], 0
                if kind == "tv":
                    for s in det.get("seasons") or []:
                        if s.get("season_number", 0) > 0 and s.get("episode_count"):
                            seasons.append({"n": s["season_number"], "eps": s["episode_count"]})
                            total += s["episode_count"]
                    max_ep = max(eps) if eps else None
                    if max_ep and total and max_ep > total:
                        conf *= 0.6                 # 本地集号超过 TMDB 容量 → 可能季数据过期/不同剧
                results.append({
                    "kind": kind, "id": r["id"], "title": title, "original": otitle,
                    "year": y, "seasons": seasons, "total_eps": total or None,
                    "confidence": round(min(conf, 0.99), 3),
                    "evidence": {"matched": t, "exact": exact, "fuzzy": round(fuzzy, 3),
                                 "local_eps": len(eps), "max_ep": max(eps) if eps else None,
                                 "source": "dir-name"}})
    # 去重取最高
    best = {}
    for c in results:
        k = (c["kind"], c["id"])
        if k not in best or c["confidence"] > best[k]["confidence"]:
            best[k] = c
    cands = sorted(best.values(), key=lambda x: -x["confidence"])[:5]
    pack_name = bool(PACK.search(name))
    multi_movie = (names and len(eps) == 0 and len(names) >= 2)
    for c in cands:
        if pack_name or multi_movie:
            c["confidence"] = round(min(c["confidence"], 0.5), 3)
            c["evidence"]["pack_guess"] = True
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name")
    ap.add_argument("--scan-unknown", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.scan_unknown:
        try:
            import sys as _sys
            _sys.path.insert(0, LIB_DIR)
            import mediaenv, urllib.request as _ur
            _base, _tok = mediaenv.openlist_login()

            def _ls(path):
                body = json.dumps({"path": path, "password": "", "page": 1,
                                   "per_page": 500, "refresh": False}).encode()
                req = _ur.Request(_base + "/api/fs/list", data=body, method="POST",
                                  headers={"Content-Type": "application/json",
                                           "Authorization": _tok})
                return (_ur.urlopen(req, timeout=40).read() and
                        (json.load(_ur.urlopen(req, timeout=40)).get("data") or {}).get("content")) or []

            def _names(d):
                out, stack = [], [("/夸克/自动转存/" + d, 0)]
                while stack:
                    pth, dep = stack.pop()
                    try:
                        items = _ls(pth)
                    except Exception:
                        continue
                    for it in items:
                        if it.get("is_dir") and dep < 2:
                            stack.append((pth + "/" + it["name"], dep + 1))
                        elif not it.get("is_dir") and it["name"].lower().endswith(VID):
                            out.append(it["name"])
                return out
        except Exception:
            _names = lambda d: []
        try:
            unknown = json.load(open(STATE_DIR + "/unknown_dirs.json")).get("unknown") or []
        except Exception:
            unknown = []
        allc = {}
        for name in unknown:
            cands = resolve(name, _names(name))
            allc[name] = cands
            top = cands[0] if cands else None
            print(f"【{name}】→ " + (f"{top['title']} ({top['year']}) {top['kind']}/{top['id']} "
                                     f"conf={top['confidence']}" if top else "无候选"))
        if args.json:
            print(json.dumps(allc, ensure_ascii=False, indent=1))
        pending = {k: v for k, v in allc.items()
                   if not v or v[0]["confidence"] < 0.9}
        os.makedirs(os.path.dirname(PENDING), exist_ok=True)
        json.dump(pending, open(PENDING, "w"), ensure_ascii=False, indent=1)
        print(f"\n待人工确认 {len(pending)} 个 → {PENDING}")
        return 0
    if not args.name:
        print("需要 --name 或 --scan-unknown")
        return 2
    for c in resolve(args.name):
        print(f"{c['kind']:5s} {c['id']:8d} {c['title']} ({c['year']}) "
              f"conf={c['confidence']} seasons={[(s['n'], s['eps']) for s in c['seasons']]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
