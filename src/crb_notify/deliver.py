"""把判定出来的结果分发出去 —— **两条路**。

1. **QQ 通知**：写进 `yqa` 工作区的 `outbox/notify/pending/*.json`，
   桥（`qq-bridge`）会取件发出去、然后搬进 `done/`。
   那是 `docs/handoff.md` §3 里冻结的目录协议，所以**不用改桥的任何代码**。
2. **语雀《审批结果》**：调 `yqa refresh-approval` 让那篇文档重生
   （写知识库归 yqa：结构约定与 token 都在它那边）。

两条路都**幂等**：

* QQ 那条约：写之前先看 `pending/` 与 `done/` 里有没有同一个 `notice_id`
  —— 有就跳过（桥已经投过或正在投，重复写会让社员收到两条）；
* 语雀那条：`yqa refresh-approval` 自身是幂等的（内容没变就一个字都不写）。
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import Settings

#: 程序写的通知种类。`kind` 词汇表见 docs/handoff.md §3.3 ——
#: 这两个是**新加的**（学校里「审核通过 / 审核不通过」与 agent 的
#: 「受理 / 退回」是两件事，不能复用 `rejected`）。
KIND_APPROVED = "borrow_approved"
KIND_REJECTED = "borrow_rejected"

#: 桥不看的审计字段（放在 extra 里，不污染契约）。
SOURCE = "crb-notify/intake"


def _message(item: dict[str, Any]) -> str:
    """渲染成给社员看的一条中文正文。**桥直接发这一条**，不加工。"""
    activity = item.get("activity") or {}
    result = item.get("result") or {}
    title = activity.get("title") or "（无标题）"
    when = " ".join(
        part
        for part in (
            str(activity.get("date") or ""),
            str(activity.get("slotStart") or ""),
            str(activity.get("campus") or ""),
        )
        if part
    )
    if item.get("type") == "approved":
        rooms = "、".join(str(r) for r in (result.get("actualRooms") or [])) or "（见办事大厅）"
        # 不写「如与预期不符请到办事大厅核对」：**意向教室本来就不保证申请得到**，
        # 学校给哪间就是哪间，这不是异常（需求方 2026-09-27 明确要求删掉）。
        return f"【教室借用】「{title}」已通过。\n时间：{when}\n教室：{rooms}"
    reason = result.get("feedback") or "学校未给原因"
    return (
        f"【教室借用】「{title}」没有通过。\n"
        f"时间：{when}\n"
        f"原因：{reason}\n"
        f"（需要改时间或换场地的，请重新提交另一份申请）"
    )


def _notice_payload(item: dict[str, Any], seq: int) -> dict[str, Any]:
    """一条 `outbox/notify/pending/*.json`（形状照 examples/notice.example.json）。"""
    activity = item.get("activity") or {}
    source_doc = activity.get("sourceDoc") or {}
    approved = item.get("type") == "approved"
    return {
        "schema_version": "1.0",
        "seq": seq,
        "notice_id": str(item.get("notificationId") or ""),
        "created_at": str(item.get("detectedAt") or ""),
        "kind": KIND_APPROVED if approved else KIND_REJECTED,
        "repo": source_doc.get("repo") or "",
        "doc": {
            "doc_id": source_doc.get("id") or "",
            "title": source_doc.get("title") or activity.get("title") or "",
            "url": "",
        },
        # 借用人姓名（文档里手填的申请人）。桥拿它去映射 QQ 号；
        # 映射不到会进 unrouted/（不会丢，但也没人收到）—— 那是桥的策略（handoff §3.4）。
        "member": {"name": activity.get("organizer") or ""},
        "summary": f"「{activity.get('title') or ''}」{'已通过' if approved else '没有通过'}",
        "message": _message(item),
        "reasons": [str(item.get("result", {}).get("feedback") or "")] if not approved else [],
        "warnings": [],
        "extra": {
            "source": SOURCE,
            "sqbh": item.get("sqbh") or "",
            "application_id": item.get("applicationId") or "",
            "actual_rooms": (item.get("result") or {}).get("actualRooms") or [],
        },
    }


def notice_files(settings: Settings, notice_id: str) -> tuple[list[Path], list[Path]]:
    """这条通知现在在哪：``(pending 里还没被桥取走的, done 里已经发出去的)``。"""
    folders = (settings.notify_pending_dir(), settings.notify_done_dir())
    found = [sorted(f.glob(f"*{notice_id}*.json")) if f.is_dir() else [] for f in folders]
    return found[0], found[1]


def _already_delivered(settings: Settings, notice_id: str) -> bool:
    """`pending/` 或 `done/` 里有同 id 的通知 → 这条不用再写。"""
    waiting, sent = notice_files(settings, notice_id)
    return bool(waiting or sent)


def write_qq_notices(settings: Settings, document: dict[str, Any]) -> dict[str, Any]:
    """把文档里**还没投过**的通知写进 `pending/`。返回写了什么。"""
    if not settings.notify_qq:
        return {"ok": True, "skipped": "CRBN_NOTIFY_QQ=off"}

    folder = settings.notify_pending_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    written: list[str] = []
    skipped: list[str] = []
    for index, item in enumerate(document.get("notifications") or [], start=1):
        notice_id = str(item.get("notificationId") or "")
        if not notice_id:
            continue
        if _already_delivered(settings, notice_id):
            skipped.append(notice_id)
            continue
        # seq 要**单调递增**（桥按它从小到大处理），所以用检测时刻的毫秒数；
        # 同一毫秒的多条用序号错开。
        stamp = int(time.time() * 1000)
        payload = _notice_payload(item, seq=stamp + index)
        name = f"{payload['seq']:014d}-{payload['kind']}-{notice_id}.json"
        try:
            (folder / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "written": written}
        written.append(name)

    return {"ok": True, "written": written, "skipped_already_sent": skipped, "dir": str(folder)}


def refresh_yuque(settings: Settings) -> dict[str, Any]:
    """让语雀那篇《审批结果》重生（幂等，失败不影响接收成功）。"""
    if not settings.notify_yuque:
        return {"ok": True, "skipped": "CRBN_NOTIFY_YUQUE=off"}
    cmd = [
        *shlex.split(settings.yqa_bin),
        "refresh-approval",
        "--workspace",
        str(settings.yuque_workspace),
        "--repo",
        settings.yqa_repo,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {
            "ok": False,
            "exit_code": proc.returncode,
            "error": " ".join((proc.stderr or proc.stdout).split())[:300],
        }
    return {"ok": True, "output": " ".join(proc.stdout.split())[:200]}


def deliver(settings: Settings, document: dict[str, Any]) -> dict[str, Any]:
    """两条路都走一遍。**任何一条失败都不抛** —— 收数据这件事本身已经成功了。"""
    return {"qq": write_qq_notices(settings, document), "yuque": refresh_yuque(settings)}


def pending_dir(settings: Settings) -> Path:
    return settings.notify_pending_dir()
