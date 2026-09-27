"""清理接口：`crb-notify forget` —— 联调期投进来的假数据怎么清。

这是**唯一会改变账本内容的地方**，所以几条规矩要钉住：

1. 账本文件只增不减（靠追加墓碑）。否则它和 intake 的追加会互相丢写 ——
   而丢写是静默的，谁都不会发现某条申请不见了。
2. 忘掉之后同一条能**重新投进来**（联调要反复重跑同一条）。
3. 已经投出去的收不回来：只能如实地讲，不能假装撤回。
"""

from __future__ import annotations

import json
from typing import Any

from starlette.testclient import TestClient
from typer.testing import CliRunner

from crb_notify import intake, notify
from crb_notify.cli import app
from crb_notify.config import Settings

URL = "/intake/records"

#: 对得上 conftest 里 plan.json 的那条（GHP / 2026-09-10 / 第4节）→ 会**真的**出通知。
MATCHED = {
    "SQBH": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "SHZT": "99",
    "SHZT_DISPLAY": "已通过",
    "SHBZ": "通过",
    "FJ": "仙林校区-新教404",
    "JASMC": "新教404",
    "KSRQ": "2026-09-10",
    "KSJC_DISPLAY": "第4节(11:10-12:00)",
    "JSJC_DISPLAY": "第4节(11:10-12:00)",
    "XXXQDM_DISPLAY": "仙林校区",
    "JYRXM": "张三",
    "JYYTMS": "GHP",
    "SQRQ": "2026-09-01",
}

#: 对不上任何活动的假数据 —— 联调时最常投的就是这种。
FAKE = {**MATCHED, "SQBH": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "JYYTMS": "一个根本不存在的活动"}


def _client(settings: Settings) -> TestClient:
    return TestClient(intake.build_app(settings))


def _send(settings: Settings, records: list[Any]) -> dict[str, Any]:
    with _client(settings) as client:
        response = client.post(
            URL, content=json.dumps({"records": records}), headers={"Content-Type": "text/plain"}
        )
    assert response.status_code == 200, response.text
    return response.json()


def _forget(monkeypatch, settings: Settings, *args: str):
    """按操作员的样子跑 CLI —— 走 `Settings.from_env()`，所以环境得摆好。"""
    monkeypatch.setenv("YQA_REPO", settings.yqa_repo)
    monkeypatch.setenv("CRBA_WORKSPACE", str(settings.workspace))
    monkeypatch.setenv("CRBA_YUQUE_WORKSPACE", str(settings.yuque_workspace))
    monkeypatch.setenv("CRBA_NOTIFY_YUQUE", "off")
    return CliRunner().invoke(app, ["forget", *args])


