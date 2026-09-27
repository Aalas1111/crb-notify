"""命令行入口。

crba serve      起接收服务（8788）—— 插件往这里投
crba show       看收到的记录、账本、已生成的通知
crba deliver    把两条出路各跑一遍（补发；幂等，重复跑不会重复通知）
crba version
"""

from __future__ import annotations

import json
import sys
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
    typer.echo(f"crba {__version__}")


@app.command("serve")
def serve_cmd(
    host: Annotated[str | None, typer.Option("--host", help="默认 CRBA_HOST 或 0.0.0.0")] = None,
    port: Annotated[int | None, typer.Option("--port", help="默认 CRBA_PORT 或 8788")] = None,
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
        rooms = "、".join(entry.get("rooms") or [])
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
