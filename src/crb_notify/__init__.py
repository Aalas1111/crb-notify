"""crb-notify —— 教室借用申请结果的接收器。

**它不调学校系统**，也不再是「agent」：浏览器插件（同学的「活动工作台」）
在用户自己的浏览器里读申请列表，投递到这里；本服务判定「哪些审批结束了」，
然后把结果分发出去：

* **QQ 通知** → 写进 `yqa` 的 `outbox/notify/pending/`（桥会取件发出去）；
* **语雀《审批结果》** → 调 `yqa refresh-approval` 让那篇文档重生。

设计契约见 `docs/intake-contract.md`；为什么是这个形态见 `docs/design.md`。
"""

__version__ = "0.1.0"
