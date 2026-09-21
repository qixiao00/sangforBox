from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal
from urllib.parse import quote, unquote, urlsplit
from pydantic import BaseModel, ConfigDict, Field, model_validator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChangeDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on_existing: Literal["abort", "update_existing", "use_different_name"] | None = Field(
        default=None, description="Required for create operations")
    initial_enabled: bool | None = Field(
        default=None, description="Required when creating a policy with an enable field")


class DeviceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(default="", description="Registered operation ID returned by discover/describe")
    endpoint_id: str = Field(default="", description="AF manual endpoint ID returned by lookup_api_endpoint")
    path_parameters: dict[str, str] = Field(default_factory=dict, description="Resource identity, e.g. name=eth3")
    query_parameters: dict[str, Any] = Field(default_factory=dict, description="Documented query parameters only; never _method")
    body: dict[str, Any] | None = Field(default=None, description="Flat AF JSON request body, not payload and not wrapped in obj")
    decisions: ChangeDecisions = Field(default_factory=ChangeDecisions)

    @model_validator(mode="after")
    def exactly_one_reference(self):
        if bool(self.operation_id.strip()) == bool(self.endpoint_id.strip()):
            raise ValueError("Supply exactly one of operation_id or endpoint_id")
        return self


@dataclass(frozen=True)
class Capability:
    operation_id: str
    title: str
    capability: str
    method: str
    path: str = ""
    endpoint_id: str = ""
    aliases: tuple[str, ...] = ()
    risk: str = "read_only"
    required_path_parameters: tuple[str, ...] = ()
    pagination: bool = False
    special_handler: str | None = None
    preflight_operation_id: str | None = None
    preflight_endpoint_id: str | None = None
    allowed_query_parameters: tuple[str, ...] = ()
    request_body_schema: dict[str, Any] | None = None
    dynamic: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "title": self.title,
            "capability": self.capability,
            "method": self.method,
            "endpoint_id": self.endpoint_id or None,
            "risk": self.risk,
            "required_path_parameters": list(self.required_path_parameters),
            "source": "api_manual" if self.dynamic else "reviewed_registry",
        }


