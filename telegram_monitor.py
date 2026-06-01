#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 频道监听 → 夸克网盘自动转存（关键字过滤版）
支持：实时监听 + 按需搜索历史消息

基于 Cp0204/quark-auto-save 扩展的 Telegram 监听模块。
"""

import os
import re
import sys
import json
import asyncio
import logging
from datetime import datetime

from telethon import TelegramClient, events

# ═══════════════════════════════════════════
# 配置（从 config.json 读取，不硬编码）
# ═══════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "monitor_config.json")

def load_config():
    """加载配置文件，缺失时给出提示"""
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ 配置文件不存在: {CONFIG_FILE}")
        print("请复制 monitor_config.example.json 为 monitor_config.json 并填写配置")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

cfg = load_config()

API_ID = cfg["api_id"]
API_HASH = cfg["api_hash"]
PHONE = cfg["phone"]
CHANNELS = cfg.get("channels", ["Quark_Movies"])
SAVE_PATH = cfg.get("save_path", "/自动转存")
KEYWORDS = cfg.get("keywords", [])
DEDUP_DAYS = cfg.get("dedup_days", 5)

# 文件路径
PROCESSED_FILE = os.path.join(SCRIPT_DIR, "processed_messages.json")
DEDUP_FILE = os.path.join(SCRIPT_DIR, "keyword_dedup.json")
SESSION_NAME = os.path.join(SCRIPT_DIR, "quark_monitor")
LOG_FILE = os.path.join(SCRIPT_DIR, "monitor.log")
NOTIFY_FILE = os.path.join(SCRIPT_DIR, "notify_queue.json")
SEARCH_CMD = os.path.join(SCRIPT_DIR, "search_command.txt")
SEARCH_RESULT = os.path.join(SCRIPT_DIR, "search_result.json")

QUARK_URL_PATTERN = re.compile(r'https?://pan\.quark\.cn/s/[a-zA-Z0-9]+')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("monitor")


# ═══════════════════════════════════════════
# 通知系统
# ═══════════════════════════════════════════
def push_notify(title, detail=""):
    import fcntl
    notifications = []
    if os.path.exists(NOTIFY_FILE):
        try:
            with open(NOTIFY_FILE, "r") as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                notifications = json.load(f)
                fcntl.flock(f, fcntl.LOCK_UN)
        except Exception:
            notifications = []
    notifications.append({
        "time": datetime.now().strftime("%m-%d %H:%M"),
        "title": title,
        "detail": detail,
        "sent": False,
    })
    with open(NOTIFY_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(notifications, f, ensure_ascii=False, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)
    log.info(f"📢 通知已入队: {title}")


# ═══════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════
def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_processed(processed):
    recent = list(processed)[-5000:]
    with open(PROCESSED_FILE, "w") as f:
        json.dump(recent, f)

def extract_quark_links(text):
    if not text:
        return []
    return QUARK_URL_PATTERN.findall(text)

def match_keywords(text):
    if not text:
        return []
    return [kw for kw in KEYWORDS if kw in text]

def load_dedup():
    if os.path.exists(DEDUP_FILE):
        try:
            with open(DEDUP_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_dedup(dedup):
    with open(DEDUP_FILE, "w") as f:
        json.dump(dedup, f, ensure_ascii=False, indent=2)

def is_duplicate_keyword(keyword, dedup):
    """检查该关键词是否在 DEDUP_DAYS 天内已转存过"""
    now = datetime.now().timestamp()
    if keyword in dedup:
        last_ts = dedup[keyword]
        elapsed_days = (now - last_ts) / 86400
        if elapsed_days < DEDUP_DAYS:
            remaining = DEDUP_DAYS - elapsed_days
            log.info(f"⏭️ 跳过: [{keyword}] {remaining:.1f}天后可再次转存（已去重）")
            return True
    return False

def update_dedup(keyword, dedup):
    """记录该关键词的转存时间"""
    dedup[keyword] = datetime.now().timestamp()
    save_dedup(dedup)


# ═══════════════════════════════════════════
# 夸克转存
# ═══════════════════════════════════════════
VIDEO_EXTENSIONS = {'mp4', 'mkv', 'mov', 'm4v', 'avi', 'mpeg', 'ts', 'flv', 'wmv', 'rmvb', 'rm'}

def fix_video_extensions(account, dir_path, depth=0):
    """递归扫描目录，将非视频后缀的大文件(>50MB)重命名为 .mp4"""
    if depth > 5:
        return
    fids = account.get_fids([dir_path])
    if not fids:
        return
    result = account.ls_dir(fids[0]['fid'])
    if result.get('code') != 0 or not result.get('data', {}).get('list'):
        return
    for item in result['data']['list']:
        if item.get('dir'):
            sub_path = f"{dir_path}/{item['file_name']}"
            fix_video_extensions(account, sub_path, depth + 1)
            continue
        fname = item['file_name']
        fid = item['fid']
        size = item.get('size', 0)
        if '.' not in fname:
            continue
        ext = fname.rsplit('.', 1)[-1].lower()
        if ext not in VIDEO_EXTENSIONS and size > 50 * 1024 * 1024:
            new_name = fname.rsplit('.', 1)[0] + '.mp4'
            r = account.rename(fid, new_name)
            if r.get('code') == 0:
                log.info(f"🔧 后缀修正: {fname} → {new_name}")
            else:
                log.warning(f"⚠️ 重命名失败: {fname} → {r.get('message')}")

def do_quark_save(share_url, save_subdir=""):
    original_dir = os.getcwd()
    os.chdir(SCRIPT_DIR)
    try:
        sys.path.insert(0, SCRIPT_DIR)
        import quark_auto_save as qas
        config_path = os.path.join(SCRIPT_DIR, "quark_config.json")
        qas.CONFIG_DATA = qas.Config.read_json(config_path)
        cookies = qas.Config.get_cookies(qas.CONFIG_DATA.get("cookie", []))
        if not cookies:
            log.error("❌ 夸克 cookie 未配置")
            return False
        account = qas.Quark(cookies[0])

        target_path = SAVE_PATH
        if save_subdir:
            target_path = f"{SAVE_PATH}/{save_subdir}"

        # 检测旧文件夹，有则删除
        if save_subdir:
            existing = account.get_fids([target_path])
            if existing:
                old_fid = existing[0]["fid"]
                old_name = existing[0]["file_name"]
                log.info(f"🗑️ 发现旧文件夹: {old_name}，准备删除...")
                account.delete([old_fid])
                log.info(f"🗑️ 旧文件夹已删除: {old_name}")

        log.info(f"🔄 正在转存: {share_url}")
        log.info(f"📁 保存到: {target_path}")

        task = {
            "taskname": save_subdir or "auto_save",
            "shareurl": share_url,
            "savepath": target_path,
            "pattern": "",
            "replace": "",
            "enddate": "2099-01-30",
            "addition": {},
        }
        account.do_save_task(task)
        log.info(f"✅ 转存完成: {share_url}")

        # 自动修正非视频后缀为 .mp4
        if save_subdir:
            try:
                fix_video_extensions(account, target_path)
            except Exception as e:
                log.warning(f"⚠️ 修正后缀异常: {e}")

        return True
    except Exception as e:
        log.error(f"❌ 转存异常: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        os.chdir(original_dir)


# ═══════════════════════════════════════════
# 搜索历史消息
# ═══════════════════════════════════════════
async def do_search(client, keywords=None, limit=500):
    """搜索频道历史消息，返回匹配结果"""
    kws = keywords or KEYWORDS
    channel = await client.get_entity(CHANNELS[0])
    results = []

    log.info(f"🔍 开始搜索: 关键字={kws}, 扫描={limit}条")

    async for msg in client.iter_messages(channel, limit=limit):
        text = msg.text or ""
        matched = [kw for kw in kws if kw in text]
        links = extract_quark_links(text)

        if matched and links:
            date = msg.date.strftime("%Y-%m-%d")
            title = text.strip().split("\n")[0][:80]
            results.append({
                "date": date,
                "keyword": matched[0],
                "title": title,
                "links": links,
            })

    log.info(f"🔍 搜索完成: 找到 {len(results)} 条匹配")
    return results


# ═══════════════════════════════════════════
# 主监听
# ═══════════════════════════════════════════
async def main():
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

    log.info("🚀 启动 Telegram 频道监听（关键字过滤版）")
    log.info(f"📡 监听频道: {CHANNELS}")
    log.info(f"🔑 关键字: {KEYWORDS}")
    log.info(f"📁 转存路径: {SAVE_PATH}")

    await client.start(phone=PHONE)
    me = await client.get_me()
    log.info(f"✅ 登录成功: {me.first_name} ({me.phone})")

    channel_ids = []
    for ch in CHANNELS:
        try:
            entity = await client.get_entity(ch)
            channel_ids.append(entity.id)
            log.info(f"📢 频道 @{ch} → ID: {entity.id}")
        except Exception as e:
            log.error(f"❌ 无法找到频道 @{ch}: {e}")
            return

    processed = load_processed()
    dedup = load_dedup()
    log.info(f"📋 已有 {len(processed)} 条已处理记录")
    log.info(f"📋 去重记录: {len(dedup)} 条 (同名 {DEDUP_DAYS} 天内跳过)")

    # 搜索命令轮询（每30秒检查一次）
    async def search_watcher():
        while True:
            await asyncio.sleep(30)
            if not os.path.exists(SEARCH_CMD):
                continue
            try:
                with open(SEARCH_CMD, "r") as f:
                    content = f.read().strip()
                os.remove(SEARCH_CMD)

                kws = KEYWORDS
                limit = 500
                for line in content.split("\n"):
                    line = line.strip()
                    if line.startswith("keywords="):
                        kws = [k.strip() for k in line.split("=", 1)[1].split(",")]
                    elif line.startswith("limit="):
                        limit = int(line.split("=", 1)[1])

                results = await do_search(client, kws, limit)

                with open(SEARCH_RESULT, "w") as f:
                    json.dump(results, f, ensure_ascii=False, indent=2)

                log.info(f"🔍 搜索结果已写入: {SEARCH_RESULT} ({len(results)}条)")

            except Exception as e:
                log.error(f"❌ 搜索异常: {e}")

    # 消息监听
    @client.on(events.NewMessage(chats=channel_ids))
    async def handler(event):
        msg_id = event.message.id
        text = event.message.text or ""
        sender = await event.get_sender()
        sender_name = getattr(sender, "title", None) or getattr(sender, "first_name", "未知")

        if msg_id in processed:
            return

        links = extract_quark_links(text)
        matched = match_keywords(text)
        any_success = False

        if links and matched:
            primary_keyword = matched[0]
            if is_duplicate_keyword(primary_keyword, dedup):
                log.info(f"⏭️ 跳过转存: [{primary_keyword}] 5天内已转存过")
                processed.add(msg_id)
                save_processed(processed)
                return

            log.info(f"🎯 命中关键字: {matched}")
            title_line = text.strip().split("\n")[0][:60]
            push_notify(f"🎯 检测到资源: {primary_keyword}", f"标题: {title_line}\n链接数: {len(links)}")

            any_success = False
            for link in links:
                subdir = primary_keyword
                log.info(f"🔗 处理链接: {link}")
                try:
                    success = do_quark_save(link, subdir)
                    if success:
                        any_success = True
                        push_notify(f"✅ 转存完成: {primary_keyword}", f"路径: {SAVE_PATH}/{subdir}")
                    else:
                        push_notify(f"❌ 转存失败: {primary_keyword}", f"链接: {link}")
                except Exception as e:
                    log.error(f"❌ 转存失败 [{link}]: {e}")
                    push_notify(f"❌ 转存异常: {primary_keyword}", str(e))

            if any_success:
                update_dedup(primary_keyword, dedup)

        elif links:
            log.info(f"📩 有链接但无关键字匹配，跳过: {text[:80]}...")
        else:
            log.debug(f"📩 新消息（无链接）: [{sender_name}] {text[:60]}...")

        if not links or not matched or any_success:
            processed.add(msg_id)
            save_processed(processed)

    log.info("👀 开始监听... (Ctrl+C 退出)")

    await asyncio.gather(
        client.run_until_disconnected(),
        search_watcher(),
    )


if __name__ == "__main__":
    asyncio.run(main())
