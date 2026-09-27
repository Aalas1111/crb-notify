"""接收端点：插件把申请记录投过来，这里判定并分发。

路由只有三条，**刻意不做别的** —— 这个端口对公网开着（安全组已放行 8788），
而插件是油猴脚本、密钥必然公开，所以它的安全模型是
**「公开可写，但无害」**：

* **只收数据**：不存在任何指令、删除、回调的入口；
* **幂等**：同一份记录重复投递 = 什么都没发生（按 `sqbh` 与账本比对）；
* **不落 PII**：手机号（`JYRDH` / `phone`）在这里就被丢掉，不进快照也不进通知；
* 载荷大小与条数都有上限。

契约见 `docs/intake-contract.md`。
"""

from __future__ import annotations

import json
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import deliver, notify
from .config import Settings

#: 载荷上限。同学的导出是每页 100 条，给它留足余量。
MAX_BODY = 4 * 1024 * 1024
MAX_RECORDS = 2000

#: 「已通过」必须能读到一个教室（负责人说过这种情况应该不存在）；读不到就不发通知，
#: 归到 unmatched 等人看 —— 猜错比认不出严重得多。
_FIELDS = (
    "SQBH",
    "SHZT",
    "SHZT_DISPLAY",
    "SHBZ",
    "SHYJ",
    "FJ",
    "JASMC",
    "KSRQ",
    "KSJC_DISPLAY",
    "JSJC_DISPLAY",
    "XXXQDM_DISPLAY",
    "JYRXM",
    "JYYTMS",
    "SQRQ",
)


def normalize_record(entry: dict[str, Any]) -> dict[str, Any]:
    """把插件投来的一条记录规范成 ``notify.classify()`` 认的形状。

    **白名单**：只取下面这些字段。手机号不在里面 —— 它在这一行就被丢弃，
    既不落盘也不进通知（服务器是明文 HTTP，PII 能少传就少传）。

    ``raw`` 优先（那是学校接口的原样返回，字段名与语义都在）；没有 ``raw`` 时
    退回插件已经扁平化好的那几个字段，所以插件就算只投自己那份导出也能用。
    """
    raw = entry.get("raw") if isinstance(entry.get("raw"), dict) else {}

    def pick(name: str, *fallbacks: Any) -> Any:
        value = raw.get(name)
        if value not in (None, ""):
            return value
        for fallback in fallbacks:
            if fallback not in (None, ""):
                return fallback
        return None

    record: dict[str, Any] = {
        "SQBH": pick("SQBH", entry.get("sqbh")) or "",
        "SHZT": pick("SHZT", entry.get("statusCode")) or "",
        "SHZT_DISPLAY": pick("SHZT_DISPLAY", entry.get("status")) or "",
        "SHBZ": pick("SHBZ") or "",
        "SHYJ": pick("SHYJ", entry.get("feedback")),
        "FJ": pick("FJ", entry.get("room")),
        "JASMC": pick("JASMC"),
        "KSRQ": pick("KSRQ", entry.get("date")) or "",
        "KSJC_DISPLAY": pick("KSJC_DISPLAY", entry.get("slot")) or "",
        "JSJC_DISPLAY": pick("JSJC_DISPLAY", entry.get("slot")) or "",
        "XXXQDM_DISPLAY": pick("XXXQDM_DISPLAY", entry.get("campus")) or "",
        "JYRXM": pick("JYRXM", entry.get("borrowerName")) or "",
        "JYYTMS": pick("JYYTMS", entry.get("purpose")) or "",
        "SQRQ": pick("SQRQ", entry.get("requestedAt")) or "",
    }
    del entry  # 显式：其余字段（含手机号）就此丢弃
    return record


