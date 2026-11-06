#!/usr/bin/env python3
"""离线单元测试：同集多源选优（不碰线上状态）"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import SCRIPT_DIR  # noqa: E402
import sys
sys.path.insert(0, SCRIPT_DIR)
import intake

cases = [
    {"_canon": "测试剧", "missing": [{"name": "EP100.1080p.WEB-DL.mkv", "size": int(1.2 * 1024 ** 3)}]},
    {"_canon": "测试剧", "missing": [{"name": "EP100.2160p.HDR.10bit.REMUX.mkv", "size": int(2.5 * 1024 ** 3)}]},
    {"_canon": "测试剧", "missing": [{"name": "EP100.1080p.HDR.mkv", "size": int(1.5 * 1024 ** 3)}]},
]
intake.select_sources(cases)
for i, p in enumerate(cases, 1):
    print("源%d 保留=%s 跳过=%s" % (i, [f["name"] for f in p["missing"]], p.get("_dropped_sources")))
print("quality:", [intake.quality_score(x) for x in
                   [{"name": "EP100.1080p.WEB-DL.mkv", "size": 1},
                    {"name": "EP100.2160p.HDR.10bit.REMUX.mkv", "size": 1},
                    {"name": "EP100.1080p.HDR.mkv", "size": 1}]])
assert len(cases[0]["missing"]) == 0 and len(cases[1]["missing"]) == 1 and len(cases[2]["missing"]) == 0
assert cases[2].get("_dropped_sources")
print("OK: 4K 胜出，1080P 被跳过")
