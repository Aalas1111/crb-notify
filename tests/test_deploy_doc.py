"""守卫：单元、部署脚本、pyproject 三者不许漂。

这里每一条都对应一次上机踩到的坑。它们**不动运行时行为**，只盯住
「改名字/重构时最容易漏的那几处」。
"""

from __future__ import annotations

import re
from pathlib import Path

from crb_notify.config import Settings

ROOT = Path(__file__).resolve().parents[1]
UNIT = (ROOT / "deploy" / "crb-notify.service").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
DEPLOY = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

_CODE = "\n".join(
    line for line in DEPLOY.splitlines() if line.strip() and not line.lstrip().startswith("#")
)

#: 单元里**真正生效**的行（注释不算 —— 注释里提到某个变量名不算「设了它」）
_UNIT_CODE = "\n".join(
    line for line in UNIT.splitlines() if line.strip() and not line.lstrip().startswith("#")
)


def test_exec_start_uses_a_command_declared_in_pyproject():
    """写错的表现：服务反复重启，日志里只有一句 `Failed to spawn: crba`。

    实测踩到 —— 换项目名时 sed 漏了 ExecStart，而验收只报「不是 active」，
    原因还得翻日志才知道。
    """
    declared = set(re.findall(r'^([\w-]+)\s*=\s*"', PYPROJECT, re.M))
    match = re.search(r"^ExecStart=.*?uv run --no-sync (\S+)", UNIT, re.M)
    assert match, "单元里没有认得出的 ExecStart"
    assert match.group(1) in declared, (
        f"ExecStart 用的是 {match.group(1)!r}，但 pyproject 里没有这个命令（有 {sorted(declared)}）"
    )


def test_unit_never_references_the_old_project_name():
    """旧名字（`crb-agent` / `crba` / `CRBA_`）不该出现在单元里 —— 改名字时最容易漏这儿。"""
    for line in UNIT.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith(("Description=", "Documentation=")):
            continue
        assert "crba " not in stripped, f"单元里还有旧命令名：{stripped}"
        assert "crb-agent" not in stripped, f"单元里还有旧项目名：{stripped}"
        assert "CRBA_" not in stripped, f"单元里还有旧变量前缀：{stripped}"


def test_the_retired_crba_name_is_gone_from_the_shipped_code():
    """`CRBA` 是 crb-agent 时代的缩写，现在叫 **CRBN**。

    半改不改是最坏的结果：`config.py` 读 `CRBN_INTAKE_KEY` 而 env 文件里还写着
    `CRBA_INTAKE_KEY` → 密钥读成空 → 端点**静默变成谁都能投**。
    所以这里钉住「一个都不许剩」，而不是只盯单元。
    """
    offenders = []
    for pattern in (
        "src/**/*.py",
        "docs/**/*.md",
        "deploy/*",
        "scripts/*",
        "README.md",
        "AGENTS.md",
    ):
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file() and "CRBA" in path.read_text(encoding="utf-8"):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, f"这些文件里还写着旧缩写 CRBA：{offenders}"


def test_unit_quotes_environment_values_with_spaces():
    """systemd 在 `Environment=` 里把空格当**多个赋值**的分隔符。

    不加引号会变成一堆 `Invalid environment assignment, ignoring: …`，
    而那个变量根本没设上（实测踩到，服务起不来）。
    """
    for line in UNIT.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        value = stripped[len("Environment=") :].strip()
        if value.startswith('"') and value.endswith('"'):
            continue
        assert " " not in value, f"`{stripped}` 的值里有空格但没加引号"


def test_unit_writes_both_our_workspace_and_the_notice_queue():
    """它要写自己的 workspace，**还要写 yqa 的 notify/pending**（QQ 通知取件处）。"""
    assert "ReadWritePaths" in UNIT
    assert "/var/lib/crb-notify" in UNIT
    assert "/var/lib/yuque-agent/workspace" in UNIT


