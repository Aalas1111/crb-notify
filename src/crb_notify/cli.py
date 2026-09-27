"""命令行入口。

crb-notify serve      起接收服务（8788）—— 插件往这里投
crb-notify show       看收到的记录、账本、已生成的通知
crb-notify deliver    把两条出路各跑一遍（补发；幂等，重复跑不会重复通知）
crb-notify version
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from . import __version__, deliver, notify
from .config import ConfigError, Settings

# Windows 控制台默认 GBK，中文会乱码；强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="教室借用申请结果的接收器：收插件投来的记录 → 判定 → 出通知。",
)


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


@app.command("version")
def version_cmd() -> None:
    """显示版本。"""
    typer.echo(f"crbn {__version__}")


@app.command("serve")
def serve_cmd(
    host: Annotated[str | None, typer.Option("--host", help="默认 CRBN_HOST 或 0.0.0.0")] = None,
    port: Annotated[int | None, typer.Option("--port", help="默认 CRBN_PORT 或 8788")] = None,
) -> None:
    """起接收服务。插件 POST 到 `http://<地址>:<端口>/intake/records`。"""
    import uvicorn

    from .intake import build_app

    settings = _settings()
    bind_host, bind_port = host or settings.host, port or settings.port
    typer.secho(
        f"接收入口已开：http://{bind_host}:{bind_port}/intake/records\n"
        f"  产物 {settings.approval_dir()}\n"
        f"  QQ 通知写入 {settings.notify_pending_dir()}（{'开' if settings.notify_qq else '关'}）\n"
        f"  语雀《审批结果》 {'开' if settings.notify_yuque else '关'}"
        + (
            f"\n  要密钥（X-Intake-Key），{len(settings.intake_key)} 字符"
            if settings.intake_key
            else "\n  未设密钥"
        ),
        fg=typer.colors.GREEN,
    )
    uvicorn.run(
        build_app(settings), host=bind_host, port=bind_port, log_level="info", access_log=False
    )


@app.command("show")
def show_cmd() -> None:
    """看收到的记录、账本、已生成的通知（只读本地文件，不碰网络）。"""
    settings = _settings()
    approval = settings.approval_dir()

    records_file = approval / "records.json"
    if records_file.is_file():
        data = json.loads(records_file.read_text(encoding="utf-8"))
        typer.secho(
            f"最后一次收到：{data.get('receivedAt', '?')}，{data.get('count', 0)} 条记录",
            fg=typer.colors.GREEN,
        )
    else:
        typer.echo("还没收到过记录（插件还没投过）")

    ledger = notify.read_ledger(approval)
    typer.echo(f"账本：{len(ledger)} 条审批结束的")
    label = {"approved": "已通过", "rejected": "已退回"}
    for entry in ledger:
        snapshot = entry.get("snapshot") or {}
        rooms = "、".join(notify.split_rooms(*(entry.get("rooms") or [])))
        tail = f"  教室 {rooms}" if rooms else ""
        typer.echo(
            f"  {str(entry.get('first_seen_ended'))[:16]}  "
            f"{label.get(entry.get('outcome'), entry.get('outcome'))}  "
            f"{notify.normalize_title(notify.text_of(snapshot, 'purpose'))}{tail}"
        )

    document_file = approval / "notifications.json"
    if document_file.is_file():
        document = json.loads(document_file.read_text(encoding="utf-8"))
        typer.echo(f"通知文档：{len(document.get('notifications') or [])} 条（{document_file}）")

    pending = settings.notify_pending_dir()
    if pending.is_dir():
        waiting = sorted(p.name for p in pending.glob("*.json"))
        typer.echo(f"待投递（{pending}）：{len(waiting)} 条")
        for name in waiting[:10]:
            typer.echo(f"  {name}")
    unmatched = approval / "unmatched.json"
    if unmatched.is_file():
        bucket = json.loads(unmatched.read_text(encoding="utf-8")).get("unmatched") or []
        if bucket:
            typer.secho(f"⚠ 认不出 {len(bucket)} 条（见 {unmatched}）", fg=typer.colors.YELLOW)


@app.command("forget")
def forget_cmd(
    sqbh: Annotated[
        list[str] | None, typer.Argument(help="要忘掉的申请编号（可给多个；只认账本里有的）")
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="忘掉账本里现在有的全部 —— 联调收尾用")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="确认执行；不给就只是列出会忘掉哪些")] = False,
) -> None:
    """忘掉几条申请（联调期的假数据），之后同一条可以重新投、重走一遍判定。

    **这是唯一会改变账本内容的地方**，但它也是追加 —— 追加一行「墓碑」把这条抹掉，
    账本本身（含墓碑）永远只增不删。所以「谁在什么时候忘掉了什么」有迹可循。

    没有 ``--yes`` 时只列清单，不动任何东西。**忘掉一条已经投出去的通知收不回来**
    （QQ 里那条已经在群里了）；还没被桥取走的会顺手从 ``pending/`` 撤回。
    """
    settings = _settings()
    approval = settings.approval_dir()
    ledger = notify.read_ledger(approval)
    known = {str(e.get("sqbh")) for e in ledger}

    if all_:
        targets = sorted(known)
    elif sqbh:
        targets = list(dict.fromkeys(str(s).strip() for s in sqbh if str(s).strip()))
        missing = [s for s in targets if s not in known]
        if missing:
            typer.secho(
                f"账本里没有这些申请编号：{'、'.join(missing)}\n"
                f"（先 `show` 看一眼；也许它已经被忘掉了）",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)
    else:
        typer.secho("给申请编号，或者 --all（--help 看用法）", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)

    if not targets:
        typer.secho("账本是空的，没什么可忘的", fg=typer.colors.YELLOW)
        return

    label = {"approved": "已通过", "rejected": "已退回", notify.OUTCOME_UNMATCHED: "认不出"}
    stuck: list[Path] = []
    gone: list[str] = []
    typer.echo(f"会忘掉这 {len(targets)} 条：")
    for entry in ledger:
        code = str(entry.get("sqbh"))
        if code not in targets:
            continue
        outcome = str(entry.get("outcome") or "")
        waiting, sent = deliver.notice_files(
            settings,
            notify.notification_id(code, outcome, [str(r) for r in (entry.get("rooms") or [])]),
        )
        stuck += waiting
        if sent:
            gone.append(code)
        mark = (
            "  ⚠ 已投递，收不回来"
            if sent
            else ("  （待投递，会从 pending/ 撤回）" if waiting else "")
        )
        typer.echo(
            f"  {str(entry.get('first_seen_ended'))[:16]}  {label.get(outcome, outcome)}  "
            f"{notify.normalize_title(notify.text_of(entry.get('snapshot') or {}, 'purpose'))}  "
            f"{code}{mark}"
        )
    if gone:
        typer.secho(
            f"⚠ 其中 {len(gone)} 条已经投递过：QQ 里那条收不回来，"
            f"《审批结果》会少这几行（账本里的墓碑留着，事后能看出是被谁忘的）",
            fg=typer.colors.YELLOW,
        )

    if not yes:
        typer.secho("（只是看看。确认要忘就加 --yes）", fg=typer.colors.YELLOW)
        return

    tombstones = notify.forget_entries(approval, targets)
    withdrawn = 0
    for path in stuck:
        try:
            path.unlink(missing_ok=True)
            withdrawn += 1
        except OSError as exc:
            typer.secho(f"⚠ 撤回 {path.name} 失败：{exc}", fg=typer.colors.YELLOW, err=True)

    summary = notify.rebuild(approval, settings.outbox(), [], repo=settings.yqa_repo)
    document = json.loads((approval / "notifications.json").read_text(encoding="utf-8"))
    result = deliver.deliver(settings, document)
    typer.secho(
        f"[OK] 忘掉 {len(targets)} 条（墓碑 {tombstones} 行），撤回待投递 {withdrawn} 条；"
        f"通知文档现在 {summary['total']} 条、认不出 {summary['unmatched']} 条",
        fg=typer.colors.GREEN,
    )
    # 账本已经改了，但两条出路任一失败都算「没弄完」—— 要看得见，且别返回 0。
    # （实测踩到：手工跑时 `CRBN_YQA_BIN` 没设 → yqa 找不到 → 这里曾经只印一个 None，
    #   看起来像成功了，实际语雀那篇没重生。）
    yuque, qq = result["yuque"], result["qq"]
    if yuque.get("ok"):
        typer.echo(f"     语雀《审批结果》：{yuque.get('output') or yuque.get('skipped')}")
    else:
        typer.secho(
            f"     [FAIL] 语雀《审批结果》：{yuque.get('error')}", fg=typer.colors.RED, err=True
        )
    if not qq.get("ok"):
        typer.secho(f"     [FAIL] QQ 通知：{qq.get('error')}", fg=typer.colors.RED, err=True)
    if not (yuque.get("ok") and qq.get("ok")):
        raise typer.Exit(1)


@app.command("deliver")
def deliver_cmd() -> None:
    """把两条出路各跑一遍。幂等：已经投过/内容没变的不会被重复处理。"""
    settings = _settings()
    document_file = settings.approval_dir() / "notifications.json"
    if not document_file.is_file():
        typer.secho("还没有通知文档（先让插件投一次）", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(1)
    document = json.loads(document_file.read_text(encoding="utf-8"))
    result = deliver.deliver(settings, document)

    qq = result["qq"]
    if qq.get("ok"):
        typer.secho(
            f"[OK] QQ 通知：新写 {len(qq.get('written') or [])} 条，"
            f"跳过已发 {len(qq.get('skipped_already_sent') or [])} 条",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(f"[FAIL] QQ 通知：{qq.get('error') or qq.get('skipped')}", fg=typer.colors.RED)

    yuque = result["yuque"]
    if yuque.get("ok"):
        typer.secho(
            f"[OK] 语雀《审批结果》：{yuque.get('output') or yuque.get('skipped')}",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(f"[FAIL] 语雀《审批结果》：{yuque.get('error')}", fg=typer.colors.RED)

    if not (qq.get("ok") and yuque.get("ok")):
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
