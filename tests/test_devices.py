import asyncio
import json
import sqlite3
from pathlib import Path

import httpx

from af_app.database import Database
from af_app.devices import (
    AFAPIClient, DeviceProfileCreate, DeviceProfileUpdate, DeviceRepository, DeviceService,
)
from af_app.operations import CapabilityRegistry


class TestProtector:
    def protect(self, secret: str) -> str:
        return "test:" + secret[::-1]

    def unprotect(self, protected: str) -> str:
        return protected.removeprefix("test:")[::-1]


def test_device_repository_never_returns_password(tmp_path):
    database = Database(tmp_path / "devices.db")
    database.initialize()
    repository = DeviceRepository(database, TestProtector())
    created = repository.create(DeviceProfileCreate(
        name="AF-Lab", base_url="https://192.0.2.10", username="api-admin",
        password="secret-password", verify_tls=False,
    ))
    assert created.has_credentials is True
    assert "password" not in created.model_dump()
    profile, password = repository.get(created.device_id, include_secret=True)
    assert password == "secret-password"
    updated = repository.update(created.device_id, DeviceProfileUpdate(namespace="vsys1"))
    assert updated.namespace == "vsys1"
    repository.delete(created.device_id)
    assert repository.list() == []
    database.engine.dispose()


def test_af_api_login_cookie_keepalive_logout():
    requests = []

    def handler(request: httpx.Request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, request.headers.get("cookie"), body))
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={
                "code": 0, "message": "成功",
                "data": {"loginResult": {"token": "TOKEN-123"}, "role": "ADMINISTRATOR",
                         "namespace": "public", "authResult": "LOCAL"},
            })
        if request.url.path.endswith("/keepalive"):
            return httpx.Response(200, json={"code": 0, "message": "成功", "data": 0})
        if request.url.path.endswith("/logout"):
            return httpx.Response(200, json={"code": 0, "message": "成功", "data": {"name": "api-admin"}})
        return httpx.Response(404, json={"code": 404, "message": "missing"})

    profile = DeviceProfileCreate(
        name="AF-Test", base_url="https://af.example.test", username="api-admin",
        password="secret-password", verify_tls=False,
    )
    from af_app.devices import DeviceProfile
    public = DeviceProfile(
        device_id="device-1", name=profile.name, base_url=profile.base_url,
        namespace=profile.namespace, username=profile.username, verify_tls=False,
        ca_bundle=None, timeout_seconds=15, enabled=True, has_credentials=True,
        created_at="now", updated_at="now",
    )

    async def run():
        async with AFAPIClient(public, "secret-password", transport=httpx.MockTransport(handler)) as client:
            data = await client.login()
            assert data["role"] == "ADMINISTRATOR"
            await client.keepalive()
            await client.logout()

    asyncio.run(run())
    assert requests[0] == (
        "POST", "/api/v1/namespaces/public/login", None,
        {"name": "api-admin", "password": "secret-password"},
    )
    assert requests[1][0:3] == ("GET", "/api/v1/namespaces/public/keepalive", "token=TOKEN-123")
    assert requests[2][0:3] == ("POST", "/api/v1/namespaces/public/logout", "token=TOKEN-123")
    assert requests[2][3] == {"loginResult": {"token": "TOKEN-123"}}


def test_tls_error_is_actionable():
    cause = OSError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    error = httpx.ConnectError("certificate verify failed")
    error.__cause__ = cause
    assert "TLS 证书校验失败" in AFAPIClient._connection_error(error)


def test_device_service_records_connection_audit(tmp_path):
    class FakeClient:
        token = None

        def __init__(self, profile, password):
            assert password == "secret-password"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def login(self):
            self.token = "TOKEN"
            return {"role": "ADMINISTRATOR", "authResult": "LOCAL", "namespace": "public"}

        async def keepalive(self):
            return {"code": 0}

        async def logout(self):
            self.token = None

    database = Database(tmp_path / "audit.db")
    database.initialize()
    service = DeviceService(database, TestProtector(), FakeClient)
    profile = service.repository.create(DeviceProfileCreate(
        name="AF-Audit", base_url="https://192.0.2.11", username="api-admin",
        password="secret-password", verify_tls=False,
    ))
    result = asyncio.run(service.test_connection(profile.device_id))
    assert result.ok and result.role == "ADMINISTRATOR"
    audits = service.repository.list_audits(profile.device_id)
    assert len(audits) == 1 and audits[0].success and audits[0].stage == "completed"
    database.engine.dispose()


