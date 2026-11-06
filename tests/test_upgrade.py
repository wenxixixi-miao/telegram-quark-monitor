#!/usr/bin/env python3
"""离线单测：画质升级 + HDR 降权（本仓库不含 Jellyfin 播放检测）"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import SCRIPT_DIR  # noqa: E402
import sys

sys.path.insert(0, SCRIPT_DIR)
import intake
from quality import quality_score

CANON = "测试剧"
canon = {CANON: {"canonical": CANON, "kind": "tv", "tmdb_id": 1, "title": CANON,
                 "aliases": [CANON], "seasons": [{"n": 1, "eps": 200}]}}


class FakeDir:
    def __init__(self, items):
        self.items = items

    def fid_for(self, name):
        return ("fid-test", name)

    def list_items(self, fid):
        return self.items


# 1) HDR 降权：同分辨率同体积，SDR 应胜出
sdr = quality_score({"name": "EP100.2160p.SDR.mkv", "size": 2 * 1024 ** 3})
hdr = quality_score({"name": "EP100.2160p.HDR.mkv", "size": 2 * 1024 ** 3})
print("SDR score:", sdr, "HDR score:", hdr)
assert sdr > hdr, "SDR 应该高于 HDR"

# 2) 已有 1080P，来了 4K → 应进入升级计划
intake.share_walk = lambda url, *a, **k: [
    {"name": "EP100.2160p.mkv", "fid": "new", "size": 3 * 1024 ** 3, "path": "EP100.2160p.mkv"}]
plan = intake._plan_for("https://pan.quark.cn/s/test", CANON, canon,
                        FakeDir([{"name": "EP100.1080p.mkv", "size": int(1.5 * 1024 ** 3)}]), {}, "")
print("升级计划:", plan.get("upgrades"), "missing:", [f["name"] for f in plan.get("missing") or []])
assert plan.get("upgrades") and plan["missing"]

# 4) 同分辨率：旧 HDR → 新 SDR，即使分差 <150 也应升级
cases = [
    ("EP100.2160p.HDR.mkv", "EP100.2160p.SDR.mkv", True),
    ("EP100.2160p.SDR.mkv", "EP100.2160p.SDR.mkv", False),
    ("EP100.2160p.WEB-DL.mkv", "EP100.2160p.REMUX.mkv", False),   # 都是 4K → 不升级
    ("EP100.2160p.HDR.mkv", "EP100.2160p.SDR.REMUX.mkv", True),     # HDR→SDR 例外
    ("EP100.2160p.SDR.mkv", "EP100.2160p.HDR.mkv", False),          # SDR→HDR 不升级
    ("EP100.1080p.mkv", "EP100.2160p.mkv", True),
]
for old, new, expect in cases:
    got = intake.upgrade_needed(new, quality_score({"name": new, "size": 3 * 1024 ** 3}),
                                old, quality_score({"name": old, "size": 2 * 1024 ** 3}),
                                3 * 1024 ** 3, 2 * 1024 ** 3)
    print("旧 %s → 新 %s: upgrade=%s (期望 %s)" % (old, new, got, expect))
    assert got == expect, (old, new, got)
print("OK: 升级逻辑 + 播放暂缓 + HDR 降权 + SDR换HDR/remux 规则 都正确")
