#!/usr/bin/env python3
"""Review4：TG 监听器频道键回归测试（纯函数，不连 TG）

背景：event.chat_id 是 -100xxxxxxxxxx，entity.id 是裸 id，
旧 handler 直接比较永远不相等 → 在线消息写进数字键，重启回扫从旧位置重复处理。
"""

import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _sub in ("lib", "pipeline"):
    _p = _os.path.join(_ROOT, _sub)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_discover as td  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ✓ %s" % name)
    else:
        print("  ✗ %s %s" % (name, detail))
        FAILS.append(name)


def main():
    print("[1] bare_chat_id / channel_name_for_chat")
    check("频道 -100... 还原裸 id",
          td.bare_chat_id(-1001234567890) == 1234567890)
    check("正数原样", td.bare_chat_id(1234567890) == 1234567890)
    check("字符串也支持", td.bare_chat_id("-1001234567890") == 1234567890)
    check("None/脏值安全", td.bare_chat_id(None) is None and td.bare_chat_id("x") is None)
    id_to_name = {1234567890: "ExampleChannel"}
    check("会话 id 命中频道名",
          td.channel_name_for_chat(-1001234567890, id_to_name) == "ExampleChannel")
    check("未知会话返回 None（不再退化成数字键）",
          td.channel_name_for_chat(-1009999999999, id_to_name) is None)

    print("[2] 旧数字键状态归一化")
    state = {"channels": {
        "ExampleChannel": {"last_msg_id": 77426, "last_seen": "2026-11-01T01:33:56+08:00"},
        "-1001234567890": {"last_msg_id": 77469, "last_seen": "2026-11-01T16:17:29+08:00"},
    }}
    merged = td.merge_legacy_channel_keys(state, id_to_name)
    chans = state["channels"]
    check("只保留频道名键", list(chans) == ["ExampleChannel"], str(list(chans)))
    check("last_msg_id 取更大值", chans["ExampleChannel"]["last_msg_id"] == 77469,
          str(chans))
    check("last_seen 取更新值", chans["ExampleChannel"]["last_seen"] == "2026-11-01T16:17:29+08:00",
          str(chans))
    check("返回迁移映射", merged == [("-1001234567890", "ExampleChannel")], str(merged))

    print("[3] 边界：只有数字键 / 认不出来的键")
    s2 = {"channels": {"-1001234567890": {"last_msg_id": 5, "last_seen": None}}}
    td.merge_legacy_channel_keys(s2, id_to_name)
    check("只有数字键 → 新建频道名键",
          s2["channels"] == {"ExampleChannel": {"last_msg_id": 5, "last_seen": None}},
          str(s2["channels"]))

    s3 = {"channels": {"-1009999999999": {"last_msg_id": 9, "last_seen": "x"}}}
    td.merge_legacy_channel_keys(s3, id_to_name)
    check("认不出来的旧键保留（不丢状态）", "-1009999999999" in s3["channels"],
          str(s3["channels"]))

    s4 = {"channels": {"ExampleChannel": {"last_msg_id": 10, "last_seen": "a"}}}
    td.merge_legacy_channel_keys(s4, id_to_name)
    check("没有旧键时不动", s4["channels"] == {"ExampleChannel": {"last_msg_id": 10, "last_seen": "a"}},
          str(s4["channels"]))

    print()
    if FAILS:
        print("FAILED: %s" % ", ".join(FAILS))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
