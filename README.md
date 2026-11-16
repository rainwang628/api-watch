# api-watch

中转站渠道监控 + 自动切换网关。给 Codex / Claude Code 一个固定入口，背后按渠道健康度自动选路，
同时持续用可验证的探针检查「后台模型有没有被换成弱模型」。

纯 Python 标准库，无第三方依赖，不需要建虚拟环境。

## 解决什么问题

- 一家中转站按上游供应商拆 Key（Claude 一个、ChatGPT 一个），任何一个渠道抖动，客户端就报错；
- 便宜的中转站可能在高峰期静默降级模型，日常使用很难察觉；
- 想知道余量、延迟、可用率，但不想天天登后台。

## 三层能力

| 层 | 做什么 | 实现 |
|---|---|---|
| 健康监控 | 每个渠道定时发最小请求，记延迟、成功率、错误 | `watchd.py` |
| 能力验真 | 固定探针题，答案由本地暴力计算得出，答错即标记降级 | `probes.py` |
| 自动切换 | 本地网关按优先级转发，失败自动退到下一个渠道并冷却 | `gateway.py` |

能力探针不依赖官方基线：题目答案由脚本自己算（孪生素数对计数、含 7 的数字个数、整除求和），
模型答错说明能力明显弱于目标型号，比「和官方对比」更省事也更能抓住降智。

## 目录

```
api-watch/
├── core.py              配置 / SQLite / 上游请求
├── probes.py            健康探针、能力探针、余额查询
├── watchd.py            定时采集 + 面板 HTTP 服务
├── gateway.py           本地转发网关（自动切换）
├── panel/index.html     面板前端（手机自适应）
├── config.example.json  配置样例
├── .env.example         Key 样例
└── data/watch.db        SQLite（自动创建）
```

## 快速开始

```bash
cd ~/rain/projs/services/api-watch
cp config.example.json config.json
cp .env.example .env
# 编辑 config.json：填 base_url、模型名、面板口令
# 编辑 .env：填各渠道 Key
```

启动监控（前台，先看是否通）：

```bash
python3 watchd.py --once      # 只跑一轮，打印汇总 JSON
python3 watchd.py             # 常驻：定时探针 + 面板
```

启动网关（另开一个终端）：

```bash
python3 gateway.py
```

## 常驻与开机自启（launchd）

两个进程都由 launchd 托管，plist 在 `~/Library/LaunchAgents/`：

| Label | 脚本 | 端口 | 日志 |
|---|---|---|---|
| `com.rain.apiwatch.watchd` | `watchd.py` | `0.0.0.0:8787` | `data/watchd.log` |
| `com.rain.apiwatch.gateway` | `gateway.py` | `127.0.0.1:8788` | `data/gateway.log` |

plist 里的 `ProgramArguments`、`WorkingDirectory`、日志路径都是绝对路径，**移动项目目录必须同步改**，否则服务起不来。

```bash
launchctl bootout   gui/$(id -u)/com.rain.apiwatch.watchd     # 停
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rain.apiwatch.watchd.plist   # 起
launchctl print     gui/$(id -u)/com.rain.apiwatch.watchd     # 看状态
```

注意（2026-11-15 实测）：从 agent 会话（DSH 沙箱）里执行 `launchctl bootstrap` 会返回 `Bootstrap failed: 5: Input/output error`，此时 job **没有**加载——拿最小 plist 复现也是同样结果，紧接 `launchctl print` 报 `Could not find service`。换成无沙箱权限（`danger-full-access`）执行一次即返回 0 并注册成功，不需要重启。`print`／`kickstart`／`bootout` 在沙箱内都正常，所以加载后用 `kickstart -k` 即可；端口若被临时进程占着，job 会一直 `Address already in use` 崩溃重启。原因与证据见 `~/rain/memory/dsh-sandbox-launchctl-bootstrap.md`。

`data/*.log` 为空不代表没起来：两个脚本只在启动时 `print` 一行，stdout 写文件时是块缓冲，进程不退出就不落盘。判断是否在跑看 `launchctl print` 的 `state`／`pid` 和面板 HTTP，不要看日志大小（`watchd.err.log` 里旧的 `Errno 48` traceback 是端口冲突期的残留）。

## 接入客户端

Codex（`~/.codex/config.toml`）：

```toml
model_provider = "apiwatch"
model = "gpt-5.3-codex"

[model_providers.apiwatch]
name = "api-watch"
base_url = "http://127.0.0.1:8788/v1"
wire_api = "responses"
env_key = "APIWATCH_DUMMY_KEY"   # 网关自己注入真实 Key，这里随便填
```

