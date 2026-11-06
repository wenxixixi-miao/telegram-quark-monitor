#!/usr/bin/env python3
"""LLM 兜底识别（DeepSeek）：规则解析不出来时，用模型认剧名 / 认集号

原则：
  - 只在规则失败时调用；结果缓存，同一输入只花钱一次
  - 输出必须经过本地校验（范围/唯一性/置信度），低置信度只提示不落盘
  - 不阻塞主链路：intake 里同步调用（几十个名字、2~5 秒）；标题兜底走独立脚本/队列

配置：$TQM_ENV/llm.env（600）
  DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
缓存：$TQM_STATE/name_llm_cache.json
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import ENV_DIR, STATE_DIR  # noqa: E402
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

ENV = ENV_DIR + "/llm.env"
CANON_CACHE = {}
try:
    CANON_CACHE = json.load(open(STATE_DIR + "/canonical.json"))
except Exception:
    pass
CACHE = STATE_DIR + "/name_llm_cache.json"
CACHE_MAX = 8000
HIGH_CONF = 0.9


def env_conf():
    conf = {}
    try:
        for line in open(ENV):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    except OSError:
        pass
    return conf


def available():
    c = env_conf()
    return bool(c.get("DEEPSEEK_API_KEY"))


def _chat(messages, max_tokens=900, temperature=0, timeout=40):
    c = env_conf()
    key = c.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY（$TQM_ENV/llm.env）")
    base = c.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = c.get("DEEPSEEK_MODEL", "deepseek-flash")
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens,
                       "temperature": temperature}).encode()
    req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.load(resp)
    return (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def _json_from_text(text):
    """从模型输出里抠出第一个完整 JSON 对象"""
    text = text.strip()
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


# ───────────────────────── cache ─────────────────────────
def load_cache():
    try:
        return json.load(open(CACHE))
    except Exception:
        return {"items": {}}


def save_cache(cache):
    items = cache.get("items", {})
    if len(items) > CACHE_MAX:
        # 丢最旧的一半
        keep = sorted(items.items(), key=lambda kv: kv[1].get("ts", 0))[-(CACHE_MAX // 2):]
        cache["items"] = dict(keep)
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


CACHE_VER = "v2"


def ckey(kind, payload):
    raw = CACHE_VER + "|" + kind + "|" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def cached_call(kind, payload, fn):
    cache = load_cache()
    k = ckey(kind, payload)
    hit = cache.get("items", {}).get(k)
    if hit:
        return hit["result"], True
    result = fn()
    cache.setdefault("items", {})[k] = {"ts": int(time.time()), "result": result}
    save_cache(cache)
    return result, False


# ───────────────────────── 标题 → 剧名 ─────────────────────────
def _sim(a, b):
    """字符相似度（0~1），用来校验模型给的 decoded_title 是否真像候选剧名"""
    import difflib
    na = re.sub(r"[^\w\u4e00-\u9fff]", "", (a or "").lower())
    nb = re.sub(r"[^\w\u4e00-\u9fff]", "", (b or "").lower())
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 0.9
    return difflib.SequenceMatcher(None, na, nb).ratio()


def classify_title(title, filenames, canon_names):
    """返回 {'canonical': str|None, 'confidence': 0~1, 'reason': str}"""
    payload = {"title": title, "files": sorted(filenames)[:12],
               "show_count": len(canon_names)}
    def run():
        cand = "\n".join("- " + n for n in canon_names)
        prompt = (
            "你是中文影视资源识别助手。下面这条 Telegram 频道消息的标题和文件名，"
            "是从正在追更的剧集列表里来的，但标题做过防和谐处理（谐音、拆字、乱码、缩写）。\n\n"
            "候选剧名列表：\n%s\n\n"
            "消息标题：%s\n"
            "分享内文件名样例：%s\n\n"
            "判断它属于候选列表里的哪一部剧；拿不准就返回 unknown，不要猜。\n"
            "只输出一个 JSON 对象：{\"canonical\":\"候选里的原名或unknown\",\"decoded_title\":\"你还原出的剧名\",\"confidence\":0到1的小数,\"reason\":\"简短理由\"}"
            % (cand, title, json.dumps(filenames[:12], ensure_ascii=False))
        )
        try:
            out = _chat([{"role": "user", "content": prompt}], max_tokens=300)
            d = _json_from_text(out) or {}
        except Exception as e:  # noqa: BLE001
            return {"canonical": None, "confidence": 0, "reason": "LLM调用失败:%s" % type(e).__name__}
        name = (d.get("canonical") or "").strip()
        decoded = (d.get("decoded_title") or "").strip()
        conf = 0.0
        try:
            conf = max(0.0, min(1.0, float(d.get("confidence") or 0)))
        except (TypeError, ValueError):
            conf = 0.0
        if name not in canon_names:
            if name.lower() in ("unknown", "其他", "无"):
                return {"canonical": None, "confidence": conf, "reason": d.get("reason") or "unknown"}
            # 模型给了不在列表里的名字 → 降置信度当 unknown
            return {"canonical": None, "confidence": min(conf, 0.3),
                    "reason": "模型返回不在候选列表: %s" % name[:20]}
        # 二次校验：模型还原的剧名必须和候选名/候选的别名像，否则降置信度（防"张冠李戴"）
        cand_meta = CANON_CACHE.get(name) or {}
        sims = [_sim(decoded, x) for x in [name, cand_meta.get("title", "")] + list(cand_meta.get("aliases") or [])]
        best_sim = max(sims or [0])
        if decoded and best_sim < 0.5:
            return {"canonical": name, "confidence": min(conf, 0.5),
                    "reason": "还原名 %r 与候选 %r 不像（sim=%.2f）→ 降置信度" % (decoded[:20], name, best_sim)}
        return {"canonical": name, "confidence": conf,
                "reason": (d.get("reason") or "") + ("｜还原:" + decoded if decoded else "")}
    result, hit = cached_call("title", payload, run)
    if hit:
        result = dict(result)
        result["cached"] = True
    return result


# ───────────────────────── 文件名 → 集号 ─────────────────────────
EP_RE = re.compile(r"^[\s\W]*(?:s(\d{1,2})\s*[._ ]?e?)?\s*(\d{1,4})", re.I)


def map_episodes(show_name, filenames, max_chars=4000):
    """返回 {'mapping': {filename: {'season':int|None,'episode':int|None,'confidence':float}}, 'cached':bool}"""
    names = sorted(set(filenames))[:30]
    payload = {"show": show_name, "files": names}
    if not names:
        return {"mapping": {}, "cached": False}

    def run():
        prompt = (
            "你在帮一个 Jellyfin 媒体库识别中文剧集文件。剧名：%s\n"
            "下面这些文件名被分享者改过名（防和谐：中文数字、字母混数字、谐音、缺集号）。\n"
            "请判断每个文件对应第几季第几集。无法确定的给 null，不要编。\n"
            "常见例子：1百 = 100；1O1 或 l0l = 101；久 = 9；S01E163 = 第1季第163集；EP67 = 第67集。\n\n"
            "文件名列表：\n%s\n\n"
            "只输出一个 JSON 对象：{\"mapping\":{\"文件名\":{\"season\":整数或null,\"episode\":整数或null,\"confidence\":0到1的小数}}}"
            % (show_name, json.dumps(names, ensure_ascii=False))
        )
        try:
            out = _chat([{"role": "user", "content": prompt}], max_tokens=1500)
            d = _json_from_text(out) or {}
        except Exception as e:  # noqa: BLE001
            return {"mapping": {}, "error": "LLM调用失败:%s" % type(e).__name__}
        raw = d.get("mapping") or {}
        mapping = {}
        used_eps = set()
        for fn in names:
            item = raw.get(fn) or {}
            season = item.get("season")
            episode = item.get("episode")
            try:
                season = int(season) if season not in (None, "", "null") else None
            except (TypeError, ValueError):
                season = None
            try:
                episode = int(episode) if episode not in (None, "", "null") else None
            except (TypeError, ValueError):
                episode = None
            conf = 0.0
            try:
                conf = max(0.0, min(1.0, float(item.get("confidence") or 0)))
            except (TypeError, ValueError):
                conf = 0.0
            # 本地校验
            if episode is not None and not (0 < episode < 3000):
                episode, conf = None, 0.0
            if season is not None and not (0 <= season <= 30):
                season, conf = None, 0.0
            if episode is not None:
                if episode in used_eps:
                    conf = min(conf, 0.5)     # 同批重复集号 → 降置信度
                used_eps.add(episode)
            mapping[fn] = {"season": season, "episode": episode, "confidence": conf}
        return {"mapping": mapping}

    result, hit = cached_call("episodes", {**payload, "n": len(names)}, run)
    if hit:
        result = dict(result)
        result["cached"] = True
    return result


def high_conf(item):
    return (item or {}).get("confidence", 0) >= HIGH_CONF and (item or {}).get("episode")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--title")
    ap.add_argument("--files", nargs="*", default=[])
    ap.add_argument("--show")
    ap.add_argument("--filenames", nargs="*", default=[])
    args = ap.parse_args()
    if args.title:
        try:
            canon = json.load(open(STATE_DIR + "/canonical.json"))
        except Exception:
            canon = {}
        print(json.dumps(classify_title(args.title, args.files, list(canon)), ensure_ascii=False, indent=1))
    elif args.show:
        print(json.dumps(map_episodes(args.show, args.filenames), ensure_ascii=False, indent=1))
    else:
        ap.print_help()
