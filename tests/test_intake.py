"""接收端点：CORS、鉴权、载荷形状、去 PII、端到端出通知。

真实样本（负责人 2026-09-27 转来的「审核不通过」记录）原样放在这里 ——
判定规则要面对的就是那些取值（`SHZT=-68` / `SHBZ=不通过` / `SHYJ=null`），
别为了「看起来干净」改成假数据。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from starlette.testclient import TestClient

from crb_notify import deliver, intake
from crb_notify.config import Settings

#: 真实的「审核不通过」记录。手机号那项**故意留着**，用来验证收到后会丢弃它。
REJECTED = {
    "SQBH": "6ded436268ee478cb4b03e3f0b9d2788",
    "SHZT": "-68",
    "SHZT_DISPLAY": "学生工作处审核不通过",
    "SHBZ": "不通过",
    "SHYJ": None,
    "FJ": None,
    "JASMC": None,
    "KSJC_DISPLAY": "第4节(11:10-12:00)",
    "JSJC_DISPLAY": "第4节(11:10-12:00)",
    "KSRQ": "2026-09-10",
    "JYRXM": "谷和平",
    "JYRDH": "15366939586",
    "JYYTMS": "GHP",
    "XXXQDM_DISPLAY": "仙林校区",
    "SQRQ": "2026-09-08 15:17:23.0",
}

#: 「已通过」按样本形状推的（样本里没有通过态）。真实取值等第一条真通过。
APPROVED = {
    **REJECTED,
    "SQBH": "7e75e389f83d4aac95bdcc28b5521041",
    "SHZT": "99",
    "SHZT_DISPLAY": "已通过",
    "SHBZ": "通过",
    "FJ": "新教404",
    "JASMC": "新教404",
    "JYYTMS": "GHP（意向：新教404）",
}

URL = "/intake/records"


def _client(settings: Settings) -> TestClient:
    return TestClient(intake.build_app(settings))


def _send(client: TestClient, records: list[Any], **kwargs: Any):
    """用 `text/plain` 发（插件的推荐姿势，不触发预检）。"""
    headers = {"Content-Type": "text/plain", **kwargs.pop("headers", {})}
    return client.post(URL, content=json.dumps({"records": records}), headers=headers, **kwargs)


def _notices(settings: Settings) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(settings.notify_pending_dir().glob("*.json"))
    ]


# ---------------------------------------------------------------- 正常路径
def test_approved_produces_a_qq_notice(settings: Settings):
    with _client(settings) as client:
        response = _send(client, [{"sqbh": APPROVED["SQBH"], "raw": APPROVED}])
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["received"] == 1 and body["new"] == 1

    notices = _notices(settings)
    assert len(notices) == 1
    notice = notices[0]
    assert notice["kind"] == deliver.KIND_APPROVED
    assert notice["notice_id"].startswith("notify-")
    assert "新教404" in notice["message"]
    assert notice["extra"]["sqbh"] == APPROVED["SQBH"]
    # 桥认的字段一个都不能少（契约见 docs/handoff.md §3.2）
    for key in (
        "schema_version",
        "seq",
        "notice_id",
        "created_at",
        "kind",
        "member",
        "summary",
        "message",
    ):
        assert key in notice, f"缺字段 {key}"


def test_rejected_carries_the_reason(settings: Settings):
    with _client(settings) as client:
        _send(client, [{"raw": {**REJECTED, "SHYJ": "申请时间不符合教室借用要求"}}])
    notice = _notices(settings)[0]
    assert notice["kind"] == deliver.KIND_REJECTED
    assert "申请时间不符合教室借用要求" in notice["message"]


def test_pending_and_draft_do_not_notify(settings: Settings):
    with _client(settings) as client:
        _send(client, [{"raw": {**REJECTED, "SHZT": "65", "SHZT_DISPLAY": "待审核", "SHBZ": ""}}])
    assert _notices(settings) == []


# ---------------------------------------------------------------- 幂等
def test_same_records_twice_only_notifies_once(settings: Settings):
    with _client(settings) as client:
        first = _send(client, [{"raw": APPROVED}]).json()
        second = _send(client, [{"raw": APPROVED}]).json()
    assert first["new"] == 1
    assert second["new"] == 0, "重复投同一份不该再产生新通知"
    assert len(_notices(settings)) == 1


def test_already_delivered_is_not_rewritten(settings: Settings):
    """桥把通知搬进 done/ 之后，同一条结果不该再写进 pending/（否则社员会收到两条）。"""
    with _client(settings) as client:
        _send(client, [{"raw": APPROVED}])
    notice = next(settings.notify_pending_dir().glob("*.json"))
    notice.rename(settings.notify_done_dir() / notice.name)  # 模拟桥投递成功
    (settings.approval_dir() / "ledger.jsonl").unlink()  # 连账本也清掉，逼它重新判定

    with _client(settings) as client:
        body = _send(client, [{"raw": APPROVED}]).json()
    assert body["new"] == 1, "账本清了，所以判定为「新」"
    assert _notices(settings) == [], "但已经发过的不能再写一次"


# ---------------------------------------------------------------- 去 PII
def test_phone_number_never_touches_the_disk(settings: Settings):
    """服务器是明文 HTTP，手机号能少存就少存：收到即丢。"""
    with _client(settings) as client:
        _send(client, [{"phone": "15366939586", "borrowerName": "谷和平", "raw": REJECTED}])
    snapshot = (settings.approval_dir() / "records.json").read_text(encoding="utf-8")
    assert "15366939586" not in snapshot
    assert "JYRDH" not in snapshot
    for path in settings.notify_pending_dir().glob("*.json"):
        assert "15366939586" not in path.read_text(encoding="utf-8")


def test_name_is_kept_for_the_qq_mapping(settings: Settings):
    with _client(settings) as client:
        _send(client, [{"raw": APPROVED}])
    assert _notices(settings)[0]["member"]["name"] == "谷和平"


# ---------------------------------------------------------------- 鉴权与 CORS
def test_key_is_required_when_configured(settings: Settings):
    locked = replace(settings, intake_key="s3cret")
    with _client(locked) as client:
        assert _send(client, [{"raw": APPROVED}]).status_code == 403
        assert (
            _send(client, [{"raw": APPROVED}], headers={"X-Intake-Key": "wrong"}).status_code == 403
        )
        assert (
            _send(client, [{"raw": APPROVED}], headers={"X-Intake-Key": "s3cret"}).status_code
            == 200
        )


def test_no_key_configured_accepts_anything(settings: Settings):
    with _client(settings) as client:
        assert _send(client, [{"raw": APPROVED}]).status_code == 200


def test_cors_is_reflected_because_the_plugin_is_cross_origin(settings: Settings):
    """插件是 `@grant none` 的油猴脚本，只能用页面上下文的 fetch —— 跨域是硬约束。"""
    origin = "https://ehallapp.nju.edu.cn"
    with _client(settings) as client:
        response = _send(client, [{"raw": APPROVED}], headers={"Origin": origin})
        assert response.headers["access-control-allow-origin"] == origin
        preflight = client.options(
            URL, headers={"Origin": origin, "Access-Control-Request-Method": "POST"}
        )
        assert preflight.status_code == 204
        assert "POST" in preflight.headers["access-control-allow-methods"]
        assert "X-Intake-Key" in preflight.headers["access-control-allow-headers"]


# ---------------------------------------------------------------- 载荷形状
def test_flat_records_without_raw_also_work(settings: Settings):
    """插件若不投 `raw`，只用它扁平化的那几个字段也要能判定。"""
    flat = {
        "sqbh": APPROVED["SQBH"],
        "status": APPROVED["SHZT_DISPLAY"],
        "statusCode": APPROVED["SHZT"],
        "room": "新教404",
        "date": "2026-09-10",
        "slot": "第4节(11:10-12:00)",
        "campus": "仙林校区",
        "purpose": "GHP",
    }
    with _client(settings) as client:
        assert _send(client, [flat]).json()["new"] == 1


def test_top_level_array_and_nested_shapes(settings: Settings):
    with _client(settings) as client:
        array = client.post(
            URL, content=json.dumps([{"raw": APPROVED}]), headers={"Content-Type": "text/plain"}
        )
        assert array.status_code == 200
        nested = client.post(
            URL,
            content=json.dumps(
                {"data": {"records": [{"raw": {**APPROVED, "SQBH": "another12345678"}}]}}
            ),
            headers={"Content-Type": "text/plain"},
        )
        assert nested.status_code == 200


def test_broken_payload_gets_a_readable_error(settings: Settings):
    with _client(settings) as client:
        assert (
            client.post(URL, content="not json", headers={"Content-Type": "text/plain"}).status_code
            == 400
        )
        bad = client.post(
            URL, content=json.dumps({"nothing": 1}), headers={"Content-Type": "text/plain"}
        )
        assert bad.status_code == 400
        assert "records" in bad.json()["error"]


def test_healthz_and_root(settings: Settings):
    with _client(settings) as client:
        assert client.get("/healthz").text == "ok"
        assert client.get("/").json()["post"] == URL


# ---------------------------------------------------------------- 认不出就不发
def test_records_that_cannot_be_matched_go_to_unmatched(settings: Settings):
    """对不上语雀活动的记录进 unmatched（等人看），**不进通知**。"""
    stranger = {
        **APPROVED,
        "SQBH": "ffffffffffffffffffffffffffffffff",
        "KSRQ": "2030-01-01",
        "JYYTMS": "毫不相干",
    }
    with _client(settings) as client:
        body = _send(client, [{"raw": stranger}]).json()
    assert body["new"] == 1 and body["unmatched"] == 1
    assert _notices(settings) == [], "认不出的不发通知"
    bucket = json.loads((settings.approval_dir() / "unmatched.json").read_text(encoding="utf-8"))
    assert bucket["unmatched"][0]["sqbh"] == stranger["SQBH"]


def test_approved_without_a_room_is_not_announced(settings: Settings):
    """「已通过」必须能读到教室；读不到就进 unmatched —— 一条没有教室的「已通过」会误导人。"""
    blind = {**APPROVED, "FJ": None, "JASMC": None}
    with _client(settings) as client:
        body = _send(client, [{"raw": blind}]).json()
    assert body["new"] == 1 and body["unmatched"] == 1
    assert _notices(settings) == []


# ---------------------------------------------------------------- 边界：这里没有入口
def test_the_endpoint_exposes_nothing_but_intake(settings: Settings):
    """路由清单是**钉死**的（`AGENTS.md` §1）。

    密钥必然公开（油猴脚本），所以最坏情况只能是「收到垃圾数据」，
    绝不能出现删除 / 触发 / 回调之类的入口。联调期要清假数据用
    `crb-notify forget`（机器上的 CLI），**别顺手给它开一条 HTTP 路由**。
    """
    routes = {
        (route.path, method)
        for route in intake.build_app(settings).routes
        for method in (getattr(route, "methods", None) or {""})
        # HEAD 是 Starlette 给 GET 白送的，不算多出来的入口
        if method in {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"}
    }
    assert routes == {
        ("/", "GET"),
        ("/healthz", "GET"),
        ("/intake/records", "POST"),
        ("/intake/records", "OPTIONS"),
    }, f"路由清单变了：{sorted(routes)}"