CAPABILITIES: tuple[Capability, ...] = (
    Capability("device.profiles.list", "列出本地设备档案", "query", "LOCAL",
               aliases=("设备列表", "设备档案", "有哪些设备", "列出设备"), special_handler="profiles"),
    Capability("device.connection.test", "测试设备 API 连接", "query", "LOCAL",
               aliases=("测试连接", "连接状态", "是否在线", "能否连接", "连通性"), special_handler="connection"),
    Capability("interface.list", "查询接口配置", "query", "GET",
               "/api/v1/namespaces/{namespace}/interfaces", "af-8.0.107-api#5.2.1",
               ("接口", "网口", "端口", "物理口", "interface")),
    Capability("route.ipv4.list", "查询 IPv4 静态路由", "query", "GET",
               "/api/v1/namespaces/{namespace}/staticroutes/ipv4", "af-8.0.107-api#5.8.2.2",
               ("静态路由", "ipv4路由", "路由表", "下一跳", "route")),
    Capability("policy.nat.list", "查询 NAT 策略", "query", "GET",
               "/api/v1/namespaces/{namespace}/nats", "af-8.0.107-api#6.1.1.1",
               ("nat策略", "nat规则", "地址转换", "源地址转换", "目的地址转换"), pagination=True),
    Capability("policy.security.list", "查询安全防护策略", "query", "GET",
               "/api/v1/namespaces/{namespace}/securitys", "af-8.0.107-api#6.2.1.2",
               ("安全策略", "安全防护", "防火墙规则", "访问控制", "放通策略"), pagination=True),
    Capability("policy.security.get", "查询指定安全防护策略", "query", "GET",
               "/api/v1/namespaces/{namespace}/securitys/{name}", "af-8.0.107-api#6.2.1.3",
               ("指定安全策略", "安全防护策略详情", "业务防护策略", "用户防护策略"),
               required_path_parameters=("name",)),
    Capability("policy.app_control.list", "查询应用控制策略", "query", "GET",
               "/api/v1/namespaces/{namespace}/appcontrols/policys", "af-8.0.107-api#6.3.1.1.1",
               ("应用控制", "应用策略", "上网行为"), pagination=True),
    Capability("policy.local_control.list", "查询本机访问控制策略", "query", "GET",
               "/api/v1/namespaces/{namespace}/localappcontrols/policys", "af-8.0.107-api#6.3.3.1",
               ("本机访问控制", "本地访问控制", "管理访问策略"), pagination=True),
    Capability("application.library.list", "查询应用识别库", "query", "GET",
               "/api/v1/namespaces/{namespace}/appidentifys", "af-8.0.107-api#4.4.1.1",
               ("应用库", "应用识别", "应用路径", "appPath", "appidentifys"), pagination=True),
    Capability("policy.app_control.group.list", "查询应用控制策略组", "query", "GET",
               "/api/v1/namespaces/{namespace}/appcontrols/groups", "af-8.0.107-api#6.3.1.2.1",
               ("应用控制策略组", "策略组", "app control group"), pagination=True),
    Capability("network_object.list", "查询网络对象", "query", "GET",
               "/api/v1/namespaces/{namespace}/ipgroups", "af-8.0.107-api#4.1.1",
               ("网络对象", "地址对象", "ip组", "ipgroups"), pagination=True),

    Capability("interface.update", "增量修改接口", "edit", "PATCH",
               "/api/v1/namespaces/{namespace}/interfaces/{name}", "af-8.0.107-api#5.2.5",
               ("修改接口", "编辑接口", "接口描述", "修改描述", "描述改成", "eth", "启用接口", "禁用接口"), "medium", ("name",),
               preflight_operation_id="interface.get"),
    Capability("interface.get", "查询指定接口", "query", "GET",
               "/api/v1/namespaces/{namespace}/interfaces/{name}", "af-8.0.107-api#5.2.2",
               ("指定接口", "接口详情"), required_path_parameters=("name",)),
    Capability("route.ipv4.create", "新增 IPv4 静态路由", "edit", "POST",
               "/api/v1/namespaces/{namespace}/staticroutes/ipv4", "af-8.0.107-api#5.8.2.3",
               ("新增路由", "添加静态路由"), "high"),
    Capability("route.ipv4.update", "增量修改 IPv4 静态路由", "edit", "PATCH",
               "/api/v1/namespaces/{namespace}/staticroutes/ipv4/{uuid}", "af-8.0.107-api#5.8.2.9",
               ("修改路由", "编辑静态路由"), "high", ("uuid",), preflight_operation_id="route.ipv4.get"),
    Capability("route.ipv4.delete", "删除 IPv4 静态路由", "edit", "DELETE",
               "/api/v1/namespaces/{namespace}/staticroutes/ipv4/{uuid}", "af-8.0.107-api#5.8.2.5",
               ("删除路由", "移除静态路由"), "critical", ("uuid",), preflight_operation_id="route.ipv4.get"),
    Capability("route.ipv4.get", "查询指定 IPv4 静态路由", "query", "GET",
               "/api/v1/namespaces/{namespace}/staticroutes/ipv4/{uuid}", "af-8.0.107-api#5.8.2.1",
               ("指定静态路由", "路由详情"), required_path_parameters=("uuid",)),
    Capability("policy.nat.create", "新增 NAT 策略", "edit", "POST",
               "/api/v1/namespaces/{namespace}/nats", "af-8.0.107-api#6.1.1.5",
               ("新增nat策略", "添加nat规则"), "high"),
    Capability("policy.nat.update", "增量修改 NAT 策略", "edit", "PATCH",
               "/api/v1/namespaces/{namespace}/nats/{name}", "af-8.0.107-api#6.1.1.7",
               ("修改nat策略", "编辑nat规则"), "high", ("name",), preflight_operation_id="policy.nat.get"),
    Capability("policy.nat.delete", "删除 NAT 策略", "edit", "DELETE",
               "/api/v1/namespaces/{namespace}/nats/{name}", "af-8.0.107-api#6.1.1.6",
               ("删除nat策略", "删除nat规则"), "critical", ("name",), preflight_operation_id="policy.nat.get"),
    Capability("policy.nat.get", "查询指定 NAT 策略", "query", "GET",
               "/api/v1/namespaces/{namespace}/nats/{name}", "af-8.0.107-api#6.1.1.4",
               ("指定nat策略", "nat策略详情"), required_path_parameters=("name",)),
    Capability("policy.security.create", "新增安全防护策略", "edit", "POST",
               "/api/v1/namespaces/{namespace}/securitys", "af-8.0.107-api#6.2.1.5",
               ("安全策略", "安全防护策略", "新增安全策略", "新增安全防护策略", "业务防护策略", "用户防护策略", "内到外", "外到内"),
               "high", preflight_operation_id="policy.security.list"),
    Capability("policy.security.update", "修改安全防护策略", "edit", "PATCH",
               "/api/v1/namespaces/{namespace}/securitys/{name}", "af-8.0.107-api#6.2.1.4",
               ("修改安全策略", "编辑安全防护策略", "调整业务防护", "调整用户防护"),
               "high", ("name",), preflight_operation_id="policy.security.get"),
    Capability("policy.security.delete", "删除安全防护策略", "edit", "DELETE",
               "/api/v1/namespaces/{namespace}/securitys/{name}", "af-8.0.107-api#6.2.1.6",
               ("删除安全策略", "删除安全防护策略"), "critical", ("name",),
               preflight_operation_id="policy.security.get"),
    Capability("policy.app_control.create", "新增应用控制策略", "edit", "POST",
               "/api/v1/namespaces/{namespace}/appcontrols/policys", "af-8.0.107-api#6.3.1.1.4",
               ("新增应用控制策略",), "high"),
    Capability("policy.app_control.update", "修改应用控制策略", "edit", "PATCH",
               "/api/v1/namespaces/{namespace}/appcontrols/policys/{name}", "af-8.0.107-api#6.3.1.1.7",
               ("修改应用控制策略",), "high", ("name",), preflight_operation_id="policy.app_control.get"),
    Capability("policy.app_control.delete", "删除应用控制策略", "edit", "DELETE",
               "/api/v1/namespaces/{namespace}/appcontrols/policys/{name}", "af-8.0.107-api#6.3.1.1.5",
               ("删除应用控制策略",), "critical", ("name",), preflight_operation_id="policy.app_control.get"),
    Capability("policy.app_control.get", "查询指定应用控制策略", "query", "GET",
               "/api/v1/namespaces/{namespace}/appcontrols/policys/{name}", "af-8.0.107-api#6.3.1.1.3",
               ("指定应用控制策略",), required_path_parameters=("name",)),
)


