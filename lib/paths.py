#!/usr/bin/env python3
"""统一路径配置：原先散落在各脚本里的绝对路径全部集中在这里。

所有路径都可用环境变量覆盖，默认值相对用户主目录，不依赖特定机器布局：

| 环境变量      | 默认值                                  | 用途                                   |
| ------------- | --------------------------------------- | -------------------------------------- |
| TQM_ROOT      | ~/.telegram-quark-monitor                | 数据根目录（配置/状态/凭据都在下面）   |
| TQM_STATE     | $TQM_ROOT/state                          | 各种状态 json、事件队列、锁文件         |
| TQM_ENV       | $TQM_ROOT/env                            | 凭据目录（llm.env、tmdb.env、openlist.env） |
| TQM_CONFIG    | $TQM_ROOT/monitor_config.json            | TG 频道 / 关键字等监听配置              |
| TQM_QUARK_CLI | $TQM_ROOT/quarkclouddrive                | 夸克网盘 CLI（qas）所在目录              |
| TQM_PYTHON    | 当前解释器                               | 子进程调用自己时用的 Python             |
"""
import os
import sys

# 仓库内的固定目录（跟着代码走，不随环境变量变）
LIB_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LIB_DIR)
SCRIPT_DIR = os.path.join(REPO_ROOT, "pipeline")


def _dir(env_name: str, default: str) -> str:
    raw = os.environ.get(env_name) or default
    return os.path.abspath(os.path.expanduser(raw))


# 用户数据目录（可用环境变量覆盖）
PIPE_ROOT = _dir("TQM_ROOT", "~/.telegram-quark-monitor")
STATE_DIR = _dir("TQM_STATE", os.path.join(PIPE_ROOT, "state"))
ENV_DIR = _dir("TQM_ENV", os.path.join(PIPE_ROOT, "env"))

# 单个文件
CONFIG_FILE = os.environ.get("TQM_CONFIG") or os.path.join(PIPE_ROOT, "monitor_config.json")
QUARK_CLI_DIR = _dir("TQM_QUARK_CLI", os.path.join(PIPE_ROOT, "quarkclouddrive"))
PYTHON_BIN = os.environ.get("TQM_PYTHON") or sys.executable


def state_file(name: str) -> str:
    """状态目录下的某个文件路径（不会自动创建目录）。"""
    return os.path.join(STATE_DIR, name)


def env_file(name: str) -> str:
    """凭据目录下的某个文件路径。"""
    return os.path.join(ENV_DIR, name)


def ensure_dirs() -> None:
    """确保状态目录和凭据目录存在。"""
    for d in (STATE_DIR, ENV_DIR, os.path.dirname(CONFIG_FILE) or "."):
        os.makedirs(d, exist_ok=True)