def _ledger_lines(settings: Settings) -> list[str]:
    """账本**文件**里的行（含墓碑）—— 和折叠后的视图不是一回事。"""
    path = notify.ledger_path(settings.approval_dir())
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _document(settings: Settings) -> dict[str, Any]:
    return json.loads((settings.approval_dir() / "notifications.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 折叠
def test_read_ledger_folds_tombstones(workspace):
    root = workspace / "approval"
    notify.append_ledger(root, [{"sqbh": "aaa", "outcome": "approved"}])
    notify.append_ledger(root, [{"sqbh": "bbb", "outcome": "rejected"}])
    notify.forget_entries(root, ["aaa"])

    assert [e["sqbh"] for e in notify.read_ledger(root)] == ["bbb"]
    # 文件本身没被重写：墓碑追加在后面，原始那行还在（所以「忘过什么」查得到）
    assert len(_lines(root)) == 3


def _lines(root) -> list[str]:
    path = notify.ledger_path(root)
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_tombstone_drops_every_entry_of_that_application(workspace):
    """一条申请可以有多条记录（outcome 变了就是新消息）—— 墓碑要全抹掉。"""
    root = workspace / "approval"
    notify.append_ledger(
        root,
        [
            {"sqbh": "aaa", "outcome": "unmatched"},
            {"sqbh": "aaa", "outcome": "approved"},
            {"sqbh": "bbb", "outcome": "approved"},
        ],
    )
    notify.forget_entries(root, ["aaa"])
    assert [e["sqbh"] for e in notify.read_ledger(root)] == ["bbb"]


def test_same_sqbh_can_be_reused_after_forget(workspace):
    """忘掉之后再记同一条，要能重新出现（联调反复重跑同一条就靠这个）。"""
    root = workspace / "approval"
    notify.append_ledger(root, [{"sqbh": "aaa", "outcome": "approved"}])
    notify.forget_entries(root, ["aaa"])
    notify.append_ledger(root, [{"sqbh": "aaa", "outcome": "approved"}])
    assert [e["sqbh"] for e in notify.read_ledger(root)] == ["aaa"]


# ---------------------------------------------------------------- 命令
def test_without_yes_nothing_changes(settings: Settings, monkeypatch):
    _send(settings, [{"raw": MATCHED}])
    before = _ledger_lines(settings)

    result = _forget(monkeypatch, settings, MATCHED["SQBH"])

    assert result.exit_code == 0, result.output
    assert "--yes" in result.output
    assert _ledger_lines(settings) == before, "没给 --yes 就不该动账本"
    assert len(_document(settings)["notifications"]) == 1


def test_forget_makes_it_postable_again(settings: Settings, monkeypatch):
    first = _send(settings, [{"raw": MATCHED}])
    assert first["new"] == 1

    result = _forget(monkeypatch, settings, MATCHED["SQBH"], "--yes")

    assert result.exit_code == 0, result.output
    assert notify.read_ledger(settings.approval_dir()) == []
    assert len(_document(settings)["notifications"]) == 0, "通知文档要跟着重建（语雀那篇同理）"

    # 同一条重新投 —— 联调要的就是这个
    assert _send(settings, [{"raw": MATCHED}])["new"] == 1


def test_unknown_sqbh_is_an_error(settings: Settings, monkeypatch):
    _send(settings, [{"raw": MATCHED}])
    result = _forget(monkeypatch, settings, "cccccccccccccccccccccccccccccccc", "--yes")
    assert result.exit_code == 1
    assert "没有这些申请编号" in result.output


def test_needs_a_target(settings: Settings, monkeypatch):
    result = _forget(monkeypatch, settings)
    assert result.exit_code == 2


def test_empty_ledger_is_not_an_error(settings: Settings, monkeypatch):
    result = _forget(monkeypatch, settings, "--all", "--yes")
    assert result.exit_code == 0
    assert "空的" in result.output


# ---------------------------------------------------------------- 两条出路
def test_pending_notice_is_withdrawn(settings: Settings, monkeypatch):
    """还没被桥取走的那条要撤回来 —— 否则「忘掉了」是假的，QQ 里照样会发出去。"""
    _send(settings, [{"raw": MATCHED}])
    assert len(list(settings.notify_pending_dir().glob("*.json"))) == 1

    result = _forget(monkeypatch, settings, MATCHED["SQBH"], "--yes")

    assert result.exit_code == 0, result.output
    assert list(settings.notify_pending_dir().glob("*.json")) == []
    assert "撤回待投递 1 条" in result.output


def test_delivered_notice_is_left_alone_and_said_so(settings: Settings, monkeypatch):
    """已经发出去的不可能撤回 —— 只能如实说「收不回来」，且不能去动 done/。"""
    _send(settings, [{"raw": MATCHED}])
    notice = next(settings.notify_pending_dir().glob("*.json"))
    notice.rename(settings.notify_done_dir() / notice.name)  # 模拟桥投递成功

    result = _forget(monkeypatch, settings, MATCHED["SQBH"], "--yes")

    assert result.exit_code == 0, result.output
    assert "收不回来" in result.output
    assert (settings.notify_done_dir() / notice.name).is_file(), "done/ 是桥的地盘，不许碰"


def test_fake_records_never_had_a_notice_to_withdraw(settings: Settings, monkeypatch):
    """联调常态：对不上的假数据只在 unmatched 里，没有通知可撤。"""
    body = _send(settings, [{"raw": FAKE}])
    assert body["unmatched"] == 1 and body["total"] == 0

    result = _forget(monkeypatch, settings, FAKE["SQBH"], "--yes")

    assert result.exit_code == 0, result.output
    assert "撤回待投递 0 条" in result.output
    assert (
        json.loads((settings.approval_dir() / "unmatched.json").read_text("utf-8"))["unmatched"]
        == []
    )


def test_forget_all_clears_the_whole_ledger(settings: Settings, monkeypatch):
    _send(settings, [{"raw": MATCHED}, {"raw": FAKE}])
    assert len(_ledger_lines(settings)) == 2

    result = _forget(monkeypatch, settings, "--all", "--yes")

    assert result.exit_code == 0, result.output
    assert notify.read_ledger(settings.approval_dir()) == []
    assert "忘掉 2 条" in result.output
    assert list(settings.notify_pending_dir().glob("*.json")) == []
