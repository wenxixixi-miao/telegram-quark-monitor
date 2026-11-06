#!/usr/bin/env python3
"""离线/沙盒式联调：待确认 -> 人工确认 -> intake 采用（测完恢复线上状态）"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import SCRIPT_DIR, STATE_DIR  # noqa: E402
import json
import os
import subprocess
import sys

PEND = STATE_DIR + "/episode_pending.json"
RES = STATE_DIR + "/episode_resolutions.json"
EV = STATE_DIR + "/tg_events.jsonl"
URL = "https://pan.quark.cn/s/pending-test"
CANON = "示例剧"
FNAME = "MYSTERY.mkv"


def read(p):
    try:
        return open(p).read()
    except OSError:
        return None


def restore(p, content):
    if content is None:
        try:
            os.remove(p)
        except OSError:
            pass
    else:
        open(p, "w").write(content)


def main():
    pend_bak, res_bak = read(PEND), read(RES)
    ev_size = os.path.getsize(EV) if os.path.exists(EV) else 0
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        json.dump({"items": {"testpending1": {
            "id": "testpending1", "canonical": CANON, "share_url": URL,
            "file": FNAME, "guess_episode": None, "confidence": 0.1,
            "title": "示例剧 4K 更新", "ts": 1, "notified": False}}},
            open(PEND, "w"), ensure_ascii=False)
        print("== --list ==")
        print(subprocess.run([sys.executable, "pending_apply.py", "--list"],
                             capture_output=True, text=True, cwd=SCRIPT_DIR).stdout)
        print("== --notify ==")
        print(subprocess.run([sys.executable, "pending_apply.py", "--notify"],
                             capture_output=True, text=True, cwd=SCRIPT_DIR).stdout)
        print("== --set 1 --episode 164 ==")
        r = subprocess.run([sys.executable, "pending_apply.py", "--set", "1", "--episode", "164"],
                           capture_output=True, text=True, cwd=SCRIPT_DIR)
        print(r.stdout, r.stderr)
        res = json.load(open(RES))
        key = "%s|%s|%s" % (CANON, URL, FNAME)
        print("resolution saved:", key in res, res.get(key))
        grew = os.path.getsize(EV) > ev_size
        print("event requeued:", grew)

        sys.path.insert(0, SCRIPT_DIR)
        import intake

        class FakeDir:
            def fid_for(self, name):
                return ("fid-test", name)

            def list_files(self, fid):
                return []

            def list_items(self, fid, depth=0, out=None):
                return []

        intake.share_walk = lambda url, *a, **k: [{"name": FNAME, "fid": "f1", "size": 1024, "path": FNAME}]
        canon = {CANON: {"canonical": CANON, "kind": "tv", "tmdb_id": 223911, "aliases": [CANON]}}
        plan = intake._plan_for(URL, CANON, canon, FakeDir(), {}, "")
        print("intake picks up manual fix:", plan.get("manual_fixed"),
              "missing:", [f["name"] for f in plan.get("missing") or []])
        assert plan.get("manual_fixed") and len(plan.get("missing") or []) == 1
        print("OK: 待确认→回数字→入库 链路通")
    finally:
        restore(PEND, pend_bak)
        restore(RES, res_bak)
        if os.path.exists(EV):
            with open(EV, "r+") as f:
                f.truncate(ev_size)
        print("（已恢复线上状态）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
