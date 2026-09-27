"""配置解析。

与上一版的区别：**不再有「调学校」的那些东西**（登录态、crb 路径、LLM key、密钥门）。
这个服务只做一件事：**收插件投来的申请记录 → 判定 → 出通知**。

凭证依旧只从环境变量读；本模块永不写、永不打印。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8788
"""8787 是 yqa 的公开下载口；本项目用 8788（安全组已放行）。"""


class ConfigError(RuntimeError):
    """配置缺失/不合法。"""


def _first(*values: str | None) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    port: int
    host: str

    #: 我们自己的工作区：记录快照 + 账本 + 产出。**不往别人的工作区里写**
    #: （唯一的例外是 QQ 通知 —— 那是契约允许的取件目录）。
    workspace: Path

    #: `yqa` 的工作区（**只读**：找 plan.json 与归档，把记录关联回语雀的 applicationId）。
    yuque_workspace: Path

    #: 语雀知识库 `<group>/<repo>`。
    yqa_repo: str

    #: 调 `yqa refresh-approval` 的命令（可以是带参数的，用 shlex 拆）。
    yqa_bin: str

    #: 投递时校验的共享密钥。**这不是安全边界** —— 插件是油猴脚本，密钥必然公开。
    #: 它只是「防呆」：挡住误投和扫描器。真正的防线是「只收数据 + 幂等 + 无副作用」。
    intake_key: str

    #: 两条出路各开各的。
    notify_qq: bool
    notify_yuque: bool

    @property
    def repo_slug(self) -> str:
        """``ghxd00/jsjysq`` → ``ghxd00_jsjysq``（与上游 ``yqa`` 的目录约定一致）。"""
        return self.yqa_repo.replace("/", "_").strip()

    def outbox(self) -> Path:
        """`yqa` 的产出目录（只读）。"""
        if not self.repo_slug:
            raise ConfigError(
                "推不出语雀产出目录：没有配置 YQA_REPO=<group>/<repo>。\n"
                "它决定 plan.json 在哪（用来把学校记录关联回语雀的申请）。"
            )
        return self.yuque_workspace / self.repo_slug / "outbox"

    def approval_dir(self) -> Path:
        """我们自己的产物：账本、记录快照、通知文档。"""
        return self.workspace / "approval"

    def notify_pending_dir(self) -> Path:
        """QQ 桥取件的目录（**唯一一处往 yqa 工作区写**，而且是契约允许的）。"""
        return self.outbox() / "notify" / "pending"

    def notify_done_dir(self) -> Path:
        """桥投递成功后会搬到这里 —— 用来判断「这条是不是已经发过了」。"""
        return self.outbox() / "notify" / "done"

    @classmethod
    def from_env(cls) -> Settings:
        repo = _first(os.environ.get("YQA_REPO"))
        if not repo:
            raise ConfigError(
                "没有配置知识库：设 YQA_REPO=<group>/<repo>（就是语雀地址里那两段）。\n"
                "它决定 plan.json 在哪 —— 没有它就无法把学校记录关联回语雀的申请。"
            )
        return cls(
            port=int(_first(os.environ.get("CRBN_PORT")) or DEFAULT_PORT),
            host=_first(os.environ.get("CRBN_HOST")) or "0.0.0.0",
            workspace=_env_path("CRBN_WORKSPACE", Path("/var/lib/crb-notify/workspace")),
            yuque_workspace=_env_path(
                "CRBN_YUQUE_WORKSPACE", Path("/var/lib/yuque-agent/workspace")
            ),
            yqa_repo=repo,
            yqa_bin=_first(os.environ.get("CRBN_YQA_BIN"), "yqa"),
            intake_key=_first(os.environ.get("CRBN_INTAKE_KEY")),
            notify_qq=_env_flag("CRBN_NOTIFY_QQ", True),
            notify_yuque=_env_flag("CRBN_NOTIFY_YUQUE", True),
        )
