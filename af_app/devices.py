from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, SecretStr, field_validator
from af_app.operations import Capability, CapabilityRegistry, OperationPlanRepository, DeviceChange
from af_app.knowledge import KnowledgeRepository
from af_app.initialization_cases import InitializationCaseRepository, _safe


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeviceProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    namespace: str = Field(default="public", min_length=1, max_length=15, pattern=r"^[A-Za-z0-9_.-]+$")
    username: str = Field(min_length=1, max_length=60)
    password: SecretStr
    verify_tls: bool = True
    ca_bundle: str | None = Field(default=None, max_length=1000)
    timeout_seconds: float = Field(default=15, ge=1, le=120)
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("base_url must be an http(s) URL with a host")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("base_url must not contain credentials, query, or fragment")
        return value


class DeviceProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    namespace: str | None = Field(default=None, min_length=1, max_length=15, pattern=r"^[A-Za-z0-9_.-]+$")
    username: str | None = Field(default=None, min_length=1, max_length=60)
    password: SecretStr | None = None
    verify_tls: bool | None = None
    ca_bundle: str | None = Field(default=None, max_length=1000)
    timeout_seconds: float | None = Field(default=None, ge=1, le=120)
    enabled: bool | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return DeviceProfileCreate.validate_base_url(value)


class DeviceProfile(BaseModel):
    device_id: str
    name: str
    base_url: str
    namespace: str
    username: str
    verify_tls: bool
    ca_bundle: str | None
    timeout_seconds: float
    enabled: bool
    has_credentials: bool
    created_at: str
    updated_at: str


class DeviceConnectionResult(BaseModel):
    ok: bool
    device_id: str
    device_name: str
    base_url: str
    namespace: str
    username: str
    role: str | None = None
    auth_result: str | None = None
    reported_namespace: str | None = None
    keepalive_ok: bool
    latency_ms: int
    tested_at: str


class DeviceConnectionError(BaseModel):
    stage: str
    message: str
    http_status: int | None = None
    device_code: int | str | None = None


class DeviceConnectionOutcome(BaseModel):
    ok: bool
    result: DeviceConnectionResult | None = None
    error: DeviceConnectionError | None = None


class DeviceConnectionAudit(BaseModel):
    audit_id: int
    device_id: str
    success: bool
    stage: str
    message: str
    latency_ms: int
    tested_at: str


class DeviceAPIError(RuntimeError):
    def __init__(self, stage: str, message: str, *, http_status: int | None = None,
                 device_code: int | str | None = None):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.http_status = http_status
        self.device_code = device_code

    def public_detail(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "message": self.message,
            "http_status": self.http_status,
            "device_code": self.device_code,
        }


class CredentialProtector:
    """Protect device passwords with the current Windows user's DPAPI key."""

    def protect(self, secret: str) -> str:
        if os.name != "nt":
            raise RuntimeError("Device credential storage requires Windows DPAPI")
        import win32crypt
        protected = win32crypt.CryptProtectData(
            secret.encode("utf-8"), "SangforBox device credential", None, None, None, 0
        )
        encrypted = protected[1] if isinstance(protected, tuple) else protected
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")

    def unprotect(self, protected: str) -> str:
        if os.name != "nt" or not protected.startswith("dpapi:"):
            raise RuntimeError("Unsupported device credential format")
        import win32crypt
        encrypted = base64.b64decode(protected.removeprefix("dpapi:"))
        unprotected = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)
        cleartext = unprotected[1] if isinstance(unprotected, tuple) else unprotected
        return cleartext.decode("utf-8")


