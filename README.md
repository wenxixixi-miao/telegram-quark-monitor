# Telegram 夸克自动转存流水线（v2 架构）

监听 Telegram 频道里的夸克网盘分享，自动判断「缺哪一集」，把缺的集增量转存进自己的网盘目录。

本分支是**新架构（v2 pipeline）**，与 `main` 分支那套单体监听器完全是两种思路。旧版仍在 `main` 分支可用。

## 与 main 分支的区别

| | `main`（旧版 v1.2） | `v2-pipeline`（本分支） |
| --- | --- | --- |
| 形态 | 单体 `telegram_monitor.py`：监听、关键字判断、去重、调夸克转存全在一个进程里 | 拆成「发现器 + 统一入库器」，中间用 append-only 事件队列连接 |
| 入库策略 | 关键字命中就整包转存 | 解析集号后**只补缺集**；同一集有多个来源时按画质选优 |
| 重放 | 状态都在进程内存里，不好重放 | 事件落在 `tg_events.jsonl`，可 `--from-scratch` 完整重放，落盘幂等（已有的集不会被重复存） |
| 安全 | 直接调用保存 | 发现器和入库器**默认都是 dry-run**，加 `--live` / `--apply` 才真写网盘；整条链路没有任何删除操作 |
| 通知 | 通知队列 + 外部脚本轮询 | 入库器自己判断该不该说话（严格静默：只有存了、失败、或出现新待确认集才输出） |

一句话：旧版是「看到就存」，新版是「先记账，再按账本地补」。

## 数据流

```
Telegram 频道
     │  监听新消息 / 回扫历史
     ▼
tg_discover.py ──写──►  $TQM_STATE/tg_events.jsonl   (append-only 事件队列)
                              │
                              ▼
                         intake.py ──写──► 夸克网盘 /自动转存/<剧名>/
                              │
                              ├─ 解析季号集号（episode_parse）
                              ├─ 同集多源选优（quality）
                              ├─ 与目标目录已有集求差集 → 只存缺的
                              └─ 认不准的集 → 待确认队列（pending_apply.py）
```

分工原则：**发现器只发现——不落盘、不删除、不退频道；入库器是唯一写网盘的入口。**

## 目录结构

```
pipeline/
  tg_discover.py        TG 发现器：监听/回扫频道，命中剧名库就写事件
  intake.py             统一入库器：消费事件，按缺集增量转存（唯一写网盘入口）
  episode_parse.py      集号/季号解析（支持中英日多种文件名写法）
  quality.py            画质打分（分辨率/编码/大小）
  resolve_series.py     剧名归一与别名解析
  add_show.py           把一部剧加进剧名库（别名、目录名、TMDB id）
  build_canonical.py    从网盘已有目录反推生成剧名库
  pending_apply.py      待确认集处理与通知
  tg_search.py          搜索频道历史消息
  airing_audit.py       播出进度审计
  guard_replay.py       重放护栏：防止历史消息被当成新集重存
  llm_recognize.py      文件名认不出时用 LLM 兜底识别集号
  llm_title_resolve.py  LLM 辅助解析剧名
  newshow.py            新剧候选检查
lib/
  paths.py              所有路径与环境变量的唯一来源
  ep_ledger.py          按剧记账（今天是否已通知过等）
  media_rules.py        媒体命名 / 画质规则
  mediaenv.py           OpenList 凭据读取
tests/                  单元测试（不依赖真实频道与网盘，用临时目录跑）
```

## 快速开始

**依赖**：Python 3.10+、Telegram API 凭据（`api_id` / `api_hash`，在 my.telegram.org 申请）、夸克网盘 CLI（`qas`）；LLM 与 TMDB 凭据可选（认不准的文件名和元数据补全才用）。

```bash
git clone -b v2-pipeline https://github.com/wenxixixi-miao/telegram-quark-monitor.git
cd telegram-quark-monitor
mkdir -p ~/.telegram-quark-monitor/{state,env}
```

