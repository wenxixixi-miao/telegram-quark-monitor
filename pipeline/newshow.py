#!/usr/bin/env python3
"""新剧 onboarding（P1）：未配置目录 → TMDB 解析 → 建规范目录 → 写入 tmdb_final/canonical

默认 dry-run，只打印计划；--apply 才真正：
  1) OpenList 在 /夸克/自动转存 下创建 "<title> (<year>)" 目录
  2) 写入 $TQM_STATE/tmdb_final.json（strm 生成器立即识别）
  3) 写入 $TQM_STATE/canonical.json（TG 发现器可命中别名）

原则：低置信度/多电影 pack 一律不动，写 pending_mapping.json 等人工确认。

用法：
  python3 newshow.py --list                 # 列出 unknown + 候选
  python3 newshow.py --name "某剧目录"       # dry-run 单条
  python3 newshow.py --all --apply --sync   # 批量执行（谨慎）
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import LIB_DIR, STATE_DIR  # noqa: E402
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LIB_DIR)
import mediaenv  # noqa: E402
import resolve_series as rs  # noqa: E402

FINAL = STATE_DIR + "/tmdb_final.json"
CANONICAL = STATE_DIR + "/canonical.json"
UNKNOWN = STATE_DIR + "/unknown_dirs.json"
PENDING = STATE_DIR + "/pending_mapping.json"
VID = (".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".rmvb", ".flv", ".wmv", ".mov", ".mpg")


def ol_login():
    return mediaenv.openlist_login()


def ls_token(base, tok, path):
    body = json.dumps({"path": path, "password": "", "page": 1,
                       "per_page": 500, "refresh": False}).encode()
    req = urllib.request.Request(base + "/api/fs/list", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": tok})
    return (json.load(urllib.request.urlopen(req, timeout=40)).get("data") or {}).get("content") or []


def dir_names(base, tok):
    return {x["name"] for x in ls_token(base, tok, "/夸克/自动转存") if x.get("is_dir")}


def file_names(base, tok, dirname):
    out, stack = [], [("/夸克/自动转存/" + dirname, 0)]
    while stack:
        pth, dep = stack.pop()
        try:
            items = ls_token(base, tok, pth)
        except Exception:
            continue
        for it in items:
            if it.get("is_dir") and dep < 2:
                stack.append((pth + "/" + it["name"], dep + 1))
            elif not it.get("is_dir") and it["name"].lower().endswith(VID):
                out.append(it["name"])
    return out


def mkdir(base, tok, path):
    body = json.dumps({"path": path}).encode()
    req = urllib.request.Request(base + "/api/fs/mkdir", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": tok})
    return json.load(urllib.request.urlopen(req, timeout=30))


def plan_one(dirname, base, tok, existing_dirs, final):
    names = file_names(base, tok, dirname)
    cands = rs.resolve(dirname, names)
    out = {"dirname": dirname, "files": len(names), "candidates": cands,
           "action": "none", "reason": ""}
    if not cands:
        out["reason"] = "无 TMDB 候选，需人工"
        return out
    top = cands[0]
    if top.get("evidence", {}).get("pack_guess"):
        out["reason"] = "疑似多电影/合集包，需人工映射"
        return out
    if top["confidence"] < 0.9:
        out["reason"] = f"置信度 {top['confidence']} < 0.9，进待确认"
        return out
    target = f"{top['title']} ({top['year']})"
    out.update({"target_dir": target, "tmdb": top})
    if target in final:
        out["action"] = "already_mapped"
        out["reason"] = "tmdb_final 已有该目录映射"
        return out
    if target in existing_dirs:
        out["action"] = "add_mapping"
        out["reason"] = "目录已存在，只需登记 tmdb_final/canonical"
    else:
        out["action"] = "mkdir_and_map"
        out["reason"] = "需要创建规范目录并登记"
    return out


def apply_one(plan, base, tok):
    top = plan["tmdb"]
    target = plan["target_dir"]
    # 1) 建目录
    if plan["action"] == "mkdir_and_map":
        r = mkdir(base, tok, "/夸克/自动转存/" + target)
        print(f"   mkdir: {r.get('code')} {r.get('message')}")
    # 2) tmdb_final
    try:
        final = json.load(open(FINAL))
    except Exception:
        final = {}
    final[target] = {"kind": top["kind"], "id": top["id"], "title": top["title"],
                     "year": str(top["year"]), "status": "",
                     "seasons": top.get("seasons") or [],
                     "total_eps": top.get("total_eps") or 0,
                     "added_by": "newshow"}
    tmp = FINAL + ".tmp"
    json.dump(final, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, FINAL)
    # 3) canonical
    try:
        canon = json.load(open(CANONICAL))
    except Exception:
        canon = {}
    aliases = set(canon.get(target, {}).get("aliases") or [])
    aliases |= set(rs.title_variants(plan["dirname"])) | {top["title"], target}
    canon[target] = {"canonical": target, "title": top["title"], "year": top["year"],
                     "tmdb_id": top["id"], "kind": top["kind"],
                     "seasons": top.get("seasons") or [],
                     "total_eps": top.get("total_eps"), "dirs": [target],
                     "aliases": sorted(a for a in aliases if a and len(a) >= 2),
                     "added_by": "newshow"}
    tmp = CANONICAL + ".tmp"
    json.dump(canon, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, CANONICAL)
    os.chmod(CANONICAL, 0o600)
    print(f"   已登记 tmdb_final + canonical：{target}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--name")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    base, tok = ol_login()
    existing = dir_names(base, tok)
    try:
        final = json.load(open(FINAL))
    except Exception:
        final = {}

    if args.name:
        names = [args.name]
    elif args.all or args.list:
        try:
            names = json.load(open(UNKNOWN)).get("unknown") or []
        except Exception:
            names = []
    else:
        print("需要 --name / --list / --all")
        return 2

    plans = []
    for d in names:
        p = plan_one(d, base, tok, existing, final)
        plans.append(p)
        print(f"【{d}】files={p['files']} action={p['action']}  {p['reason']}")
        for c in p["candidates"][:3]:
            ev = c.get("evidence", {})
            print(f"   {c['kind']:5s} {c['id']:8d} {c['title']} ({c['year']}) "
                  f"conf={c['confidence']} pack={ev.get('pack_guess', False)}")

    if args.list:
        todo = [p for p in plans if p["action"] in ("mkdir_and_map", "add_mapping")]
        pending = [p for p in plans if p["action"] == "none"]
        print(f"\n可自动入库 {len(todo)}，需人工 {len(pending)}")
        if pending:
            json.dump({p["dirname"]: p["candidates"] for p in pending},
                      open(PENDING, "w"), ensure_ascii=False, indent=1)
            print(f"已写 {PENDING}")
        return 0

    if not args.apply:
        print("\n[dry-run] 加 --apply 执行")
        return 0

    done = 0
    for p in plans:
        if p["action"] in ("mkdir_and_map", "add_mapping"):
            print(f"→ 执行 {p['dirname']} → {p['target_dir']}")
            if apply_one(p, base, tok):
                done += 1
    print(f"\n完成 {done} 个")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"❌ newshow 异常：{e}")
        sys.exit(1)
