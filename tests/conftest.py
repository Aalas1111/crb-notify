"""测试夹具。

这个项目**不调学校系统**，唯一的外部调用是 `yqa refresh-approval`
（写语雀那篇文档）—— 所以夹具只需造出「yqa 的工作区长什么样」，
并在不需要走那条路时把 `notify_yuque` 关掉。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crb_notify.config import Settings


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """本项目的工作区（账本 / 记录快照 / 产出）。"""
    root = tmp_path / "crba"
    root.mkdir()
    return root


@pytest.fixture
def yuque_workspace(tmp_path: Path) -> Path:
    """造一个像 ``yqa`` 那样的产出目录。

    目录名用 ``ghxd00_jsjysq``（下划线）而 ``YQA_REPO`` 是 ``ghxd00/jsjysq``
    （斜杠）—— 这是生产上的真实形状。早先夹具用不带斜杠的 repo，
    把「按末段推目录名」的 bug 盖住了。
    """
    outbox = tmp_path / "yq" / "ghxd00_jsjysq" / "outbox"
    (outbox / "notify" / "pending").mkdir(parents=True)
    (outbox / "notify" / "done").mkdir(parents=True)
    (outbox / "plan.json").write_text(
        json.dumps(
            {
                "cycle": "0927-1003",
                "generated_at": "2026-09-27T10:18:40+08:00",
                "defaults": {"JYDWDM": "400760", "JYRXM": "张三", "JSJYLXDM": "02"},
                # 这条要**和测试里那份真实样本对得上**（标题 GHP / 2026-09-10 / 第 4 节），
                # 否则记录会全部落进 unmatched，测试就测不到「出通知」那条路。
                "activities": [
                    {
                        "title": "GHP",
                        "date": "2026-09-10",
                        "period": "4",
                        "people": 30,
                        "campus": "3",
                        "_application_id": "2026-09-10-1234567",
                        "_doc_id": 1234567,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return tmp_path / "yq"


@pytest.fixture
def settings(workspace: Path, yuque_workspace: Path) -> Settings:
    """默认**关掉语雀那条路**（否则测试会去调 yqa）。

    它本身有一组单独的测试，那里会用一个假命令替身。
    """
    return Settings(
        port=8788,
        host="127.0.0.1",
        workspace=workspace,
        yuque_workspace=yuque_workspace,
        yqa_repo="ghxd00/jsjysq",
        yqa_bin="yqa",
        intake_key="",
        notify_qq=True,
        notify_yuque=False,
    )
