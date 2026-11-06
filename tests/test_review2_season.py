#!/usr/bin/env python3
"""P0-C 回归：跨季 E01 不再被当成已有旧集；follow 历史集按季比较"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import SCRIPT_DIR  # noqa: E402
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)
import intake  # noqa: E402

CANON = "测试剧-跨季"
canon = {CANON: {"canonical": CANON, "kind": "tv", "tmdb_id": 1, "aliases": [CANON]}}


class FakeDir:
    def __init__(self, items):
        self.items = items
        self.error = None

    def fid_for(self, name):
        return ("dir-fid", name)

    def list_items(self, fid):
        return self.items


def _plan(existing, share_files, mode="follow"):
    intake.MODE = mode
    intake.playing_items = lambda: set()
    intake.share_walk = lambda url, *a, **k: share_files
    real = intake.load_json

    def load(path, default):
        if path in (intake.UPGRADES, intake.RESOLUTIONS):
            return {}
        return real(path, default)
    intake.load_json = load
    mod = types.ModuleType("llm_recognize")
    mod.map_episodes = lambda canon_name, names: {"mapping": {}, "cached": True}
    sys.modules["llm_recognize"] = mod
    canon_name = CANON
    return intake._plan_for("https://pan.quark.cn/s/season-test", canon_name, canon,
                            FakeDir(existing), {}, "")


def _names(plan):
    return [f["name"] for f in plan.get("missing") or []]


def test_new_season_not_skipped():
    """已有 S01E01 + 新 S02E01 → 不能跳过"""
    plan = _plan([{"name": "S01E01.1080p.mkv", "size": 1}],
                 [{"name": "S02E01.1080p.mkv", "fid": "f1", "size": 1}])
    print("S01E01 已有 + S02E01 新 → missing:", _names(plan),
          "skipped:", plan.get("historical_skipped"), "default_season:", plan.get("default_season"))
    assert "S02E01.1080p.mkv" in _names(plan), "新一季 E01 被当成已有旧集"


def test_same_episode_upgrade():
    """已有 S01E01 + 新 S01E01 更优 → 升级，而不是跳过"""
    plan = _plan([{"name": "S01E01.1080p.WEB-DL.mkv", "size": 1 * 1024 ** 3}],
                 [{"name": "S01E01.2160p.mkv", "fid": "f2", "size": 3 * 1024 ** 3}])
    print("同季同集更优 → upgrades:", plan.get("upgrades"), "missing:", _names(plan))
    assert plan.get("upgrades"), "更优版本应进入升级"
    assert "S01E01.2160p.mkv" in _names(plan)


def test_season_6_after_217():
    """斗破样例：已有 S05E217 + 新 S06E01 → 不跳过"""
    plan = _plan([{"name": "S05E217.2160p.mkv", "size": 1}],
                 [{"name": "S06E01.2160p.mkv", "fid": "f3", "size": 1}])
    print("S05E217 已有 + S06E01 新 → missing:", _names(plan),
          "max_by_season:", plan.get("max_by_season"))
    assert "S06E01.2160p.mkv" in _names(plan)
    assert plan.get("max_by_season") == {5: 217}


def test_no_season_in_multi_season_dir():
    """无季号 01.mkv 在多季目录 → 按最大季推断（明确行为：S05E01，属历史集跳过）"""
    plan = _plan([{"name": "S01E01.1080p.mkv", "size": 1},
                  {"name": "S05E217.1080p.mkv", "size": 1}],
                 [{"name": "01.mkv", "fid": "f4", "size": 1}])
    print("无季号 01.mkv 多季目录 → default_season:", plan.get("default_season"),
          "missing:", _names(plan), "historical_skipped:", plan.get("historical_skipped"))
    assert plan.get("default_season") == 5, "多季目录默认季应为最大季"
    assert plan.get("max_by_season") == {1: 1, 5: 217}
    assert _names(plan) == [] and plan.get("historical_skipped") == 1


def test_no_season_unique_season_dir():
    """无季号 06.mkv 在唯一季（S03）目录 → 归入 S03，正常入库"""
    plan = _plan([{"name": "S03E05.1080p.mkv", "size": 1}],
                 [{"name": "06.mkv", "fid": "f5", "size": 1}])
    print("无季号 06.mkv 唯一季目录 → default_season:", plan.get("default_season"),
          "missing:", _names(plan))
    assert plan.get("default_season") == 3
    assert "06.mkv" in _names(plan)


def test_no_season_backfill_multi():
    """backfill 模式下无季号文件仍按默认季补缺"""
    plan = _plan([{"name": "S01E01.1080p.mkv", "size": 1},
                  {"name": "S05E217.1080p.mkv", "size": 1}],
                 [{"name": "06.mkv", "fid": "f6", "size": 1}], mode="backfill")
    print("backfill 多季 → default_season:", plan.get("default_season"), "missing:", _names(plan))
    assert plan.get("default_season") == 5 and "06.mkv" in _names(plan)


if __name__ == "__main__":
    test_new_season_not_skipped()
    test_same_episode_upgrade()
    test_season_6_after_217()
    test_no_season_in_multi_season_dir()
    test_no_season_unique_season_dir()
    test_no_season_backfill_multi()
    print("OK: P0-C 跨季判定（(season, ep) 键 + 无季号推断）")
