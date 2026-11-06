#!/usr/bin/env python3
"""构建 canonical.json：剧名 → TMDB 元数据 + 别名（P1）

数据来源：
  - $TQM_STATE/tmdb_final.json（人工核准的 TMDB 映射，20 条）
  - /夸克/自动转存 顶层目录（OpenList 只读）
  - 人工整理的历史别名（TG 频道标题里出现过的乱码/谐音名，来自旧 monitor.log 统计）
输出：$TQM_STATE/canonical.json（600，不进 git 也可）

用法：python3 build_canonical.py [--dry]
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import LIB_DIR, STATE_DIR  # noqa: E402
import json
import os
import sys

sys.path.insert(0, LIB_DIR)
import mediaenv  # noqa: E402
import urllib.request  # noqa: E402

FINAL = STATE_DIR + "/tmdb_final.json"
OUT = STATE_DIR + "/canonical.json"
DRY = "--dry" in sys.argv

# 人工别名：历史 TG 标题/目录里出现过的写法（会做子串匹配，长别名优先）
# 人工别名表：剧名 → 别名列表（TG 频道标题里出现过的各种写法，长别名优先）
# 放在 $TQM_STATE/aliases.json（格式见 aliases.example.json），不随代码发布
ALIASES_FILE = os.environ.get("TQM_ALIASES") or (STATE_DIR + "/aliases.json")


def load_aliases():
    try:
        with open(ALIASES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(v, list) and v}
    except FileNotFoundError:
        print(f"提示：{ALIASES_FILE} 不存在，别名表按空处理"
              f"（可从 aliases.example.json 复制一份开始）")
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 别名表读取失败：{e}")
        return {}


ALIASES = load_aliases()


def openlist_dirs():
    base, tok = mediaenv.openlist_login()
    body = json.dumps({"path": "/夸克/自动转存", "password": "", "page": 1,
                       "per_page": 500, "refresh": False}).encode()
    req = urllib.request.Request(base + "/api/fs/list", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": tok})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    return [x["name"] for x in ((d.get("data") or {}).get("content") or []) if x.get("is_dir")]


def main():
    final = json.load(open(FINAL))
    dirs = []
    try:
        dirs = openlist_dirs()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ OpenList 列举失败（将只用 tmdb_final）：{e}")

    canonical = {}
    for key, meta in final.items():
        title = meta.get("title") or key
        name = key
        aliases = set(ALIASES.get(key, [])) | {key, title}
        if meta.get("kind") == "tv":
            aliases |= {title.replace("：", ":"), title.split("·")[0]}
        canonical[name] = {
            "canonical": name,
            "title": title,
            "year": meta.get("year"),
            "tmdb_id": meta.get("id"),
            "kind": meta.get("kind"),
            "seasons": meta.get("seasons") or [],
            "total_eps": meta.get("total_eps"),
            "dirs": sorted({d for d in dirs if d == key or key in d or d in key}) or [key],
            "aliases": sorted(a for a in aliases if a and len(a) >= 2),
            "added_by": "build_canonical",
        }
    print(f"canonical 条目：{len(canonical)}（tmdb_final {len(final)}，目录 {len(dirs)}）")
    for k in list(canonical)[:6]:
        print("  ", k, "→", canonical[k]["title"], canonical[k]["year"],
              "别名", len(canonical[k]["aliases"]))
    if DRY:
        return 0
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(canonical, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    os.chmod(OUT, 0o600)
    print(f"✅ 写入 {OUT}（600）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