def parse_payload(payload: Any) -> tuple[list[dict[str, Any]], str]:
    """接受三种形状，返回 ``(记录列表, 说给插件听的备注)``。

    1. `{"records": [...]}` —— 同学那份导出的形状（**推荐**，插件不用改）；
    2. `[...]` —— 直接一个数组；
    3. `{"data": {"records": [...]}}` —— 万一包了一层。
    """
    if isinstance(payload, list):
        return payload, "顶层数组"
    if isinstance(payload, dict):
        records = payload.get("records")
        if isinstance(records, list):
            return records, "records 字段"
        nested = payload.get("data")
        if isinstance(nested, dict) and isinstance(nested.get("records"), list):
            return nested["records"], "data.records"
    raise ValueError("载荷里找不到 records 数组（见 docs/intake-contract.md）")


def build_app(settings: Settings) -> Starlette:
    async def healthz(_request: Request) -> Response:
        return PlainTextResponse("ok")

    async def root(_request: Request) -> Response:
        return JSONResponse(
            {
                "service": "crb-notify intake",
                "what": "接收浏览器插件投来的教室借用申请记录 → 判定 → 出通知",
                "post": "/intake/records",
                "contract": "docs/intake-contract.md",
            }
        )

    def _cors(response: Response, origin: str) -> Response:
        """反射来源。安全模型本来就是「公开可写但无害」，所以不挑来源；
        但必须给跨域头，否则油猴脚本（`@grant none`，只能用页面上下文的 fetch）发不出来。"""
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    async def intake(request: Request) -> Response:
        origin = request.headers.get("origin", "")

        if request.method == "OPTIONS":
            # 预检。插件若用 `Content-Type: text/plain` 发就不会走到这里，
            # 但用 `application/json` 会 —— 两条都支持，省得它纠结。
            response = Response(status_code=204)
            response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Intake-Key"
            response.headers["Access-Control-Max-Age"] = "86400"
            return _cors(response, origin)

        body = await request.body()
        if len(body) > MAX_BODY:
            return _cors(JSONResponse({"ok": False, "error": "载荷过大"}, status_code=413), origin)

        if settings.intake_key:
            given = request.headers.get("x-intake-key") or request.query_params.get("key") or ""
            if given != settings.intake_key:
                return _cors(
                    JSONResponse({"ok": False, "error": "密钥不对"}, status_code=403), origin
                )

        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return _cors(
                JSONResponse({"ok": False, "error": f"载荷不是合法 JSON：{exc}"}, status_code=400),
                origin,
            )

        try:
            entries, shape = parse_payload(payload)
        except ValueError as exc:
            return _cors(JSONResponse({"ok": False, "error": str(exc)}, status_code=400), origin)

        if len(entries) > MAX_RECORDS:
            return _cors(
                JSONResponse(
                    {"ok": False, "error": f"一次最多 {MAX_RECORDS} 条，收到 {len(entries)}"},
                    status_code=413,
                ),
                origin,
            )

        records = [normalize_record(e) for e in entries if isinstance(e, dict)]
        result = await _process(settings, records)
        result["ok"] = True
        result["received"] = len(records)
        result["shape"] = shape
        return _cors(JSONResponse(result), origin)

    app = Starlette(
        routes=[
            Route("/", root),
            Route("/healthz", healthz),
            Route("/intake/records", intake, methods=["POST", "OPTIONS"]),
        ]
    )
    app.state.settings = settings
    return app


async def _process(settings: Settings, records: list[dict[str, Any]]) -> dict[str, Any]:
    """判定 → 出文档 → 分发。**整条链路是幂等的**：重复投同一份，什么都不做。"""
    import asyncio

    def run() -> dict[str, Any]:
        # 1) 留一份快照（已去 PII），便于人工核对插件投了什么
        settings.approval_dir().mkdir(parents=True, exist_ok=True)
        (settings.approval_dir() / "records.json").write_text(
            json.dumps(
                {
                    "receivedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "count": len(records),
                    "records": records,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # 2) 判定 + 记账 + 重生对外文档（全在 notify 里）
        summary = notify.rebuild(
            settings.approval_dir(), settings.outbox(), records, repo=settings.yqa_repo
        )

        # 3) 两条出路
        document = json.loads(
            (settings.approval_dir() / "notifications.json").read_text(encoding="utf-8")
        )
        summary["delivery"] = deliver.deliver(settings, document)
        return summary

    return await asyncio.to_thread(run)
