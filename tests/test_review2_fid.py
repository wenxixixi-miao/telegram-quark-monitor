#!/usr/bin/env python3
"""P0-0 回归：标题兜底/人工确认的文件必须带 fid，do_save 不得抛 KeyError"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import SCRIPT_DIR  # noqa: E402
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)
import intake  # noqa: E402

CANON = "测试剧-P0"
canon = {CANON: {"canonical": CANON, "kind": "tv", "tmdb_id": 1, "aliases": [CANON]}}


class FakeDir:
    def __init__(self, items=None):
        self.items = items or []

    def fid_for(self, name):
        return ("dir-fid", name)

    def list_items(self, fid):
        return self.items


def _fake_llm(mapping=None):
    mod = types.ModuleType("llm_recognize")
    mod.map_episodes = lambda canon_name, names: {"mapping": mapping or {}, "cached": True}
    sys.modules["llm_recognize"] = mod


def _no_state_upgrades():
    real = intake.load_json

    def load(path, default):
        if path in (intake.UPGRADES, intake.RESOLUTIONS):
            return {}
        return real(path, default)
    intake.load_json = load


def test_title_fallback_keeps_fid():
    """1 个认不出的文件 + 标题更新至164集 → plan.missing[0] 必须带 fid"""
    _fake_llm()
    _no_state_upgrades()
    intake.playing_items = lambda: set()
    intake.share_walk = lambda url, *a, **k: [
        {"name": "MYSTERY.mkv", "fid": "F1", "token": "T1", "size": 123, "path": "MYSTERY.mkv"}]
    plan = intake._plan_for("https://pan.quark.cn/s/fid-test", CANON, canon,
                            FakeDir(), {}, "某剧 更新至164集 4K")
    print("title_hint_used:", plan.get("title_hint_used"))
    print("missing:", plan.get("missing"))
    assert plan.get("title_hint_used"), "标题兜底没有生效"
    assert len(plan["missing"]) == 1, plan["missing"]
    f = plan["missing"][0]
    assert f.get("fid") == "F1", "标题兜底丢了 fid：%r" % f
    assert f.get("size") == 123, "标题兜底丢了 size：%r" % f
    assert f.get("token") == "T1", "标题兜底丢了 token：%r" % f


def test_manual_resolution_keeps_fid():
    """人工确认（episode_resolutions）同样必须复用原始 file 对象"""
    _fake_llm()
    _no_state_upgrades()
    intake.playing_items = lambda: set()
    intake.share_walk = lambda url, *a, **k: [
        {"name": "MYSTERY.mkv", "fid": "F2", "token": "T2", "size": 456, "path": "MYSTERY.mkv"}]
    url = "https://pan.quark.cn/s/manual-test"
    real = intake.load_json

    def load(path, default):
        if path == intake.RESOLUTIONS:
            return {"%s|%s|%s" % (CANON, url, "MYSTERY.mkv"): {"episode": 164}}
        if path == intake.UPGRADES:
            return {}
        return real(path, default)
    intake.load_json = load
    plan = intake._plan_for(url, CANON, canon, FakeDir(), {}, "")
    print("manual_fixed:", plan.get("manual_fixed"), "missing:", plan.get("missing"))
    assert plan.get("manual_fixed")
    assert plan["missing"][0].get("fid") == "F2", plan["missing"]


def test_do_save_no_keyerror():
    """do_save: 缺 fid 返回明确错误；正常路径调用 CLI 不抛 KeyError"""
    called = {}

    def fake_cli(*args, **kwargs):
        called["args"] = args
        return [{"type": "result", "code": 0, "data": {"save_path": "/夸克/测试"}}]

    intake.cli = fake_cli
    ok, msg = intake.do_save({"share_url": "u", "dir_fid": "d",
                              "missing": [{"name": "MYSTERY.mkv", "fid": "F1", "size": 1}]})
    assert ok is True, msg
    assert "F1" in called["args"], called
    ok, msg = intake.do_save({"share_url": "u", "dir_fid": "d",
                              "missing": [{"name": "MYSTERY.mkv", "size": 0}]})
    print("缺 fid →", ok, msg)
    assert ok is False and "missing fid" in msg, (ok, msg)


if __name__ == "__main__":
    test_title_fallback_keeps_fid()
    test_manual_resolution_keeps_fid()
    test_do_save_no_keyerror()
    print("OK: P0-0 标题兜底/人工确认 保留 fid；do_save 无 KeyError")
