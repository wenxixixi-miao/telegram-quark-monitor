#!/usr/bin/env python3
"""通用集号解析器（P1）

输入任意来源文件名（TG 乱码/拆字/EP/第N集/纯数字/方括号），输出：
  {"season": int|None, "episode": int|None, "special": bool, "confidence": float, "why": str}

只依赖标准库；供 save_new_episodes / plan_strm / strm 生成共用，避免三套逻辑各自为政。
"""
import re
import unicodedata

JUNK = re.compile(r"(?i)(sample|预告|花絮|海报|fanart|poster|trailer|teaser|截图|字幕组招募|招募)")
QUALITY = re.compile(
    r"(?i)(4k\w*|8k\w*|\d{3,4}p\w*|hdr\d*\w*|sdr|web[-\s]?dl|webrip|bluray|blu-ray|remux|bdrip|hdtv|"
    r"hevc|h\.?26[45]|x26[45]|avc|av01|10bit|8bit|aac|dts|flac|ddp?\d?(?:\.\d)?|ac3|truehd|"
    r"hq|高码率|超清|官方中字|中字|内封字幕|简繁英字幕|杜比|立体环绕声|纯净版|臻彩)")
SPECIAL = re.compile(r"(?i)(剧场版|剧场|ova|oad|sp\d*|特别篇|番外|特典|\bpv\b|\bmv\b|预告片)")
# 这些"数字"不是集号：60fps / 5.1 声道 / v2 版本 / Part 2 分段 —— 必须在兜底前剔除
FPS = re.compile(r"(?i)(?<!\d)\d{2,3}\s*fps(?!\w)")
AUDIO_CH = re.compile(r"(?i)(?<!\d)\d\.\d(?!\d)")
VERSION = re.compile(r"(?i)(?<![a-z0-9])v\d{1,2}(?!\d)")
PART = re.compile(r"(?i)(?<![a-z0-9])part\s*\.?\s*\d{1,2}(?!\d)")
# 重复副本后缀：(1) / (2) / （1）……不是集号。只认 1-2 位数字（年份 4 位且已先剔除），
# 且必须位于名字末尾，避免把 "第5集 (2025)" 之类误删。
DUP_SUFFIX = re.compile(r"\s*[\(（]\s*\d{1,2}\s*[\)）]\s*$")
CN_NUM = str.maketrans({"〇": "0", "零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
                        "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
                        "久": "9", "１": "1", "２": "2", "３": "3", "４": "4", "５": "5",
                        "６": "6", "７": "7", "８": "8", "９": "9", "０": "0"})
VIDEO_EXT = re.compile(r"\.(mp4|mkv|avi|ts|m2ts|rmvb|flv|wmv|mov|mpg|m4v|zip|rar|7z)$", re.I)