def test_unit_does_not_shadow_config_that_lives_in_the_env_file():
    """`CRBN_*` 配置只放 `/home/yuque/.crb-notify/env` —— 单元与手工 CLI 共用一份。

    实测踩到：它们曾经只写在单元的 `Environment=` 里。服务一切正常，但**手工跑
    CLI**（`show` / `forget` / `deliver`）时没有这些值：`CRBN_YQA_BIN` 回落到机器上
    根本没装的 `yqa`，于是 `refresh-approval` 失败 —— 而当时的 `forget` 只印了一句
    「语雀《审批结果》：None」就返回 0，看着像成功。配置放两处就一定会漂。
    """
    deploy_doc = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
    assert "CRBN_YQA_BIN" not in _UNIT_CODE, "它该在 env 文件里；写进单元就等于只有服务拿得到"
    assert "CRBN_YQA_BIN" in deploy_doc, "文档要说清它从哪来（手工跑要 source 那个文件）"


def test_deploy_script_keeps_the_hard_won_fixes():
    """这些「坑」都是上机实测换来的，别在重构时顺手删掉。"""
    assert "flock -n" in _CODE, "要有部署锁（一次只有一个写者）"
    assert "--ff-only" in _CODE, "生产机只允许快进"
    assert "sudo -u yuque git" in _CODE, "git 必须以仓库所有者身份跑（否则静默部署旧 commit）"
    assert 'HOME="$SANDBOX"' in _CODE, "测试要在临时 HOME 里跑"
    assert 'systemctl is-active "$UNIT" || true' in _CODE, "否则 set -e 会杀掉验收"
    assert "SELF_BEFORE=" in _CODE, "脚本自我更新后要用新版重跑（否则「修了没用」）"
    assert "journalctl" not in _CODE or "grep -q" not in _CODE, (
        "`journalctl | grep -q` 在 pipefail 下会误报「启动行没有」（间歇性）"
    )


def test_contract_doc_covers_the_fields_we_ask_the_plugin_for():
    """契约是给插件那一侧的，字段表不能和代码里的白名单漂开。"""
    contract = (ROOT / "docs" / "intake-contract.md").read_text(encoding="utf-8")
    code = (ROOT / "src" / "crb_notify" / "intake.py").read_text(encoding="utf-8")
    for field in ("SQBH", "SHZT", "SHZT_DISPLAY", "SHBZ", "SHYJ", "KSRQ", "KSJC_DISPLAY", "JYYTMS"):
        assert field in contract, f"契约里没写 {field}"
        assert f'"{field}"' in code, f"代码白名单里没有 {field}"
    # 手机号必须明确「不要投」——它是唯一被刻意排除的字段
    assert "JYRDH" in contract and "手机号" in contract


def test_env_file_path_is_the_same_everywhere():
    """env 文件的路径在**单元、部署文档**里必须一致，而且要是本项目自己的目录。

    实测踩到：换项目名时把单元里的路径也一起 sed 成了 `.crb-notify/env`，
    但实际创建的还是旧目录 —— `EnvironmentFile=-…` 的 `-` 会让它**静默跳过**，
    于是服务起来时「未设密钥」，而整条链路看起来完全正常（谁都能投）。
    """
    deploy_doc = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
    assert "/home/yuque/.crb-notify/env" in UNIT, "单元要指到本项目自己的 env"
    assert "/home/yuque/.crb-notify/env" in deploy_doc, "文档要写同一个路径"
    assert ".crb-agent/env" not in UNIT, "别指向旧项目的目录"
    assert ".crb-agent/env" not in deploy_doc, "文档里也别留旧路径"


def test_approval_dir_is_where_yqa_reads_the_notifications():
    """产物目录必须落在 `<yuque 工作区>/<repo>/outbox/approval`。

    `yqa` 那侧就是从这儿读 `notifications.json` 渲染《审批结果》的
    （它的 `approvaldoc.approval_dir()` 认的是同一个字符串）。两边各写各的，
    结果是「QQ 通知照发、文档一直是空的」，**而且一点都不报错**。

    实测踩到（2026-09-27 联调）：从 crb-agent 拆出本项目时把它改成了自己的
    `workspace/approval`，文档空了整整一轮，直到有人投了一条能发文通知的
    真数据、去文档里找却找不到才发现。
    """
    settings = Settings(
        port=8788,
        host="127.0.0.1",
        yuque_workspace=Path("/yw"),
        yqa_repo="g/r",
        yqa_bin="yqa",
        intake_key="",
        notify_qq=True,
        notify_yuque=False,
    )
    assert settings.approval_dir() == Path("/yw/g_r/outbox/approval")
    assert settings.approval_dir().parent == settings.outbox()