Claude Code：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8788"
export ANTHROPIC_AUTH_TOKEN="dummy"   # 同上，网关会替换成真实 Key
```

## 手机上看面板

面板默认绑定 `0.0.0.0:8787`，同一 Wi-Fi 下手机浏览器打开：

```
http://<Mac 内网 IP>:8787/?token=<panel.token>
```

查内网 IP：`ipconfig getifaddr en0`。

两件事必须注意：

- `panel.token` 一定要改，别用样例值。面板只读，但会暴露你的渠道名和用量。
- 网关（8788）只绑 `127.0.0.1`，不对局域网暴露，避免有人借你的 Key 刷额度。

## 探针什么时候跑

探针只在**你正在用**的时候跑，因为每次探针都是真实的计费请求：

| 条件 | 行为 |
|---|---|
| `probe.quiet_hours` 时间段（默认 `[0, 8]`） | 完全不探针 |
| 最近 `probe.idle_stop_sec`（默认 7200 秒）内经网关有请求 | 按 `interval_sec` 正常探针 |
| 超过 2 小时没有经网关的请求 | 暂停探针，每 `idle_check_sec`（默认 300 秒）看一次有没有新请求 |
| 又出现新请求 | 立刻恢复探针 |

判据是 `relay_log` 里最近一次请求时间，不是面板访问、也不是探针本身。
暂停/恢复都会写一条 `info` 事件进 `events` 表，面板事件流里能看到，所以「面板数据不动」不等于服务挂了。

改这段逻辑前备份在 `watchd.py.bak-20261116-0810`；回滚把备份覆盖回 `watchd.py`，再 `launchctl kickstart -k gui/$(id -u)/com.rain.apiwatch.watchd`。想完全关掉门控就把 `probe.idle_stop_sec` 设为 `0`。

## 自动切换的边界

- 只在**同一族模型**之间切换：Claude 挂了切另一个 Claude 渠道，不会把 Claude 的请求丢给 GPT，
  否则回答口径全变。
- 冷却期内的渠道排到最后，过了冷却自动回到正常优先级。
- 4xx（除 429）视为请求本身有问题，不重试，避免把客户端的错误放大成多渠道计费。

## 已知局限

- 余额查询是 best-effort：依赖中转站是否开放 one-api 体系的 `/v1/dashboard/billing/*`，
  读不到就显示 `—`，不影响健康监控。
- 能力探针是抽样，抓得住「整体降级」，抓不住「按请求特征分流掺水」。
- 没有官方基线，因此判据是「绝对正确性 + 历史自我对比」，不是「与官方逐字一致」。
- 面板和网关都在本机跑，Mac 关机/睡眠时不工作。

## 安全

- Key 从环境变量或 `.env` 读取；面板口令要改掉样例值。
- 别让中转站的响应直接进有凭据的环境（视频里提到的注入风险），涉及密钥、私钥的操作走官方通道。

## 2026-11-15 接 AIGoCode 实测记录

第一次真正接上 AIGoCode（`https://api.aigocode.dev`）时踩到三个坑，都已修在代码里：

| 现象 | 原因 | 处理 |
|---|---|---|
| 探针全部 `error code: 1010`，curl 却正常 | 上游前挂 Cloudflare，`Python-urllib/3.x` 的 UA 被判坏客户端 | `core._headers` 统一带 `user-agent`，默认 `curl/8.7.1`，可用渠道字段 `user_agent` 覆盖 |
| 经网关请求 OpenAI 端点回 `404 page not found` | 渠道 `base_url` 已含 `/v1`，客户端又带 `/v1`，拼成 `/v1/v1/...` | `gateway.split_target` 去掉重复前缀 |
| Codex 长任务在流中途断开 | 转发沿用了探针的 60s 超时 | 网关单独用 `gateway.timeout_sec`（示例配置 600） |

模型 ID 也换过一轮：`gpt-5.3-codex`、`claude-opus-4-6` 已不在当前分组里，现用 `gpt-6.1-sol` / `gpt-6-astra` / `claude-opus-5-5`。

**分组限制**：AIGoCode 的 Key 按分组授权。「仅限 Claude Code 官方客户端」的分组只能从真正的 Claude Code 客户端调用，
其它入口（curl、网关探针、DSH）一律 403，报文案就是这句。要用在通用 API 上，得建「可外接」分组的 Key。
目前 `.env` 里那把 Claude Key 就属于受限分组，所以该渠道健康检查恒失败，属预期。