# Reviewed resource paths from the local AF 8.0.107 API manual.
for domain, title, resource, section, entries, key in (
    ("virline", "虚拟网线组", "virlines", "5.5", {"list":1,"get":2,"create":3,"delete":4,"update":6}, "name"),
    ("zone", "区域", "zones", "5.1", {"list":1,"get":2,"create":3,"delete":4,"update":7}, "name"),
    ("blacklist", "黑白名单", "whiteblacklist", "8.2", {"list":1,"get":2,"create":3,"delete":5,"update":7}, "url"),
):
    for action, number in entries.items():
        method = {"list":"GET", "get":"GET", "create":"POST", "delete":"DELETE", "update":"PATCH"}[action]
        single = action in {"get", "delete", "update"}
        CAPABILITIES += (Capability(
            f"{domain}.{action}", f"{title} {action}", "query" if method == "GET" else "edit", method,
            f"/api/v1/namespaces/{{namespace}}/{resource}" + ("/{" + key + "}" if single else ""),
            f"af-8.0.107-api#{section}.{number}",
            (title, title.removesuffix("组"), domain, resource, *( ("黑名单", "白名单", "封禁IP") if domain == "blacklist" else ())),
            "read_only" if method == "GET" else "high", (key,) if single else (),
            preflight_operation_id=f"{domain}.get" if action in {"update", "delete"} else None,
        ),)


