#!/usr/bin/env python3
"""统一的画质打分（intake 与 plan_strm 共用）

用户口径（2026-11-01）：
  - 分辨率优先：4K > 1440P > 1080P > 720P
  - HDR 在夸克压缩后色彩不准 → HDR 扣分，不再加分；SDR 略加分
  - 10bit / remux / HEVC 小幅加分，体积只做平分项
"""
import re


def quality_score(f):
    """入参 dict：{"name": 文件名, "size": 字节}；返回可比较的 tuple"""
    name = (f.get("name") or "").lower()
    size = f.get("size") or 0
    score = 0
    if re.search(r"(2160p|\b4k\b|uhd|8k)", name):
        score += 1000
    elif "1440" in name or re.search(r"\b2k\b", name):
        score += 900
    elif "1080" in name:
        score += 800
    elif "720" in name:
        score += 600
    elif "480" in name:
        score += 400
    else:
        score += 300
    # HDR：夸克压缩后色彩不准确 → 扣分；显式 SDR 版本加分
    if re.search(r"(hdr|dolby[.\s]?vision|\bdv\b)", name):
        score -= 30
    if re.search(r"(^|[.\s_-])sdr([.\s_-]|$)", name):
        score += 20
    if "10bit" in name or "10bits" in name:
        score += 10
    if re.search(r"(hevc|h\.?265|x265)", name):
        score += 20
    if re.search(r"(av1|av01)", name):
        score += 25
    if "remux" in name:
        score += 60
    if re.search(r"(bluray|blu-ray)", name):
        score += 30
    score += min(30, int(size / (1024 ** 3)))       # 体积平分项
    return (score, size)
