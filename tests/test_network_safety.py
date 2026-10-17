import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest
from mcp import Client

from af_app.database import Database
from af_app.devices import DeviceProfileCreate, DeviceProfileUpdate, DeviceService
from af_app.network_safety import NetworkSafetyError, inventory_complete, network_report
from af_app.mcp_server import create_mcp


def interface(name, address):
    return {"name": name, "uuid": name + "-id", "shutdown": False, "ifMode": "ROUTE",
            "ipv4": {"ipv4Mode": "STATIC", "staticIp": [{"ipaddress": {
                "start": address, "end": address, "bits": 24}}]}}


def inventory():
    return [{"operation_id": key, "ok": True, "data": values} for key, values in {
        "interface.list": [interface("eth0", "10.251.251.251"), interface("eth1", "198.51.100.2")],
        "zone.list": [{"name": "manager", "interfaces": ["eth0"]}],
        "virline.list": [], "route.ipv4.list": [],
    }.items()]


def data(results, operation):
    return next(x["data"] for x in results if x["operation_id"] == operation)


def codes(report):
    return {x["code"] for x in report["issues"]}


ROUTE_PATH = "/api/v1/namespaces/{namespace}/staticroutes/ipv4"
ROUTE = {"prefix": "0.0.0.0/0", "gateway": "198.51.100.1", "ifname": "eth1", "enable": True}
BASE_URL = "https://10.251.251.251:443"


@pytest.mark.parametrize("root,target,body", [
    ("interfaces", "eth0", {"description": "even description changes are forbidden"}),
    ("interfaces", "eth1", {"ipv4": {"staticIp": [{"ipaddress": {"start": "10.251.251.251"}}]}}),
    ("zones", "manager", {"interfaces": []}),
    ("zones", "other-zone", {"interfaces": ["eth0"]}),
    ("virlines", "management-line", {"line1": "eth1", "line2": "eth1"}),
    ("localappcontrols/policys", "allow-api", {"enable": False}),
])
def test_management_ip_and_indirect_dependencies_are_blocked(root, target, body):
    results = inventory()
    data(results, "virline.list").append({"name": "management-line", "line1": "eth0", "line2": "eth1"})
    report = network_report(BASE_URL, results, path=f"/api/v1/namespaces/{{namespace}}/{root}/{{name}}",
                            method="PATCH", step={"path_parameters": {"name": target}, "body": body})
    assert not report["write_allowed"]


def test_aggregate_parent_and_peer_member_are_protected():
    results = inventory()
    interfaces = data(results, "interface.list")
    interfaces[0]["subif"] = {"fatherInterface": "agg0"}
    interfaces += [{"name": "agg0", "channelif": {"ethSelect": ["eth2", "eth3"]}},
                   {"name": "eth2"}, {"name": "eth3"}]
    for name in ("agg0", "eth2", "eth3"):
        report = network_report(BASE_URL, results, step={"path_parameters": {"name": name}, "body": {"shutdown": True}},
                                path="/api/v1/namespaces/{namespace}/interfaces/{name}", method="PATCH")
        assert not report["write_allowed"]


@pytest.mark.parametrize("url,mutator", [
    ("https://af.example.test", lambda x: None),
    (BASE_URL, lambda x: data(x, "interface.list").clear()),
    (BASE_URL, lambda x: data(x, "interface.list").append(interface("eth2", "10.251.251.251"))),
])
def test_ambiguous_management_identity_never_allows_network_writes(url, mutator):
    results = inventory()
    mutator(results)
    report = network_report(url, results, step={"body": ROUTE}, path=ROUTE_PATH, method="POST")
    assert not report["write_allowed"] and "management_unknown" in codes(report)


def test_description_containing_ip_is_not_an_interface_address():
    results = inventory()
    data(results, "interface.list")[1]["description"] = "10.251.251.251"
    report = network_report(BASE_URL, results)
    assert report["management_connection"]["interface"] == "eth0"


def test_business_interface_cannot_add_an_overlapping_management_subnet():
    report = network_report(BASE_URL, inventory(), method="PATCH",
                            path="/api/v1/namespaces/{namespace}/interfaces/{name}",
                            step={"path_parameters": {"name": "eth1"}, "body": interface("eth1", "10.251.251.1")})
    assert not report["write_allowed"] and "management_subnet_overlap" in codes(report)


