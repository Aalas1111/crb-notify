# AGENTS.md —— 在这个仓库 / 这台机器上干活的规矩

> 动手前先读 [`docs/design.md`](docs/design.md)（为什么是接收器而不是 agent）
> 与 [`docs/intake-contract.md`](docs/intake-contract.md)（对插件的契约）。

## 0. 这个项目干什么、不干什么

**干**：收浏览器插件投来的申请记录 → 判定「哪些审批结束了」→ 出通知（QQ + 语雀）。

**不干**：**不调学校系统**。这一条是拿实测换来的（`docs/deploy.md` §7 有七项
「试过但不成立」的清单）。看到「让服务器自己去查一下申请列表」之类的需求，
先回去读那份清单 —— 那条路已经走死了。

## 1. 五条规矩

1. **只收数据，不接受指令。** 这个端口对公网开着，而插件的密钥必然公开
   （油猴脚本，源码谁都能看）。所以端点里**不许**出现删除、触发、回调、
   转发之类的入口。最坏情况必须是「收到垃圾数据」。
   **清理数据是 `crb-notify forget`（在机器上敲的 CLI），不是接口**
   —— 别因为「联调要清假数据」就给它开一条 HTTP 路由。
2. **幂等是硬要求。** 插件每次都投全量是最省心的用法，所以账本按 `SQBH` 比对、
   `notificationId` **不含时间戳**、写 `pending/` 前先查 `pending/` 与 `done/`。
   改这三处之前先想清楚「重复投一次会怎样」。
3. **不落 PII。** 手机号在 `intake.normalize_record()` 的白名单外，**收到即丢**。
   服务器是明文 HTTP，能少存就少存。别为了「以后可能有用」把它加进白名单。
4. **认不出就说认不出。** 判定是查表 + 正则（没有 LLM）。认不出的进
   `unmatched.json` 等人看 —— **绝不猜**，也**绝不静默丢弃**（静默丢弃 =
   社员以为没事、实际没通知）。
5. **不改桥的代码。** QQ 通知是**写文件**（`outbox/notify/pending/`），
   那是 `yqa/docs/handoff.md` §3 冻结的目录协议。要加字段就加在 `extra` 里。

## 2. 这台机器上的铁律（沿用旧项目，都是事故换来的）

* **不许在生产机上裸跑测试。** 走 `scripts/deploy.sh`（它把 `HOME` 关进临时目录）。
  测试里不许假设「默认路径下没有东西」，更不许真的去删凭证。
* **只有 `crb-notify.service` 一个单元**，权威副本在 `deploy/`。改单元 =
  改 `deploy/*.service` → `install` 到 `/etc` → `daemon-reload` → 同步 `docs/deploy.md`。
* **不许 `nohup` / `setsid` / `&` 起常驻进程。**
* **8787 是 `yuque-agent-plan` 的下载口**（公开、无鉴权），别去占；本项目用 8788。
* 部署只走 `scripts/deploy.sh`：取 flock、只允许快进、验收、记 `ops.log`。
  一次只有一个写者。

## 3. 上游与提交

* 上游 = `Aalas1111/crb-notify`。干活顺序：`git fetch` → 开 feature 分支 → 提交 →
  推到自己的 fork → 请上游合并。**服务器上永远不许 rebase / 手工 merge。**
* **不许在生产机上写代码、直接 `git commit`。**
* 提交信息第一行说清「改了什么」，正文说「为什么 + 依据」（现象 / 日志 / 测试结果）。
  代理提交时写明是代理执行。
* 收工前跟踪的文件必须干净（`git status --porcelain --untracked-files=no` 为空）。

## 4. 交付前自检

1. `uv run ruff check . && uv run ruff format --check .`
2. `uv run pytest`
3. 碰了 `docs/intake-contract.md` → **同步告诉插件那一侧**（那是对外契约）
4. 碰了 `deploy/*.service` → 同步 `docs/deploy.md`
5. 部署后：单元 `systemctl is-active`、日志里有 `接收入口已开`、`/healthz` 200、
   `ops.log` 多一行