DIGIT_CONF = set("0OoIl|")
CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s):
    """中文数字（支持 百/千/十 简化写法）：一百=100、1百零1=101、一百六十三=163"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in CN_DIGIT:
            num = CN_DIGIT[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        elif ch == "千":
            section += (num or 1) * 1000
            num = 0
        elif ch.isdigit():
            num = num * 10 + int(ch)
        elif ch in "零〇":
            continue
        else:
            return None
    return total + section + num


def _fix_digit_confusion(s):
    """把 1O1 / l0l / IOI 这类字母数字混淆还原成数字（只动"整段都像数字"的 token）"""
    def repl(m):
        tok = m.group(0)
        if len(tok) < 2:
            return tok
        if not any(c.isdigit() for c in tok) and len(tok) > 3:
            return tok
        return tok.replace("O", "0").replace("o", "0").replace("I", "1") \
                  .replace("l", "1").replace("|", "1")
    return re.sub(r"[0-9OoIl|]{2,6}", repl, s)


def _cn_episode(s):
    """从字符串里提取中文数字/百千形式的集号，返回 (ep, why) 或 (None, None)"""
    # 1) 阿拉伯数字 + 百/千：1百、2百零3、1千2
    m = re.search(r"(?<!\d)(\d{1,3})\s*百\s*(?:零|〇)?\s*(\d{1,3})?", s)
    if m:
        return int(m.group(1)) * 100 + int(m.group(2) or 0), "阿拉伯+百"
    m = re.search(r"(?<!\d)(\d{1,2})\s*千\s*(?:零|〇)?\s*(\d{1,3})?", s)
    if m:
        return int(m.group(1)) * 1000 + int(m.group(2) or 0), "阿拉伯+千"
    # 2) 纯中文数字 + 百/千/十：一百、一百零一、一百六十三
    m = re.search(r"第?\s*([一二三四五六七八九十]{1,4})\s*[集话話]", s)
    if m:
        v = _cn_to_int(m.group(1))
        if v and 0 < v < 3000:
            return v, "中文集号"
    m = re.search(r"([一二两三四五六七八九十]{1,5})\s*(百|千)(?:\s*零?\s*([一二三四五六七八九十]{0,4}))?", s)
    if m:
        base = _cn_to_int(m.group(1) + m.group(2))
        tail = _cn_to_int(m.group(3)) if m.group(3) else 0
        if base is not None:
            return base + (tail or 0), "中文+百千"
    return None, None


def _strip_quality(name):
    b = name
    for _ in range(3):
        b2 = VIDEO_EXT.sub("", b)
        if b2 == b:
            break
        b = b2
    b = QUALITY.sub(" ", b)
    b = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ", b)      # 去年份
    b = FPS.sub(" ", b)                                      # 60fps
    b = AUDIO_CH.sub(" ", b)                                 # 5.1 / 7.1 声道
    b = VERSION.sub(" ", b)                                  # v2 / V2 版本号
    b = PART.sub(" ", b)                                     # Part 2 分段
    while True:                                              # 重复副本后缀 283 4K(1)
        b2 = DUP_SUFFIX.sub("", b)
        if b2 == b:
            break
        b = b2
    return b


def parse_episode(filename):
    """返回 dict(season, episode, special, confidence, why)"""
    if not filename:
        return {"season": None, "episode": None, "special": False, "confidence": 0, "why": "空"}
    base = unicodedata.normalize("NFKC", filename)
    if JUNK.search(base):
        return {"season": None, "episode": None, "special": False, "confidence": 0, "why": "junk"}
    if SPECIAL.search(base):
        sm = re.search(r"(?i)(?:sp(?:e)?|特别篇|番外|特典)\s*[._\- ]*0*(\d{1,3})", base)
        ep = int(sm.group(1)) if sm else None
        return {"season": 0 if ep else None, "episode": ep, "special": True,
                "confidence": 0.9 if ep else 0.8, "why": "special" + ("+集号" if ep else "")}
    b = _strip_quality(base)
    b = _fix_digit_confusion(b)

    cn_ep, cn_why = _cn_episode(b)
    if cn_ep and 0 < cn_ep < 3000:
        return {"season": None, "episode": cn_ep, "special": False,
                "confidence": 0.9, "why": cn_why}

    b = b.translate(CN_NUM)          # 久→9、〇→0、四→4 等；放在数字拼接/模式匹配之前
    m = re.search(r"(?i)\bs(\d{1,2})\s*[._ ]?e(?:p)?\s*0*(\d{1,4})(?!\d)", b)
    if m:
        s, e = int(m.group(1)), int(m.group(2))
        if 0 < e < 3000:
            return {"season": s, "episode": e, "special": False, "confidence": 0.98, "why": "SxxExx"}
    # 明确集号标记（EP / 第N集 / [N]）优先于"末尾数字"兜底：
    # 否则「第28集 Part 2」「第28集 60fps」会被兜底成 2 / 60
    for pat, why, conf in (
        (r"(?i)\bEP?\.?\s*0*(\d{1,4})(?!\d)", "EPxx", 0.95),
        (r"第\s*0*(\d{1,4})\s*[集话話]", "第N集", 0.95),
        (r"\[0*(\d{1,4})(?:v\d)?\]", "方括号", 0.85),
    ):
        mm = re.search(pat, b)
        if mm:
            e = int(mm.group(1))
            if 0 < e < 3000:
                return {"season": None, "episode": e, "special": False,
                        "confidence": conf, "why": why}
    groups = re.findall(r"\d{1,4}", b)
    if len(groups) >= 2 and all(len(g) == 1 for g in groups) and len(groups) <= 4:
        v = int("".join(groups))
        if 0 < v < 3000:
            return {"season": None, "episode": v, "special": False,
                    "confidence": 0.55, "why": "数字拼接"}
    if len(groups) >= 2 and 0 < int(groups[-1]) < 3000:
        return {"season": None, "episode": int(groups[-1]), "special": False,
                "confidence": 0.7, "why": "末尾数字(多数字)"}
    for pat, why, conf in (
        (r"^\s*0*(\d{1,4})(?=[\s._\-]|$)", "开头数字", 0.82),
        (r"[\s._\-]0*(\d{1,4})(?=[\s._\-]|$)", "分隔数字", 0.75),
    ):
        mm = re.search(pat, b)
        if mm:
            e = int(mm.group(1))
            if 0 < e < 3000:
                return {"season": None, "episode": e, "special": False,
                        "confidence": conf, "why": why}
    # 谐音/全角（如 "久"→9、"一六三"→163）
    t = b.translate(CN_NUM)
    digits = re.findall(r"\d", t)
    if 1 <= len(digits) <= 4:
        v = int("".join(digits))
        if 0 < v < 3000:
            return {"season": None, "episode": v, "special": False,
                    "confidence": 0.55, "why": "数字拼接"}
    return {"season": None, "episode": None, "special": False, "confidence": 0, "why": "无法识别"}


def ep_key(filename):
    """用于去重的集键：优先 (S,E)，否则 ('ep', N)，special 用去掉质量标记的整名"""
    p = parse_episode(filename)
    if p.get("special"):
        b = _strip_quality(unicodedata.normalize("NFKC", filename))
        return ("sp", re.sub(r"\s+", "", b).lower()[:60])
    if p.get("episode"):
        if p.get("season") is not None:
            return ("se", p["season"], p["episode"])
        return ("ep", p["episode"])
    return None


if __name__ == "__main__":
    for n in ["S01E163.4K-GM.mp4", "EP101.4K.mp4", "第01集.mp4", "1！8！3.mp4",
              "244 4K.mp4.zip", "示例剧 (2025) 190 4K.mp4", "示例剧 第163话 4K预告片.mp4",
              "z", "1@8&4.mp4", "11.【4K_Super】终物语 13.mkv"]:
        print(n, "->", parse_episode(n))


def parse_title_hint(title):
    """从 Telegram 标题里提取"更新到第几集"（用户口径：标题是真实标题，可直接用）
    支持：更新至101集 / 更至EP217 / 第164集 / 全24集 / S04E13 / EP177
    返回 {'episode': int|None, 'season': int|None, 'raw': str}
    """
    if not title:
        return {"episode": None, "season": None, "raw": ""}
    t = unicodedata.normalize("NFKC", title)
    m = re.search(r"(?i)\bs(\d{1,2})\s*[._ ]?e(?:p)?\s*0*(\d{1,4})", t)
    if m:
        return {"episode": int(m.group(2)), "season": int(m.group(1)), "raw": m.group(0)}
    for pat in (r"更新至\s*0*(\d{1,4})\s*[集话話]",
                r"更至\s*(?:EP?\s*)?0*(\d{1,4})\s*[集话話]?",
                r"更新\s*0*(\d{1,4})\s*[集话話]",
                r"第\s*0*(\d{1,4})\s*[集话話]",
                r"全\s*0*(\d{1,4})\s*[集话話]",
                r"EP?\s*0*(\d{1,4})(?!\d)",
                r"0*(\d{1,4})\s*[集话話]"):
        mm = re.search(pat, t)
        if mm:
            e = int(mm.group(1))
            if 0 < e < 3000:
                return {"episode": e, "season": None, "raw": mm.group(0)}
    return {"episode": None, "season": None, "raw": ""}
