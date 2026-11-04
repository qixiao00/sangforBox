import asyncio
from types import SimpleNamespace

import pytest

from af_app.operations import CapabilityRegistry
from af_app.policy_snapshot import PolicySnapshotCollector, SnapshotError


def fixture(mutate=None):
    calls = []
    records = [dict(uuid=str(i), name=f"r{i}", position=i, hits=0,
                    dst={"applications": [f"app{j}" for j in range(150)]}) for i in range(3)]
    class Client:
        token = None
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def login(self):
            self.token = "secret"
        async def logout(self):
            self.token = None
        async def request(self, method, path, params):
            calls.append((method, path, params.copy()))
            start, length = params["_start"], params["_length"]
            data = {"totalItems": 3, "itemsOffset": start, "itemLength": len(records[start:start + length]),
                    "items": [dict(r) for r in records[start:start + length]]}
            if mutate:
                mutate(data, len(calls))
            return {"code": 0, "data": data}
    client = Client()
    service = SimpleNamespace(resolve_device=lambda d: d, capabilities=CapabilityRegistry(),
                              repository=SimpleNamespace(get=lambda *a, **k: (SimpleNamespace(enabled=True, namespace="public"), "pw")),
                              client_factory=lambda *a: client)
    return PolicySnapshotCollector(service, page_size=2), calls, client


def test_full_capture_preserves_nested_lists_and_order():
    collector, calls, client = fixture()
    snapshot = asyncio.run(collector.capture("d"))
    rules = snapshot["configuration"]["policy.app_control.list"]
    assert [r["position"] for r in rules] == [0, 1, 2]
    assert len(rules[0]["dst"]["applications"]) == 150
    assert "hits" not in rules[0]
    assert len(calls) == 12 and all(c[0] == "GET" for c in calls)
    assert client.token is None


@pytest.mark.parametrize("field,value", [("totalItems", 4), ("itemsOffset", 0), ("itemLength", 2)])
def test_inconsistent_pages_rejected(field, value):
    def mutate(data, call):
        if call == 2:
            data[field] = value
    collector, _, client = fixture(mutate)
    with pytest.raises(SnapshotError):
        asyncio.run(collector.capture("d"))
    assert client.token is None


def test_duplicate_resource_rejected():
    def mutate(data, call):
        if call == 2:
            data["items"][0]["uuid"] = "0"
    collector, _, _ = fixture(mutate)
    with pytest.raises(SnapshotError, match="duplicate"):
        asyncio.run(collector.capture("d"))


def test_drift_rejected_but_hit_counters_not_configuration():
    def mutate(data, call):
        if call > 6:
            for item in data["items"]:
                item["description"] = "changed"
    collector, _, _ = fixture(mutate)
    with pytest.raises(SnapshotError, match="changed during capture"):
        asyncio.run(collector.capture("d"))
    def hits(data, call):
        if call in (7, 8):
            for item in data["items"]:
                item["hits"] = 100
    collector, _, _ = fixture(hits)
    assert asyncio.run(collector.capture("d"))["configuration_hash"]