class CapabilityRegistry:
    def __init__(self, capabilities: Iterable[Capability] = CAPABILITIES, database=None):
        self._items = {item.operation_id: item for item in capabilities}
        self.database = database

    @staticmethod
    def _normalise_documented_path(value: str) -> tuple[str, tuple[str, ...]]:
        parsed = urlsplit(value)
        path = unquote(parsed.path)
        path = path.replace("@namespace", "{namespace}")
        path = re.sub(r"@([A-Za-z_][A-Za-z0-9_]*)", r"{\1}", path)
        if not path.startswith("/api/") or path.startswith("//"):
            raise ValueError("Documented endpoint is not an AF /api/ path")
        names = tuple(dict.fromkeys(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path)))
        unsupported = [name for name in names if name == "host"]
        if unsupported:
            raise ValueError("Documented endpoint contains an unresolved host placeholder")
        return path, tuple(name for name in names if name != "namespace")

    def from_endpoint(self, endpoint_id: str, capability: str | None = None) -> Capability:
        if self.database is None:
            raise ValueError("Dynamic API endpoint execution is unavailable")
        with sqlite3.connect(self.database.path) as connection:
            row = connection.execute(
                "SELECT title,method,path,raw_json FROM api_endpoints WHERE endpoint_id=?", (endpoint_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown endpoint_id: {endpoint_id}")
        title, method, documented_path, raw_json = row
        method = str(method or "").upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(f"Unsupported documented HTTP method: {method}")
        derived_capability = "query" if method == "GET" else "edit"
        if capability and capability != derived_capability:
            raise ValueError(f"Endpoint {endpoint_id} is {derived_capability}, not {capability}")
        path, required = self._normalise_documented_path(str(documented_path or ""))
        raw = json.loads(raw_json)
        allowed_query = tuple(
            str(item["name"]) for item in raw.get("query_parameters") or []
            if isinstance(item, dict) and item.get("name")
        )
        preflight_endpoint_id = None
        if derived_capability == "edit":
            with sqlite3.connect(self.database.path) as connection:
                candidate = connection.execute(
                    "SELECT endpoint_id FROM api_endpoints WHERE method='GET' AND path=? LIMIT 1",
                    (documented_path,),
                ).fetchone()
            if candidate:
                preflight_endpoint_id = str(candidate[0])
        return Capability(
            operation_id=f"api:{endpoint_id}", title=str(title or raw.get("summary") or endpoint_id),
            capability=derived_capability, method=method, path=path, endpoint_id=endpoint_id,
            risk="read_only" if method == "GET" else ("critical" if method == "DELETE" else "high"),
            required_path_parameters=required,
            pagination="_start" in allowed_query or "_length" in allowed_query,
            allowed_query_parameters=allowed_query,
            request_body_schema=raw.get("request_body_schema") if isinstance(raw.get("request_body_schema"), dict) else None,
            preflight_endpoint_id=preflight_endpoint_id,
            dynamic=True,
        )

    def resolve(self, *, operation_id: str = "", endpoint_id: str = "",
                capability: str | None = None) -> Capability:
        if bool(operation_id) == bool(endpoint_id):
            raise ValueError("Supply exactly one of operation_id or endpoint_id")
        return self.get(operation_id, capability) if operation_id else self.from_endpoint(endpoint_id, capability)

    def get(self, operation_id: str, capability: str | None = None) -> Capability:
        item = self._items.get(operation_id)
        if item is None or (capability and item.capability != capability):
            raise ValueError(f"Unsupported {capability or ''} operation_id: {operation_id}")
        return item

    def search(self, request: str, capability: str, limit: int = 8) -> list[Capability]:
        text = re.sub(r"\s+", "", request.lower())
        if not text:
            return []
        scored: list[tuple[int, Capability]] = []
        for item in self._items.values():
            if item.capability != capability:
                continue
            score = 0
            for alias in (*item.aliases, item.title.lower(), item.operation_id.lower()):
                compact = re.sub(r"\s+", "", alias.lower())
                if compact and compact in text:
                    score = max(score, 100 + len(compact))
                else:
                    score += sum(2 for token in re.split(r"[._\-/]", compact) if len(token) > 1 and token in text)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1].operation_id))
        matches = [item for _, item in scored[:limit]]
        if capability == "query" and (any(word in text for word in ("全部策略", "所有策略"))
                                      or ("策略" in text and not any(item.operation_id.startswith("policy.") for item in matches))):
            policy_operation_ids = {
                "policy.nat.list", "policy.security.list",
                "policy.app_control.list", "policy.local_control.list",
            }
            policy_items = [item for item in self._items.values()
                            if item.operation_id in policy_operation_ids]
            matches = list(dict.fromkeys([*policy_items, *matches]))[:limit]
        return matches

    def catalog(self, capability: str) -> list[dict[str, Any]]:
        return [item.public() for item in self._items.values() if item.capability == capability]

    def public_search(self, request: str, capability: str, limit: int = 8) -> list[dict[str, Any]]:
        return [item.public() for item in self.search(request, capability, limit)]

    @staticmethod
    def render_path(item: Capability, namespace: str, path_parameters: dict[str, str]) -> str:
        unexpected = set(path_parameters) - set(item.required_path_parameters)
        if unexpected:
            raise ValueError(f"Unexpected path parameters: {sorted(unexpected)}")
        values = {**path_parameters, "namespace": namespace}
        missing = [name for name in item.required_path_parameters if not values.get(name)]
        if missing:
            raise ValueError(f"{item.operation_id} missing path parameters: {', '.join(missing)}")
        path = item.path
        for name in ("namespace", *item.required_path_parameters):
            value = str(values[name])
            if not value or value in {".", ".."} or "\x00" in value:
                raise ValueError(f"Invalid path parameter: {name}")
            path = path.replace("{" + name + "}", quote(value, safe="._:@-"))
        if "{" in path or "}" in path:
            raise ValueError(f"Unresolved path parameter for {item.operation_id}")
        return path

    @staticmethod
    def validate_query_parameters(item: Capability, query_parameters: dict[str, Any]) -> None:
        if "_method" in query_parameters:
            raise ValueError("HTTP method override is not allowed")
        if item.dynamic:
            unexpected = set(query_parameters) - set(item.allowed_query_parameters)
            if unexpected:
                raise ValueError(
                    f"Undocumented query parameters for {item.endpoint_id}: {sorted(unexpected)}"
                )

    @staticmethod
    def validate_body(item: Capability, body: dict[str, Any] | None) -> None:
        if item.method in {"POST", "PUT", "PATCH"} and not isinstance(body, dict):
            raise ValueError(f"{item.operation_id} requires a JSON object body")
        schema = item.request_body_schema
        if not item.dynamic or not isinstance(body, dict) or not isinstance(schema, dict):
            return
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        missing = [name for name in required if name not in body]
        if missing:
            raise ValueError(f"{item.endpoint_id} missing required body fields: {', '.join(missing)}")
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if properties:
            unexpected = set(body) - set(properties)
            if unexpected:
                raise ValueError(f"Undocumented body fields for {item.endpoint_id}: {sorted(unexpected)}")