@pytest.mark.parametrize("missing", ["identity", "mask"])
def test_partial_management_configuration_is_not_safe_to_write(missing):
    results = inventory()
    management = data(results, "interface.list")[0]
    if missing == "identity":
        management.pop("name")
        management.pop("uuid")
    else:
        management["ipv4"]["staticIp"][0]["ipaddress"].pop("bits")
    report = network_report(BASE_URL, results, path=ROUTE_PATH, method="POST", step={"body": ROUTE})
    assert not report["write_allowed"]


@pytest.mark.parametrize("patch,expected", [
    ({"gateway": "198.51.100.2"}, "gateway_is_self"),
    ({"gateway": "203.0.113.1"}, "gateway_not_on_link"),
    ({"gateway": "198.51.100.255"}, "gateway_network_address"),
    ({"gateway": "224.1.1.1"}, "gateway_invalid"),
    ({"ifname": "eth99"}, "route_interface_missing"),
    ({"ifname": "eth0", "gateway": "10.251.251.1"}, "management_business_route"),
    ({"tableId": 250}, "management_route"),
    ({"prefix": "bad prefix"}, "route_address_invalid"),
])
def test_route_errors_are_blocked(patch, expected):
    report = network_report(BASE_URL, inventory(), step={"body": {**ROUTE, **patch}}, path=ROUTE_PATH, method="POST")
    assert not report["write_allowed"] and expected in codes(report)


def test_disabled_route_interface_and_duplicate_prefix_are_blocked():
    results = inventory()
    data(results, "interface.list")[1]["shutdown"] = True
    data(results, "route.ipv4.list").append({**ROUTE, "prefix": "0.1.2.3/0"})
    report = network_report(BASE_URL, results, step={"body": ROUTE}, path=ROUTE_PATH, method="POST")
    assert {"route_interface_disabled", "route_prefix_exists"} <= codes(report)
    assert not report["write_allowed"]


def test_cannot_move_existing_management_route_to_business_table():
    results = inventory()
    data(results, "route.ipv4.list").append({**ROUTE, "uuid": "route-1", "tableId": 250})
    report = network_report(BASE_URL, results, path=ROUTE_PATH + "/{uuid}", method="PATCH",
                            step={"path_parameters": {"uuid": "route-1"}, "body": {"tableId": 251}})
    assert "management_route" in codes(report) and not report["write_allowed"]


@pytest.mark.parametrize("payload", [
    {"items": [{"name": "eth0"}], "totalItems": 2},
    {"items": [], "totalPages": 2},
    {"items": [], "itemsOffset": 1},
    {"items": [], "pageNumber": 2},
    {"items": [{"name": "eth0"}] * 100},
    {"items": [{"channelif": {"ethSelect": ["eth1"] * 100}}]},
    {"items": {}}, None,
])
def test_truncated_or_unknown_inventory_is_not_complete(payload):
    assert not inventory_complete(payload, 100)


def test_design_routes_and_live_routes_have_different_completion_states():
    results = inventory()
    design = {"network_context": {"needs_default_route": True, "routing_mode": "static", "uplink_interface": "eth1"}, "routing": []}
    report = network_report(BASE_URL, results, design=design)
    assert report["design_ready"] and "route_missing" in codes(report)
    design["routing"] = [dict(ROUTE)]
    report = network_report(BASE_URL, results, design=design)
    assert "route_pending" in codes(report) and not report["configuration_complete"]
    data(results, "route.ipv4.list").append(dict(ROUTE))
    report = network_report(BASE_URL, results, design=design)
    assert report["configuration_complete"] and not report["business_connectivity_verified"]


def test_transparent_and_dynamic_modes_do_not_invent_default_routes():
    transparent = {"network_context": {"needs_default_route": False, "routing_mode": "none"}, "routing": []}
    report = network_report(BASE_URL, inventory(), design=transparent)
    assert report["design_ready"] and "route_missing" not in codes(report)
    dynamic = {"network_context": {"needs_default_route": True, "routing_mode": "dynamic", "uplink_interface": "eth1"}, "routing": []}
    report = network_report(BASE_URL, inventory(), design=dynamic)
    assert "dynamic_routes_unverified" in codes(report) and "route_missing" not in codes(report)
    assert not report["configuration_complete"]


def test_business_subnet_requires_a_specific_or_connected_path():
    results = inventory()
    data(results, "route.ipv4.list").append(dict(ROUTE))
    design = {"network_context": {"needs_default_route": True, "routing_mode": "static", "uplink_interface": "eth1"},
              "business_subnets": ["172.16.10.0/24"], "routing": []}
    report = network_report(BASE_URL, results, design=design)
    assert "route_missing" in codes(report) and not report["configuration_complete"]
    data(results, "interface.list").append(interface("eth2", "172.16.10.1"))
    report = network_report(BASE_URL, results, design=design)
    assert report["configuration_complete"]


