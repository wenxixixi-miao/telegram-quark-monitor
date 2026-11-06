#!/usr/bin/env python3
"""统一入库入口（P1）：把 TG/官方发现的新分享，按「缺集」增量转存到规范目录

设计原则：
  - 唯一写网盘入口：TG 发现器只写事件，本脚本负责决定"存哪些、存哪里"
  - 落盘 = /夸克/自动转存/<canonical 顶层目录>（用官方 CLI saveas --fid-list --to-pdir-fid 写真实目录）
  - 只存缺集：分享内文件按集号与目标目录已有集号求差集；绝不删除
  - 默认 dry-run：只打印计划；--apply 才真正写网盘（P1 灰度期间先 dry-run 验收）

用法：
  python3 intake.py --from-events --dry
  python3 intake.py --share <quark_url> --canonical 示例剧 --dry
  python3 intake.py --from-events --apply        # 灰度上线后
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import LIB_DIR, QUARK_CLI_DIR, STATE_DIR  # noqa: E402
import argparse
import json
import os
import re
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
for _p in (LIB_DIR, os.path.join(os.path.dirname(_HERE), "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import ep_ledger  # noqa: E402
import media_rules  # noqa: E402
from episode_parse import ep_key, parse_title_hint, parse_episode  # noqa: E402

SKILL = QUARK_CLI_DIR
CLI = os.path.join(SKILL, "scripts", "quark-drive.cjs")
CANONICAL = STATE_DIR + "/canonical.json"
REGISTRY = os.environ.get("INTAKE_REGISTRY", STATE_DIR + "/share_registry.json")
EVENTS = os.environ.get("INTAKE_EVENTS", STATE_DIR + "/tg_events.jsonl")
STATS = os.environ.get("INTAKE_STATS", STATE_DIR + "/intake_stats.json")
PENDING = STATE_DIR + "/episode_pending.json"
RESOLUTIONS = STATE_DIR + "/episode_resolutions.json"
UPGRADES = os.environ.get("INTAKE_UPGRADES", STATE_DIR + "/upgrades.json")
UPGRADE_MIN = 150              # 新版本比已有版本至少高这么多分才升级
UPGRADE_COOLDOWN = 30 * 86400  # 同一集 30 天内最多升级一次

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".rmvb", ".flv", ".wmv", ".mov", ".mpg")
MODE = "follow"


CODEC_RISK = (("hevc", "H.265"), ("x265", "H.265"), ("h.265", "H.265"),
              ("10bit", "10bit"), ("av1", "AV1"), ("dovi", "DolbyVision"),
              ("dv.", "DolbyVision"), ("truehd", "TrueHD"))


try:
    from quality import quality_score
except ImportError:                       # 兜底：老环境没有 quality.py
    def quality_score(f):
        return (f.get("size") or 0, f.get("size") or 0)


def res_class(name):
    low = (name or "").lower()
    if re.search(r"(2160p|\b4k\b|uhd|8k)", low):
        return 4
    if "1440" in low or re.search(r"\b2k\b", low):
        return 3
    if "1080" in low:
        return 2
    if "720" in low:
        return 1
    return 0


def _is_hdr(name):
    return bool(re.search(r"(hdr|dolby[.\s]?vision|\bdv\b)", (name or "").lower()))


def upgrade_needed(new_name, new_score, old_name, old_score, new_size, old_size):
    """是否值得把已有版本换成新版本（用户口径 2026-11-01）：
      - 都是 4K：**不升级**，唯一例外=旧的 HDR、新的 SDR（夸克压缩后 HDR 色彩不准）
      - 分辨率提高（如 1080P→4K）：分差 ≥150 才换
      - 非 4K 同分辨率：remux 且体积 ≥1.2 倍可换，否则同样要 ≥150
    """
    diff = new_score[0] - old_score[0]
    if diff <= 0:
        return False
    rc_new, rc_old = res_class(new_name), res_class(old_name)
    if rc_new == rc_old:
        if rc_new == 4:
            return _is_hdr(old_name) and not _is_hdr(new_name) and diff >= 40
        if "remux" in new_name.lower() and "remux" not in old_name.lower() \
                and old_size and new_size >= old_size * 1.2:
            return True
    return diff >= UPGRADE_MIN


def item_ep(plan, f):
    """取 plan 里某个待存文件的集号（含标题兜底的文件）"""
    k = ep_key(f.get("name") or "")
    if k:
        if k[0] == "se":
            return k[2]
        if k[0] == "ep":
            return k[1]
        return None
    th = plan.get("title_hint_used") or {}
    if th.get("name") == f.get("name"):
        return th.get("episode")
    return None



def _pending_id(canon, url, name):
    import hashlib
    return hashlib.sha1(("%s|%s|%s" % (canon, url, name)).encode()).hexdigest()[:10]


def record_pending(plan):
    """把认不出的文件写入待确认队列（只新增，不覆盖）"""
    try:
        d = load_json(PENDING, {})
    except Exception:
        d = {}
    items = d.setdefault("items", {})
    new = []
    for item in plan.get("llm_pending") or []:
        pid = _pending_id(plan.get("_canon"), plan.get("_url"), item["name"])
        if pid in items:
            continue
        rec = {"id": pid, "canonical": plan.get("_canon"), "share_url": plan.get("_url"),
               "file": item["name"], "guess_episode": item.get("episode"),
               "confidence": item.get("confidence"), "title": plan.get("_title"),
               "ts": now(), "notified": False}
        items[pid] = rec
        new.append(rec)
    if new:
        tmp = PENDING + ".tmp"
        json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=1)
        os.replace(tmp, PENDING)
        try:
            os.chmod(PENDING, 0o600)
        except OSError:
            pass
    return new



def item_ep_key(plan, f):
    """(season, ep)：同集多源选优必须季感知，否则 S02E01 会被 S01E01 顶掉"""
    k = ep_key(f.get("name") or "")
    if k and k[0] in ("se", "ep"):
        return (k[1], k[2]) if k[0] == "se" else (plan.get("default_season"), k[1])
    th = plan.get("title_hint_used") or {}
    if th.get("name") == f.get("name") and th.get("episode") is not None:
        season = th.get("season")
        return (season if season is not None else plan.get("default_season"), th.get("episode"))
    return None


def select_sources(plans):
    """同剧同一集多个来源 → 只留最高分，其余记入 plan['_dropped_sources']"""
    groups = {}
    for plan in plans:
        for f in plan.get("missing") or []:
            key = item_ep_key(plan, f)
            if key is None or key[1] is None:
                continue
            groups.setdefault((plan.get("_canon"), key[0], int(key[1])), []) \
                  .append((quality_score(f), plan, f))
    picked = {id(p): [] for p in plans}
    for key, cands in groups.items():
        cands.sort(key=lambda x: x[0], reverse=True)
        best = cands[0]
        picked[id(best[1])].append(best[2])
        for score, p2, f2 in cands[1:]:
            p2.setdefault("_dropped_sources", []).append(f2["name"])
    for plan in plans:
        orig = plan.get("missing") or []
        keep = picked.get(id(plan), orig)
        if len(keep) != len(orig):
            keep_ids = {id(x) for x in keep}
            plan["missing"] = [f for f in orig if id(f) in keep_ids]
    return plans


def codec_hint(name):
    low = name.lower()
    hits = [label for key, label in CODEC_RISK if key in low]
    return "、".join(sorted(set(hits)))


def now():
    return int(time.time())


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class CliTimeout(Exception):
    """CLI 调用超时（读失败，可重试；不是"链接失效"）"""


class CliFailed(Exception):
    """CLI 无法执行 / 无任何可用输出（读失败，可重试）"""


def cli(*args, timeout=90):
    env = dict(os.environ)
    env.setdefault("HERMES_SESSION_ID", "p1-intake")
    env.setdefault("HERMES_KANBAN_BOARD", "default")
    cmd = ["node", CLI] + list(args) + ["--session-input", "P1 统一入库", "--session-id",
                                        time.strftime("%s") + "-intake"]
    try:
        r = subprocess.run(cmd, cwd=SKILL, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise CliTimeout("cli %s 超时" % (args[0] if args else "?")) from e
    except OSError as e:
        raise CliFailed("cli 无法执行：%s" % e) from e
    rows = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    if not rows and r.returncode not in (0, None):
        raise CliFailed("cli %s 退出码 %s" % (args[0] if args else "?", r.returncode))
    return rows


def result_of(rows):
    for r in rows:
        if r.get("type") == "result":
            return r
    return None


# ─────────────── 目录树（真实网盘 fid） ───────────────
class DirIndex:
    def __init__(self, canonical):
        self.canonical = canonical
        self.auto_fid = None
        self.dirs = {}          # canonical name -> fid
        self._cache = {}        # fid -> [items]
        self.read_error = None  # 最近一次 browse 读失败（超时/CLI 失败）
        self.error = None       # 目录索引整体不可用
        self.read_errors = []   # 本轮 list_items 过程中累积的读失败
        self._load()

    def _browse(self, fid):
        """必须 --all：CLI stdout 只给前 5 个预览，全量在 artifact JSONL 里"""
        if fid in self._cache:
            return self._cache[fid]
        self.read_error = None
        try:
            rows = cli("browse", "--parent-fid", str(fid), "--all", timeout=120)
        except (CliTimeout, CliFailed) as e:
            self.read_error = str(e)
            self.read_errors.append(self.read_error)
            return []
        art = next((r for r in rows if r.get("type") == "artifact"), None)
        fp = (art or {}).get("data", {}).get("file_path")
        if fp and os.path.exists(fp):
            try:
                items = [json.loads(line) for line in open(fp)
                         if line.strip().startswith("{")]
                self._cache[fid] = items
                return items
            except Exception:
                pass
            finally:
                try:
                    os.remove(fp)
                except OSError:
                    pass
        r = result_of(rows)
        items = ((r or {}).get("data") or {}).get("file_list") or []
        self._cache[fid] = items
        return items

    def _load(self):
        root = self._browse("0")
        if self.read_error:
            self.error = "目录索引读取失败：%s" % self.read_error
            return
        for it in root:
            if it.get("file_type") == "0" and it.get("filename") == "自动转存":
                self.auto_fid = it.get("fid")
                break
        if not self.auto_fid:
            return
        for it in self._browse(self.auto_fid):
            if it.get("file_type") != "0":
                continue
            name = it.get("filename") or ""
            self.dirs[name] = it.get("fid")
        if self.read_error:
            self.error = "目录索引读取失败：%s" % self.read_error

    def fid_for(self, name):
        meta = self.canonical.get(name) or {}
        for cand in [name] + list(meta.get("dirs") or []):
            if cand in self.dirs:
                return self.dirs[cand], cand
        return None, None

    def list_items(self, fid, depth=0, out=None):
        """递归列出目录内视频文件 [{'name','size'}]"""
        if out is None:
            out = []
        if depth > 2:
            return out
        for it in self._browse(fid):
            fn = it.get("filename") or ""
            if it.get("file_type") == "0":
                self.list_items(it.get("fid"), depth + 1, out)
            elif fn.lower().endswith(VIDEO_EXT):
                out.append({"name": fn, "size": it.get("size") or 0})
        return out

    def list_files(self, fid, depth=0, out=None):
        """递归列出目录内视频文件名（相对路径）"""
        return [x["name"] for x in self.list_items(fid, depth, out)]


SHARE_ERR = {}

# 只有这些才算"确认失效"；其它错误（限流/超时/未知码）一律当读失败，下轮重试，
# 并由失败计数器兜底，避免未知码把事件永久吞掉。
EXPIRED_CODES = {41011}
EXPIRED_MSG = ("失效", "不存在", "已取消", "取消分享", "已过期", "已删除", "违规", "封禁")


class ShareReadError(Exception):
    """分享读取失败（超时/限流/网络/未知错误）——可重试，绝不能当链接失效"""


class ShareExpired(Exception):
    """分享确认失效（41011 / 明确的不存在/取消/过期文案）——终态"""

    def __init__(self, code, msg):
        super().__init__("%s %s" % (code, msg))
        self.code = code
        self.msg = msg


def share_walk(url, pdir_fid="0", depth=0, out=None, max_files=300):
    """返回分享内视频文件 [{name, fid, token, size, path}]，最多 max_files 个

    读失败抛 ShareReadError（可重试），确认失效抛 ShareExpired（终态）。
    空分享返回 []（由调用方决定是否当读失败重试）。
    """
    if out is None:
        out = []
    if depth > 2 or len(out) >= max_files:
        return out
    try:
        rows = cli("share-detail", "--url", url, "--pdir-fid", str(pdir_fid), "--size", "100")
    except (CliTimeout, CliFailed) as e:
        raise ShareReadError(str(e)) from e
    r = result_of(rows)
    if not r:
        raise ShareReadError("share-detail 无返回（可能超时/限流）")
    if r.get("code") != 0:
        code = r.get("code") or 0
        msg = (r.get("msg") or "unknown")[:80]
        SHARE_ERR[url] = (code, msg)
        if code in EXPIRED_CODES or any(k in msg for k in EXPIRED_MSG):
            raise ShareExpired(code, msg)
        raise ShareReadError("share-detail %s %s" % (code, msg))
    for it in ((r.get("data") or {}).get("files") or []):
        fn = it.get("filename") or ""
        if it.get("file_type") == "0":
            share_walk(url, it.get("fid"), depth + 1, out, max_files)
        elif fn.lower().endswith(VIDEO_EXT):
            out.append({"name": fn, "fid": it.get("fid"),
                        "token": it.get("share_fid_token"),
                        "size": it.get("size") or 0,
                        "path": (it.get("path") or fn)})
        if len(out) >= max_files:
            break
    return out


def plan_for(share_url, canon_name, canonical, dirindex, registry, title=None):
    """返回 plan dict：missing 集、已有集数、目标 fid、是否可执行"""
    try:
        return _plan_for(share_url, canon_name, canonical, dirindex, registry, title)
    except Exception as e:  # noqa: BLE001
        return {"share_url": share_url, "canonical": canon_name, "dir_fid": None,
                "dir_name": None, "missing": [], "existing": 0, "share_files": 0,
                "special": 0, "skipped": [], "ready": False,
                "reason": f"规划失败（{type(e).__name__}）"}


def _infer_season(season, default_season):
    """无季号文件 → 目录唯一季 / 当前最大季；都没有则 None（沿用纯集号语义）"""
    return default_season if season is None else season


def _season_pair(k, default_season):
    """把 ep_key 归一成 (season, ep)；special 返回 None"""
    if not k or k[0] not in ("se", "ep"):
        return None
    if k[0] == "se":
        return (k[1], k[2])
    return (_infer_season(None, default_season), k[1])


def _orig_file(by_name, item_name, files):
    """优先复用分享里的原始 file 对象（保留 fid/token/size/path）"""
    f = by_name.get(item_name)
    if f:
        return f
    return {"name": item_name, "size": 0}


def _upgrade_key(canon_name, season, ep):
    return "%s|%s|%d" % (canon_name, "N" if season is None else "S%d" % season, ep)


def _plan_for(share_url, canon_name, canonical, dirindex, registry, title=None):
    fid, dir_name = dirindex.fid_for(canon_name)
    plan = {"share_url": share_url, "canonical": canon_name, "dir_fid": fid, "mode": MODE,
            "dir_name": dir_name, "missing": [], "existing": 0,
            "share_files": 0, "special": 0, "skipped": [], "ready": bool(fid)}
    if getattr(dirindex, "error", None):
        plan["ready"] = False
        plan["read_failed"] = True
        plan["reason"] = "%s（下轮重试，不消耗事件）" % dirindex.error
        return plan
    if not fid:
        plan["reason"] = "规范目录不存在（新剧需先建目录并加入 canonical）"
        return plan
    errors_before = len(getattr(dirindex, "read_errors", []) or [])
    existing_items = dirindex.list_items(fid)
    errors_after = getattr(dirindex, "read_errors", []) or []
    if len(errors_after) > errors_before:
        # 目录没读全 → 已有集判定不可信，绝不能当成"缺集"盲目存一遍
        plan["ready"] = False
        plan["read_failed"] = True
        plan["reason"] = "目标目录读取失败（%s，下轮重试）" % errors_after[-1]
        return plan
    existing_names = [x["name"] for x in existing_items]
    existing_keys = set()       # 原始 ep_key，special 去重用
    existing_pairs = set()      # (season, ep)：跨季不再碰撞
    existing_eps = set()        # 纯集号（兼容打印/统计）
    existing_best = {}          # (season, ep) -> (score, name, size)
    explicit_seasons = set()
    for x in existing_items:
        k = ep_key(x["name"])
        if k and k[0] == "se":
            explicit_seasons.add(k[1])
    if len(explicit_seasons) == 1:
        default_season = next(iter(explicit_seasons))
    elif explicit_seasons:
        default_season = max(explicit_seasons)
    else:
        default_season = None
    plan["default_season"] = default_season
    max_by_season = {}
    for x in existing_items:
        k = ep_key(x["name"])
        if not k:
            continue
        if k[0] == "sp":
            existing_keys.add(k)
            continue
        season, e = _season_pair(k, default_season)
        existing_keys.add(k)
        existing_pairs.add((season, e))
        existing_eps.add(e)
        max_by_season[season] = max(max_by_season.get(season, 0), e)
        sc = quality_score(x)
        cur = existing_best.get((season, e))
        if cur is None or sc[0] > cur[0][0]:
            existing_best[(season, e)] = (sc, x["name"], x.get("size") or 0)
    plan["existing"] = len(existing_eps)
    plan["max_existing"] = max(existing_eps) if existing_eps else None
    plan["max_by_season"] = max_by_season
    SHARE_ERR.pop(share_url, None)
    # 读失败 ≠ 失效：超时/限流重试一次；仍失败标记 read_failed（不标 expired）
    files, read_err, expired_err = [], None, None
    for attempt in (0, 1):
        try:
            files = share_walk(share_url)
            if files:
                read_err = None
                break
            read_err = "空返回（可能超时/限流）"
        except ShareExpired as e:
            files, expired_err = [], e
            break
        except ShareReadError as e:
            files, read_err = [], str(e)
        if attempt == 0:
            time.sleep(3)                  # 读失败/空返回：重试一次再判定
    plan["share_files"] = len(files)
    # Telegram 标题里的"更新至N集"直接当集数锚点（用户口径：标题是真实的）
    try:
        plan["title_hint"] = parse_title_hint(title or "")
    except Exception:
        plan["title_hint"] = {}
    if expired_err is not None:
        plan["expired"] = True
        plan["ready"] = False
        plan["share_error"] = "%s %s" % (expired_err.code, expired_err.msg)
        plan["reason"] = "分享链接已失效/为空（%s）——TG 链接寿命短的已知风险" % plan["share_error"]
    elif read_err is not None:
        plan["read_failed"] = True
        plan["ready"] = False
        plan["reason"] = "分享内容读取失败（%s，非确认失效，下轮重试）" % read_err

    by_name = {f["name"]: f for f in files}
    # 1) 规则解析；解析不出的走 LLM 兜底（带缓存 + 置信度校验）
    parsed, unresolved = [], []
    for f in files:
        k = ep_key(f["name"])
        if k is None:
            unresolved.append(f)
        else:
            parsed.append((f, k))
    plan["llm_pending"] = []
    if unresolved:
        try:
            import llm_recognize
            res = llm_recognize.map_episodes(canon_name, [f["name"] for f in unresolved])
            mapping = res.get("mapping", {})
            plan["llm_called"] = len(unresolved)
            plan["llm_cached"] = bool(res.get("cached"))
            for f in unresolved:
                item = mapping.get(f["name"]) or {}
                if item.get("episode") and item.get("confidence", 0) >= 0.9:
                    ep = int(item["episode"])
                    season = _infer_season(item.get("season"), default_season)
                    k = ("se", int(season), ep) if season is not None else ("ep", ep)
                    parsed.append((f, k))
                    plan.setdefault("llm_mapped", []).append((f["name"], k, item.get("confidence")))
                else:
                    plan["llm_pending"].append({"name": f["name"],
                                                "episode": item.get("episode"),
                                                "confidence": item.get("confidence", 0)})
        except Exception as e:  # noqa: BLE001
            plan["llm_error"] = type(e).__name__
            plan["llm_pending"] = [{"name": f["name"], "episode": None, "confidence": 0}
                                   for f in unresolved]
    plan["skipped"] = [x["name"] for x in plan["llm_pending"]]

    # 1.5) 标题集数兜底：分享里只剩 1 个文件认不出，而标题明确写了"更新至N集"→ 就是这一集
    #      必须复用原始 file 对象（带 fid），否则 do_save 会 KeyError
    hint = plan.get("title_hint") or {}
    hint_season = _infer_season(hint.get("season"), default_season) if hint.get("episode") else None
    hint_pair = (hint_season, int(hint["episode"])) if hint.get("episode") else None
    _hint_only = plan["llm_pending"][0]["name"] if len(plan["llm_pending"]) == 1 else ""
    if (hint_pair and len(plan["llm_pending"]) == 1 and hint_pair not in existing_pairs
            and media_rules.is_episode_file(_hint_only)):
        # Review5：预告片/图片等不能被"更新至N集"的标题兜底成某一集（_hint_only 已过滤）
        only = plan["llm_pending"][0]
        orig = _orig_file(by_name, only["name"], files)
        k = ("se", int(hint_season), int(hint["episode"])) if hint_season is not None \
            else ("ep", int(hint["episode"]))
        parsed.append((orig, k))
        plan["llm_pending"] = []
        plan["skipped"] = []
        plan["title_hint_used"] = {"name": only["name"], "episode": hint["episode"],
                                   "season": hint_season, "raw": hint.get("raw", "")}

    # 1.6) 人工确认过的集（pending_apply.py 写入）：直接采用（同样复用原始 file 对象）
    if plan["llm_pending"]:
        try:
            fixes = load_json(RESOLUTIONS, {})
        except Exception:
            fixes = {}
        remain = []
        for item in plan["llm_pending"]:
            key = "%s|%s|%s" % (canon_name, share_url, item["name"])
            fix = fixes.get(key) or {}
            if fix.get("episode"):
                ep = int(fix["episode"])
                season = _infer_season(fix.get("season"), default_season)
                k = ("se", int(season), ep) if season is not None else ("ep", ep)
                parsed.append((_orig_file(by_name, item["name"], files), k))
                plan.setdefault("manual_fixed", []).append((item["name"], k))
            else:
                remain.append(item)
        if len(remain) != len(plan["llm_pending"]):
            plan["llm_pending"] = remain
            plan["skipped"] = [x["name"] for x in remain]

    # 2) 按集号去重 + 已有判定 + follow/backfill（全部按 (season, ep)）
    best = {}
    plan["upgrades"] = []
    plan["upgrade_deferred"] = []  # 本仓库不含 Jellyfin 播放检测，保留空列表兼容下游
    up_state = load_json(UPGRADES, {})
    applied_up = up_state.get("applied") or {}
    for f, k in parsed:
        if k[0] == "sp":
            if k in existing_keys:
                continue
            plan["special"] += 1
            continue
        season, e = _season_pair(k, default_season)
        if (season, e) in existing_pairs:
            # 已存在 → 只有"明显更好"才升级；正在看的先跳过（下一轮再试）
            if f["name"] in existing_names:
                continue
            sc_new = quality_score(f)
            old = existing_best.get((season, e))
            if old is None:
                continue
            sc_old, old_name, old_size = old
            if not upgrade_needed(f["name"], sc_new, old_name, sc_old, f.get("size") or 0, old_size):
                continue
            ukey = _upgrade_key(canon_name, season, e)
            # 兼容旧格式 "%s|%d"（上线前写入的 applied 记录），否则冷却期被重置
            last = int(applied_up.get(ukey) or applied_up.get("%s|%d" % (canon_name, e)) or 0)
            if last and now() - last < UPGRADE_COOLDOWN:
                continue
            norm_k = ("se", season, e) if season is not None else ("ep", e)
            if norm_k not in best or sc_new > quality_score(best[norm_k]):
                best[norm_k] = f
                plan["upgrades"].append({"episode": e, "season": season, "name": f["name"],
                                         "old_score": sc_old[0], "new_score": sc_new[0]})
            continue
        # follow：历史集只和「同一季」的最大集比较；新季从 E01 起不会被当历史
        same_season_max = max_by_season.get(season)
        if plan.get("mode", "follow") == "follow" and same_season_max is not None \
                and e <= same_season_max:
            plan["historical_skipped"] = plan.get("historical_skipped", 0) + 1
            continue
        norm_k = ("se", season, e) if season is not None else ("ep", e)
        if norm_k not in best or f["size"] > best[norm_k]["size"]:
            best[norm_k] = f

    def _sort_key(item):
        nk = item[0]
        if nk[0] == "se":
            return (0 if nk[1] is None else nk[1], nk[2])
        return (0, nk[1])
    plan["missing"] = [f for _, f in sorted(best.items(), key=_sort_key)]
    plan["mode"] = plan.get("mode", "follow")
    return plan


def do_save(plan):
    """调用官方 CLI saveas --fid-list --to-pdir-fid（真实目录）

    防御：缺 fid 的文件直接返回明确错误，绝不把 KeyError 抛到顶层（否则 offset 不推进，
    同一事件每 5 分钟复崩）。"""
    missing = plan.get("missing") or []
    if not missing or not plan.get("dir_fid"):
        return False, "无可存文件或目录未解析"
    bad = [f.get("name") or "?" for f in missing if not f.get("fid")]
    if bad:
        return False, "missing fid for %s" % ", ".join(bad[:3])
    fids = [f["fid"] for f in missing]
    try:
        rows = cli("saveas", "--url", plan["share_url"], "--fid-list", ",".join(fids),
                   "--to-pdir-fid", plan["dir_fid"], timeout=180)
    except (CliTimeout, CliFailed) as e:
        return False, "保存调用失败（%s）" % e
    r = result_of(rows)
    if not r:
        return False, "无返回（可能超时）"
    if r.get("code") == 0:
        return True, (r.get("data") or {}).get("save_path") or "ok"
    return False, f"{r.get('code')} {r.get('msg')}"


# ─────────────── 事件 offset 状态机（P0-A） ───────────────
MAX_EVENT_FAILS = 5


def compute_new_offset(task_ends, statuses, current_offset, file_end=None):
    """offset 只推进到「第一个失败任务之前」。

    - statuses 与 task_ends 一一对应；"ok"=成功或明确终态，"failed"=下轮要重试
    - 有失败 → 返回失败前最后一个任务的末尾（第一个任务就失败则原样不动）
    - 无失败 → 若未截断（file_end 给定）推进到 file_end，否则最后一个已处理任务末尾
    """
    last_ok = -1
    for i, st in enumerate(statuses):
        if st != "ok":
            break
        last_ok = i
    if last_ok < 0:
        return current_offset
    if last_ok == len(statuses) - 1 and file_end is not None:
        return file_end
    return task_ends[last_ok]


def _event_key(url, canon):
    return "%s|%s" % (url or "", canon or "")


def bump_failure(stats, key, reason):
    """同一事件连续失败计数。返回 (count, terminal)。

    连续失败 >= MAX_EVENT_FAILS 时标记终态（放行 offset）并只告警一次，
    避免一个坏事件永久堵住队列（毒丸）。
    """
    fe = stats.setdefault("failed_events", {})
    rec = fe.get(key) or {"count": 0, "first_ts": now()}
    rec["count"] = int(rec.get("count") or 0) + 1
    rec["last_ts"] = now()
    rec["reason"] = (reason or "")[:200]
    fe[key] = rec
    if rec["count"] >= MAX_EVENT_FAILS:
        stats.setdefault("terminal_events", {})[key] = {
            "ts": now(), "count": rec["count"], "reason": rec["reason"],
            "kind": "failed_too_many"}
        fe.pop(key, None)
        return rec["count"], True
    return rec["count"], False


def clear_failure(stats, key):
    (stats.get("failed_events") or {}).pop(key, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-events", action="store_true", help="消费 tg_events.jsonl")
    ap.add_argument("--share")
    ap.add_argument("--canonical")
    ap.add_argument("--apply", action="store_true", help="真正写网盘（默认 dry-run）")
    ap.add_argument("--max", type=int, default=25, help="单轮最多处理几个分享（剩下的下一轮继续，不会丢）")
    ap.add_argument("--from-scratch", action="store_true", help="从头重放全部事件（调试用）")
    ap.add_argument("--mode", choices=["follow", "backfill"], default="follow",
                    help="follow=只存比已有最大集更新的集；backfill=补历史缺集")
    args = ap.parse_args()
    global MODE
    MODE = args.mode

    canonical = load_json(CANONICAL, {})
    registry = load_json(REGISTRY, {})
    stats = load_json(STATS, {})
    saved_canons = set()
    if not canonical:
        print("⚠️ canonical.json 不存在，先跑 build_canonical.py")
        return 2
    dirindex = DirIndex(canonical)
    print(f"目录索引：自动转存 fid={'ok' if dirindex.auto_fid else 'MISSING'}，"
          f"剧目录 {len(dirindex.dirs)} 个")


    tasks = []
    event_ends = []
    offset = None
    file_end = None
    leftover = 0
    if args.share:
        if not args.canonical:
            print("用法：--share <url> --canonical <剧名>")
            return 2
        tasks.append((args.share, args.canonical, ""))
    elif args.from_events:
        seen = set()
        offset = 0 if args.from_scratch else int(stats.get("events_offset") or 0)
        task_ends = []
        file_end = offset
        try:
            with open(EVENTS) as f:
                f.seek(offset)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    file_end = f.tell()
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("type") not in ("would_save", "new_share"):
                        continue
                    key = (ev.get("share_url"), ev.get("canonical"))
                    if key in seen:
                        continue
                    seen.add(key)
                    tasks.append((ev.get("share_url"), ev.get("canonical"),
                                  ev.get("title") or ""))
                    task_ends.append(file_end)
        except FileNotFoundError:
            print("没有事件文件")
            return 0
        # 单轮只处理前 max 个；剩下的下一轮继续（更新日多个源同时进来时不会丢事件）
        leftover = max(0, len(tasks) - args.max)
        event_ends = task_ends[:args.max]
        tasks = tasks[:args.max]
        print(f"事件消费：从 offset {offset}，本轮读取 {len(tasks)} 个"
              + (f"（剩 {leftover} 个下轮继续）" if leftover else ""))
    else:
        print("需要 --share 或 --from-events")
        return 2
    print(f"待处理 {len(tasks)} 个分享{'（dry-run）' if not args.apply else '（会写网盘！）'}")

    total_saved = 0
    expired = 0
    results = []
    plans = []
    pending_new = []

    # 与 tasks 一一对应：ok=成功或明确终态（可提交 offset），failed=下轮重试
    statuses = ["ok"] * len(tasks)

    def _fail(idx, key, reason, prefix="", url=None):
        """记一次失败：未达上限 → 保留事件；达到上限 → 终态 + 告警一次

        dry-run 只打印，不写失败计数（避免沙盒 dry-run 把事件提前判死）。"""
        if args.apply:
            cnt, terminal = bump_failure(stats, key, reason)
        else:
            cnt, terminal = 0, False
        statuses[idx] = "ok" if terminal else "failed"
        if not args.apply:
            print(f"{prefix}⚠️ 失败（dry-run 不计数，offset 不推进）：{reason}")
            return
        if terminal:
            if url and url in registry:
                registry[url]["status"] = "failed_terminal"
            print(f"{prefix}⚠️ 连续失败 {cnt} 次，标记终态并放行 offset（不再重试）：{reason}")
        else:
            print(f"{prefix}⚠️ 失败（连续 {cnt}/{MAX_EVENT_FAILS} 次），offset 不推进，下轮重试")

    # 阶段 1：先完成全部规划（不写盘），再做"同剧同一集多源选优"
    for idx, (url, canon, ev_title) in enumerate(tasks):
        key = _event_key(url, canon)
        if canon not in canonical:
            _fail(idx, key, "canonical 不认识：%s" % canon, prefix=f"【{canon}】", url=url)
            continue
        plan = plan_for(url, canon, canonical, dirindex, registry, ev_title)
        plan["_canon"], plan["_url"], plan["_title"] = canon, url, ev_title
        plan["_idx"], plan["_key"] = idx, key
        registry.setdefault(url, {}).update({
            "canonical": canon, "dir_name": plan.get("dir_name"),
            "dir_fid": plan.get("dir_fid"), "last_check": now(),
            "share_files": plan["share_files"]})
        if plan.get("expired"):
            registry[url]["status"] = "expired"
            expired += 1
            clear_failure(stats, key)
            print(f"【{canon}】{url} → ⏭️ 链接已失效（{plan.get('share_error')}），跳过")
            continue
        if plan.get("read_failed"):
            plan["_failed"] = True
            _fail(idx, key, plan.get("reason") or "读取失败", prefix=f"【{canon}】{url} → ", url=url)
            plans.append(plan)
            continue
        plans.append(plan)

    select_sources(plans)

    # 阶段 1.5：跨路径账本去重（官方追更已入库的集，P1 不再重复存）
    for plan in plans:
        canon = plan["_canon"]
        dups = []
        if plan.get("missing"):
            done = ep_ledger.saved_pairs(canon)
            keep = []
            for f in plan["missing"]:
                pair = item_ep_key(plan, f)
                if pair and pair[1] is not None and pair in done:
                    dups.append(pair)
                    continue
                keep.append(f)
            if dups:
                plan["missing"] = keep
                plan["_ledger_dups"] = dups

    # 阶段 2：打印 + 写入
    for plan in plans:
        url, canon = plan["_url"], plan["_canon"]
        registry[url]["missing"] = len(plan["missing"])
        head = (f"【{canon}】{url} → {plan.get('dir_name') or '?'}："
                f"分享 {plan['share_files']} 文件 / 已有 {plan['existing']} 集 / "
                f"缺 {len(plan['missing'])} / special {plan['special']}")
        print(head)
        for f in plan["missing"][:6]:
            pair = item_ep_key(plan, f) or (None, None)
            if pair[1] is None:
                k = "?"
            elif pair[0] is not None:
                k = "S%02dE%02d" % (pair[0], pair[1])
            else:
                k = "第%d集" % pair[1]
            risk = codec_hint(f["name"])
            print(f"   + {k}  {f['name'][:70]}"
                  + (f"  [编码:{risk}，DirectPlay 需客户端支持]" if risk else ""))
        if plan.get("_ledger_dups"):
            print(f"   🔁 今日已由其它路径入库 {len(plan['_ledger_dups'])} 集，跳过重复转存")
        if plan.get("upgrades"):
            up_txt = "; ".join("第%d集 %d→%d（%s）" %
                               (u["episode"], u["old_score"], u["new_score"], u["name"][:40])
                               for u in plan["upgrades"][:3])
            print(f"   🔼 画质升级：{up_txt}")
        if plan.get("upgrade_deferred"):
            print(f"   ⏸️ {len(plan['upgrade_deferred'])} 集有更好画质，但你正在播放，播完再升级")
        if plan.get("_dropped_sources"):
            print(f"   🏆 同一集有多个源，已选最优；跳过较差版本 {len(plan['_dropped_sources'])} 个"
                  f"（例：{plan['_dropped_sources'][0][:40]}）")
        if plan.get("manual_fixed"):
            print(f"   🙋 人工确认：{[(n, '%s' % (k,)) for n, k in plan['manual_fixed']]}")
        if plan.get("title_hint_used"):
            th = plan["title_hint_used"]
            print(f"   📌 用标题集数兜底：{th['name']} → 第{th['episode']}集（来自标题“{th.get('raw','')}”）")
        if plan.get("llm_mapped"):
            print(f"   🤖 LLM 兜底认出 {len(plan['llm_mapped'])} 个："
                  f"{[(n, '%s' % (k,), c) for n, k, c in plan['llm_mapped'][:3]]}")
        if plan.get("llm_pending"):
            print(f"   ? 认不出 {len(plan['llm_pending'])} 个（已问 LLM，低置信）："
                  f"{[x['name'] for x in plan['llm_pending'][:3]]}")
        if plan.get("llm_error"):
            print(f"   ⚠️ LLM 兜底不可用：{plan['llm_error']}")
        if not plan.get("ready"):
            print(f"   ⏭️ 跳过：{plan.get('reason')}")
            if not plan.get("_failed"):        # 读失败已在阶段 1 计数，避免重复累加
                _fail(plan["_idx"], plan["_key"], plan.get("reason") or "未就绪",
                      prefix=f"【{canon}】", url=url)
            continue
        if args.apply and plan.get("llm_pending"):
            pending_new += record_pending(plan)
        if args.apply and plan.get("upgrade_deferred"):
            try:
                st_up = load_json(UPGRADES, {})
                d = st_up.setdefault("deferred", {})
                for x in plan["upgrade_deferred"]:
                    d[x["key"]] = x
                save_json(UPGRADES, st_up)
            except Exception:
                pass
        if not plan["missing"]:
            registry[url]["status"] = "ok"
            clear_failure(stats, plan["_key"])
            print("   ✅ 无缺集，不需要存")
            continue
        results.append(plan)
        if args.apply:
            ok, msg = do_save(plan)
            print(f"   {'✅ 已存入' if ok else '❌ 保存失败'}：{msg}")
            if ok:
                registry[url]["status"] = "ok"
                clear_failure(stats, plan["_key"])
                total_saved += len(plan["missing"])
                try:
                    pairs = [item_ep_key(plan, f) for f in plan["missing"]]
                    ep_ledger.record_saved(canon, [p for p in pairs if p])
                    saved_canons.add(canon)
                except Exception:  # noqa: BLE001
                    pass
                registry[url]["last_saved"] = now()
                registry[url]["saved_files"] = [f["name"] for f in plan["missing"]]
                if plan.get("upgrades"):
                    try:
                        st_up = load_json(UPGRADES, {})
                        ap = st_up.setdefault("applied", {})
                        for u in plan["upgrades"]:
                            ap[_upgrade_key(canon, u.get("season"), u["episode"])] = now()
                            ap.pop("%s|%d" % (canon, u["episode"]), None)   # 清掉旧格式键
                        u0 = plan["upgrades"][0]
                        st_up.setdefault("deferred", {}).pop(
                            _upgrade_key(canon, u0.get("season"), u0["episode"]), None)
                        save_json(UPGRADES, st_up)
                    except Exception:
                        pass
                # 存成功后清掉这些文件的待确认记录
                try:
                    pend = load_json(PENDING, {})
                    for f in plan["missing"]:
                        pid = _pending_id(canon, url, f["name"])
                        pend.get("items", {}).pop(pid, None)
                    json.dump(pend, open(PENDING + ".tmp", "w"), ensure_ascii=False, indent=1)
                    os.replace(PENDING + ".tmp", PENDING)
                except Exception:
                    pass
            else:
                _fail(plan["_idx"], plan["_key"], "保存失败：%s" % msg, prefix=f"【{canon}】", url=url)

    save_json(REGISTRY, registry)
    if args.apply and args.from_events:
        # 核心：offset 只推进到「第一个失败任务之前」；失败任务保留给下一轮重试
        if not statuses:
            new_offset = file_end if (file_end is not None and not leftover) else (offset or 0)
        else:
            new_offset = compute_new_offset(event_ends, statuses, offset or 0,
                                            None if leftover else file_end)
        stats["events_offset"] = new_offset
        print(f"事件 offset：{offset} → {new_offset}"
              + ("" if new_offset != offset else "（有失败，下轮重试）"))
    upgraded = sum(len(p.get("upgrades") or []) for p in results)
    stats.update({"ts": now(), "dry": not args.apply, "tasks": len(tasks),
                      "planned": sum(len(p["missing"]) for p in results),
                      "saved": total_saved, "expired": expired,
                      "pending_new": len(pending_new), "upgraded": upgraded})
    save_json(STATS, stats)
    failed_count = sum(1 for st in statuses if st != "ok")
    notify_canons = [c for c in saved_canons if not ep_ledger.notified_today(c)]
    if total_saved and not notify_canons and not failed_count and not pending_new and not expired:
        # 用户口径：同一部剧当天已通知过 → 后续入库静默（照常入库），审计写文件
        try:
            with open(STATE_DIR + "/p1_intake.log", "a") as f:
                f.write("%s quiet-notify saved=%d shows=%s\n"
                        % (time.strftime("%F %T"), total_saved, ",".join(sorted(saved_canons))))
        except OSError:
            pass
        print("\n本轮：计划 0 集，实际存 0 集")
        return 0
    print(f"\n本轮：计划 {sum(len(p['missing']) for p in results)} 集，实际存 {total_saved} 集")
    for c in notify_canons:
        if total_saved:
            ep_ledger.record_notified(c)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"❌ intake 异常：{e}")
        sys.exit(1)
