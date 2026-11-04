"""PPPoE / 链路探测 / 安全模板的新增契约。

对应 2026-10-19 现场实测：POST /pppoes 返回 HTTP 200 但业务码非 0（code 22、code 1009），
以及僵尸网络模板缺失时设备直接拒绝。这里锁定后端的校验与阶段归属。
"""
import asyncio

import pytest

from af_app.database import Database
from af_app.devices import DeviceProfileCreate, DeviceService, security_options
from af_app.network_safety import NetworkSafetyError


class TestProtector:
    def protect(self, secret: str) -> str:
        return "test:" + secret[::-1]

    def unprotect(self, protected: str) -> str:
        return protected.removeprefix("test:")[::-1]


def network_interface(name, address, bits=24):
    return {"name": name, "ifMode": "ROUTE", "shutdown": False,
            "ipv4": {"ipv4Mode": "STATIC", "staticIp": [{"ipaddress": {
                "start": address, "end": address, "bits": bits}}]}}


MANAGEMENT = network_interface("eth0", "192.0.2.13")
BUSINESS = network_interface("eth1", "198.51.100.2")


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
        if path.endswith("/interfaces"):
            data = [MANAGEMENT, BUSINESS]
        elif path.endswith("/interfaces/eth0"):
            data = MANAGEMENT
        elif path.endswith("/interfaces/eth1"):
            data = BUSINESS
        else:
            data = []
        return {"code": 0, "data": data, "message": "成功"}


def build_service(tmp_path, name):
    database = Database(tmp_path / name)
    database.initialize()
    service = DeviceService(database, TestProtector(), FakeClient)
    profile = service.repository.create(DeviceProfileCreate(
        name="AF-WAN", base_url="https://192.0.2.13", username="api-admin",
        password="secret-password", verify_tls=False,
    ))
    return database, service, profile


def test_pppoe_and_linkdetect_capabilities_are_registered(tmp_path):
    database, service, _ = build_service(tmp_path, "capabilities.db")
    registry = service.capabilities
    expected = {
        "wan.pppoe.list": ("GET", "pppoes"),
        "wan.pppoe.create": ("POST", "pppoes"),
        "wan.pppoe.update": ("PATCH", "pppoes/{name}"),
        "wan.pppoe.status": ("GET", "pppoestatus/{name}"),
        "wan.pppoe.connect": ("POST", "pppoeconnect/{name}"),
        "probe.linkdetect.list": ("GET", "linkdetects"),
        "probe.linkdetect.status.list": ("GET", "status/linkdetects"),
    }
    for operation_id, (method, suffix) in expected.items():
        capability = registry.resolve(operation_id=operation_id, capability="edit"
                                     if method != "GET" else "query")
        assert capability.method == method
        assert capability.path.endswith("/" + suffix), capability.path
    database.engine.dispose()


def test_pppoe_body_must_be_flat_with_credentials(tmp_path):
    database, service, _ = build_service(tmp_path, "pppoe-body.db")
    with pytest.raises(ValueError, match="扁平对象"):
        service._validate_changes([{
            "operation_id": "wan.pppoe.create",
            "body": {"obj": {"name": "eth1.1", "user": "u", "password": "p"}},
            "decisions": {"on_existing": "abort"},
        }])
    with pytest.raises(ValueError, match="必须提供 password"):
        service._validate_changes([{
            "operation_id": "wan.pppoe.create",
            "body": {"name": "eth1.1", "user": "u"},
            "decisions": {"on_existing": "abort"},
        }])
    with pytest.raises(ValueError, match="PPPoE 实例名"):
        service._validate_changes([{
            "operation_id": "wan.pppoe.create",
            "body": {"name": "eth 1", "user": "u", "password": "p"},
            "decisions": {"on_existing": "abort"},
        }])
    validated, questions = service._validate_changes([{
        "operation_id": "wan.pppoe.create",
        "body": {"name": "eth1.1", "user": "0592923668393", "password": "secret"},
        "decisions": {"on_existing": "abort"},
    }])
    assert questions == []
    assert validated[0]["body"] == {"name": "eth1.1", "user": "0592923668393", "password": "secret"}
    database.engine.dispose()