def test_unknown_route_state_cannot_satisfy_the_default_route_requirement():
    results = inventory()
    route = dict(ROUTE)
    route.pop("enable")
    data(results, "route.ipv4.list").append(route)
    design = {"network_context": {"needs_default_route": True, "routing_mode": "static", "uplink_interface": "eth1"}, "routing": []}
    report = network_report(BASE_URL, results, design=design)
    assert {"route_state_unverified", "route_missing"} <= codes(report)
    assert not report["configuration_complete"]


@pytest.fixture
def service_state(tmp_path):
    from test_devices import TestProtector
    state = inventory()
    writes = []
    class FakeClient:
        token = None
        def __init__(self, *_):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def login(self):
            self.token = "test"
        async def logout(self):
            self.token = None
        async def request(self, method, path, **kwargs):
            if method != "GET":
                writes.append((method, path, kwargs))
                if method == "POST":
                    data(state, "route.ipv4.list").append(copy.deepcopy(kwargs["json_body"]))
                return {"data": copy.deepcopy(kwargs["json_body"])}
            roots = {"interfaces": "interface.list", "zones": "zone.list", "virlines": "virline.list", "staticroutes/ipv4": "route.ipv4.list"}
            for root, operation in roots.items():
                if path.endswith("/" + root):
                    values = copy.deepcopy(data(state, operation))
                    return {"data": {"items": values, "totalItems": len(values), "totalPages": 1}}
                if "/" + root + "/" in path:
                    name = path.rsplit("/", 1)[-1]
                    return {"data": copy.deepcopy(next(x for x in data(state, operation) if name in (x.get("name"), x.get("uuid"))))}
            return {"data": []}
    db = Database(tmp_path / "network.db")
    db.initialize()
    service = DeviceService(db, TestProtector(), FakeClient)
    profile = service.repository.create(DeviceProfileCreate(name="AF", base_url=BASE_URL, username="admin", password="test"))
    yield service, profile, state, writes
    db.engine.dispose()


