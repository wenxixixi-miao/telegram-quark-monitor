#!/usr/bin/env python3
"""媒体文件分类（官方追更 / P1 intake / plan_strm 共用，Review5）

口径（用户 2026-11-01）：预告片/花絮/图片等"非正片"不能当成新集转存，更不能入库。
统一分类避免各脚本各写一份 JUNK 列表、互相漏判。

classify(name) 返回：
  "video_episode"  正片视频（可入库，按集号解析）
  "special"        剧场版/OVA/特别篇/特典等特典候选（只能进 Season 00，需显式映射）
  "noise"          预告片/花絮/PV/teaser/图片/压缩包/广告等，不转存、不入库
"""
import os
import re

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".rmvb", ".flv",
             ".wmv", ".mov", ".mpg", ".webm", ".m4v")

# 名称里出现就算"非正片"（中英文都算）
NOISE_WORDS = (
    "预告", "預告", "花絮", "彩蛋", "特辑", "特輯", "访谈", "采访", "幕后", "解说",
    "盘点", "混剪", "片段", "抢先看", "扫一扫", "二维码", "教程", "招募", "海报",
    "壁纸", "截图", "字幕组招", "sample", "preview", "teaser", "trailer",
    "screenshot", "poster", "wallpaper",
)
# 特典类（保留，但只能进 Season 00）
SPECIAL_WORDS = ("剧场版", "劇場版", "电影版", "電影版", "ova", "oad", "特别篇", "特別篇",
                 "番外", "特典", "总集篇", "总集編", "sp篇", "sp ")

_PV_RE = re.compile(r"(?i)(?:^|[.\s_\-\[\]()（）【】])(?:pv)(?:$|[.\s_\-\[\]()（）【】])")
_VIDEO_RE = re.compile(r"(?i)(" + "|".join(re.escape(e) for e in VIDEO_EXT) + r")$")
_JUNK_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic",
             ".pdf", ".zip", ".rar", ".7z", ".txt", ".apk", ".exe", ".torrent",
             ".srt", ".ass", ".ssa", ".nfo", ".json")


def ext(name):
    return os.path.splitext(str(name or ""))[-1].lower()


def is_video(name):
    return bool(_VIDEO_RE.search(str(name or "").strip()))


def has_noise_word(name):
    low = str(name or "").lower()
    for w in NOISE_WORDS:
        if w.lower() in low:
            return True
    return bool(_PV_RE.search(str(name or "")))


def is_noise(name):
    """非正片：非视频扩展名，或文件名带预告/PV/图片/教程等词。"""
    if not is_video(name):
        return True
    return has_noise_word(name)


def is_special(name):
    low = str(name or "").lower()
    return any(w.lower() in low for w in SPECIAL_WORDS)


def classify(name):
    if is_noise(name):
        return "noise"
    if is_special(name):
        return "special"
    return "video_episode"


def is_episode_file(name):
    """只有正片视频算"新集"（特典不算追更集，走 specials 手工映射）。"""
    return classify(name) == "video_episode"


def split(files):
    """files: 文件名列表或 dict(name=...) 列表；返回 (episodes, specials, noise)。

    dict 保留原对象（官方追更需要 fid）。
    """
    eps, specs, noise = [], [], []
    for f in files or []:
        name = f.get("name") if isinstance(f, dict) else f
        c = classify(name)
        if c == "video_episode":
            eps.append(f)
        elif c == "special":
            specs.append(f)
        else:
            noise.append(f)
    return eps, specs, noise
