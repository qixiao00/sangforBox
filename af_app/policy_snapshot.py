"""Full configuration capture through the existing device-service client.

Two matching reads detect drift; they are not a device transaction or a promise
that an external administrator cannot change configuration afterwards.
"""
import json
from datetime import datetime, timezone

from af_app.policy_review import fingerprint


class SnapshotError(ValueError):
    pass


OPERATIONS = ("policy.app_control.list", "network_object.list", "policy.app_control.group.list")
VOLATILE_POLICY_FIELDS = {"hits", "lastHitTime"}


def configuration(snapshot):
    result = {}
    for key, rows in snapshot.items():
        result[key] = [{k: v for k, v in row.items()
                        if not (key == "policy.app_control.list" and k in VOLATILE_POLICY_FIELDS)}
                       for row in rows]
    return result


class PolicySnapshotCollector:
    def __init__(self, devices, *, page_size=100, max_items=100000, max_bytes=50_000_000):
        if not 1 <= page_size <= 1000:
            raise ValueError("Invalid page size")
        self.devices = devices
        self.page_size, self.max_items, self.max_bytes = page_size, max_items, max_bytes

    async def _read(self, client, profile, operation_id):
        capability = self.devices.capabilities.get(operation_id, "query")
        if capability.method != "GET":
            raise SnapshotError("Snapshot operation must be GET")
        path = self.devices.capabilities.render_path(capability, profile.namespace, {})
        rows, seen, offset, total, size = [], set(), 0, None, 0
        while True:
            payload = await client.request("GET", path, params={"_start": offset, "_length": self.page_size})
            if payload.get("code") != 0:
                raise SnapshotError(f"Device rejected snapshot read: {operation_id}")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise SnapshotError(f"Unsupported pagination shape: {operation_id}")
            items = data["items"]
            for field in ("totalItems", "itemsOffset", "itemLength"):
                if type(data.get(field)) is not int or data[field] < 0:
                    raise SnapshotError(f"Missing or invalid pagination metadata: {field}")
            if data["itemsOffset"] != offset or data["itemLength"] != len(items):
                raise SnapshotError("Offset or page length mismatch")
            if total is None:
                total = data["totalItems"]
            if total != data["totalItems"] or total > self.max_items:
                raise SnapshotError("Collection changed or exceeds snapshot item limit")
            if len(items) > self.page_size or offset + len(items) > total:
                raise SnapshotError("Inconsistent page size or total")
            if not items and offset < total:
                raise SnapshotError("Missing page before advertised total")
            for item in items:
                if not isinstance(item, dict):
                    raise SnapshotError("Invalid resource record")
                identity = item.get("uuid") or item.get("name")
                if not isinstance(identity, str) or not identity or identity in seen:
                    raise SnapshotError("Missing or duplicate resource identity")
                seen.add(identity)
                size += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                if size > self.max_bytes:
                    raise SnapshotError("Snapshot resource exceeds byte limit")
                rows.append(item)
            offset += len(items)
            if offset == total:
                break
        if operation_id == "policy.app_control.list":
            positions = [r.get("position") for r in rows]
            if any(type(p) is not int or p < 0 for p in positions) or len(set(positions)) != len(positions):
                raise SnapshotError("Missing or ambiguous policy order")
            rows.sort(key=lambda r: r["position"])
        else:
            rows.sort(key=lambda r: r.get("uuid") or r["name"])
        return rows

    async def capture(self, device_id):
        device_id = self.devices.resolve_device(device_id)
        profile, password = self.devices.repository.get(device_id, include_secret=True)
        if not profile.enabled:
            raise SnapshotError("Device profile is disabled")
        async with self.devices.client_factory(profile, password or "") as client:
            await client.login()
            try:
                first = {op: await self._read(client, profile, op) for op in OPERATIONS}
                second = {op: await self._read(client, profile, op) for op in OPERATIONS}
                stable = configuration(second)
                if fingerprint(configuration(first)) != fingerprint(stable):
                    raise SnapshotError("Configuration changed during capture; no stable snapshot produced")
            finally:
                # Use the existing client's session lifecycle; never expose credentials.
                if client.token:
                    await client.logout()
        return {"device_id": device_id, "namespace": profile.namespace,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "configuration": stable, "observations": second,
                "configuration_hash": fingerprint({"device_id": device_id,
                                                   "namespace": profile.namespace, "configuration": stable}),
                "consistency": "two_matching_reads_not_atomic",
                "source": "existing_device_service"}
