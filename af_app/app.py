import asyncio
import json
import sqlite3
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from mcp import Client
from mcp.server.transport_security import TransportSecuritySettings
from af_app.config import Settings
from af_app.database import Database
from af_app.mcp_server import create_mcp
from af_app.service import (
    ProbeService, ProbeRequest, ProbeResult,
    KnowledgeSearchRequest, KnowledgeSearchResponse,
)
from af_app.events import Trace
from af_app.devices import (
    DeviceProfile, DeviceProfileCreate, DeviceProfileUpdate,
    DeviceConnectionResult, DeviceConnectionAudit,
)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    traces = {}
    service = ProbeService(database, settings.output_directory)
    server = create_mcp(service, traces)
    settings.output_directory.mkdir(parents=True, exist_ok=True)

    def persist_export(filename: str, content: str) -> None:
        target = settings.output_directory / filename
        descriptor, temporary = tempfile.mkstemp(prefix=filename + ".", suffix=".tmp",
                                                  dir=settings.output_directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    mcp_http = server.streamable_http_app(streamable_http_path="/", transport_security=TransportSecuritySettings(
        allowed_hosts=list(settings.allowed_hosts), allowed_origins=[]))

    @asynccontextmanager
    async def lifespan(app):
        database.initialize()
        try:
            async with server.session_manager.run():
                yield
        finally:
            await service.policy_optimizer.close()
            database.engine.dispose()

    app = FastAPI(title="AF Workbench", version="1.0.6", lifespan=lifespan)
    app.state.mcp_server = server
    app.state.database = database
    app.state.device_service = service.devices
    app.state.policy_optimizer = service.policy_optimizer

    @app.get("/api/app-control/analyses/{analysis_id}/review.csv")
    def download_policy_review(analysis_id: str):
        try:
            content = service.policy_optimizer.store.export_csv(analysis_id)
        except KeyError as exc:
            raise HTTPException(404, "Analysis not found") from exc
        persist_export(f"app-control-review-{analysis_id}.csv", content)
        return Response(content.encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="app-control-review.csv"',
                                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/app-control/analyses/{analysis_id}/findings.csv")
    def download_policy_findings(analysis_id: str):
        try:
            content = service.policy_optimizer.store.export_findings_csv(analysis_id)
        except KeyError as exc:
            raise HTTPException(404, "Analysis not found") from exc
        persist_export(f"app-control-findings-{analysis_id}.csv", content)
        return Response(content.encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="app-control-findings.csv"',
                                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/app-control/feedback/{feedback_id}.json")
    def download_policy_feedback(feedback_id: str):
        try:
            document = service.policy_optimizer.store.get_feedback(feedback_id)
            content = service.policy_optimizer.store.export_feedback_json(feedback_id)
        except KeyError as exc:
            raise HTTPException(404, "Feedback log not found") from exc
        except ValueError as exc:
            raise HTTPException(409, "Feedback log integrity check failed") from exc
        persist_export(f"app-control-feedback-{document['feedback_id']}.json", content)
        return Response(content.encode("utf-8"), media_type="application/json; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="app-control-feedback-{document["feedback_id"]}.json"',
                                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/test", response_model=ProbeResult)
    async def test_api(request: ProbeRequest, stream: bool = False):
        # Real in-process MCP protocol call; never call the service directly here.
        trace = Trace()
        traces[trace.id] = trace

        async def execute():
            trace.emit("Web", "running", "请求已接收，正在调用 MCP")
            try:
                async with asyncio.timeout(30):
                    async with Client(server) as client:
                        result = await client.call_tool("framework_probe", {"message": request.message, "request_id": trace.id})
                        if result.is_error:
                            raise RuntimeError("MCP tool failed")
                        payload = ProbeResult.model_validate(result.structured_content)
                trace.emit("Web", "completed", "请求完成", result=payload.model_dump())
                return payload
            except asyncio.CancelledError:
                trace.emit("Web", "cancelled", "请求已取消")
                raise
            except Exception:
                trace.emit("Web", "failed", "请求失败，请检查服务与数据库状态")
                raise
            finally:
                traces.pop(trace.id, None)
                trace.queue.put_nowait(None)

        if not stream:
            try:
                return await execute()
            except Exception as exc:
                raise HTTPException(502, "Framework probe failed") from exc

        async def events():
            task = asyncio.create_task(execute())
            try:
                while True:
                    event = await trace.queue.get()
                    if event is None:
                        break
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        return StreamingResponse(events(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/knowledge/stats")
    def knowledge_stats():
        return service.knowledge.stats()

    @app.get("/api/knowledge/overview")
    def knowledge_overview():
        return service.knowledge.overview()

    @app.post("/api/knowledge/search", response_model=KnowledgeSearchResponse)
    def knowledge_search(request: KnowledgeSearchRequest):
        return service.search_knowledge(request)

    @app.get("/api/knowledge/chunks/{chunk_id:path}")
    def knowledge_chunk(chunk_id: str):
        chunk = service.knowledge.get_chunk(chunk_id)
        if chunk is None:
            raise HTTPException(404, "Knowledge chunk not found")
        return chunk

    @app.get("/api/knowledge/api-endpoints")
    def api_endpoints(path: str, method: str = "", limit: int = 10):
        return {"items": service.knowledge.lookup_api(path, method or None, min(max(limit, 1), 20))}

    @app.get("/api/knowledge/api-endpoints/{endpoint_id:path}")
    def api_endpoint_detail(endpoint_id: str, section: str = "overview", offset: int = 0, limit: int = 40):
        try:
            endpoint = service.knowledge.get_api_endpoint(
                endpoint_id, section, max(offset, 0), min(max(limit, 1), 80))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if endpoint is None:
            raise HTTPException(404, "API endpoint not found")
        return endpoint

    @app.get("/api/knowledge/cli-commands")
    def cli_commands(q: str, limit: int = 10):
        return {"items": service.knowledge.lookup_cli(q, min(max(limit, 1), 20))}

    @app.get("/api/devices", response_model=list[DeviceProfile])
    def list_devices():
        return service.devices.repository.list()

    @app.post("/api/devices", response_model=DeviceProfile, status_code=201)
    def create_device(request: DeviceProfileCreate):
        try:
            return service.devices.repository.create(request)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Device profile name already exists") from exc

    @app.get("/api/devices/{device_id}", response_model=DeviceProfile)
    def get_device(device_id: str):
        try:
            return service.devices.repository.get(device_id)[0]
        except KeyError as exc:
            raise HTTPException(404, "Device profile not found") from exc

    @app.patch("/api/devices/{device_id}", response_model=DeviceProfile)
    def update_device(device_id: str, request: DeviceProfileUpdate):
        try:
            return service.devices.repository.update(device_id, request)
        except KeyError as exc:
            raise HTTPException(404, "Device profile not found") from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Device profile name already exists") from exc

    @app.delete("/api/devices/{device_id}", status_code=204)
    def delete_device(device_id: str):
        try:
            service.devices.repository.delete(device_id)
        except KeyError as exc:
            raise HTTPException(404, "Device profile not found") from exc

    @app.post("/api/devices/{device_id}/test", response_model=DeviceConnectionResult)
    async def test_device_connection(device_id: str):
        async with Client(server) as client:
            tool_result = await client.call_tool("query_device", {
                "request": "测试设备 API 连接",
                "device_id": device_id,
                "operation_ids": ["device.connection.test"],
            })
        if tool_result.is_error:
            raise HTTPException(502, "Device connection tool failed")
        outcome = tool_result.structured_content
        if outcome is None and tool_result.content:
            text_content = getattr(tool_result.content[0], "text", "")
            outcome = json.loads(text_content) if text_content else None
        if not isinstance(outcome, dict):
            raise HTTPException(502, "Device connection tool returned an invalid payload")
        results = outcome.get("results") or []
        first = results[0] if results else {}
        if not outcome.get("ok") or not first.get("ok"):
            error = first.get("error") or outcome.get("error") or {"message": outcome.get("message", "Unknown device error")}
            status = 404 if outcome.get("status") == "device_not_found" else 502
            raise HTTPException(status, error)
        return DeviceConnectionResult.model_validate(first["data"])

    @app.get("/api/devices/{device_id}/audits", response_model=list[DeviceConnectionAudit])
    def device_connection_audits(device_id: str, limit: int = 50):
        try:
            return service.devices.repository.list_audits(device_id, min(max(limit, 1), 200))
        except KeyError as exc:
            raise HTTPException(404, "Device profile not found") from exc

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")
    app.mount("/mcp", mcp_http)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    return app
