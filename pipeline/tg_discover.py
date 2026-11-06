#!/usr/bin/env python3
"""TG 发现器（P1）：只发现/验活/登记，不落盘、不删除、不退频道

职责：
  - Telegram push → 标题行命中 canonical → 护栏 → 提取 quark 链接 → 写事件
  - 落盘由统一入口 intake.py 负责（--from-events）
  - 默认 dry-run（只写 would_save 事件）；灰度时 --live 才写 new_share 事件

运行方式：
  # 一次性回扫最近 N 分钟
  python3 tg_discover.py --once --backlog-minutes 180
  # 常驻（systemd 灰度用；默认 dry-run，只写事件不落盘）
  python3 tg_discover.py --daemon --live

状态：$TQM_STATE/tg_discover.json（每频道 last_msg_id、心跳、统计）
事件：$TQM_STATE/tg_events.jsonl（append-only，intake 消费）
配置：$TQM_CONFIG（600）
      $TQM_STATE/canonical.json（剧名/别名/TMDB）
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import PIPE_ROOT, STATE_DIR  # noqa: E402
import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

BASE = PIPE_ROOT
CONFIG = os.path.join(BASE, "monitor_config.json")
STATE = STATE_DIR + "/tg_discover.json"
EVENTS = STATE_DIR + "/tg_events.jsonl"
CANONICAL = STATE_DIR + "/canonical.json"
LOG_DIR = os.path.join(BASE, "logs")
UNMATCHED_RE = re.compile(r"(更新|更至|更新至|\d+\s*集|S\d{1,2}|EP?\s*\d+)", re.I)
MOVIE_RE = re.compile(r"(电影|剧场版|大电影|纪录片)")
JUNK_RE = re.compile(r"(短剧|更新目录|目录合集|合集目录|预告|花絮|采访|解说|盘点|混剪|片段)")
OBFUSCATED_RE = re.compile(r"[｜|○●◇◆□■☆★]"
                           r"|(?:[A-Za-z]{1,4}[\u4e00-\u9fff][A-Za-z]{1,4})"
                           r"|(?:[\u4e00-\u9fff]{1,2}(?:[.·・][\u4e00-\u9fff]{1,2}){1,3})")
# 全大写 token 只有不在"常见质量/格式词"里才算可疑（IQ、EP、4K 这类不是防和谐）
UPPER_OK = {"IQ", "EP", "EPS", "SP", "OVA", "OAD", "PV", "MV", "TV", "WEB", "DL",
            "WEB-DL", "WEBRIP", "HDR", "HDR10", "SDR", "UHD", "FHD", "HD", "HQ", "MAX",
            "HEVC", "AVC", "H264", "H265", "AV1", "AAC", "AC3", "DDP", "DTS", "DV",
            "REMUX", "BLURAY", "BD", "BDMV", "ISO", "MKV", "MP4", "FPS", "CN", "EN",
            "ZH", "CHS", "CHT", "GB", "TB", "AI", "HIFI", "DOLBY", "ATMOS", "HDR10P"}
UPPER_RE = re.compile(r"\b[A-Z]{2,8}\b")


def _has_suspicious_upper(title):
    for tok in UPPER_RE.findall(title):
        if tok.upper() in UPPER_OK:
            continue
        return True
    return False


def looks_obfuscated(title_line):
    """只把"像防和谐改名"的标题交给 LLM，普通中文剧名不浪费调用"""
    core = re.sub(r"(?i)(4k|8k|hdr|hq|ddp|dv|s\d{1,2}|e\d{1,4}|\d+p|更新\d*集?|更至\d*集?)", " ", title_line)
    core = re.sub(r"[\[\]【】()（）]", " ", core).strip()
    if OBFUSCATED_RE.search(core):
        return True
    if _has_suspicious_upper(core):
        return True
    # 去掉常见词后只剩 1~4 个汉字/字母的极短标题，也当作可疑
    short = re.sub(r"[\s\d.、，,：:【】\[\]()（）\-_/~!！]+", "", core)
    if 1 <= len(short) <= 4 and re.search(r"[A-Za-z]", short):
        return True
    return False
QUARK_URL_RE = re.compile(r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+")

EXCLUDE = ("短剧", "微短剧", "更新目录", "目录合集", "合集目录",
           "预告", "花絮", "采访", "解说", "盘点", "混剪", "片段")

ARGS = None
CANON = {}
_CANON_MTIME = 0.0
EVENT_KEYS = set()


def now_ts():
    return int(time.time())


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_event_keys(max_lines=3000):
    """载入最近事件的 (msg_id, share_url) 去重键，避免重启/重复回扫写重复事件"""
    EVENT_KEYS.clear()
    try:
        lines = open(EVENTS).read().splitlines()[-max_lines:]
        for line in lines:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("share_url"):
                EVENT_KEYS.add((ev.get("msg_id"), ev.get("share_url")))
    except FileNotFoundError:
        pass


def append_event(ev):
    key = (ev.get("msg_id"), ev.get("share_url"))
    if ev.get("share_url") and key in EVENT_KEYS:
        return False
    EVENT_KEYS.add(key)
    with open(EVENTS, "a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return True


def bare_chat_id(chat_id):
    """把 Telethon 的 -100xxxxxxxxxx 会话 id 还原成 entity.id（频道裸 id）。"""
    try:
        v = int(chat_id)
    except (TypeError, ValueError):
        return None
    if v < 0 and str(v).startswith("-100"):
        return -v - 10 ** 12
    return abs(v) or None


def channel_name_for_chat(chat_id, id_to_name):
    """会话 id -> 配置里的频道名；未知返回 None（不要退化成数字键）。"""
    bare = bare_chat_id(chat_id)
    return id_to_name.get(bare) if bare is not None else None


def merge_legacy_channel_keys(state, id_to_name):
    """旧版本把频道状态存在 -100... 数字键下：并入频道名键，保留更大的 last_msg_id。

    返回 [(旧键, 新键), ...]；认不出来的旧键原样保留（不丢状态）。
    """
    chans = state.setdefault("channels", {})
    merged = []
    for key in [k for k in chans if str(k).startswith("-100")]:
        name = channel_name_for_chat(key, id_to_name)
        if not name:
            continue
        old = chans.get(key) or {}
        cur = chans.get(name)
        if not isinstance(cur, dict):
            chans[name] = {"last_msg_id": int(old.get("last_msg_id") or 0),
                           "last_seen": old.get("last_seen")}
        else:
            cur["last_msg_id"] = max(int(cur.get("last_msg_id") or 0),
                                     int(old.get("last_msg_id") or 0))
            if old.get("last_seen") and (not cur.get("last_seen")
                                         or old["last_seen"] > cur["last_seen"]):
                cur["last_seen"] = old["last_seen"]
        chans.pop(key, None)
        merged.append((key, name))
    return merged


def resolve_canonical(title_line):
    best = None
    for name, meta in CANON.items():
        for alias in meta.get("aliases", []) + [name]:
            if alias and alias in title_line:
                if best is None or len(alias) > best[1]:
                    best = (name, len(alias), alias)
    return (best[0], best[2]) if best else (None, None)


def guard(title_line):
    for bad in EXCLUDE:
        if bad in title_line:
            return False, f"标题含排除词「{bad}」"
    return True, ""


def process_message(msg, channel_name, state):
    """处理一条消息；返回 (channel_name, 事件数, 是否候选)"""
    text = msg.text or ""
    title_line = text.strip().split("\n")[0][:160]
    ch_state = state.setdefault("channels", {}).setdefault(channel_name, {
        "last_msg_id": 0, "last_seen": None})
    ch_state["last_msg_id"] = max(int(ch_state.get("last_msg_id") or 0), msg.id)
    ch_state["last_seen"] = datetime.now().astimezone().isoformat(timespec="seconds")

    links = QUARK_URL_RE.findall(text)
    if not links:
        return channel_name, 0, False
    canon, alias = resolve_canonical(title_line)
    if not canon:
        # 未命中：只对"看起来像周更剧集"的帖子写 unmatched 事件，交给 LLM 兜底识别
        if (UNMATCHED_RE.search(title_line) and not MOVIE_RE.search(title_line)
                and not JUNK_RE.search(title_line) and looks_obfuscated(title_line)):
            n = 0
            for url in links:
                if append_event({"ts": now_ts(), "type": "unmatched",
                                 "canonical": None, "alias": "", "share_url": url,
                                 "channel": channel_name, "msg_id": msg.id,
                                 "title": title_line,
                                 "msg_date": msg.date.astimezone().isoformat(timespec="seconds")}):
                    n += 1
                    print(f"❓ 未识别（交 LLM）：{url}  ← {title_line[:60]}")
            return channel_name, n, False
        return channel_name, 0, False
    ok, reason = guard(title_line)
    if not ok:
        append_event({"ts": now_ts(), "type": "guarded", "canonical": canon,
                      "channel": channel_name, "msg_id": msg.id,
                      "title": title_line, "reason": reason})
        print(f"🛡️  拦截 [{canon}] {reason}：{title_line[:60]}")
        return channel_name, 0, False
    n = 0
    for url in links:
        if append_event({"ts": now_ts(),
                      "type": "new_share" if ARGS.live else "would_save",
                      "canonical": canon, "alias": alias, "share_url": url,
                      "channel": channel_name, "msg_id": msg.id,
                      "title": title_line,
                      "msg_date": msg.date.astimezone().isoformat(timespec="seconds")}):
            n += 1
            print(f"🎯 [{canon}] {url}  ← {title_line[:60]}")
    return channel_name, n, True


async def catch_up(client, entities, state, minutes, force=False):
    """force=True：按时间窗回扫（--backlog）；否则从 last_msg_id 续扫（防重复处理）"""
    cutoff = datetime.now(timezone.utc).timestamp() - minutes * 60 if force else None
    total = 0
    for ch_name, entity in entities.items():
        ch_state = state.setdefault("channels", {}).setdefault(ch_name, {
            "last_msg_id": 0, "last_seen": None})
        last_id = int(ch_state.get("last_msg_id") or 0)
        min_id = 0 if (force or last_id == 0) else last_id
        msgs = []
        async for msg in client.iter_messages(entity, min_id=min_id, limit=ARGS.limit):
            if cutoff and msg.date.timestamp() < cutoff:
                break
            msgs.append(msg)
        msgs.reverse()
        for msg in msgs:
            _, n, cand = process_message(msg, ch_name, state)
            total += n
        print(f"📡 @{ch_name}: 回扫 {len(msgs)} 条，事件 {total}")
    return total


async def daemon(client, entities, state, id_to_name):
    from telethon import events

    async def handler(event):
        # 注意：event.chat_id 是 -100xxxxxxxxxx，entity.id 是裸 id；以前直接比较永远不相等，
        # 结果在线消息全写进数字键，重启回扫又从旧位置重复处理（Review4 修复）
        ch_name = channel_name_for_chat(getattr(event, "chat_id", None), id_to_name)
        if ch_name is None:
            ch_name = str(getattr(event, "chat_id", "unknown"))
            print(f"⚠️ 未知频道消息（实体未匹配）：{ch_name}")
        try:
            process_message(event.message, ch_name, state)
            save_json_atomic(STATE, state)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ handler 异常：{e}")

    client.add_event_handler(handler, events.NewMessage(chats=list(entities.values())))

    async def heartbeat():
        global CANON, _CANON_MTIME
        while True:
            await asyncio.sleep(60)
            # canonical.json 被 add_show.py 改了 → 自动热加载（不用重启监听器）
            try:
                mt = os.path.getmtime(CANONICAL)
                if mt != _CANON_MTIME:
                    new = load_json(CANONICAL, {})
                    if new:
                        CANON = new
                        _CANON_MTIME = mt
                        print("🔄 canonical 已热加载（%d 部）" % len(CANON))
            except OSError:
                pass
            state["heartbeat"] = {"ts": now_ts(), "pid": os.getpid(),
                                  "session_ok": True,
                                  "last_run": datetime.now().astimezone().isoformat(timespec="seconds")}
            try:
                save_json_atomic(STATE, state)
            except OSError:
                pass

    asyncio.create_task(heartbeat())
    print("👀 开始监听（dry-run=%s），Ctrl+C 退出" % (not ARGS.live))
    await client.run_until_disconnected()


async def main():
    global ARGS, CANON
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--live", action="store_true", help="灰度：写 new_share 事件（仍不落盘）")
    ap.add_argument("--backlog-minutes", type=int, default=0)
    ap.add_argument("--limit", type=int, default=300)
    ARGS = ap.parse_args()
    if not ARGS.once and not ARGS.daemon:
        ARGS.once = True

    conf = load_json(CONFIG, {})
    if not conf.get("api_id") or not conf.get("api_hash"):
        print("❌ 缺少 monitor_config.json")
        return 2
    CANON = load_json(CANONICAL, {})
    try:
        _CANON_MTIME = os.path.getmtime(CANONICAL)
    except OSError:
        _CANON_MTIME = 0.0
    load_event_keys()
    if not CANON:
        print("⚠️ canonical.json 不存在，先跑 build_canonical.py")
        return 4
    os.makedirs(LOG_DIR, exist_ok=True)

    from telethon import TelegramClient
    client = TelegramClient(os.path.join(BASE, conf.get("session", "quark_monitor")),
                            int(conf["api_id"]), str(conf["api_hash"]))
    client.parse_mode = None
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ TG session 未授权，需要人工扫码（不自动登录）")
        return 3
    me = await client.get_me()
    print(f"✅ TG session ok: {me.first_name} (@{me.username})")

    names = conf.get("channels") or ["ExampleChannel"]
    entities = {}
    for ch_name in names:
        try:
            entities[ch_name] = await client.get_entity(ch_name)
        except Exception as e:  # noqa: BLE001
            print(f"❌ 无法获取频道 @{ch_name}: {e}")
    id_to_name = {int(e.id): n for n, e in entities.items()
                  if getattr(e, "id", None) is not None}

    state = load_json(STATE, {"channels": {}, "heartbeat": {}})
    # 旧版本把频道状态存在 -100... 数字键下（且没有 name 字段，旧迁移逻辑会直接丢弃）。
    # 现在按 entity.id 映射并回，保留更大的 last_msg_id，避免重启后重复回扫。
    merged = merge_legacy_channel_keys(state, id_to_name)
    if merged:
        print("🔧 频道状态归一化：%s" % ", ".join("%s → %s" % (a, b) for a, b in merged))
        save_json_atomic(STATE, state)

    if ARGS.once:
        n = await catch_up(client, entities, state, ARGS.backlog_minutes, force=ARGS.backlog_minutes > 0)
        state["heartbeat"] = {"ts": now_ts(), "pid": os.getpid(), "session_ok": True,
                              "last_run": datetime.now().astimezone().isoformat(timespec="seconds")}
        save_json_atomic(STATE, state)
        print(f"✅ 本轮事件 {n}（模式：{'live' if ARGS.live else 'dry-run'}）")
        await client.disconnect()
        return 0

    # daemon：先补收最近 30 分钟，再进入事件监听
    await catch_up(client, entities, state, 30, force=False)
    save_json_atomic(STATE, state)
    await daemon(client, entities, state, id_to_name)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"❌ TG 发现器异常: {e}")
        sys.exit(1)