**1. 配置监听**

```bash
cp monitor_config.example.json ~/.telegram-quark-monitor/monitor_config.json
chmod 600 ~/.telegram-quark-monitor/monitor_config.json
# 填 api_id / api_hash / phone / channels；先保持 dry_run=true
```

**2. 准备剧名库**

`canonical.json` 决定「哪些剧会被认领」，以及每部剧的别名和网盘目录名：

```bash
cp canonical.example.json ~/.telegram-quark-monitor/state/canonical.json
cp aliases.example.json ~/.telegram-quark-monitor/state/aliases.json    # build_canonical.py 用的别名表
python3 pipeline/add_show.py --name "剧名" --alias "别名" --apply   # 一部一部加
python3 pipeline/build_canonical.py                                # 或从网盘现成目录反推
```

**3. 先 dry-run 看一遍**

```bash
# 发现器默认 dry-run：只写 would_save 事件，不落盘
python3 pipeline/tg_discover.py --once --backlog-minutes 180

# 入库器默认 dry-run：只打印「计划存哪些集」
python3 pipeline/intake.py --from-events --dry
```

确认计划无误再放行：

```bash
python3 pipeline/tg_discover.py --daemon --live      # 常驻监听（也可以每分钟 --once）
python3 pipeline/intake.py --from-events --apply     # 真正写网盘
```

**4. 定时跑（cron 示例）**

```cron
* * * * * cd /path/to/telegram-quark-monitor && python3 pipeline/tg_discover.py --once >> ~/.telegram-quark-monitor/state/tg_discover.log 2>&1
*/5 * * * * cd /path/to/telegram-quark-monitor && python3 pipeline/intake.py --from-events --apply >> ~/.telegram-quark-monitor/state/intake.log 2>&1
```

## 路径与环境变量

所有路径集中在 `lib/paths.py`，默认都在 `~/.telegram-quark-monitor/` 下，全部可用环境变量覆盖：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `TQM_ROOT` | `~/.telegram-quark-monitor` | 数据根目录 |
| `TQM_STATE` | `$TQM_ROOT/state` | 状态文件、事件队列、锁 |
| `TQM_ENV` | `$TQM_ROOT/env` | 凭据（LLM / TMDB / OpenList） |
| `TQM_CONFIG` | `$TQM_ROOT/monitor_config.json` | 频道与关键字配置 |
| `TQM_QUARK_CLI` | `$TQM_ROOT/quarkclouddrive` | 夸克 CLI 所在目录 |
| `TQM_PYTHON` | 当前解释器 | 子进程回调自己时用的 Python |

凭据放 `env/` 下，格式见 `env.example`（建议 600 权限）。

## 跑测试

```bash
for f in tests/test_*.py; do python3 "$f"; done
```

8 个用例里 7 个通过，全部不需要真实频道或网盘（用临时目录 + 假目录对象）。
`tests/test_review2_state.py` 的端到端用例与当前保存路径不同步，目前是失败的——上游同一份代码同样不通过，不影响其它功能。

## 本仓库**不包含**的部分

- **Jellyfin / strm 媒体库同步**：原作者那套「入库后重建 strm、刷新媒体库、按播放状态延后画质升级」的私有链路没有包含进来。本仓库做到「文件进夸克网盘规范目录」为止，之后怎么接播放器请自行处理。
- 真实频道名、在追剧单（`canonical.json` 里是示例）、cookie 与任何凭据。

## 致谢与协议

- 夸克网盘转存能力沿用 [Cp0204/quark-auto-save](https://github.com/Cp0204/quark-auto-save)（AGPL-3.0），本仓库通过其 CLI 调用，不包含其代码。
- 本分支代码部分：MIT，见 [LICENSE](LICENSE)。
- 使用时请遵守 Telegram 与夸克网盘的服务条款，风险自负。
