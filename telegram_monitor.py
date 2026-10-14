#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 频道监听 → 夸克网盘自动转存（关键字过滤版）
支持：实时监听 + 按需搜索历史消息
"""
import os
import re
import sys
import json
import asyncio
import time
import logging
from datetime import datetime

from telethon import TelegramClient, events

# ═══════════════════════════════════════════
# 配置（从 monitor_config.json 读取，不硬编码）
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

QUARK_URL_PATTERN = re.compile(r'https?://pan\.quark\.cn/s/[a-zA-Z0-9]+')

# ═══════════════════════════════════════════
# 🛡️ 转存准入规则（2026-09-29 新增，防误存）
# ───────────────────────────────────────────
# 事故背景：频道每天推「YYYY年M月D日 短剧更新目录N」合集帖，正文列十几部短剧，
# 其中偶有剧名含关键字子串（《凡手遮天》→"遮天"，《…剑仙逆世重修》→"仙逆"），
# 旧逻辑对**整条消息**做子串匹配 → 把整包短剧存进 /自动转存/<关键字>/。
# 已发生 3 次：2026-08-20 仙逆、2026-09-14 遮天×2、2026-09-29 遮天。
# 设计原则：只做“少存/不存”，**绝不删除任何已有文件**；拿不准时只通知不转存。
# ═══════════════════════════════════════════

# R1 排除词：**只看消息标题行**命中即整条跳过（不看关键字）
#   ⚠️ 2026-09-30 修正：原版扫全文，把「灵境行者」这种正经国漫误拦了——因为帖子正文里
#   会提"改编自同名小说"。目录帖的致命信号（短剧/更新目录）永远出现在**标题行**，
#   所以收紧到标题行匹配；「小说/漫画/广播剧/OST/原声带/主题曲」这类容易出现在正经资源
#   元信息里的词直接移出黑名单（真要是小说/漫画资源，R3 的"没有剧集标记"会拦下）。
EXCLUDE_KEYWORDS = [
    "短剧", "微短剧", "更新目录", "目录合集", "合集目录",
    "预告", "花絮", "采访", "解说", "盘点", "混剪", "片段",
]

# R2 关键字必须出现在“标题区”（频道格式：名称：**遮天(2025)4K臻彩 更新至180集**）
#    合集目录帖的正文里才出现剧名 → 标题区没有 = 不是这个资源
# ⚠️ 已停用（2026-10 起不参与判定）：该规则曾把「仙逆剧场版」这类没有“第N集”标记的资源误拦，
#    现仅保留常量作记录，真正的准入判定只用 R1（标题排除词）+ R4（内容守卫）。
TITLE_SCOPE_CHARS = 120

# R3 标题区必须含“剧集形态”标记，否则只通知、不自动转存
SERIES_MARKER_PATTERNS = [
    r"更新至\s*\d+\s*集", r"更新\s*\d+\s*集", r"第\s*\d+\s*集",
    r"EP?\s*\d+", r"S\d{1,2}\s*E\d{1,3}",
    r"全\s*\d+\s*集", r"共\s*\d+\s*集",
    r"第[一二三四五六七八九十\d]+季", r"年番",
]
# ⚠️ 已停用（2026-10 起不参与判定）：原因同上，剧场版/电影/OVA 没有“第N集”标记，
#    不能被当成可疑资源；保留常量作记录。

# R4 内容守卫：分享根目录“多剧并列且无剧集命名” → 判定为合集目录帖，跳过
CATALOG_MIN_ITEMS = 8
EPISODE_NAME_PATTERN = re.compile(
    r"[Ee][Pp]?\s*\d{1,4}|第\s*\d{1,4}\s*[集话]|S\d{1,2}\s*E\d{1,3}|\d{1,4}\s*[集话]"
)

# 文件路径
PROCESSED_FILE = os.path.join(SCRIPT_DIR, "processed_messages.json")
DEDUP_FILE = os.path.join(SCRIPT_DIR, "keyword_dedup.json")  # {keyword: timestamp}
SESSION_NAME = os.path.join(SCRIPT_DIR, "quark_monitor")
LOG_FILE = os.path.join(SCRIPT_DIR, "monitor.log")
NOTIFY_FILE = os.path.join(SCRIPT_DIR, "notify_queue.json")

# 搜索命令文件：写入此文件触发搜索，完成后自动删除
SEARCH_CMD = os.path.join(SCRIPT_DIR, "search_command.txt")
SEARCH_RESULT = os.path.join(SCRIPT_DIR, "search_result.json")

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
        except:
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
        except:
            return {}
    return {}

def save_dedup(dedup):
    with open(DEDUP_FILE, "w") as f:
        json.dump(dedup, f, ensure_ascii=False, indent=2)

def is_duplicate_keyword(keyword, dedup):
    """检查该关键词是否在 DEDUP_DAYS 天内已转存过

    🔓 空窗期规则（2026-10-06 用户要求）：**上次成功转存的那一天**全天放行 ——
    当天再检测到该关键词就再转存一次（频道常见"先推上一集、几小时后再推新集"，
    不能因为冷却把当天真正的新集锁在门外）；过了当天，继续按 DEDUP_DAYS 天冷却。
    空窗期不需要额外状态：直接看 keyword_dedup.json 里那条时间戳是不是"今天"。
    """
    now = datetime.now()
    if keyword in dedup:
        last = datetime.fromtimestamp(dedup[keyword])
        if last.date() == now.date():
            log.info(f"🔓 空窗期: [{keyword}] 当天已成功转存过，本次放行（当天不冷却）")
            return False
        elapsed_days = (now.timestamp() - dedup[keyword]) / 86400
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
            # 递归扫描子目录
            sub_path = f"{dir_path}/{item['file_name']}"
            fix_video_extensions(account, sub_path, depth + 1)
            continue
        fname = item['file_name']
        fid = item['fid']
        size = item.get('size', 0)
        if '.' not in fname:
            continue
        ext = fname.rsplit('.', 1)[-1].lower()
        # 大于50MB且不是视频后缀 → 改为 .mp4
        if ext not in VIDEO_EXTENSIONS and size > 50 * 1024 * 1024:
            new_name = fname.rsplit('.', 1)[0] + '.mp4'
            r = account.rename(fid, new_name)
            if r.get('code') == 0:
                log.info(f"🔧 后缀修正: {fname} → {new_name}")
            else:
                log.warning(f"⚠️ 重命名失败: {fname} → {r.get('message')}")

def screen_message(text, matched):
    """🛡️ 转存准入筛查 —— 只保留**高精度的一条**：短剧目录帖。

    ⚠️ 2026-10-01 用户明确要求撤掉误拦：原来的 R2（关键字必须出现在标题区）和
    R3（标题区必须有"更新至N集/第N集/SxxExx"标记）把**正经资源**也拦了：
      · 灵境行者 3 条正经推送（正文提"改编自同名小说"）
      · 仙逆剧场版《弑仙之战》4K 臻彩MAX（剧场版没有"第N集"这种标记）
    用户原话"把你的拦截去掉吧"。所以现在**只拦短剧目录帖**（标题行出现
    短剧/更新目录/预告花絮解说的那种，即 2026-09-29 误存那一类），
    其余一律放行 —— 宁可偶尔多存一个包，也不再挡住他要的资源。

    返回 (ok, reason, matched)：
    - ok=False → 不该自动转存，reason 说明原因（只通知不动手）
    - ok=True  → matched 原样返回，调用方用 matched[0] 当 primary_keyword
    """
    title_line = text.strip().split("\n")[0]
    for bad in EXCLUDE_KEYWORDS:
        if bad in title_line:
            return False, f"标题含排除词「{bad}」（短剧目录/预告花絮/解说类）", []
    return True, "", matched


def _catalog_signature(names, is_dirs, keyword):
    """合集目录帖特征：≥3 个同级文件夹，且多数是「（N集）+演员名」式短剧条目。

    注意：**不能因为"有个文件夹名里含关键字"就放行** —— 短剧目录里正好可能有一部
    叫《遮天：九龙拉棺》的短剧（2026-09-29 实测），单个资源永远不会有 ≥3 个同级文件夹。
    """
    dirs = [n for n, d in zip(names, is_dirs) if d]
    if len(dirs) < 3:
        return False
    ep_like = re.compile(r"[（(]\s*(全)?\d+\s*集\s*[)）]")
    hit = sum(1 for n in dirs if ep_like.search(n) or ("&" in n) or ("＆" in n))
    return hit >= max(3, int(len(dirs) * 0.6))


def precheck_share_content(share_url, keyword):
    """🛡️ R4 内容守卫：识别"多剧并列"的合集目录帖（短剧更新目录）。

    检查根目录；若根目录只有一个文件夹，则再看一层（实战结构：根=「5」，
    里面躺着 13 个「剧名（N集）演员A&演员B」文件夹 = 短剧目录包）。
    返回 (ok, reason)。取不到详情时放行（交给后面的转存流程自己报错），
    避免因为网络抖动把正常资源拦掉。
    """
    original_dir = os.getcwd()
    os.chdir(SCRIPT_DIR)
    try:
        sys.path.insert(0, SCRIPT_DIR)
        import quark_auto_save as qas
        config_path = os.path.join(SCRIPT_DIR, "quark_config.json")
        qas.CONFIG_DATA = qas.Config.read_json(config_path)
        cookies = qas.Config.get_cookies(qas.CONFIG_DATA.get("cookie", []))
        if not cookies:
            return True, ""
        account = qas.Quark(cookies[0])
        pwd_id, passcode, pdir_fid, _ = account.extract_url(share_url)
        st = account.get_stoken(pwd_id, passcode)
        stoken = (st.get("data") or {}).get("stoken")
        if not stoken:
            return True, ""
        detail = account.get_detail(pwd_id, stoken, pdir_fid)
        items = (detail.get("data") or {}).get("list") or []
        if not items:
            return True, ""

        def sig(lst):
            return _catalog_signature([i.get("file_name", "") for i in lst],
                                      [bool(i.get("dir")) for i in lst], keyword)

        # 根目录本身
        if len(items) >= CATALOG_MIN_ITEMS and not any(EPISODE_NAME_PATTERN.search(i.get("file_name", "")) for i in items):
            return False, f"根目录 {len(items)} 项且无剧集命名（疑似合集目录帖）"
        if sig(items):
            return False, f"根目录 {len([i for i in items if i.get('dir')])} 个无关文件夹（疑似合集目录帖）"

        # 根目录只有一个文件夹 → 再看一层
        if len(items) == 1 and items[0].get("dir"):
            inner = (account.get_detail(pwd_id, stoken, items[0]["fid"]).get("data") or {}).get("list") or []
            if sig(inner):
                sample = "、".join([i.get("file_name", "")[:22] for i in inner if i.get("dir")][:2])
                return False, f"包内 {len([i for i in inner if i.get('dir')])} 个剧集文件夹并列（疑似短剧目录帖：{sample}…）"
        return True, ""
    except Exception as e:
        log.warning(f"⚠️ 内容守卫检查异常（放行）: {e}")
        return True, ""
    finally:
        os.chdir(original_dir)


def clean_old_folders(account, save_subdir):
    """删掉 /自动转存/<keyword>/ 下的**所有旧资源文件夹**（递归），只留关键词目录本身。

    2026-10-06 用户要求：冷却期结束后的"第一次"转存，先清旧文件夹再存新批次，
    避免老批次乱码文件夹越堆越多；当天后续转存不再清理。
    ⚠️ 调用方必须先确认分享有效且非空（见 do_quark_save 里的前置检查），
    否则会出现"删完却没东西可存"（2026-06-27 光阴之外事故形态）。
    返回删除成功的文件夹个数。
    """
    target_path = f"{SAVE_PATH}/{save_subdir}"
    fids = account.get_fids([target_path])
    if not fids:
        return 0
    base = fids[0]["fid"]
    children = (account.ls_dir(base).get("data") or {}).get("list") or []
    n = 0
    for item in [c for c in children if c.get("dir")]:
        r = account.delete([item["fid"]])
        code = (r or {}).get("code")
        if code in (0, 23004):          # 0=成功；23004=已经删掉了，同样算成功
            n += 1
            log.info(f"🗑️ 已删除旧资源文件夹: {item['file_name']}")
        else:
            log.warning(f"⚠️ 删除旧文件夹失败: {item['file_name']} -> {r.get('message') if r else r}")
    if n:
        log.info(f"🗑️ 清理完成: {target_path} 下共删除 {n} 个旧文件夹")
    return n


def do_quark_save(share_url, save_subdir="", clean_old=False):
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

        # ── 冷却期后的"第一次"转存：先清掉该资源目录下的旧文件夹（2026-10-06 用户要求）──
        # 🔴 前置校验：分享打不开/是空的 → 既不清也不存（防"删完没东西可存"，见 clean_old_folders 注释）
        if save_subdir and clean_old:
            pwd_id, passcode, pdir_fid, _ = account.extract_url(share_url)
            st = account.get_stoken(pwd_id, passcode)
            stoken = (st.get("data") or {}).get("stoken")
            items = []
            if stoken:
                items = (account.get_detail(pwd_id, stoken, pdir_fid).get("data") or {}).get("list") or []
            if not stoken or not items:
                log.warning(f"⚠️ 分享无效或为空（{st.get('message')}），本次不清理也不转存")
                return False
            log.info(f"🧹 本轮首次转存，先清空旧资源文件夹: {target_path}")
            clean_old_folders(account, save_subdir)

        # ⚠️ 不再先删除旧文件夹（原逻辑先删后存，一旦转存失败旧文件也全没了）。
        # 改为靠 do_save_task 的增量"追更"：只保存新文件，已存在的跳过，绝不丢已有文件。
        log.info(f"🔄 正在转存: {share_url}")
        log.info(f"📁 保存到: {target_path}")

        # 用 do_save_task 正式转存
        task = {
            "taskname": save_subdir or "auto_save",
            "shareurl": share_url,
            "savepath": target_path,
            "pattern": "",
            "replace": "",
            "enddate": "2099-01-30",
            "addition": {},
        }
        # ⚠️ 必须检查返回值：do_save_task 返回 None(失败)/False(无新任务)/树(成功)。
        # 原代码无视返回值，失败也报"转存完成"，导致"收到成功通知但网盘没文件"。
        result = account.do_save_task(task)
        if not result:
            reason = task.get("shareurl_ban") or "没有新的转存任务（文件已存在或分享失效）"
            if "没有新的转存任务" in reason:
                # 2026-10-06 用户要求：这类"没有新文件"不再当成失败、不发通知（空窗期里会反复出现）
                log.info(f"⏭️ 无新文件可存（不通知）: {reason}")
                return None
            log.error(f"❌ 转存失败: {reason}")
            return False
        log.info(f"✅ 转存完成: {share_url}")

        # ── 自动修正非视频后缀为 .mp4 ──
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
    channel = await client.get_entity("Quark_Movies")
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

    # ── 搜索命令轮询（每30秒检查一次）──
    async def search_watcher():
        while True:
            await asyncio.sleep(30)
            if not os.path.exists(SEARCH_CMD):
                continue
            try:
                with open(SEARCH_CMD, "r") as f:
                    content = f.read().strip()
                # 删除命令文件
                os.remove(SEARCH_CMD)

                # 解析参数
                kws = KEYWORDS
                limit = 500
                for line in content.split("\n"):
                    line = line.strip()
                    if line.startswith("keywords="):
                        kws = [k.strip() for k in line.split("=", 1)[1].split(",")]
                    elif line.startswith("limit="):
                        limit = int(line.split("=", 1)[1])

                results = await do_search(client, kws, limit)

                # 写入结果文件
                with open(SEARCH_RESULT, "w") as f:
                    json.dump(results, f, ensure_ascii=False, indent=2)

                log.info(f"🔍 搜索结果已写入: {SEARCH_RESULT} ({len(results)}条)")

            except Exception as e:
                log.error(f"❌ 搜索异常: {e}")

    # ── 消息监听 ──
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
        any_handled = False

        if links and matched:
            title_line = text.strip().split("\n")[0][:80]

            # 🛡️ R1–R3 准入筛查：不合规只通知、不转存（避免短剧目录帖误存）
            ok, reason, title_matched = screen_message(text, matched)
            if not ok:
                log.info(f"🛡️ 规则拦截（不自动转存）: [{matched[0]}] {reason}")
                push_notify(f"🛡️ 已拦截可疑转存: {matched[0]}", f"原因: {reason}\n标题: {title_line}")
                processed.add(msg_id)
                save_processed(processed)
                return

            # 去重检查：同一关键词5天内已转存过则跳过（"当天已成功转存过"算空窗期，见 is_duplicate_keyword）
            primary_keyword = title_matched[0]
            if is_duplicate_keyword(primary_keyword, dedup):
                log.info(f"⏭️ 跳过转存: [{primary_keyword}] 5天内已转存过")
                processed.add(msg_id)
                save_processed(processed)
                return

            # 冷却期结束后的"第一次"转存 = 当天还没成功存过（空窗期内则不清理旧文件夹）
            _last = dedup.get(primary_keyword)
            clean_first = (_last is None) or (datetime.fromtimestamp(_last).date() != datetime.now().date())
            log.info(f"🎯 命中关键字: {title_matched}"
                     + ("（本轮首次：转存前先清旧资源文件夹）" if clean_first else "（空窗期内：不清理、不通知）"))
            # 2026-10-06 用户要求：当天成功转存过一次后，当天后续转存不再通知（含"检测到资源"）
            if clean_first:
                push_notify(f"🎯 检测到资源: {primary_keyword}", f"标题: {title_line}\n链接数: {len(links)}")

            any_success = False
            any_handled = False
            for _idx, link in enumerate(links):
                subdir = primary_keyword
                log.info(f"🔗 处理链接: {link}")
                try:
                    # 🛡️ R4 内容守卫：多剧并列 + 无剧集命名 → 合集目录帖，不转存
                    ok2, reason2 = precheck_share_content(link, primary_keyword)
                    if not ok2:
                        log.info(f"🛡️ 内容守卫拦截（不自动转存）: {reason2}")
                        push_notify(f"🛡️ 已拦截可疑转存: {primary_keyword}", f"原因: {reason2}\n链接: {link}")
                        continue
                    # 清理只在本轮第一条链接做（否则第二条会把第一条刚存的清掉）
                    success = do_quark_save(link, subdir, clean_old=(clean_first and _idx == 0))
                    if success:
                        any_success = True
                        any_handled = True
                        if clean_first:
                            push_notify(f"✅ 转存完成: {primary_keyword}", f"路径: {SAVE_PATH}/{subdir}")
                        else:
                            # 当天已成功转过一次 → 本次静默（2026-10-06 用户要求）
                            log.info(f"✅ 已转存（当天第 2+ 次，按用户要求不通知）: [{primary_keyword}]")
                    elif success is None:
                        # 夸克回"没有新的转存任务"：不是失败，不发通知（2026-10-06 用户要求）
                        log.info(f"⏭️ 无新文件，忽略: [{primary_keyword}]")
                        any_handled = True
                    else:
                        push_notify(f"❌ 转存失败: {primary_keyword}", f"链接: {link}")
                except Exception as e:
                    log.error(f"❌ 转存失败 [{link}]: {e}")
                    push_notify(f"❌ 转存异常: {primary_keyword}", str(e))

            # 转存成功后记录去重时间戳
            if any_success:
                update_dedup(primary_keyword, dedup)

        elif links:
            log.info(f"📩 有链接但无关键字匹配，跳过: {text[:80]}...")
        else:
            log.debug(f"📩 新消息（无链接）: [{sender_name}] {text[:60]}...")

        # 无链接 / 无关键字匹配 / 已处理（真存了 或 夸克说无新文件）才标记已处理；真失败留给下次重试
        if not links or not matched or any_handled:
            processed.add(msg_id)
            save_processed(processed)

    log.info("👀 开始监听... (Ctrl+C 退出)")

    # 同时运行监听和搜索轮询
    await asyncio.gather(
        client.run_until_disconnected(),
        search_watcher(),
    )


if __name__ == "__main__":
    """入口：支持自动重启，崩溃时记录日志并重连"""
    max_retries = 10
    retry_delay = 10  # 首次重试延迟（秒），指数退避
    for attempt in range(max_retries):
        try:
            asyncio.run(main())
            break  # 正常退出（Ctrl+C），不重试
        except asyncio.CancelledError:
            log.info("🛑 监听器被取消")
            break
        except KeyboardInterrupt:
            log.info("🛑 监听器手动停止")
            break
        except Exception as e:
            log.error(f"💥 监听器崩溃 (第{attempt+1}次): {e}", exc_info=True)
            if attempt < max_retries - 1:
                wait = retry_delay * (2 ** attempt)
                log.warning(f"🔄 {wait}秒后自动重启...")
                time.sleep(wait)
            else:
                log.critical("❌ 监听器已达最大重试次数，彻底退出")
