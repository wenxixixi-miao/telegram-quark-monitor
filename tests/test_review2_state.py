#!/usr/bin/env python3
"""P0-A 回归：读失败/保存失败不丢事件、offset 只在成功/终态后提交、wrapper 不吞告警

全部离线：mock CLI/share_walk，不写任何线上 state。"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import SCRIPT_DIR  # noqa: E402
import os
import subprocess
import sys
import tempfile
import json
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)
import intake  # noqa: E402

CANON = "测试剧-状态"
canon = {CANON: {"canonical": CANON, "kind": "tv", "tmdb_id": 1, "aliases": [CANON]}}


class FakeDir:
    def __init__(self, items=None, dir_error=None):
        self.items = items or []
        self.error = None
        self.read_error = None
        self.read_errors = []
        self.dir_error = dir_error
        self.auto_fid = "auto-fid"
        self.dirs = {"测试剧A": "d", "测试剧B": "d", "测试剧P": "d"}

    def fid_for(self, name):
        return ("dir-fid", name)

    def list_items(self, fid):
        if self.dir_error:
            self.read_errors.append(self.dir_error)
        return self.items


def _patch_plan_env():
    intake.playing_items = lambda: set()
    intake.time.sleep = lambda *a, **k: None
    real = intake.load_json

    def load(path, default):
        if path in (intake.UPGRADES, intake.RESOLUTIONS):
            return {}
        return real(path, default)
    intake.load_json = load
    mod = types.ModuleType("llm_recognize")
    mod.map_episodes = lambda canon_name, names: {"mapping": {}, "cached": True}
    sys.modules["llm_recognize"] = mod


# ── 1) cli() 超时可识别（不再返回 [] 冒充"空分享"） ──
def test_cli_timeout_sentinel():
    real_run = intake.subprocess.run

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="node", timeout=90)
    intake.subprocess.run = boom
    try:
        try:
            intake.cli("share-detail", "--url", "x")
        except intake.CliTimeout:
            print("cli 超时 → CliTimeout ✓")
        else:
            raise AssertionError("超时未被识别为 CliTimeout")
    finally:
        intake.subprocess.run = real_run


# ── 2) share_walk 区分「读失败」与「确认失效」 ──
def test_share_walk_classification():
    intake.SHARE_ERR.clear()
    intake.cli = lambda *a, **k: (_ for _ in ()).throw(intake.CliTimeout("超时"))
    try:
        intake.share_walk("u1")
        raise AssertionError("超时应当抛 ShareReadError")
    except intake.ShareReadError as e:
        print("超时 → ShareReadError ✓", e)

    intake.cli = lambda *a, **k: [{"type": "result", "code": 41011, "msg": "分享不存在"}]
    try:
        intake.share_walk("u2")
        raise AssertionError("41011 应当抛 ShareExpired")
    except intake.ShareExpired:
        print("41011 → ShareExpired ✓")

    intake.cli = lambda *a, **k: [{"type": "result", "code": 31001, "msg": "请求过于频繁"}]
    try:
        intake.share_walk("u3")
        raise AssertionError("限流应当抛 ShareReadError（不是失效）")
    except intake.ShareReadError as e:
        print("限流/未知码 → ShareReadError ✓", e)

    intake.cli = lambda *a, **k: [{"type": "result", "code": 0, "data": {"files": [
        {"filename": "EP01.mp4", "fid": "f1", "size": 10, "file_type": "1"}]}}]
    files = intake.share_walk("u4")
    assert len(files) == 1 and files[0]["fid"] == "f1"
    print("正常 → 文件列表 ✓")


# ── 3) _plan_for：读失败不标 expired、ready=False、重试一次 ──
def test_plan_read_failed():
    _patch_plan_env()
    calls = {"n": 0}

    def fail(url, *a, **k):
        calls["n"] += 1
        raise intake.ShareReadError("cli share-detail 超时")
    intake.share_walk = fail
    plan = intake._plan_for("https://pan.quark.cn/s/timeout", CANON, canon, FakeDir(), {}, "")
    print("读超时 plan:", {k: plan.get(k) for k in ("read_failed", "expired", "ready", "reason")})
    assert plan.get("read_failed") is True
    assert not plan.get("expired"), "读超时不能被标成失效"
    assert plan.get("ready") is False
    assert calls["n"] == 2, "读失败应重试一次（实际 %d 次）" % calls["n"]


def test_plan_dir_read_failed():
    """目标目录读不全 → 不能当成"全缺集"盲目存，按读失败重试"""
    _patch_plan_env()
    intake.share_walk = lambda url, *a, **k: [
        {"name": "EP01.mkv", "fid": "f1", "size": 1}]
    d = FakeDir(items=[{"name": "EP01.mkv", "size": 1}], dir_error="browse 超时")
    plan = intake._plan_for("https://pan.quark.cn/s/dirfail", CANON, canon, d, {}, "")
    print("目录读失败 plan:", {k: plan.get(k) for k in ("read_failed", "ready", "reason")})
    assert plan.get("read_failed") is True and not plan.get("expired")
    assert not plan.get("missing"), "目录没读全时不能规划保存"


def test_plan_expired():
    _patch_plan_env()

    def gone(url, *a, **k):
        raise intake.ShareExpired(41011, "分享不存在")
    intake.share_walk = gone
    plan = intake._plan_for("https://pan.quark.cn/s/gone", CANON, canon, FakeDir(), {}, "")
    print("失效 plan:", {k: plan.get(k) for k in ("read_failed", "expired", "ready", "share_error")})
    assert plan.get("expired") is True
    assert not plan.get("read_failed")
    assert plan.get("ready") is False


# ── 4) 保存失败：do_save 不抛异常、返回 False ──
def test_do_save_failure():
    intake.cli = lambda *a, **k: (_ for _ in ()).throw(intake.CliTimeout("超时"))
    ok, msg = intake.do_save({"share_url": "u", "dir_fid": "d",
                              "missing": [{"name": "EP01.mkv", "fid": "f1"}]})
    print("保存超时 →", ok, msg)
    assert ok is False and ("失败" in msg or "超时" in msg)

    intake.cli = lambda *a, **k: [{"type": "result", "code": 23018, "msg": "容量不足"}]
    ok, msg = intake.do_save({"share_url": "u", "dir_fid": "d",
                              "missing": [{"name": "EP01.mkv", "fid": "f1"}]})
    print("保存业务失败 →", ok, msg)
    assert ok is False


# ── 5) offset 只推进到第一个失败之前 ──
def test_offset_semantics():
    ends = [100, 200, 300]
    assert intake.compute_new_offset(ends, ["failed", "ok", "ok"], 0) == 0, "首个失败 → offset 不动"
    assert intake.compute_new_offset(ends, ["ok", "failed", "ok"], 0) == 100, "只提交失败前"
    assert intake.compute_new_offset(ends, ["ok", "ok", "failed"], 0) == 200
    assert intake.compute_new_offset(ends, ["ok", "ok", "ok"], 0, 350) == 350, "全成功 → 推进到文件末尾"
    assert intake.compute_new_offset(ends, ["ok", "ok"], 0, None) == 200, "截断轮次只提交已处理部分"
    assert intake.compute_new_offset([], [], 77, 99) == 77, "无任务不动"
    print("offset 语义 ✓")


# ── 6) 连续失败 5 次 → 终态、offset 放行、告警一次 ──
def test_failure_counter():
    stats = {}
    key = "u|测试剧"
    for i in range(1, 5):
        cnt, terminal = intake.bump_failure(stats, key, "read timeout")
        assert (cnt, terminal) == (i, False), (cnt, terminal)
    cnt, terminal = intake.bump_failure(stats, key, "read timeout")
    print("第 5 次失败：count=%d terminal=%s" % (cnt, terminal))
    assert cnt == 5 and terminal is True
    assert key in stats["terminal_events"], "终态应持久化"
    assert key not in stats["failed_events"], "终态后清掉计数器"
    # 终态后放行 offset（主循环把这轮状态记为 ok）
    assert intake.compute_new_offset([100], ["ok"], 0, 100) == 100
    # 再失败从 1 开始计数 → 不会重复告警
    cnt2, terminal2 = intake.bump_failure(stats, key, "again")
    assert (cnt2, terminal2) == (1, False)
    # 成功则清零
    intake.clear_failure(stats, key)
    assert key not in stats["failed_events"]
    print("失败计数/终态/告警一次 ✓")


# ── 7) wrapper：严格 0/0 才静默，任何告警都输出 ──
def _run_wrapper(stub_out, rc=0, note=""):
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "p1_intake.sh")).read()
    tmp = tempfile.mkdtemp(prefix="intake-wrapper-")
    stub = os.path.join(tmp, "stubpy")
    with open(stub, "w") as f:
        f.write("#!/bin/sh\ncase \"$*\" in\n"
                "  *intake.py*) printf '%s\\n' \"$STUB_OUT\"; exit \"${STUB_RC:-0}\";;\n"
                "  *pending_apply.py*) [ -n \"$STUB_NOTE\" ] && printf '%s\\n' \"$STUB_NOTE\"; exit 0;;\n"
                "esac\nexit 0\n")
    os.chmod(stub, 0o755)
    script = os.path.join(tmp, "p1_intake.sh")
    body = src.replace("LOCK=$TQM_STATE/p1_intake.lock", "LOCK=%s/lock" % tmp)
    body = body.replace('exec 9>"$LOCK"\nflock -n 9 || exit 0', ":")
    body = body.replace("timeout 280 ", "").replace("timeout 60 ", "")
    body = body.replace("$TQM_ROOT/python3", stub)
    with open(script, "w") as f:
        f.write(body)
    env = dict(os.environ, STUB_OUT=stub_out, STUB_RC=str(rc), STUB_NOTE=note)
    p = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def test_wrapper_quiet():
    base = ("目录索引：自动转存 fid=ok，剧目录 3 个\n"
            "事件消费：从 offset 0，本轮读取 0 个\n"
            "待处理 0 个分享（会写网盘！）\n\n"
            "本轮：计划 0 集，实际存 0 集")
    rc, out = _run_wrapper(base, 0)
    print("正常 0/0 → rc=%s 输出=%r" % (rc, out.strip()[:80]))
    assert rc == 0 and out.strip() == "", "严格 0/0 必须静默"

    for warn in ("⚠️ 失败（连续 1/5 次），offset 不推进，下轮重试",
                 "❌ 保存失败：23018 容量不足",
                 "⏭️ 链接已失效（41011 分享不存在）",
                 "分享内容读取失败（超时）"):
        rc, out = _run_wrapper(base + "\n" + warn, 0)
        assert out.strip(), "含告警的 0/0 不能被吞：%r" % warn
        print("含告警 → 有输出 ✓", warn[:20])

    rc, out = _run_wrapper(base, 2)
    assert rc == 2 and out.strip(), "非零退出必须输出"
    rc, out = _run_wrapper(base, 0, note="📋 有 1 个待确认")
    assert out.strip(), "有待确认通知必须输出"
    print("wrapper 静默/输出规则 ✓")


# ── 8) 端到端：main() 消费事件，读失败不推进 offset，下一轮重试成功 ──
def test_main_end_to_end():
    _patch_plan_env()
    tmp = tempfile.mkdtemp(prefix="intake-e2e-")
    ev = os.path.join(tmp, "events.jsonl")
    canon = {"测试剧A": {"canonical": "测试剧A", "aliases": ["测试剧A"]},
             "测试剧B": {"canonical": "测试剧B", "aliases": ["测试剧B"]}}
    paths = {}
    for name in ("canonical", "registry", "stats", "upgrades", "pending", "resolutions"):
        paths[name] = os.path.join(tmp, name + ".json")
    with open(paths["canonical"], "w") as f:
        json.dump(canon, f, ensure_ascii=False)

    def ev_line(url, name):
        return json.dumps({"type": "new_share", "share_url": url, "canonical": name,
                           "title": ""}, ensure_ascii=False) + "\n"
    with open(ev, "w") as f:
        f.write(ev_line("https://pan.quark.cn/s/A", "测试剧A"))
        f.flush()
        end_a = f.tell()
        f.write(ev_line("https://pan.quark.cn/s/B", "测试剧B"))

    saved = []

    def fake_save(plan):
        saved.append(plan["share_url"])
        return True, "/夸克/测试"

    intake.do_save = fake_save
    intake.DirIndex = lambda c: FakeDir()
    intake.CANONICAL, intake.REGISTRY = paths["canonical"], paths["registry"]
    intake.STATS, intake.UPGRADES = paths["stats"], paths["upgrades"]
    intake.PENDING, intake.RESOLUTIONS = paths["pending"], paths["resolutions"]
    intake.EVENTS = ev

    state = {"B_fail": True}

    def walk(url, *a, **k):
        if url.endswith("/B") and state["B_fail"]:
            raise intake.ShareReadError("cli share-detail 超时")
        return [{"name": "EP05.mkv", "fid": "f-" + url[-1], "size": 1, "path": "EP05.mkv", "is_dir": False}]
    intake.share_walk = walk

    real_argv = sys.argv
    try:
        sys.argv = ["intake.py", "--from-events", "--apply"]
        rc = intake.main()
    finally:
        sys.argv = real_argv
    stats = json.load(open(paths["stats"]))
    print("第 1 轮：rc=%s offset=%s failed_events=%s saved=%s"
          % (rc, stats.get("events_offset"), list((stats.get("failed_events") or {})), saved))
    assert rc == 0, rc
    assert "https://pan.quark.cn/s/A" in saved, "成功的任务必须保存"
    assert stats["events_offset"] == end_a, "offset 应只推进到失败任务之前（A 行末）"
    assert any("quark.cn/s/B" in k for k in (stats.get("failed_events") or {})), stats
    assert len(saved) == 1, saved

    # 第 2 轮：B 恢复正常 → 从失败任务处继续，保存 B 并推进到文件末尾
    state["B_fail"] = False
    real_argv = sys.argv
    try:
        sys.argv = ["intake.py", "--from-events", "--apply"]
        rc = intake.main()
    finally:
        sys.argv = real_argv
    stats = json.load(open(paths["stats"]))
    print("第 2 轮：rc=%s offset=%s saved=%s" % (rc, stats.get("events_offset"), saved))
    assert rc == 0
    assert saved.count("https://pan.quark.cn/s/B") == 1
    assert saved.count("https://pan.quark.cn/s/A") == 1, "已成功的事件不应重复保存"
    assert stats["events_offset"] == os.path.getsize(ev), "恢复后 offset 应推进到文件末尾"
    assert not (stats.get("failed_events") or {}), "成功后失败计数清零"

    # 第 3 轮：没有新事件 → 0/0，offset 不动
    real_argv = sys.argv
    try:
        sys.argv = ["intake.py", "--from-events", "--apply"]
        rc = intake.main()
    finally:
        sys.argv = real_argv
    stats = json.load(open(paths["stats"]))
    assert stats["events_offset"] == os.path.getsize(ev)
    print("第 3 轮：无新事件 → offset 不变 ✓")


# ── 8b) 失败任务回放时，已成功任务不会被重复保存 ──
def test_no_double_save_on_replay():
    _patch_plan_env()
    tmp = tempfile.mkdtemp(prefix="intake-replay-")
    ev = os.path.join(tmp, "events.jsonl")
    names = {"https://pan.quark.cn/s/A": "EP07-A.mkv",
             "https://pan.quark.cn/s/B": "EP06-B.mkv"}
    with open(ev, "w") as f:
        for url in names:
            f.write(json.dumps({"type": "new_share", "share_url": url,
                                "canonical": "测试剧R", "title": ""}) + "\n")
    paths = {n: os.path.join(tmp, n + ".json") for n in
             ("canonical", "registry", "stats", "upgrades", "pending", "resolutions")}
    with open(paths["canonical"], "w") as f:
        json.dump({"测试剧R": {"canonical": "测试剧R", "aliases": ["测试剧R"]}}, f,
                  ensure_ascii=False)
    intake.CANONICAL, intake.REGISTRY = paths["canonical"], paths["registry"]
    intake.STATS, intake.UPGRADES = paths["stats"], paths["upgrades"]
    intake.PENDING, intake.RESOLUTIONS = paths["pending"], paths["resolutions"]
    intake.EVENTS = ev

    dir_items = []          # 模拟目标目录：保存成功后出现在目录里
    saved = []

    class LiveDir(FakeDir):
        def list_items(self, fid):
            return [dict(x) for x in dir_items]

    intake.DirIndex = lambda c: LiveDir()
    state = {"A_fail": True}

    def walk(url, *a, **k):
        if url.endswith("/A") and state["A_fail"]:
            raise intake.ShareReadError("超时")
        return [{"name": names[url], "fid": "f-" + url[-1], "size": 1}]

    def save(plan):
        saved.append(plan["share_url"])
        for f in plan["missing"]:
            dir_items.append({"name": f["name"], "size": f["size"]})
        return True, "/夸克/测试"
    intake.share_walk = walk
    intake.do_save = save

    real_argv = sys.argv
    try:
        sys.argv = ["intake.py", "--from-events", "--apply"]
        intake.main()
        state["A_fail"] = False
        intake.main()
    finally:
        sys.argv = real_argv
    print("回放场景 saved:", saved)
    assert saved.count("https://pan.quark.cn/s/B") == 1, "B 只应保存一次"
    assert saved.count("https://pan.quark.cn/s/A") == 1, "A 恢复后保存一次"
    stats = json.load(open(paths["stats"]))
    assert stats["events_offset"] == os.path.getsize(ev)
    assert not (stats.get("failed_events") or {})
    print("失败回放不重复保存 ✓")


# ── 9) 毒丸：同一任务连续失败 5 次 → 终态并放行 offset ──
def test_main_poison_pill_released():
    _patch_plan_env()
    tmp = tempfile.mkdtemp(prefix="intake-poison-")
    ev = os.path.join(tmp, "events.jsonl")
    with open(ev, "w") as f:
        f.write(json.dumps({"type": "new_share", "share_url": "https://pan.quark.cn/s/poison",
                            "canonical": "测试剧P"}) + "\n")
    paths = {n: os.path.join(tmp, n + ".json") for n in
             ("canonical", "registry", "stats", "upgrades", "pending", "resolutions")}
    with open(paths["canonical"], "w") as f:
        json.dump({"测试剧P": {"canonical": "测试剧P", "aliases": ["测试剧P"]}}, f)
    intake.CANONICAL, intake.REGISTRY = paths["canonical"], paths["registry"]
    intake.STATS, intake.UPGRADES = paths["stats"], paths["upgrades"]
    intake.PENDING, intake.RESOLUTIONS = paths["pending"], paths["resolutions"]
    intake.EVENTS = ev
    intake.DirIndex = lambda c: FakeDir( dir_error="browse 超时")

    real_argv = sys.argv
    for i in range(1, intake.MAX_EVENT_FAILS + 1):
        try:
            sys.argv = ["intake.py", "--from-events", "--apply"]
            intake.main()
        finally:
            sys.argv = real_argv
        stats = json.load(open(paths["stats"]))
        fe = stats.get("failed_events") or {}
        term = stats.get("terminal_events") or {}
        print("第 %d 次失败：offset=%s failed=%s terminal=%s"
              % (i, stats["events_offset"], list(fe), list(term)))
        if i < intake.MAX_EVENT_FAILS:
            assert stats["events_offset"] == 0, "未到上限前 offset 不能推进"
        else:
            assert stats["events_offset"] == os.path.getsize(ev), "达到上限后应放行 offset"
            assert term, "应记录终态"
            assert not fe, "终态后计数器清空（只告警一次）"


if __name__ == "__main__":
    test_cli_timeout_sentinel()
    test_share_walk_classification()
    test_plan_read_failed()
    test_plan_dir_read_failed()
    test_plan_expired()
    test_do_save_failure()
    test_offset_semantics()
    test_failure_counter()
    test_main_end_to_end()
    test_no_double_save_on_replay()
    test_main_poison_pill_released()
    test_wrapper_quiet()
    print("OK: P0-A 读失败/保存失败不丢事件、offset 语义、wrapper 告警规则")