def test_ordinary_editor_cannot_bypass_management_protection(service_state):
    service, profile, _, writes = service_state
    with pytest.raises(NetworkSafetyError):
        asyncio.run(service.create_change_plan(profile.device_id, "rename management zone", [{
            "operation_id": "zone.update", "path_parameters": {"name": "manager"}, "body": {"interfaces": []}}]))
    assert writes == []
    with sqlite3.connect(service.plans.database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operation_plans").fetchone()[0] == 0


def route_change():
    return {"operation_id": "route.ipv4.create", "body": dict(ROUTE), "decisions": {"on_existing": "abort"}}


@pytest.mark.parametrize("mutation", ["address", "inventory", "invalid_gateway", "old_plan"])
def test_apply_rechecks_connection_and_inventory_before_writing(service_state, mutation):
    service, profile, state, writes = service_state
    plan = asyncio.run(service.create_change_plan(profile.device_id, "default route", [route_change()]))
    if mutation == "address":
        service.repository.update(profile.device_id, DeviceProfileUpdate(base_url="https://10.251.251.251:8443"))
    elif mutation == "inventory":
        data(state, "zone.list").append({"name": "changed"})
    elif mutation == "invalid_gateway":
        data(state, "interface.list")[1] = interface("eth1", "198.51.100.1")
    else:
        with sqlite3.connect(service.plans.database.path) as connection:
            connection.execute("UPDATE operation_plans SET snapshots_json='[]' WHERE plan_id=?", (plan["plan_id"],))
    with pytest.raises(ValueError):
        asyncio.run(service.apply_change_plan(plan["plan_id"], "edit"))
    assert writes == [] and service.plans.get(plan["plan_id"])["status"] == "planned"


def test_route_plan_apply_and_verify_with_paginated_af_response(service_state):
    service, profile, _, writes = service_state
    plan = asyncio.run(service.initialize_device("plan", profile.device_id, stage="routing", changes=[route_change()]))
    assert plan["network_checks"]["management_connection"]["interface"] == "eth0" and writes == []
    asyncio.run(service.initialize_device("apply", profile.device_id, plan_id=plan["plan_id"]))
    verified = asyncio.run(service.initialize_device("verify", profile.device_id, plan_id=plan["plan_id"]))
    assert verified["status"] == "verified" and len(writes) == 1
    assert not verified["network_checks"]["business_connectivity_verified"]


def complete_design():
    return {"deployment_mode": "routed", "topology": "PC on eth0, LAN eth2, uplink eth1", "interfaces": [{"name": "eth1"}],
            "zones": [{"name": "LAN"}], "routing": [], "preserve_existing": True,
            "business_subnets": ["172.16.0.0/24"], "protection_scope": "LAN", "default_choices": {
                "nat": "review", "security": "review", "application": "review"},
            "network_context": {"needs_default_route": True, "routing_mode": "static", "uplink_interface": "eth1"}}


def test_missing_final_route_does_not_block_earlier_stage_or_repeat_answers(service_state):
    service, profile, _, writes = service_state
    case = asyncio.run(service.initialize_device("design", profile.device_id, design=complete_design(), confirm_design=True))
    assert "route_missing" in codes(case["network_checks"])
    change = {"operation_id": "interface.update", "path_parameters": {"name": "eth1"}, "body": {"description": "business"}}
    prepared = asyncio.run(service.initialize_device("prepare", profile.device_id, case_id=case["case_id"], stage="topology", changes=[change]))
    assert prepared["required_context"] == []
    plan = asyncio.run(service.initialize_device("plan", profile.device_id, case_id=case["case_id"], stage="topology", changes=[change]))
    assert plan["status"] == "planned" and writes == []


def test_confirmation_rejects_target_topology_using_management_port(service_state):
    service, profile, _, writes = service_state
    design = complete_design()
    design["network_context"]["uplink_interface"] = "eth0"
    case = asyncio.run(service.initialize_device("design", profile.device_id, design=design))
    with pytest.raises(NetworkSafetyError):
        asyncio.run(service.initialize_device("design", profile.device_id, case_id=case["case_id"], confirm_design=True))
    assert not service.initialization_cases.get(case["case_id"], profile.device_id)["design_confirmed"]
    assert writes == []


def test_manual_endpoint_and_mcp_replies_enforce_the_same_protection(service_state):
    service, profile, _, writes = service_state
    # A documented endpoint uses the same resource guard as the operation registry.
    raw = {"request_body_schema": {"type": "object", "properties": {"description": {"type": "string"}}}}
    with sqlite3.connect(service.plans.database.path) as connection:
        connection.execute("INSERT INTO api_endpoints(endpoint_id,doc_id,title,method,path,raw_json,source_chunk_ids_json) VALUES(?,?,?,?,?,?,?)",
            ("manual#interface-patch", "test", "Interface", "PATCH",
             "https://{host}/api/v1/namespaces/@namespace/interfaces/{name}", json.dumps(raw), "[]"))
    async def run():
        server = create_mcp(SimpleNamespace(devices=service), None)
        async with Client(server) as client:
            for tool, arguments in [
                ("edit_device", {"action": "plan", "device_id": profile.device_id, "changes": [{
                    "endpoint_id": "manual#interface-patch", "path_parameters": {"name": "eth0"}, "body": {"description": "no"}}]}),
                ("initialize_device", {"action": "plan", "stage": "topology", "device_id": profile.device_id, "changes": [{
                    "operation_id": "zone.update", "path_parameters": {"name": "manager"}, "body": {"interfaces": []}}]}),
            ]:
                response = await client.call_tool(tool, arguments)
                assert not response.is_error
                result = response.structured_content or json.loads(response.content[0].text)
                assert result["status"] == "network_check_blocked"
                assert not result["network_checks"]["write_allowed"]
                assert "10.251.251.251" in result["network_checks"]["user_message"]
    asyncio.run(run())
    assert writes == []


def test_incomplete_pagination_blocks_planning_before_any_write(service_state):
    service, profile, state, writes = service_state
    data(state, "zone.list").extend({"name": f"zone-{i}"} for i in range(100))
    with pytest.raises(ValueError, match="未读全"):
        asyncio.run(service.create_change_plan(profile.device_id, "route", [route_change()]))
    assert writes == []


def test_target_interface_config_is_not_mistaken_for_live_connectivity():
    results = inventory()
    data(results, "route.ipv4.list").append(dict(ROUTE))
    data(results, "interface.list")[1] = interface("eth1", "203.0.113.2")
    design = complete_design()
    design["interfaces"] = [interface("eth1", "198.51.100.2")]
    report = network_report(BASE_URL, results, design=design)
    assert "interface_pending" in codes(report) and not report["configuration_complete"]
    # The route write itself is checked against current interfaces, never the future design.
    report = network_report(BASE_URL, results, design=design, step={"body": ROUTE}, path=ROUTE_PATH, method="POST")
    assert not report["write_allowed"] and "gateway_not_on_link" in codes(report)
