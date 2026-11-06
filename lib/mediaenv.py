#!/usr/bin/env python3
"""媒体链路统一凭据读取（P0 安全整改：口令不再写在脚本里）

凭据文件：$TQM_ENV/openlist.env（权限 600）
  OPENLIST_URL=http://127.0.0.1:5244
  OPENLIST_USER=admin
  OPENLIST_PASS=<轮换后的强口令>
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "lib")))
from paths import ENV_DIR  # noqa: E402
import json
import os
import urllib.request

ENV = ENV_DIR + "/openlist.env"


def read_env(path=ENV):
    conf = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    except OSError:
        pass
    return conf


def openlist_creds():
    c = read_env()
    return (c.get("OPENLIST_URL", "http://127.0.0.1:5244"),
            c.get("OPENLIST_USER", "admin"),
            c.get("OPENLIST_PASS", ""))


def openlist_login(timeout=20):
    """返回 (base_url, token)"""
    base, user, pwd = openlist_creds()
    if not pwd:
        raise RuntimeError("缺少 OpenList 凭据：请检查 $TQM_ENV/openlist.env")
    body = json.dumps({"username": user, "password": pwd}).encode()
    req = urllib.request.Request(base + "/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    token = (data.get("data") or {}).get("token")
    if not token:
        raise RuntimeError("OpenList 登录失败：%s" % (data.get("message") or data))
    return base, token