class DeviceRepository:
    def __init__(self, database, protector: CredentialProtector | None = None):
        self.database = database
        self.protector = protector or CredentialProtector()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _public(row: sqlite3.Row) -> DeviceProfile:
        return DeviceProfile(
            device_id=row["device_id"], name=row["name"], base_url=row["base_url"],
            namespace=row["namespace"], username=row["username"],
            verify_tls=bool(row["verify_tls"]), ca_bundle=row["ca_bundle"],
            timeout_seconds=row["timeout_seconds"], enabled=bool(row["enabled"]),
            has_credentials=bool(row["password_ciphertext"]), created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create(self, request: DeviceProfileCreate) -> DeviceProfile:
        device_id = str(uuid.uuid4())
        timestamp = _now()
        ciphertext = self.protector.protect(request.password.get_secret_value())
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO device_profiles
                   (device_id,name,base_url,namespace,username,password_ciphertext,verify_tls,
                    ca_bundle,timeout_seconds,enabled,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (device_id, request.name, request.base_url, request.namespace, request.username,
                 ciphertext, int(request.verify_tls), request.ca_bundle, request.timeout_seconds,
                 int(request.enabled), timestamp, timestamp),
            )
        return self.get(device_id, include_secret=False)[0]

    def list(self) -> list[DeviceProfile]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM device_profiles ORDER BY name, device_id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def get(self, device_id: str, *, include_secret: bool = False) -> tuple[DeviceProfile, str | None]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM device_profiles WHERE device_id=?", (device_id,)
            ).fetchone()
        if row is None:
            raise KeyError(device_id)
        secret = self.protector.unprotect(row["password_ciphertext"]) if include_secret else None
        return self._public(row), secret

    def update(self, device_id: str, request: DeviceProfileUpdate) -> DeviceProfile:
        self.get(device_id)
        changes = request.model_dump(exclude_unset=True)
        if not changes:
            return self.get(device_id)[0]
        if "password" in changes:
            password = changes.pop("password")
            changes["password_ciphertext"] = self.protector.protect(password.get_secret_value())
        for boolean in ("verify_tls", "enabled"):
            if boolean in changes:
                changes[boolean] = int(changes[boolean])
        changes["updated_at"] = _now()
        assignments = ",".join(f"{field}=?" for field in changes)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE device_profiles SET {assignments} WHERE device_id=?",
                [*changes.values(), device_id],
            )
        return self.get(device_id)[0]

    def delete(self, device_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM device_profiles WHERE device_id=?", (device_id,))
        if cursor.rowcount == 0:
            raise KeyError(device_id)

    def record_test(self, device_id: str, *, success: bool, stage: str,
                    message: str, latency_ms: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO device_connection_audit
                   (device_id,success,stage,message,latency_ms,tested_at)
                   VALUES (?,?,?,?,?,?)""",
                (device_id, int(success), stage, message[:500], latency_ms, _now()),
            )

    def list_audits(self, device_id: str, limit: int = 50) -> list[DeviceConnectionAudit]:
        self.get(device_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM device_connection_audit WHERE device_id=?
                   ORDER BY audit_id DESC LIMIT ?""", (device_id, limit),
            ).fetchall()
        return [DeviceConnectionAudit(
            audit_id=row["audit_id"], device_id=row["device_id"],
            success=bool(row["success"]), stage=row["stage"], message=row["message"],
            latency_ms=row["latency_ms"], tested_at=row["tested_at"],
        ) for row in rows]


class AFAPIClient:
    def __init__(self, profile: DeviceProfile, password: str, *, transport=None):
        self.profile = profile
        self.password = password
        self.token: str | None = None
        verify: bool | str = profile.ca_bundle if profile.verify_tls and profile.ca_bundle else profile.verify_tls
        self.http = httpx.AsyncClient(
            base_url=profile.base_url, verify=verify, timeout=profile.timeout_seconds,
            follow_redirects=False, transport=transport,
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.http.aclose()

    @property
    def namespace_root(self) -> str:
        return f"/api/v1/namespaces/{self.profile.namespace}"

    @staticmethod
    def _payload(response: httpx.Response, stage: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise DeviceAPIError(stage, "设备返回了非 JSON 响应", http_status=response.status_code) from exc
        if not isinstance(payload, dict):
            raise DeviceAPIError(stage, "设备返回的数据格式无效", http_status=response.status_code)
        if response.is_error or payload.get("code") not in (None, 0):
            message = str(payload.get("message") or f"HTTP {response.status_code}")[:500]
            raise DeviceAPIError(stage, message, http_status=response.status_code,
                                 device_code=payload.get("code"))
        return payload

    @staticmethod
    def _connection_error(exc: httpx.HTTPError) -> str:
        parts: list[str] = []
        current: BaseException | None = exc
        while current is not None and len(parts) < 5:
            parts.append(str(current).lower())
            current = current.__cause__
        detail = " ".join(parts)
        if "certificate_verify_failed" in detail or "certificate verify failed" in detail:
            return "TLS 证书校验失败，请配置设备 CA 文件，或仅在受控环境中关闭证书校验"
        if isinstance(exc, httpx.TimeoutException) or "timed out" in detail:
            return "连接设备超时，请检查地址、路由、防火墙和连接超时设置"
        if "connection refused" in detail:
            return "设备拒绝 HTTPS 连接，请确认 WEB API 服务与端口已启用"
        return f"连接设备失败：{type(exc).__name__}"

    async def login(self) -> dict[str, Any]:
        try:
            response = await self.http.post(
                f"{self.namespace_root}/login",
                json={"name": self.profile.username, "password": self.password},
            )
        except httpx.HTTPError as exc:
            raise DeviceAPIError("login", self._connection_error(exc)) from exc
        payload = self._payload(response, "login")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        login_result = data.get("loginResult") if isinstance(data.get("loginResult"), dict) else {}
        token = login_result.get("token")
        if not isinstance(token, str) or not token:
            raise DeviceAPIError("login", "登录成功响应中缺少 data.loginResult.token",
                                 http_status=response.status_code, device_code=payload.get("code"))
        self.token = token
        self.http.cookies.set("token", token)
        return data

    async def request(self, method: str, path: str, *, params=None, json_body=None) -> dict[str, Any]:
        if not path.startswith("/api/") or path.startswith("//"):
            raise ValueError("Only absolute AF API paths beginning with /api/ are allowed")
        if not self.token:
            await self.login()
        try:
            response = await self.http.request(method.upper(), path, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise DeviceAPIError("request", self._connection_error(exc)) from exc
        return self._payload(response, "request")

    async def keepalive(self) -> dict[str, Any]:
        return await self.request("GET", f"{self.namespace_root}/keepalive")

    async def logout(self) -> None:
        if not self.token:
            return
        token = self.token
        try:
            response = await self.http.post(
                f"{self.namespace_root}/logout", json={"loginResult": {"token": token}}
            )
            self._payload(response, "logout")
        finally:
            self.token = None
            self.http.cookies.delete("token")


class DeviceService:
    INITIALIZATION_STAGES = (
        "management", "topology", "objects", "routing", "nat", "security", "application",
    )
    INITIALIZATION_READS = {
        "management": ("interface.list", "policy.local_control.list"),
        "topology": ("interface.list", "zone.list", "virline.list"),
        "objects": ("network_object.list",),
        "routing": ("route.ipv4.list", "interface.list"),
        "nat": ("policy.nat.list", "network_object.list", "zone.list"),
        "security": ("policy.security.list", "network_object.list", "zone.list"),
        "application": ("policy.app_control.list", "policy.app_control.group.list", "network_object.list", "zone.list"),
    }
    INITIALIZATION_RESOURCES = {
        "management": ("interfaces", "localappcontrols"),
        "topology": ("interfaces", "zones", "virlines"),
        "objects": ("ipgroups",),
        "routing": ("staticroutes/ipv4",),
        "nat": ("nats",),
        "security": ("securitys",),
        "application": ("appcontrols",),
    }
    def __init__(self, database, protector: CredentialProtector | None = None,
                 client_factory: Callable[..., AFAPIClient] = AFAPIClient,
                 output_directory=None):
        self.repository = DeviceRepository(database, protector)
        self.client_factory = client_factory
        self.capabilities = CapabilityRegistry(database=database)
        self.knowledge = KnowledgeRepository(database)
        self.plans = OperationPlanRepository(database)
        self.initialization_cases = InitializationCaseRepository(database, output_directory)

    def resolve_device(self, reference: str) -> str:
        profiles = self.repository.list()
        normalized = reference.strip().rstrip("/")
        matches = [p for p in profiles if normalized in {
            p.device_id, p.name, p.base_url.rstrip("/"), urlparse(p.base_url).hostname
        }] if normalized else [p for p in profiles if p.enabled]
        if len(matches) != 1:
            raise ValueError("Device selection is ambiguous or missing. Call query_device(action='devices') and choose a device_id.")
        return matches[0].device_id

    def discover(self, request: str, capability: str) -> dict[str, Any]:
        catalog = self.capabilities.catalog(capability)
        matches = self.capabilities.public_search(request, capability, 30) if request.strip() else catalog
        documented: list[dict[str, Any]] = []
        if request.strip():
            registered_endpoint_ids = {item["endpoint_id"] for item in catalog if item.get("endpoint_id")}
            for endpoint in self.knowledge.lookup_api(request, None, 30):
                method = str(endpoint.get("method") or "").upper()
                endpoint_capability = "query" if method == "GET" else "edit"
                if endpoint_capability != capability or endpoint.get("endpoint_id") in registered_endpoint_ids:
                    continue
                documented.append({
                    **endpoint,
                    "capability": endpoint_capability,
                    "risk": "critical" if method == "DELETE" else ("read_only" if method == "GET" else "high"),
                    "source": "api_manual",
                    "executable": True,
                    "execution_reference": {"endpoint_id": endpoint.get("endpoint_id")},
                })
        return {"ok": True, "transport": "AF HTTP API", "capabilities": matches,
                "documented_endpoints": documented,
                "available_operations": catalog if not matches else [],
                "next_action": (
                    "Use a matching operation_id, or use a documented endpoint_id directly with prepare/plan. "
                    "All documented endpoints marked executable run inside MCP; never write a direct API script."
                )}

    @staticmethod
    def _is_policy_operation(item: Capability) -> bool:
        path = item.path.lower()
        return (item.operation_id.startswith("policy.") or "/securitys" in path
                or "/policys" in path or "/policies" in path)

    def prepare_change_questions(self, request: str, operation_ids: list[str] | None = None,
                                 endpoint_ids: list[str] | None = None) -> dict[str, Any]:
        if operation_ids and endpoint_ids:
            raise ValueError("Use operation_ids or endpoint_ids, not both")
        selected = ([self.capabilities.get(item, "edit") for item in operation_ids or []]
                    if operation_ids else [self.capabilities.from_endpoint(item, "edit")
                                           for item in endpoint_ids or []])
        if not selected:
            selected = self.capabilities.search(request, "edit")
        if not selected:
            return {"ok": False, "status": "capability_not_found", "questions": []}
        methods = {item.method for item in selected}
        questions: list[dict[str, Any]] = []
        if "POST" in methods and methods.intersection({"PUT", "PATCH"}):
            questions.append({
                "id": "operation_mode", "required": True,
                "prompt": "目标是新增一个配置，还是修改已有配置？",
                "options": [
                    {"value": "create_new", "label": "新增配置"},
                    {"value": "update_existing", "label": "修改已有配置"},
                ],
                "effect": "答案决定使用 POST 还是 PUT/PATCH 接口。",
            })
        if any(item.method == "POST" for item in selected):
            questions.append({
                "id": "on_existing", "required": True,
                "prompt": "如果发现同名或同标识配置已经存在，如何处理？",
                "options": [
                    {"value": "abort", "label": "停止并报告"},
                    {"value": "update_existing", "label": "改为修改已有配置"},
                    {"value": "use_different_name", "label": "使用新的名称"},
                ],
                "change_field": "decisions.on_existing",
            })
        policy_creates = [item for item in selected if item.method == "POST" and self._is_policy_operation(item)]
        if policy_creates:
            questions.append({
                "id": "initial_enabled", "required": True,
                "prompt": "新增策略创建后是否立即启用？",
                "options": [
                    {"value": True, "label": "立即启用"},
                    {"value": False, "label": "保持停用"},
                ],
                "change_field": "decisions.initial_enabled",
            })
        return {
            "ok": True, "status": "clarification_required" if questions else "ready",
            "request": request, "operations": [item.public() for item in selected],
            "questions": questions,
            "next_action": "Collect the required answers, select the matching endpoint, then call plan.",
        }

    @staticmethod
    def _bounded(value: Any, limit: int) -> Any:
        """Bound nested device arrays so one MCP result cannot explode in size."""
        if isinstance(value, list):
            return [DeviceService._bounded(item, limit) for item in value[:limit]]
        if isinstance(value, dict):
            return {str(key): DeviceService._bounded(item, limit) for key, item in value.items()}
        return value

    async def _run_remote_operations(self, device_id: str, operations: list[dict[str, Any]],
                                     limit: int = 20) -> list[dict[str, Any]]:
        profile, password = self.repository.get(device_id, include_secret=True)
        if not profile.enabled:
            raise DeviceAPIError("configuration", "设备配置已停用")
        results: list[dict[str, Any]] = []
        async with self.client_factory(profile, password or "") as client:
            await client.login()
            try:
                for operation in operations:
                    operation_id = str(operation.get("operation_id") or "")
                    endpoint_id = str(operation.get("endpoint_id") or "")
                    capability = self.capabilities.resolve(
                        operation_id=operation_id, endpoint_id=endpoint_id)
                    path_parameters = operation.get("path_parameters") or {}
                    query_parameters = dict(operation.get("query_parameters") or {})
                    self.capabilities.validate_query_parameters(capability, query_parameters)
                    if capability.pagination:
                        query_parameters.setdefault("_start", 0)
                        query_parameters.setdefault("_length", limit)
                    path = self.capabilities.render_path(capability, profile.namespace, path_parameters)
                    try:
                        payload = await client.request(
                            capability.method, path, params=query_parameters or None,
                            json_body=operation.get("body"),
                        )
                        data = self._bounded(payload.get("data"), limit)
                        results.append({
                            "operation_id": operation_id or None,
                            "title": capability.title,
                            "endpoint_id": capability.endpoint_id,
                            "method": capability.method,
                            "path": path,
                            "ok": True,
                            "data": data,
                            "message": payload.get("message"),
                        })
                    except DeviceAPIError as exc:
                        results.append({
                            "operation_id": operation_id or None,
                            "title": capability.title,
                            "endpoint_id": capability.endpoint_id,
                            "method": capability.method,
                            "path": path,
                            "ok": False,
                            "error": exc.public_detail(),
                        })
                        if capability.capability == "edit":
                            break
            finally:
                if client.token:
                    try:
                        await client.logout()
                    except DeviceAPIError:
                        pass
        return results

    async def query_device(self, device_id: str, request: str,
                           operation_ids: list[str] | None = None,
                           endpoint_ids: list[str] | None = None,
                           path_parameters: dict[str, str] | None = None,
                           query_parameters: dict[str, Any] | None = None,
                           limit: int = 20) -> dict[str, Any]:
        if device_id:
            device_id = self.resolve_device(device_id)
        if operation_ids and endpoint_ids:
            raise ValueError("Use operation_ids or endpoint_ids, not both")
        selected = ([self.capabilities.get(item, "query") for item in operation_ids]
                    if operation_ids else [self.capabilities.from_endpoint(item, "query") for item in endpoint_ids or []])
        if not operation_ids and not endpoint_ids:
            selected = self.capabilities.search(request, "query")
            selected = [item for item in selected if not item.required_path_parameters or all(
                key in (path_parameters or {}) for key in item.required_path_parameters)]
        if not selected:
            return {
                "ok": False,
                "status": "capability_not_found",
                "request": request,
                "message": "未在当前能力注册表中匹配到只读操作",
                "candidates": [],
            }
        if len(selected) > 1 and (path_parameters or query_parameters):
            return {
                "ok": False,
                "status": "ambiguous_parameters",
                "request": request,
                "message": "多个操作不能共用路径参数或查询参数；请分别调用或传入明确 operation_id",
                "resolved_operations": [item.public() for item in selected],
            }
        results: list[dict[str, Any]] = []
        remote: list[dict[str, Any]] = []
        for item in selected:
            if item.special_handler == "profiles":
                results.append({"operation_id": item.operation_id, "title": item.title, "ok": True,
                                "data": [profile.model_dump() for profile in self.repository.list()]})
            elif item.special_handler == "connection":
                if not device_id:
                    results.append({"operation_id": item.operation_id, "title": item.title, "ok": False,
                                    "error": {"stage": "configuration", "message": "device_id is required"}})
                else:
                    try:
                        outcome = await self.test_connection(device_id)
                        results.append({"operation_id": item.operation_id, "title": item.title,
                                        "ok": True, "data": outcome.model_dump()})
                    except DeviceAPIError as exc:
                        results.append({"operation_id": item.operation_id, "title": item.title,
                                        "ok": False, "error": exc.public_detail()})
            else:
                remote.append({
                    "operation_id": None if item.dynamic else item.operation_id,
                    "endpoint_id": item.endpoint_id if item.dynamic else "",
                    "path_parameters": path_parameters or {},
                    "query_parameters": query_parameters or {},
                })
        if remote:
            if not device_id:
                return {
                    "ok": False, "status": "device_required", "request": request,
                    "message": "这些设备查询需要 device_id",
                    "resolved_operations": [item.public() for item in selected],
                }
            results.extend(await self._run_remote_operations(device_id, remote, limit))
        return {
            "ok": all(item.get("ok") for item in results),
            "status": "completed",
            "request": request,
            "device_id": device_id or None,
            "read_only": True,
            "resolved_operations": [item.public() for item in selected],
            "results": results,
            "queried_at": _now(),
        }

    def _validate_changes(self, changes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not changes:
            raise ValueError("At least one structured change is required")
        validated: list[dict[str, Any]] = []
        questions: list[dict[str, Any]] = []
        for index, raw in enumerate(changes):
            raw = DeviceChange.model_validate(raw).model_dump()
            operation_id = str(raw.get("operation_id", ""))
            endpoint_id = str(raw.get("endpoint_id", ""))
            item = self.capabilities.resolve(
                operation_id=operation_id, endpoint_id=endpoint_id, capability="edit")
            path_parameters = raw.get("path_parameters") or {}
            if not isinstance(path_parameters, dict):
                raise ValueError(f"{operation_id} path_parameters must be an object")
            self.capabilities.render_path(item, "public", path_parameters)
            body = raw.get("body")
            decisions = raw.get("decisions") or {}
            if item.method == "POST" and not decisions.get("on_existing"):
                questions.append({
                    "id": f"changes.{index}.on_existing", "required": True,
                    "prompt": "如果同名或同标识配置已经存在，停止、改为更新，还是换一个名称？",
                    "options": ["abort", "update_existing", "use_different_name"],
                    "change_field": "decisions.on_existing",
                })
            if item.method == "POST" and decisions.get("on_existing") == "update_existing":
                questions.append({
                    "id": f"changes.{index}.select_update_endpoint", "required": True,
                    "prompt": "已选择修改已有配置，请改用对应的 PUT/PATCH endpoint_id 或 operation_id。",
                    "options": ["select_update_endpoint"],
                })
            schema_properties = (item.request_body_schema or {}).get("properties") or {}
            enable_field = next((name for name in ("enable", "enabled") if name in schema_properties), None)
            is_policy_create = item.method == "POST" and self._is_policy_operation(item)
            if is_policy_create and enable_field is None:
                enable_field = "enable"
            if is_policy_create and enable_field and isinstance(body, dict) and enable_field not in body:
                if decisions.get("initial_enabled") is None:
                    questions.append({
                        "id": f"changes.{index}.initial_enabled", "required": True,
                        "prompt": "新增策略创建后是否立即启用？",
                        "options": [True, False],
                        "change_field": "decisions.initial_enabled",
                    })
                else:
                    body[enable_field] = bool(decisions["initial_enabled"])
            if "/securitys" in item.path and isinstance(body, dict):
                if "security" in body or "obj" in body:
                    raise ValueError("安全防护策略请求体必须是扁平对象，不得包裹 security 或 obj")
                if item.method == "POST":
                    response = body.setdefault("response", {})
                    if not isinstance(response, dict):
                        raise ValueError("安全防护策略 response 必须是对象")
                    botnet = response.setdefault("block", {})
                    if not isinstance(botnet, dict):
                        raise ValueError("安全防护策略 response.block 必须是对象")
                    botnet.setdefault("enable", True)
                    botnet.setdefault("type", "HIGH_THREATS")
                description = body.get("description")
                if isinstance(description, str):
                    if len(description.encode("utf-8")) > 95:
                        raise ValueError("安全防护策略 description 不得超过 95 字节")
                    banned = set("~!#$%^&*+\\|{};:\"'/<>?") & set(description)
                    if banned:
                        raise ValueError(f"安全防护策略 description 含设备不允许的字符: {sorted(banned)}")
                policy_type = body.get("policyType")
                if policy_type is not None and policy_type not in {"SERVER", "INTERNET_ACCESS"}:
                    raise ValueError("安全防护策略 policyType 只能是 SERVER 或 INTERNET_ACCESS")
            self.capabilities.validate_query_parameters(item, raw.get("query_parameters") or {})
            self.capabilities.validate_body(item, body)
            validated.append({
                "operation_id": operation_id or None,
                "endpoint_id": endpoint_id or None,
                "documentation_endpoint_id": item.endpoint_id or None,
                "title": item.title,
                "risk": item.risk,
                "path_parameters": path_parameters,
                "query_parameters": raw.get("query_parameters") or {},
                "body": body,
                "decisions": decisions,
            })
        return validated, questions

    async def create_change_plan(self, device_id: str, request: str,
                                 changes: list[dict[str, Any]], kind: str = "edit",
                                 extra_snapshots: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        device_id = self.resolve_device(device_id)
        self.repository.get(device_id)
        validated, questions = self._validate_changes(changes)
        if questions:
            return {
                "ok": False, "status": "clarification_required", "request": request,
                "questions": questions, "requires_confirmation": False,
                "message": "操作条件尚未明确，回答问题后重新生成计划；设备未被修改。",
            }
        snapshots: list[dict[str, Any]] = []
        preflight: list[dict[str, Any]] = []
        for step in validated:
            item = self.capabilities.resolve(
                operation_id=step.get("operation_id") or "",
                endpoint_id=step.get("endpoint_id") or "", capability="edit")
            if item.preflight_operation_id:
                preflight.append({
                    "operation_id": item.preflight_operation_id,
                    "path_parameters": step["path_parameters"],
                })
            elif item.preflight_endpoint_id:
                preflight.append({
                    "endpoint_id": item.preflight_endpoint_id,
                    "path_parameters": step["path_parameters"],
                })
        if preflight:
            snapshots = await self._run_remote_operations(device_id, preflight, limit=20)
            if not all(item.get("ok") for item in snapshots):
                raise ValueError("Preflight failed; no executable plan was created")
        plan = self.plans.create(device_id, kind, request, validated, [*(extra_snapshots or []), *snapshots])
        plan["requires_confirmation"] = True
        plan["message"] = "计划已生成，尚未修改设备。确认后使用同一工具的 apply 动作和 plan_id 执行。"
        return plan

    async def apply_change_plan(self, plan_id: str, expected_kind: str) -> dict[str, Any]:
        plan = self.plans.get(plan_id)
        if plan["kind"] != expected_kind:
            raise ValueError(f"Plan {plan_id} is not a {expected_kind} plan")
        if plan["status"] != "planned":
            raise ValueError(f"Plan status is {plan['status']}; only planned plans can be applied")
        if datetime.fromisoformat(plan["expires_at"]) <= datetime.now(timezone.utc):
            self.plans.mark_applied(plan_id, "expired", {"message": "计划已过期，未执行"})
            raise ValueError("Plan has expired; create a new plan")
        if expected_kind.startswith("initialization:"):
            phase = expected_kind.split(":", 1)[1]
            saved = next((item["results"] for item in plan["snapshots"]
                          if item.get("snapshot_type") == "initialization_inventory"), None)
            current = await self.initialize_device("inspect", plan["device_id"], stage=phase, limit=100)
            if not current["ok"] or not current["inventory_complete"] or saved != current["results"]:
                raise ValueError("Initialization inventory changed or could not be read; create a new plan")
        if not self.plans.claim(plan_id):
            raise ValueError("Plan is already being applied or was applied")
        try:
            results = await self._run_remote_operations(plan["device_id"], plan["steps"], limit=50)
        except Exception:
            self.plans.mark_applied(plan_id, "unknown", {"message": "Write outcome unknown; inspect device before any retry"})
            raise
        ok = bool(results) and all(item.get("ok") for item in results)
        result = {"ok": ok, "results": results, "applied_at": _now()}
        updated = self.plans.mark_applied(plan_id, "applied" if ok else "failed", result)
        updated["requires_confirmation"] = False
        return updated

    async def initialize_device(self, action: str, device_id: str, request: str = "",
                                changes: list[dict[str, Any]] | None = None,
                                plan_id: str = "", limit: int = 20,
                                stage: str = "", case_id: str = "",
                                design: dict[str, Any] | None = None,
                                confirm_design: bool = False,
                                feedback_csv: str = "") -> dict[str, Any]:
        device_id = self.resolve_device(device_id)
        if stage and stage not in self.INITIALIZATION_STAGES:
            raise ValueError(f"Unknown initialization stage: {stage}")
        if action == "design":
            return self.initialization_cases.save_design(
                device_id, design or {}, case_id, confirm_design)
        if action == "case":
            if not case_id:
                raise ValueError("case_id is required")
            record = self.initialization_cases.get(case_id, device_id)
            record["items"] = self.initialization_cases.plans(case_id, device_id)
            return record
        if action == "events":
            if not case_id:
                raise ValueError("case_id is required")
            return {"ok": True, "case_id": case_id,
                    "events": self.initialization_cases.events(case_id, device_id, limit)}
        if action in {"feedback_review", "feedback_submit"}:
            if not case_id:
                raise ValueError("case_id is required")
            review = await self.review_initialization_case(device_id, case_id)
            if action == "feedback_review":
                self.initialization_cases.event(case_id, action, review["status"])
                return review
            if not review["ok"]:
                raise ValueError("Current device inventory is incomplete; feedback cannot be bound to a reliable snapshot")
            return self.initialization_cases.submit_feedback(
                case_id, device_id, review["rows"], feedback_csv)
        if action == "inspect":
            operation_ids = list(dict.fromkeys(
                operation for phase in (stage,) if stage for operation in self.INITIALIZATION_READS[phase]
            )) if stage else list(dict.fromkeys(
                operation for phase in self.INITIALIZATION_STAGES
                for operation in self.INITIALIZATION_READS[phase]
            ))
            result = await self.query_device(device_id, request or "检查设备初始化状态", operation_ids, limit=limit)
            result.update(initialization_action=action, stage=stage or "all",
                          stages=list(self.INITIALIZATION_STAGES),
                          inventory_complete=result["ok"] and all(
                              not isinstance(item.get("data"), list) or len(item["data"]) < limit
                              for item in result["results"]
                          ))
            return result
        if action == "prepare":
            if not stage:
                raise ValueError("stage is required for prepare")
            selected = changes or []
            operation_ids = [x["operation_id"] for x in selected if x.get("operation_id")]
            endpoint_ids = [x["endpoint_id"] for x in selected if x.get("endpoint_id")]
            if operation_ids and endpoint_ids:
                questions = self.prepare_change_questions(request, operation_ids)["questions"]
                questions += self.prepare_change_questions(request, endpoint_ids=endpoint_ids)["questions"]
            else:
                questions = self.prepare_change_questions(request, operation_ids or None, endpoint_ids or None)["questions"]
            return {"ok": True, "status": "clarification_required", "stage": stage,
                    "questions": questions, "required_context": [
                        "deployment_mode", "interface_and_zone_roles", "address_plan", "default_route",
                        "preserve_existing", "on_existing", "policy_initial_enabled",
                        "business_subnets_and_protection_scope",
                    ], "next_action": "Read live state and collect user decisions before plan."}
        if action == "plan":
            if not stage:
                raise ValueError("stage is required for plan")
            if not changes or len(changes) != 1:
                raise ValueError("Plan exactly one initialization change per stage; verify before the next change")
            if case_id:
                case = self.initialization_cases.get(case_id, device_id)
                if not case["design_confirmed"]:
                    raise ValueError("Confirm the current case design revision before planning")
                if stage == "management" and not case["design"].get("management_recovery"):
                    raise ValueError("Management stage requires an engineer-confirmed recovery path")
            change = changes[0]
            capability = self.capabilities.resolve(
                operation_id=change.get("operation_id") or "",
                endpoint_id=change.get("endpoint_id") or "", capability="edit")
            resource = capability.path.split("/namespaces/{namespace}/", 1)[-1]
            if not any(resource.startswith(prefix) for prefix in self.INITIALIZATION_RESOURCES[stage]):
                raise ValueError(f"Endpoint does not belong to initialization stage {stage}")
            with sqlite3.connect(self.plans.database.path) as connection:
                pending = connection.execute(
                    "SELECT plan_id FROM operation_plans WHERE device_id=? AND kind LIKE 'initialization:%' "
                    "AND status IN ('planned','applying','applied','failed','unknown') LIMIT 1", (device_id,),
                ).fetchone()
            if pending:
                raise ValueError("Previous initialization plan awaits apply, verification, or review")
            inventory = await self.initialize_device("inspect", device_id, stage=stage, limit=100)
            if not inventory["ok"] or not inventory["inventory_complete"]:
                raise ValueError("Stage inventory failed or may be truncated; no plan was created")
            name = (change.get("body") or {}).get("name") or (change.get("body") or {}).get("prefix")
            if capability.method == "POST" and name:
                for item in inventory["results"]:
                    rows = item.get("data") if isinstance(item.get("data"), list) else []
                    if any(isinstance(row, dict) and (row.get("name") == name or row.get("prefix") == name) for row in rows):
                        raise ValueError(f"Existing object named {name}; select an update endpoint or another name")
            plan = await self.create_change_plan(
                device_id, request, changes, f"initialization:{stage}",
                extra_snapshots=[{"snapshot_type": "initialization_inventory", "results": inventory["results"]}],
            )
            if plan.get("status") == "planned":
                plan["stage"] = stage
                plan["inventory"] = inventory
                plan["next_action"] = "Show impact and rollback; obtain user confirmation before apply"
                if case_id:
                    plan["case_id"] = case_id
                    plan["item_id"] = self.initialization_cases.link_plan(
                        case_id, device_id, plan["plan_id"], stage)
            return plan
        if action == "apply":
            if not plan_id:
                raise ValueError("plan_id is required for apply")
            plan = self.plans.get(plan_id)
            if plan["device_id"] != device_id or not plan["kind"].startswith("initialization:"):
                raise ValueError("Plan is not for this device initialization")
            linked_case_id = self.initialization_cases.case_for_plan(plan_id)
            if linked_case_id and linked_case_id != case_id:
                raise ValueError("case_id is required for this linked initialization plan")
            if case_id:
                link = self.initialization_cases.linked_plan(case_id, device_id, plan_id)
                if link["design_revision"] != self.initialization_cases.get(case_id, device_id)["design_revision"]:
                    raise ValueError("Case design changed after planning; create a new plan")
            applied = await self.apply_change_plan(plan_id, plan["kind"])
            if case_id:
                self.initialization_cases.event(case_id, "apply", applied["status"],
                                                plan["kind"].split(":", 1)[1], plan_id)
            return applied
        if action == "status":
            if not plan_id:
                raise ValueError("plan_id is required for status")
            plan = self.plans.get(plan_id)
            if plan["device_id"] != device_id or not plan["kind"].startswith("initialization:"):
                raise ValueError("Plan is not for this device initialization")
            return plan
        if action == "resolve":
            if not case_id or not plan_id:
                raise ValueError("case_id and plan_id are required for manual resolution")
            self.initialization_cases.linked_plan(case_id, device_id, plan_id)
            plan = self.plans.get(plan_id)
            if plan["status"] not in {"applied", "failed", "unknown"}:
                raise ValueError("This plan does not need manual resolution")
            if design is None or design.get("resolution") not in {"accepted_manual", "abandoned"}:
                raise ValueError("resolution must be accepted_manual or abandoned")
            if plan["status"] != "applied" and design["resolution"] == "accepted_manual":
                raise ValueError("Unknown or failed write cannot be accepted as applied")
            reason = design.get("reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                raise ValueError("A concise engineer review reason is required")
            phase = plan["kind"].split(":", 1)[1]
            inventory = await self.initialize_device("inspect", device_id, stage=phase, limit=100)
            if not inventory["ok"] or not inventory["inventory_complete"]:
                raise ValueError("Live inventory is required before manual resolution")
            result = {"previous_result": plan["result"], "manual_resolution": design["resolution"],
                      "reason": reason.strip(), "reviewed_at": _now(),
                      "inventory_fingerprint": hashlib.sha256(
                          json.dumps(inventory["results"], sort_keys=True, ensure_ascii=False).encode("utf-8")
                      ).hexdigest()}
            updated = self.plans.mark_applied(plan_id, "reviewed", result)
            self.initialization_cases.event(case_id, "resolve", design["resolution"], phase, plan_id)
            return {"ok": True, "status": "reviewed", "plan_id": plan_id,
                    "manual_resolution": design["resolution"], "inventory_fingerprint": result["inventory_fingerprint"]}
        if action == "verify":
            if not plan_id:
                raise ValueError("plan_id is required for verify")
            plan = self.plans.get(plan_id)
            if plan["device_id"] != device_id or not plan["kind"].startswith("initialization:"):
                raise ValueError("Plan is not for this device initialization")
            linked_case_id = self.initialization_cases.case_for_plan(plan_id)
            if linked_case_id and linked_case_id != case_id:
                raise ValueError("case_id is required for this linked initialization plan")
            if case_id:
                self.initialization_cases.linked_plan(case_id, device_id, plan_id)
            if plan["status"] != "applied":
                raise ValueError("Only an applied initialization plan can be verified")
            phase = plan["kind"].split(":", 1)[1]
            inventory = await self.initialize_device("inspect", device_id, stage=phase, limit=100)
            step = plan["steps"][0]
            body = step.get("body") or {}
            identity = (body.get("name") or body.get("prefix") or
                        step.get("path_parameters", {}).get("name") or
                        step.get("path_parameters", {}).get("uuid"))
            matches = [row for item in inventory["results"] if isinstance(item.get("data"), list)
                       for row in item["data"] if isinstance(row, dict) and identity in
                       (row.get("name"), row.get("prefix"), row.get("uuid"))]
            verified = bool(identity and matches and body and inventory["inventory_complete"]
                            and all(key in matches[0] and matches[0][key] == value
                                    for key, value in body.items()))
            outcome = {"ok": verified, "status": "verified" if verified else "review_required",
                       "stage": phase, "plan_id": plan_id, "inventory": inventory,
                       "message": "回读字段与计划一致" if verified else "无法自动证明变更已生效，请人工核对设备回读结果"}
            if verified:
                self.plans.mark_applied(plan_id, "verified", {
                    "apply": plan["result"], "verification": outcome,
                })
            if case_id:
                resource = self.capabilities.resolve(
                    operation_id=step.get("operation_id") or "",
                    endpoint_id=step.get("endpoint_id") or "", capability="edit").path
                selected = self._inventory_item(inventory["results"], resource, identity)
                self.initialization_cases.record_implementation(
                    case_id, device_id, plan_id, selected, outcome["status"])
            return outcome
        raise ValueError(f"Unsupported initialization action: {action}")

    @staticmethod
    def _inventory_item(results: list[dict[str, Any]], resource: str,
                        identity: Any) -> dict[str, Any] | None:
        operation = DeviceService._inventory_operation(resource)
        result = next((item for item in results if item.get("operation_id") == operation), None)
        rows = result.get("data") if result and isinstance(result.get("data"), list) else []
        return next((row for row in rows if isinstance(row, dict) and identity is not None
                     and identity in (row.get("name"), row.get("prefix"), row.get("uuid"))), None)

    async def review_initialization_case(self, device_id: str, case_id: str) -> dict[str, Any]:
        record = self.initialization_cases.get(case_id, device_id)
        links = self.initialization_cases.plans(case_id, device_id)
        if not links:
            return {"ok": False, "status": "no_plans", "case_id": case_id, "rows": []}
        inventories: dict[str, dict[str, Any]] = {}
        for phase in dict.fromkeys(item["stage"] for item in links):
            inventories[phase] = await self.initialize_device(
                "inspect", device_id, stage=phase, limit=100)
        if any(not result["ok"] or not result["inventory_complete"]
               for result in inventories.values()):
            return {"ok": False, "status": "inventory_incomplete", "case_id": case_id,
                    "message": "设备回读失败或截断，无法可靠生成反馈差异表", "rows": []}
        rows = []
        tracked: set[tuple[str, str, str]] = set()
        baseline_by_stage: dict[str, list[dict[str, Any]]] = {}
        for link in links:
            plan = self.plans.get(link["plan_id"])
            step = plan["steps"][0]
            capability = self.capabilities.resolve(
                operation_id=step.get("operation_id") or "",
                endpoint_id=step.get("endpoint_id") or "", capability="edit")
            body = step.get("body") or {}
            identity = (body.get("name") or body.get("prefix") or
                        step.get("path_parameters", {}).get("name") or
                        step.get("path_parameters", {}).get("uuid"))
            baseline = next((item["results"] for item in plan["snapshots"]
                             if item.get("snapshot_type") == "initialization_inventory"), [])
            baseline_by_stage.setdefault(link["stage"], baseline)
            source_operation = self._inventory_operation(capability.path)
            if identity is not None:
                tracked.add((link["stage"], source_operation, str(identity)))
            before = self._inventory_item(baseline, capability.path, identity)
            current = self._inventory_item(inventories[link["stage"]]["results"],
                                           capability.path, identity)
            implemented = json.loads(link["implementation_json"]) if link["implementation_json"] else None
            if implemented is None:
                difference = "implementation_not_captured"
            elif current is None:
                difference = "object_missing"
            else:
                changed = sorted(key for key in set(implemented) | set(current)
                                 if implemented.get(key) != current.get(key))
                difference = changed if changed else "none"
            row = {"case_id": case_id, "item_id": link["item_id"], "stage": link["stage"],
                   "configuration": identity or capability.title, "plan_status": plan["status"],
                   "design_revision": link["design_revision"], "before": _safe(before),
                   "planned": _safe(body), "implemented": _safe(implemented),
                   "current": _safe(current), "difference": difference,
                   "difference_confirmed": "", "satisfaction": "", "reason": "", "comment": ""}
            row["review_hash"] = self.initialization_cases.review_hash(row)
            rows.append(row)
        for phase, baseline in baseline_by_stage.items():
            old = self._inventory_objects(baseline)
            new = self._inventory_objects(inventories[phase]["results"])
            for (operation, identity) in sorted(set(old) | set(new)):
                if (phase, operation, identity) in tracked or old.get((operation, identity)) == new.get((operation, identity)):
                    continue
                item_id = "extra:" + hashlib.sha256(
                    f"{case_id}:{phase}:{operation}:{identity}".encode("utf-8")
                ).hexdigest()[:24]
                before, current = old.get((operation, identity)), new.get((operation, identity))
                if before is None:
                    difference = "engineer_added_or_external_change"
                elif current is None:
                    difference = "engineer_removed_or_external_change"
                else:
                    difference = sorted(key for key in set(before) | set(current)
                                        if before.get(key) != current.get(key))
                row = {"case_id": case_id, "item_id": item_id, "stage": phase,
                       "configuration": f"{operation}:{identity}", "plan_status": "unplanned_change",
                       "design_revision": record["design_revision"], "before": _safe(before),
                       "planned": None, "implemented": None, "current": _safe(current),
                       "difference": difference, "difference_confirmed": "", "satisfaction": "",
                       "reason": "", "comment": ""}
                row["review_hash"] = self.initialization_cases.review_hash(row)
                rows.append(row)
        csv_template = self.initialization_cases.csv_template(rows)
        csv_path = self.initialization_cases.persist_review_csv(case_id, csv_template)
        return {"ok": True, "status": "ready", "case_id": case_id,
                "design_revision": record["design_revision"], "rows": rows,
                "feedback": self.initialization_cases.feedback(case_id, device_id),
                "csv_template": csv_template, "csv_path": csv_path}

    @staticmethod
    def _inventory_operation(resource: str) -> str:
        mapping = {
            "interfaces": "interface.list", "zones": "zone.list", "virlines": "virline.list",
            "ipgroups": "network_object.list", "staticroutes/ipv4": "route.ipv4.list",
            "nats": "policy.nat.list", "securitys": "policy.security.list",
            "appcontrols/policys": "policy.app_control.list",
            "localappcontrols/policys": "policy.local_control.list",
        }
        suffix = resource.split("/namespaces/{namespace}/", 1)[-1]
        return next((value for prefix, value in mapping.items() if suffix.startswith(prefix)), "")

    @staticmethod
    def _inventory_objects(results: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        objects = {}
        for result in results:
            operation = result.get("operation_id")
            rows = result.get("data")
            if not operation or not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                identity = row.get("name") or row.get("prefix") or row.get("uuid")
                if identity is not None:
                    objects[(operation, str(identity))] = row
        return objects

    async def test_connection(self, device_id: str) -> DeviceConnectionResult:
        profile, password = self.repository.get(device_id, include_secret=True)
        started = time.perf_counter()
        if not profile.enabled:
            self.repository.record_test(device_id, success=False, stage="configuration",
                                        message="设备配置已停用", latency_ms=0)
            raise DeviceAPIError("configuration", "设备配置已停用")
        login_data: dict[str, Any] = {}
        try:
            try:
                client_context = self.client_factory(profile, password or "")
            except Exception as exc:
                raise DeviceAPIError("configuration", f"设备客户端配置无效：{type(exc).__name__}") from exc
            async with client_context as client:
                try:
                    login_data = await client.login()
                    await client.keepalive()
                finally:
                    if client.token:
                        try:
                            await client.logout()
                        except DeviceAPIError:
                            pass
            latency_ms = round((time.perf_counter() - started) * 1000)
            result = DeviceConnectionResult(
                ok=True, device_id=profile.device_id, device_name=profile.name,
                base_url=profile.base_url, namespace=profile.namespace, username=profile.username,
                role=login_data.get("role"), auth_result=login_data.get("authResult"),
                reported_namespace=login_data.get("namespace"), keepalive_ok=True,
                latency_ms=latency_ms, tested_at=_now(),
            )
            self.repository.record_test(device_id, success=True, stage="completed",
                                        message="登录、保活和注销完成", latency_ms=latency_ms)
            return result
        except DeviceAPIError as exc:
            self.repository.record_test(
                device_id, success=False, stage=exc.stage, message=exc.message,
                latency_ms=round((time.perf_counter() - started) * 1000),
            )
            raise