def test_capability_registry_generalizes_policy_language():
    registry = CapabilityRegistry()
    matches = registry.search("检查一下当前 AF 的全部策略配置", "query")
    operation_ids = {item.operation_id for item in matches}
    assert operation_ids == {
        "policy.nat.list", "policy.security.list",
        "policy.app_control.list", "policy.local_control.list",
    }
    assert registry.search("把 eth1 的描述改成电信出口", "edit")[0].operation_id == "interface.update"


def test_query_and_edit_plan_apply_are_separate(tmp_path):
    calls = []

    class FakeClient:
        token = None

        def __init__(self, profile, password):
            assert password == "secret-password"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def login(self):
            self.token = "TOKEN"
            return {}

        async def logout(self):
            self.token = None

        async def request(self, method, path, *, params=None, json_body=None):
            calls.append((method, path, params, json_body))
            if method == "GET" and path.endswith("/interfaces/eth1"):
                return {"code": 0, "data": {"name": "eth1", "description": "WAN"}, "message": "成功"}
            if method == "GET":
                return {"code": 0, "data": [{"name": "rule-1"}], "message": "成功"}
            return {"code": 0, "data": {"name": "eth1", "description": "电信出口"}, "message": "成功"}

    database = Database(tmp_path / "operations.db")
    database.initialize()
    service = DeviceService(database, TestProtector(), FakeClient)
    profile = service.repository.create(DeviceProfileCreate(
        name="AF-Operations", base_url="https://192.0.2.12", username="api-admin",
        password="secret-password", verify_tls=False,
    ))

    query = asyncio.run(service.query_device(
        profile.device_id, "检查当前全部策略配置", limit=10))
    assert query["ok"] is True
    assert len(query["resolved_operations"]) == 4
    assert all(call[0] == "GET" for call in calls)

    calls.clear()
    plan = asyncio.run(service.create_change_plan(
        profile.device_id, "把 eth1 描述改成电信出口", [{
            "operation_id": "interface.update",
            "path_parameters": {"name": "eth1"},
            "body": {"name": "eth1", "ifType": "PHYSICALIF", "description": "电信出口"},
        }], "edit"))
    assert plan["status"] == "planned" and plan["requires_confirmation"] is True
    assert [call[0] for call in calls] == ["GET"]  # preflight snapshot only

    applied = asyncio.run(service.apply_change_plan(plan["plan_id"], "edit"))
    assert applied["status"] == "applied"
    assert [call[0] for call in calls] == ["GET", "PATCH"]
    assert calls[-1][1].endswith("/interfaces/eth1")
    database.engine.dispose()


def test_initialization_inspect_and_plan(tmp_path):
    class FakeClient:
        token = None

        def __init__(self, profile, password):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def login(self):
            self.token = "TOKEN"
            return {}

        async def logout(self):
            self.token = None

        async def request(self, method, path, *, params=None, json_body=None):
            return {"code": 0, "data": [], "message": "成功"}

    database = Database(tmp_path / "initialize.db")
    database.initialize()
    service = DeviceService(database, TestProtector(), FakeClient)
    profile = service.repository.create(DeviceProfileCreate(
        name="AF-Initialize", base_url="https://192.0.2.13", username="api-admin",
        password="secret-password", verify_tls=False,
    ))
    inspection = asyncio.run(service.initialize_device("inspect", profile.device_id))
    assert inspection["read_only"] is True and len(inspection["results"]) == 10
    plan = asyncio.run(service.initialize_device("plan", profile.device_id, "初始化默认路由", [{
        "operation_id": "route.ipv4.create",
        "body": {"prefix": "0.0.0.0/0", "gateway": "192.0.2.1", "ifname": "auto"},
        "decisions": {"on_existing": "abort"},
    }], stage="routing"))
    assert plan["kind"] == "initialization:routing" and plan["status"] == "planned"
    database.engine.dispose()


def test_discovery_supports_short_resource_names_and_catalog():
    registry = CapabilityRegistry()
    assert {x.operation_id for x in registry.search("虚拟网线", "edit")} == {
        "virline.create", "virline.update", "virline.delete"}
    assert registry.search("黑名单", "edit")
    assert any(x["operation_id"] == "zone.update" for x in registry.catalog("edit"))
    assert registry.search("新增外到内安全防护策略", "edit")[0].operation_id == "policy.security.create"


