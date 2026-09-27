# crb-notify — 教室借用结果的**接收器**

> 浏览器插件（同学的「活动工作台」）在用户自己的浏览器里读教室借用申请列表，
> POST 到这里；本服务判定「哪些审批结束了」，然后把结果分发出去：
>
> ```
> 浏览器插件（校园网 / 用户浏览器）
>     │ POST /intake/records
>     ▼
> crb-notify（8788）── 判定 ── 账本（只追加）
>     │
>     ├─▶ QQ 通知：写进 yqa 的 outbox/notify/pending/   ← 桥取件发出去
>     └─▶ 语雀《审批结果》：调 yqa refresh-approval
> ```
>
> **契约见 [`docs/intake-contract.md`](docs/intake-contract.md)** —— 插件那边照它写就行。
> 为什么是这个形态见 [`docs/design.md`](docs/design.md)。

## 它**不**做什么（重要）

**它不调学校系统。** 这一条是拿实测换来的：服务器（机房 IP）调不动办事大厅的接口，
而且**换出口也没用** —— 同一份登录态、同一个出口 IP，从本机发出 200、
从服务器发出 403（七项候选都试过，清单在
[`docs/deploy.md`](docs/deploy.md) §「试过但不成立的方向」）。

所以「读申请列表」这件事交给插件（它跑在用户的浏览器里，天然满足），
本服务只负责**收下、判定、分发**。这也正是它从 `crb-agent` 独立出来的原因：
那一版里「和 LLM 聊天 + 扫码登录 + 提交申请」的部分都依赖「服务器能调学校」，
不成立；**只有「判定 + 出通知」这一半是成立的**。

## 快速开始

```bash
uv sync
export YQA_REPO='<group>/<repo>'        # 语雀知识库（决定 plan.json 在哪）
export CRBA_INTAKE_KEY='<一串随机>'
uv run crb-notify serve                  # → http://127.0.0.1:8788/intake/records
```

投一条试试：

```bash
curl -s -X POST http://127.0.0.1:8788/intake/records \
  -H 'Content-Type: text/plain' -H 'X-Intake-Key: <那串随机>' \
  -d '{"records":[{"sqbh":"7e75e389f83d4aac95bdcc28b5521041","statusCode":"99",
       "status":"已通过","raw":{"FJ":"新教404","KSRQ":"2026-10-01",
       "KSJC_DISPLAY":"第7节(16:10-17:00)","JSJC_DISPLAY":"第8节(17:10-18:00)",
       "XXXQDM_DISPLAY":"仙林校区","JYYTMS":"新生见面会","SHZT":"99","SHBZ":"通过"}}]}'
```

## 命令

```bash
crb-notify serve      # 起接收服务（8788）
crb-notify show       # 看收到的记录、账本、待投递
crb-notify forget     # 忘掉几条（联调期的假数据）；没 --yes 只列清单
crb-notify deliver    # 补发（幂等：已投过/内容没变的不会重复处理）
crb-notify version
```

## 部署

见 [`docs/deploy.md`](docs/deploy.md)。

## License

[MIT](LICENSE) © 2026 NOVA Contributors
