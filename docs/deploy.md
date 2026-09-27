# 部署

> 服务器地址与账号**不进仓库**。本文只写「什么在哪里、怎么验、出事了怎么找」。

## 1. 目录布局

| 路径 | 是什么 |
|---|---|
| `/opt/crb-notify` | 代码检出（只允许快进） |
| `/var/lib/crb-notify/workspace` | 账本、记录快照、产出（本项目自己的） |
| `/etc/systemd/system/crb-notify.service` | 生效的单元（权威副本在 `deploy/`） |
| `/var/lib/yuque-agent/workspace/<repo>/outbox/notify/pending/` | QQ 通知取件处（**桥会搬走**） |
| `/home/yuque/.crb-notify/env` | `CRBA_INTAKE_KEY` |

## 2. 依赖

```bash
# yqa：**不要再装一份** —— 它已经在 /opt/yuque-agent 里
sudo -u yuque env HOME=/home/yuque /usr/local/bin/uv run \
    --no-sync --project /opt/yuque-agent yqa version     # 验证能跑

# 本项目
sudo -u yuque env HOME=/home/yuque /usr/local/bin/uv sync --project /opt/crb-notify
```

单元里 `CRBA_YQA_BIN` 指着那个检出（`uv run --no-sync --project /opt/yuque-agent yqa`）——
**让两个服务共用同一份 yqa**，版本与文案不会漂。

## 3. 凭证

| 变量 | 放哪 | 说明 |
|---|---|---|
| `CRBA_INTAKE_KEY` | `/home/yuque/.crb-notify/env` | 投递密钥。**不是安全边界**（插件是油猴脚本，源码谁都能看），只是挡误投与扫描器 |
| `YQA_REPO` | `/home/yuque/.yuque/agent.env` | 已存在（`ghxd00/jsjysq`），决定 plan.json 在哪 |

## 4. 部署

```bash
cd /opt/crb-notify
sudo ./scripts/deploy.sh          # 取锁 / 干净检查 / 快进 / 跑测试 / 对齐单元 / 重启 / 验收
```

第一次引导：见仓库根 `README.md`，或照旧项目的做法（把代码送上去、`uv sync`、跑一次 `deploy.sh`）。

## 5. 验收

```bash
systemctl is-active crb-notify.service
journalctl -u crb-notify -n 20 --no-pager | grep "接收入口已开"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/healthz        # 200
```

**从外网**（8788 已放行）：

```bash
curl -s -X POST http://<地址>:8788/intake/records \
  -H 'Content-Type: text/plain' -H 'X-Intake-Key: <密钥>' \
  -d '{"records":[]}'
# 期望 {"ok": true, ..., "received": 0}
```

看账本与待投递 —— **`show` 要 `YQA_REPO`**（它靠这个找 `plan.json`），
而系统的环境在 `agent.env` 里，所以得像单元那样把环境带上（直接敲会报
「没有配置知识库」）：

```bash
sudo -u yuque sh -c 'set -a; . /home/yuque/.yuque/agent.env; set +a; \
  uv run --no-sync --project /opt/crb-notify crb-notify show'
```

## 6. 出事了怎么办

1. **先留证据**：`journalctl -u crb-notify`、上面的 `crb-notify show`、`git log`。
2. **通知没发出去**：先看 `crb-notify show` 里 `pending/` 有几条 —— 有就是桥没取走
   （那是桥的事，看 `journalctl -u qq-bridge`）；没有就是我们的判定或关联有问题。
3. **认不出的变多了**：`unmatched.json` 里 `why` 会写原因。多半是
   「学校改了状态文案」或「活动在语雀那边被改了标题」—— 都该改规则或人工处理，
   **不要改成静默丢弃**。
4. 结论写进提交信息或 `docs/`，不要只留在聊天里。

## 7. ⚠️ 试过但不成立的方向（**别重复试验**）

这份清单是从已归档的 `crb-agent` 带过来的。为了让「服务器直接调学校」成立，
下面每一项都实测过，**都不成立**：

| 候选 | 结果 |
|---|---|
| 换出口 IP（SSH 隧道走校园网） | 仍 403 ✗ |
| cookie 是否完整 | 实际发出去的 ehallapp cookie 齐全 ✗ |
| User-Agent / 各类 header | 与能通的那台机器完全一致 ✗ |
| TLS 版本（强制 1.2 / 1.3） | 都 403 ✗ |
| 浏览器 TLS 指纹（`curl_cffi` impersonate chrome/safari） | 都 403 ✗ |
| HTTP 客户端库（curl / httpx） | 都一样 ✗ |
| cookie 是否过期 | 同一份 auth 拷到能通的那台机器立刻 200 ✗ |

**结论**：请求必须从那台能过的机器发出。**别在服务器侧再试着调学校接口。**