def test_prepare_returns_preset_change_questions(tmp_path):
    database = Database(tmp_path / "prepare.db")
    database.initialize()
    service = DeviceService(database, TestProtector())
    result = service.prepare_change_questions(
        "新增或修改应用控制策略",
        operation_ids=["policy.app_control.create", "policy.app_control.update"],
    )
    question_ids = {item["id"] for item in result["questions"]}
    assert result["status"] == "clarification_required"
    assert question_ids == {"operation_mode", "on_existing", "initial_enabled"}

    _, missing = service._validate_changes([{
        "operation_id": "policy.app_control.create",
        "body": {"name": "policy-1"},
    }])
    assert {item["id"] for item in missing} == {
        "changes.0.on_existing", "changes.0.initial_enabled"}

    validated, missing = service._validate_changes([{
        "operation_id": "policy.app_control.create",
        "body": {"name": "policy-1"},
        "decisions": {"on_existing": "abort", "initial_enabled": False},
    }])
    assert missing == []
    assert validated[0]["body"]["enable"] is False

    security, missing = service._validate_changes([{
        "operation_id": "policy.security.create",
        "body": {"name": "server-protect", "policyType": "SERVER", "description": "外到内服务器防护"},
        "decisions": {"on_existing": "abort", "initial_enabled": True},
    }])
    assert missing == [] and security[0]["body"]["enable"] is True
    assert security[0]["body"]["response"]["block"] == {
        "enable": True, "type": "HIGH_THREATS"}

    explicit_off, missing = service._validate_changes([{
        "operation_id": "policy.security.create",
        "body": {
            "name": "server-protect-off", "policyType": "SERVER",
            "response": {"block": {"enable": False}},
        },
        "decisions": {"on_existing": "abort", "initial_enabled": True},
    }])
    assert missing == []
    assert explicit_off[0]["body"]["response"]["block"] == {
        "enable": False, "type": "HIGH_THREATS"}

    import pytest
    with pytest.raises(ValueError, match="扁平对象"):
        service._validate_changes([{
            "operation_id": "policy.security.create",
            "body": {"security": {"name": "bad"}},
            "decisions": {"on_existing": "abort", "initial_enabled": True},
        }])
    database.engine.dispose()


def test_initialization_requires_stage_and_verifies_before_next_change(tmp_path):
    import pytest

    routes = []
    calls = []

    class FakeClient:
        token = None

        def __init__(self, profile, password):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def login(self):
            self.token = "TOKEN"
            return {}

        async def logout(self):
            self.token = None

        async def request(self, method, path, *, params=None, json_body=None):
            calls.append(method)
            if method == "POST":
                routes.append(dict(json_body))
                return {"code": 0, "data": json_body, "message": "成功"}
            return {"code": 0, "data": routes if path.endswith("/staticroutes/ipv4") else [],
                    "message": "成功"}

    database = Database(tmp_path / "staged.db")
    database.initialize()
    service = DeviceService(database, TestProtector(), FakeClient)
    profile = service.repository.create(DeviceProfileCreate(
        name="AF-Staged", base_url="https://192.0.2.30", username="api-admin",
        password="secret-password", verify_tls=False,
    ))
    change = {"operation_id": "route.ipv4.create",
              "body": {"prefix": "0.0.0.0/0", "gateway": "192.0.2.1", "ifname": "eth1"},
              "decisions": {"on_existing": "abort"}}
    with pytest.raises(ValueError, match="stage is required"):
        asyncio.run(service.initialize_device("plan", profile.device_id, changes=[change]))
    plan = asyncio.run(service.initialize_device("plan", profile.device_id, changes=[change], stage="routing"))
    with pytest.raises(ValueError, match="Previous initialization plan"):
        asyncio.run(service.initialize_device("plan", profile.device_id, changes=[change], stage="routing"))
    assert "POST" not in calls
    applied = asyncio.run(service.initialize_device("apply", profile.device_id, plan_id=plan["plan_id"]))
    assert applied["status"] == "applied"
    with pytest.raises(ValueError, match="only planned"):
        asyncio.run(service.initialize_device("apply", profile.device_id, plan_id=plan["plan_id"]))
    verified = asyncio.run(service.initialize_device("verify", profile.device_id, plan_id=plan["plan_id"]))
    assert verified["status"] == "verified"
    database.engine.dispose()