def test_pppoe_connect_requires_oprtype(tmp_path):
    database, service, _ = build_service(tmp_path, "pppoe-connect.db")
    with pytest.raises(ValueError, match="oprtype"):
        service._validate_changes([{
            "operation_id": "wan.pppoe.connect",
            "path_parameters": {"name": "eth1.1"},
            "body": {"name": "eth1.1"},
        }])
    validated, _ = service._validate_changes([{
        "operation_id": "wan.pppoe.connect",
        "path_parameters": {"name": "eth1.1"},
        "body": {"name": "eth1.1", "oprtype": "LINKON"},
    }])
    assert validated[0]["body"]["oprtype"] == "LINKON"
    database.engine.dispose()


def test_wan_stage_plans_pppoe_and_rejects_other_stage_endpoint(tmp_path):
    database, service, profile = build_service(tmp_path, "pppoe-stage.db")
    plan = asyncio.run(service.initialize_device("plan", profile.device_id, "配置 PPPoE 拨号", [{
        "operation_id": "wan.pppoe.create",
        "body": {"name": "eth1.1", "user": "0592923668393", "password": "secret"},
        "decisions": {"on_existing": "abort"},
    }], stage="wan"))
    assert plan["kind"] == "initialization:wan" and plan["status"] == "planned"

    with pytest.raises(ValueError, match="does not belong to initialization stage"):
        asyncio.run(service.initialize_device("plan", profile.device_id, "错阶段", [{
            "operation_id": "policy.security.create",
            "body": {"name": "x", "policyType": "SERVER"},
            "decisions": {"on_existing": "abort", "initial_enabled": True},
        }], stage="wan"))
    database.engine.dispose()


def test_pppoe_instance_on_management_interface_is_blocked(tmp_path):
    database, service, profile = build_service(tmp_path, "pppoe-guard.db")
    with pytest.raises(NetworkSafetyError):
        asyncio.run(service.initialize_device("plan", profile.device_id, "管理口拨号", [{
            "operation_id": "wan.pppoe.create",
            "body": {"name": "eth0.1", "user": "u", "password": "p"},
            "decisions": {"on_existing": "abort"},
        }], stage="wan"))
    database.engine.dispose()


def test_security_options_only_reports_observed_values():
    results = [{"operation_id": "policy.security.list", "ok": True, "data": [
        {"name": "user", "response": {"block": {"enable": True, "type": "HIGH_THREATS"},
                                      "utm": {"enable": True, "template": "Default Template",
                                              "action": "DENY"}}},
        {"name": "server", "response": {"block": {"enable": False}}},
    ]}]
    options = security_options(results)
    assert options["block_types"] == ["HIGH_THREATS"]
    assert options["utm_templates"] == ["Default Template"]
    assert options["utm_actions"] == ["DENY"]
    assert options["authoritative"] is False
    assert options["observed_policies"] == 2


def test_route_description_rejects_device_banned_characters(tmp_path):
    database, service, _ = build_service(tmp_path, "route-desc.db")
    with pytest.raises(ValueError, match="不允许的字符"):
        service._validate_changes([{
            "operation_id": "route.ipv4.create",
            "body": {"prefix": "192.168.122.0/24", "gateway": "2.2.2.249",
                     "ifname": "eth5", "description": "回程/路由"},
            "decisions": {"on_existing": "abort"},
        }])
    validated, _ = service._validate_changes([{
        "operation_id": "route.ipv4.create",
        "body": {"prefix": "192.168.122.0/24", "gateway": "2.2.2.249",
                 "ifname": "eth5", "description": "回程路由"},
        "decisions": {"on_existing": "abort"},
    }])
    assert validated[0]["body"]["description"] == "回程路由"
    database.engine.dispose()