class OperationPlanRepository:
    def __init__(self, database):
        self.database = database

    def create(self, device_id: str, kind: str, request: str, steps: list[dict[str, Any]],
               snapshots: list[dict[str, Any]] | None = None, ttl_minutes: int = 30) -> dict[str, Any]:
        plan_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(minutes=ttl_minutes)
        with sqlite3.connect(self.database.path) as connection:
            connection.execute(
                """INSERT INTO operation_plans
                   (plan_id,device_id,kind,request,steps_json,snapshots_json,status,created_at,expires_at,applied_at,result_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (plan_id, device_id, kind, request, json.dumps(steps, ensure_ascii=False),
                 json.dumps(snapshots or [], ensure_ascii=False), "planned", created_at.isoformat(),
                 expires_at.isoformat(), None, None),
            )
        return self.get(plan_id)

    def get(self, plan_id: str) -> dict[str, Any]:
        with sqlite3.connect(self.database.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM operation_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(plan_id)
        result = dict(row)
        result["steps"] = json.loads(result.pop("steps_json"))
        result["snapshots"] = json.loads(result.pop("snapshots_json"))
        raw_result = result.pop("result_json")
        result["result"] = json.loads(raw_result) if raw_result else None
        return result

    def mark_applied(self, plan_id: str, status: str, result: dict[str, Any]) -> dict[str, Any]:
        with sqlite3.connect(self.database.path) as connection:
            connection.execute(
                "UPDATE operation_plans SET status=?,applied_at=COALESCE(applied_at,?),result_json=? WHERE plan_id=?",
                (status, _now(), json.dumps(result, ensure_ascii=False), plan_id),
            )
        return self.get(plan_id)

    def claim(self, plan_id: str) -> bool:
        """Atomically prevent a planned change from being submitted twice."""
        with sqlite3.connect(self.database.path) as connection:
            updated = connection.execute(
                "UPDATE operation_plans SET status='applying' WHERE plan_id=? AND status='planned'",
                (plan_id,),
            )
            return updated.rowcount == 1