def test_initialization_case_feedback_keeps_implementation_snapshot(tmp_path):
    import csv
    import io
    import pytest

    interface = {"name": "eth1", "ifType": "PHYSICALIF", "description": "before"}
    zones = []

    class FakeClient:
        token = None

        def __init__(self, profile, password):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def login(self):
            self.token = "TOKEN"
            return {}

        async def logout(self):
            self.token = None

        async def request(self, method, path, *, params=None, json_body=None):
            if method == "PATCH":
                interface.update(json_body)
                return {"code": 0, "data": dict(interface), "message": "成功"}
            data = dict(interface) if path.endswith("/interfaces/eth1") else (
                [dict(interface)] if path.endswith("/interfaces") else
                [dict(zone) for zone in zones] if path.endswith("/zones") else [])
            return {"code": 0, "data": data, "message": "成功"}

    database = Database(tmp_path / "case.db")
    database.initialize()
    output_directory = tmp_path / "results"
    service = DeviceService(database, TestProtector(), FakeClient, output_directory)
    profile = service.repository.create(DeviceProfileCreate(
        name="AF-Case", base_url="https://192.0.2.31", username="api-admin",
        password="secret-password", verify_tls=False,
    ))
    empty_case = asyncio.run(service.initialize_device("design", profile.device_id, design={}))
    assert empty_case["design_revision"] == 0 and "topology" in empty_case["missing_context"]
    case = asyncio.run(service.initialize_device(
        "design", profile.device_id, design={"deployment_mode": "routed", "interfaces": [{"name": "eth1"}],
                                             "default_choices": {"nat": "review proposal"}}))
    case_id = case["case_id"]
    assert not case["design_confirmed"]
    with pytest.raises(ValueError, match="Design is incomplete"):
        asyncio.run(service.initialize_device(
            "design", profile.device_id, case_id=case_id, confirm_design=True))
    with pytest.raises(ValueError, match="credential field"):
        asyncio.run(service.initialize_device(
            "design", profile.device_id, case_id=case_id,
            design={"notes": {"token": "must-not-be-stored"}}))
    case = asyncio.run(service.initialize_device(
        "design", profile.device_id, case_id=case_id,
        design={"topology": "AF between LAN and uplink", "zones": [{"name": "LAN", "role": "inside"}],
                "routing": [], "preserve_existing": True, "business_subnets": ["192.0.2.0/24"],
                "protection_scope": "LAN egress", "default_choices": {
                    "security": "review deny baseline", "application": "review scoped allow",
                }}, confirm_design=True))
    assert case["design_revision"] == 2 and case["design_confirmed"]
    assert case["design"]["default_choices"]["nat"] == "review proposal"
    change = {"operation_id": "interface.update", "path_parameters": {"name": "eth1"},
              "body": {"name": "eth1", "ifType": "PHYSICALIF", "description": "trial"}}
    plan = asyncio.run(service.initialize_device(
        "plan", profile.device_id, changes=[change], stage="topology", case_id=case_id))
    with pytest.raises(ValueError, match="case_id is required"):
        asyncio.run(service.initialize_device("apply", profile.device_id, plan_id=plan["plan_id"]))
    asyncio.run(service.initialize_device("apply", profile.device_id, plan_id=plan["plan_id"], case_id=case_id))
    verified = asyncio.run(service.initialize_device(
        "verify", profile.device_id, plan_id=plan["plan_id"], case_id=case_id))
    assert verified["status"] == "verified"
    stored = service.plans.get(plan["plan_id"])
    assert stored["result"]["apply"]["ok"] and stored["result"]["verification"]["ok"]
    interface["description"] = "engineer-adjusted"
    zones.append({"name": "NEW-ZONE", "type": "ROUTING"})
    review = asyncio.run(service.initialize_device("feedback_review", profile.device_id, case_id=case_id))
    assert review["ok"] and len(review["rows"]) == 2
    assert Path(review["csv_path"]).parent == output_directory
    assert Path(review["csv_path"]).read_text(encoding="utf-8").startswith("\ufeffcase_id")
    row = review["rows"][0]
    assert row["before"]["description"] == "before"
    assert row["implemented"]["description"] == "trial"
    assert row["current"]["description"] == "engineer-adjusted"
    assert "description" in row["difference"]
    assert review["rows"][1]["difference"] == "engineer_added_or_external_change"
    csv_rows = list(csv.DictReader(io.StringIO(review["csv_template"].lstrip("\ufeff"))))
    for index, csv_row in enumerate(csv_rows):
        csv_row["difference_confirmed"] = "yes"
        csv_row["satisfaction"] = "unsatisfied" if index == 0 else "satisfied"
        csv_row["reason"] = "现场描述需调整" if index == 0 else ""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=csv_rows[0].keys())
    writer.writeheader()
    writer.writerows(csv_rows)
    feedback = asyncio.run(service.initialize_device(
        "feedback_submit", profile.device_id, case_id=case_id, feedback_csv=output.getvalue()))
    assert feedback["recorded"] == 2 and feedback["unsatisfied"] == 1
    interface["description"] = "changed-again"
    with pytest.raises(ValueError, match="snapshot changed"):
        asyncio.run(service.initialize_device(
            "feedback_submit", profile.device_id, case_id=case_id, feedback_csv=output.getvalue()))
    events = asyncio.run(service.initialize_device("events", profile.device_id, case_id=case_id))
    assert any(item["action"] == "feedback_submit" for item in events["events"])
    assert "secret-password" not in str(events)
    next_plan = asyncio.run(service.initialize_device(
        "plan", profile.device_id, changes=[change], stage="topology", case_id=case_id))
    asyncio.run(service.initialize_device(
        "design", profile.device_id, case_id=case_id, design={"notes": "site requirement changed"}))
    assert service.plans.get(next_plan["plan_id"])["status"] == "stale"
    with pytest.raises(ValueError, match="design changed"):
        asyncio.run(service.initialize_device(
            "apply", profile.device_id, plan_id=next_plan["plan_id"], case_id=case_id))
    database.engine.dispose()


