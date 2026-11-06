#!/usr/bin/env python3
"""添加新剧 / 新关键词（给 Hermes 或人工用的一条命令）

默认 dry-run：只解析 TMDB + 打印计划，不动任何文件。
--apply 才会：建目录（如果不存在）→ 写 tmdb_final.json + canonical.json（带别名/关键词）。

示例：
  python3 add_show.py --name "凡人修仙传"                 # 看候选和计划
  python3 add_show.py --name "凡人修仙传" --apply          # 真正加入
  python3 add_show.py --name "凡人修仙传" --keyword "凡人" --apply
  python3 add_show.py --name "凡人修仙传" --year 2020 --tmdb-id 94266 --apply
  python3 add_show.py --list                             # 看当前在追的剧和别名
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import LIB_DIR, SCRIPT_DIR, STATE_DIR  # noqa: E402
import argparse
import json
import os
import re
import sys

sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, LIB_DIR)

import resolve_series as rs          # noqa: E402
import mediaenv                      # noqa: E402

FINAL = STATE_DIR + "/tmdb_final.json"
CANONICAL = STATE_DIR + "/canonical.json"


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


def ol():
    return mediaenv.openlist_login()


def ol_list(base, tok, path):
    import urllib.request
    body = json.dumps({"path": path, "password": "", "page": 1,
                       "per_page": 500, "refresh": True}).encode()
    req = urllib.request.Request(base + "/api/fs/list", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": tok})
    return (json.load(urllib.request.urlopen(req, timeout=40)).get("data") or {}).get("content") or []


def ol_mkdir(base, tok, path):
    import urllib.request
    body = json.dumps({"path": path}).encode()
    req = urllib.request.Request(base + "/api/fs/mkdir", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": tok})
    return json.load(urllib.request.urlopen(req, timeout=30))


def show_detail(tmdb_id):
    return rs.tmdb("/tv/%d" % int(tmdb_id))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", help="剧名（中文/别名都行）")
    ap.add_argument("--year", type=int)
    ap.add_argument("--tmdb-id", type=int)
    ap.add_argument("--dir-name", help="自有目录名（默认用 TMDB 标题）")
    ap.add_argument("--keyword", action="append", default=[], help="额外的匹配关键词（可多次）")
    ap.add_argument("--alias", action="append", default=[], help="额外别名（可多次，防和谐写法）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--candidates", action="store_true", help="查看频道里出现但没收录的新剧")
    args = ap.parse_args()

    final = load(FINAL, {})
    canon = load(CANONICAL, {})

    if args.candidates:
        try:
            items = json.load(open(STATE_DIR + "/new_show_candidates.json")).get("items") or {}
        except Exception:
            items = {}
        if not items:
            print("没有待收录的新剧候选 ✅")
            return 0
        print("频道里出现、但还没收录的剧（%d 部）：" % len(items))
        for k, v in sorted(items.items(), key=lambda kv: kv[1].get("ts") or 0, reverse=True):
            print("  - %s" % (v.get("title") or k))
        print("\n要收录就：python3 add_show.py --name \"剧名\" [--year 2026] --apply（先看计划）")
        return 0

    if args.list:
        print("当前在追 %d 部：" % len(canon))
        for k, v in canon.items():
            print("  %-24s 别名 %d 个：%s" % (k, len(v.get("aliases") or []),
                                              "、".join((v.get("aliases") or [])[:6])))
        return 0

    if not args.name and not args.tmdb_id:
        print("需要 --name 或 --tmdb-id（或 --list）")
        return 2

    # 1) 解析 TMDB
    if args.tmdb_id:
        det = show_detail(args.tmdb_id)
        if not det.get("id"):
            print("❌ TMDB 查不到 id=%s" % args.tmdb_id)
            return 3
        cand = {"kind": "tv", "id": det["id"], "title": det.get("name") or args.name,
                "year": int((det.get("first_air_date") or "0000")[:4] or 0) or None,
                "seasons": [{"n": s["season_number"], "eps": s["episode_count"]}
                            for s in det.get("seasons") or []
                            if s.get("season_number", 0) > 0 and s.get("episode_count")],
                "total_eps": sum(s["episode_count"] for s in det.get("seasons") or []
                                 if s.get("season_number", 0) > 0 and s.get("episode_count")),
                "confidence": 1.0, "evidence": {"source": "manual-tmdb-id"}}
        cands = [cand]
    else:
        cands = rs.resolve(args.name, [], "tv")
    if not cands:
        print("❌ 没有找到匹配的 TMDB 剧集，请用 --tmdb-id 指定")
        return 3
    print("TMDB 候选：")
    for i, c in enumerate(cands, 1):
        print("  %d) %s (%s) id=%s conf=%.2f 季=%s" %
              (i, c["title"], c.get("year"), c["id"], c.get("confidence", 0),
               [(s["n"], s["eps"]) for s in c.get("seasons") or []]))
    top = cands[0]
    if args.year:
        same = [c for c in cands if c.get("year") == args.year]
        if same:
            top = same[0]
    if (top.get("confidence") or 1) < 0.9 and not args.tmdb_id:
        print("⚠️ 置信度偏低，仍可用 --tmdb-id 指定；先不执行")
        return 4

    title = top["title"]
    year = args.year or top.get("year")

    # 复用已有条目：优先按 tmdb_id，其次按规范化标题；再退到夸克里同名目录
    existing_key = None
    for k, v in canon.items():
        if int(v.get("tmdb_id") or 0) == int(top["id"]):
            existing_key = k
            break
    if existing_key is None:
        nt = rs.norm(title)
        for k, v in canon.items():
            if rs.norm(v.get("title") or k) == nt:
                existing_key = k
                break
    base, tok = ol()
    dirs = {x["name"] for x in ol_list(base, tok, "/夸克/自动转存") if x.get("is_dir")}
    if existing_key is None:
        for d in dirs:
            if rs.norm(re.sub(r"\(\s*\d{4}\s*\)", "", d)) == rs.norm(title):
                existing_key = d
                break
    dirname = args.dir_name or existing_key or title
    exists = bool(existing_key)

    aliases = set(canon.get(dirname, {}).get("aliases") or [])
    aliases |= set(rs.title_variants(args.name or title)) | {title, dirname}
    aliases |= set(args.keyword) | set(args.alias)
    aliases = sorted(a for a in aliases if a and len(a) >= 2)

    print("\n计划：")
    print("  规范目录：/自动转存/%s" % dirname)
    print("  TMDB：%s (%s) id=%s，季=%s" %
          (title, year, top["id"], [(s["n"], s["eps"]) for s in top.get("seasons") or []]))
    print("  别名/关键词（%d）：%s" % (len(aliases), "、".join(aliases[:12])))
    if exists:
        print("  状态：已在追更列表（只更新别名/关键词，不新建目录）")
    need_mkdir = dirname not in dirs
    print("  夸克目录：%s" % ("已存在" if not need_mkdir else "不存在，将创建"))

    if not args.apply:
        print("\n[dry-run] 加 --apply 才会真正创建目录/写入配置")
        return 0

    if need_mkdir:
        r = ol_mkdir(base, tok, "/夸克/自动转存/" + dirname)
        print("  建目录：%s %s" % (r.get("code"), r.get("message") or ""))
    final[dirname] = {"kind": "tv", "id": top["id"], "title": title,
                      "year": str(year or ""), "status": "",
                      "seasons": top.get("seasons") or [],
                      "total_eps": top.get("total_eps") or 0,
                      "added_by": "add_show"}
    save(FINAL, final)
    canon[dirname] = {"canonical": dirname, "title": title, "year": year,
                      "tmdb_id": top["id"], "kind": "tv",
                      "seasons": top.get("seasons") or [],
                      "total_eps": top.get("total_eps"),
                      "dirs": [dirname], "aliases": aliases,
                      "added_by": "add_show"}
    save(CANONICAL, canon)
    try:
        os.chmod(CANONICAL, 0o600)
    except OSError:
        pass
    print("\n✅ 已加入：%s" % dirname)
    print("   之后频道发帖标题/关键词命中就会自动按缺集入库；官方追更也会开始跟。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
