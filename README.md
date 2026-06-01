# Telegram 夸克自动转存监听

监听 Telegram 频道消息，自动匹配关键字并转存夸克网盘。

## 🙏 致谢

本项目基于 [Cp0204/quark-auto-save](https://github.com/Cp0204/quark-auto-save) 扩展开发。

原项目提供夸克网盘自动转存核心功能，本项目在其基础上增加了 Telegram 频道实时监听、关键字过滤、去重等能力。

> 原项目协议: AGPL-3.0 | 本项目协议: MIT（本项目代码部分）

## 功能

- 📡 实时监听 Telegram 频道新消息
- 🔑 关键字过滤，只转存匹配的资源
- 🔗 自动提取夸克网盘链接并转存
- ⏭️ 同名去重：同一关键字 N 天内已转存过则跳过
- 🔧 自动修正非视频后缀（大文件重命名为 .mp4）
- 🔍 支持搜索频道历史消息
- 📢 转存结果通知队列（可对接 Hermes Agent 推送）
- 🛡️ 失败重试：转存失败的消息不会标记为已处理

## 安装

```bash
# 克隆项目
git clone https://github.com/wenxixixi-miao/telegram-quark-monitor.git
cd telegram-quark-monitor

# 安装依赖
pip install telethon
```

## 配置

1. 复制配置文件：
```bash
cp monitor_config.example.json monitor_config.json
```

2. 编辑 `monitor_config.json`，填写你的配置：

```json
{
    "api_id": 你的Telegram_API_ID,
    "api_hash": "你的Telegram_API_HASH",
    "phone": "+86你的手机号",
    "channels": ["Quark_Movies"],
    "save_path": "/自动转存",
    "keywords": ["斗破苍穹", "完美世界"],
    "dedup_days": 5
}
```

### 获取 Telegram API 凭据

1. 访问 https://my.telegram.org
2. 登录你的 Telegram 账号
3. 进入 "API development tools"
4. 创建应用，获取 `api_id` 和 `api_hash`

## 使用

```bash
python telegram_monitor.py
```

首次运行需要输入 Telegram 验证码，之后 session 会自动保存。

## 搜索历史消息

创建 `search_command.txt` 触发搜索：

```bash
# 搜索最近500条消息
echo "limit=500" > search_command.txt

# 搜索指定关键字
echo "keywords=斗破苍穹,完美世界" > search_command.txt
```

结果会写入 `search_result.json`。

## 文件说明

| 文件 | 说明 |
|------|------|
| `telegram_monitor.py` | 主监听脚本 |
| `monitor_config.json` | 配置文件（不提交） |
| `quark_monitor.session` | Telegram 登录 session（不提交） |
| `processed_messages.json` | 已处理消息记录 |
| `keyword_dedup.json` | 关键字去重时间戳 |
| `notify_queue.json` | 通知队列 |
| `monitor.log` | 运行日志 |

## 对接 Hermes Agent

配合 `check_notify.py` 可实现自动推送通知到微信/Telegram：

```python
#!/usr/bin/env python3
"""检查未发送通知并输出"""
import json, sys, os

QUEUE_FILE = "notify_queue.json"
if not os.path.exists(QUEUE_FILE):
    sys.exit(0)

with open(QUEUE_FILE, "r", encoding="utf-8") as f:
    items = json.load(f)

unsent = [item for item in items if not item.get("sent", True)]
if not unsent:
    sys.exit(0)

lines = ["📦 **夸克转存通知**\n"]
for item in unsent:
    lines.append(f"[{item.get('time', '?')}] {item.get('title', '')}")
    detail = item.get("detail", "")
    if detail:
        lines.append(f"  {detail}")
    lines.append("")

print("\n".join(lines).strip())

for item in items:
    if not item.get("sent", True):
        item["sent"] = True

with open(QUEUE_FILE, "w", encoding="utf-8") as f:
    json.dump(items, f, ensure_ascii=False, indent=2)
```

然后在 Hermes cron 中配置定时检查：

```yaml
- name: quark-monitor-notify
  schedule: "every 2m"
  script: check_notify.py
  no_agent: true
```

## License

MIT License - 详见 [LICENSE](LICENSE)

本项目基于 [Cp0204/quark-auto-save](https://github.com/Cp0204/quark-auto-save)（AGPL-3.0）扩展开发。