def test_change_contract_rejects_payload_and_namespace_override():
    import pytest
    from pydantic import ValidationError
    from af_app.operations import DeviceChange
    with pytest.raises(ValidationError):
        DeviceChange.model_validate({"operation_id":"interface.update", "payload":{"name":"eth3"}})
    registry = CapabilityRegistry()
    with pytest.raises(ValueError, match="Unexpected path"):
        registry.render_path(registry.get("interface.get"), "public", {"name":"eth3", "namespace":"other"})


def test_unregistered_manual_endpoints_are_executable_through_endpoint_id(tmp_path):
    calls = []

    class FakeClient:
        token = None

        def __init__(self, profile, password):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def login(self):
            self.token = "TOKEN"
            return {}

        async def logout(self):
            self.token = None

        async def request(self, method, path, *, params=None, json_body=None):
            calls.append((method, path, params, json_body))
            return {"code": 0, "data": {"name": "custom-1"}, "message": "成功"}

    database = Database(tmp_path / "dynamic.db")
    database.initialize()
    get_raw = {
        "endpoint_id": "manual#get", "method": "GET",
        "path": "https://{host}/api/v1/namespaces/@namespace/customs/@name",
        "query_parameters": [{"name": "detail"}], "request_body_schema": None,
    }
    patch_raw = {
        "endpoint_id": "manual#patch", "method": "PATCH",
        "path": "https://{host}/api/v1/namespaces/@namespace/customs/@name",
        "query_parameters": [],
        "request_body_schema": {
            "type": "object", "required": ["name"],
            "properties": {"name": {"type": "string"}, "enabled": {"type": "boolean"}},
        },
    }
    with sqlite3.connect(database.path) as connection:
        for endpoint_id, title, method, raw in (
            ("manual#get", "查询自定义资源", "GET", get_raw),
            ("manual#patch", "修改自定义资源", "PATCH", patch_raw),
        ):
            connection.execute(
                """INSERT INTO api_endpoints
                   (endpoint_id,doc_id,title,method,path,summary,page_start,page_end,source_chunk_ids_json,raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (endpoint_id, "manual", title, method, raw["path"], title, 1, 1, "[]", json.dumps(raw)),
            )

    service = DeviceService(database, TestProtector(), FakeClient)
    profile = service.repository.create(DeviceProfileCreate(
        name="AF-Dynamic", base_url="https://192.0.2.20", username="api-admin",
        password="secret-password", verify_tls=False,
    ))
    queried = asyncio.run(service.query_device(
        profile.device_id, "", endpoint_ids=["manual#get"],
        path_parameters={"name": "custom-1"}, query_parameters={"detail": True},
    ))
    assert queried["ok"] is True
    assert calls[-1][:3] == (
        "GET", "/api/v1/namespaces/public/customs/custom-1", {"detail": True})

    discovered = service.discover("查询自定义资源", "query")
    assert discovered["documented_endpoints"][0]["endpoint_id"] == "manual#get"
    assert discovered["documented_endpoints"][0]["executable"] is True

    plan = asyncio.run(service.create_change_plan(profile.device_id, "启用 custom-1", [{
        "endpoint_id": "manual#patch", "path_parameters": {"name": "custom-1"},
        "body": {"name": "custom-1", "enabled": True},
    }]))
    assert plan["status"] == "planned"
    assert plan["snapshots"][0]["endpoint_id"] == "manual#get"
    applied = asyncio.run(service.apply_change_plan(plan["plan_id"], "edit"))
    assert applied["status"] == "applied"
    assert calls[-1][0] == "PATCH"
    database.engine.dispose()
