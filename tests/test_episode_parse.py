#!/usr/bin/env python3
"""测试通用集号解析器：离线断言 + （有环境时）真实夸克目录覆盖率（只读）"""

import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _sub in ("lib", "pipeline"):
    _p = _os.path.join(_ROOT, _sub)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import LIB_DIR  # noqa: E402
import json
import os
import sys
import urllib.request

sys.path.insert(0, LIB_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from episode_parse import ep_key, parse_episode  # noqa: E402

VID = (".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".rmvb", ".flv", ".wmv", ".mov", ".mpg")
ROOT = "/夸克/自动转存"


def check_offline():
    """C-03：EP/第N集/SxxExx 优先于"末尾数字"；fps/声道/版本/分段不当集号"""
    cases = [
        ("第28集 Part 2.mp4", 28, None),
        ("第28集.Part.2.60fps.mkv", 28, None),
        ("EP28.mp4", 28, None),
        ("第28集.mp4", 28, None),
        ("S01E02.mkv", 2, 1),
        ("S02E01.2160p.WEB-DL.mkv", 1, 2),
        ("示例剧 年番 第217集 4K.mp4", 217, None),
        ("EP05.1080p.x265.mkv", 5, None),
        ("1！8！3.mp4", 183, None),            # 拆字拼接仍要能认
        ("示例剧 (2025) 190 4K.mp4", 190, None),
        # 重复副本后缀 (1)/(2)/（1）不得被当成集号（示例剧 E01 错映射根因）
        ("283 4K(1).mp4", 283, None),
        ("283 4K（1）.mp4", 283, None),
        ("S01E01(1).mkv", 1, 1),
        ("第28集(1).mp4", 28, None),
        ("283 4K(2).mp4", 283, None),
    ]
    for name, ep, season in cases:
        got = parse_episode(name)
        assert got["episode"] == ep and got["season"] == season, (name, got)
    rejected = ["60fps.mkv", "movie.5.1.mkv", "show.v2.mkv", "sample.mkv", "海报.jpg"]
    for name in rejected:
        got = parse_episode(name)
        assert got["episode"] is None, (name, got)
        assert ep_key(name) is None, (name, ep_key(name))
    print("离线断言：C-03 解析优先级 ✓（%d 正例 / %d 反例）" % (len(cases), len(rejected)))


def login():
    import mediaenv
    base, tok = mediaenv.openlist_login()
    return base or "http://127.0.0.1:5244", tok


def ls(base, tok, path):
    body = json.dumps({"path": path, "password": "", "page": 1,
                       "per_page": 500, "refresh": False}).encode()
    req = urllib.request.Request(base + "/api/fs/list", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": tok})
    d = json.load(urllib.request.urlopen(req, timeout=40))
    return (d.get("data") or {}).get("content") or []


def main():
    base, tok = login()
    dirs = [x["name"] for x in ls(base, tok, ROOT) if x.get("is_dir")]
    total = parsed = low = sp = 0
    unrec = []
    print(f"目录 {len(dirs)} 个")
    for d in dirs:
        files, stack = [], [(ROOT + "/" + d, 0)]
        while stack:
            path, depth = stack.pop()
            try:
                items = ls(base, tok, path)
            except Exception:
                continue
            for it in items:
                if it.get("is_dir") and depth < 2:
                    stack.append((path + "/" + it["name"], depth + 1))
                elif not it.get("is_dir") and it["name"].lower().endswith(VID):
                    files.append(it["name"])
        if not files:
            continue
        n = len(files)
        ok = lowc = spc = 0
        for f in files:
            p = parse_episode(f)
            if p.get("special"):
                spc += 1
                continue
            if p.get("episode"):
                ok += 1
                if p["confidence"] < 0.7:
                    lowc += 1
            else:
                unrec.append((d, f))
        total += n
        parsed += ok
        low += lowc
        sp += spc
        print(f"  {d[:24]:26s} 文件 {n:4d} 可解析 {ok:4d} ({ok*100//max(1,n):3d}%) "
              f"低置信 {lowc:2d} special {spc:2d} 认不出 {n-ok-spc:2d}")
    print(f"\n总计：文件 {total}，可解析 {parsed} ({parsed*100//max(1,total)}%)，"
          f"低置信 {low}，special {sp}，认不出 {len(unrec)}")
    for d, f in unrec[:15]:
        print("  未识别:", d, "|", f[:80])


if __name__ == "__main__":
    check_offline()
    if "--offline" in sys.argv:
        print("跳过线上扫描（--offline）")
        sys.exit(0)
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"跳过线上扫描（mediaenv/OpenList 不可用：{type(e).__name__}: {e}）")
    sys.exit(0)
