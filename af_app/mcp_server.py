from typing import Annotated, Any, Literal
from pydantic import Field
from mcp.server.mcpserver import MCPServer
from af_app.service import ProbeResult, KnowledgeSearchRequest, KnowledgeSearchResponse
from af_app.devices import DeviceAPIError
from af_app.operations import DeviceChange
from af_app.policy_optimizer import OptimizationRequest, ReviewDecision, FeedbackRequest


def create_mcp(service, traces):
    server = MCPServer(
        "AF Workbench",
        instructions="""你是深信服 AF 运维连接器。默认且仅通过本 MCP 内部的 AF HTTP API 查询或操作设备；凭据、登录和 Cookie 由服务器管理。不要编写 Python/curl、读取本地凭据、修改本地 Skill，或自动切换 SSH/CLI。query_device 用于实时查询，edit_device 用于明确的单项配置变更，initialize_device 仅用于初始化工作流。应用控制策略的整表梳理/优化必须调用 analyze_app_control、get_app_control_analysis，先展示审核 CSV 和问题 CSV，再按用户决定使用 review_app_control、verify_app_control_review、preflight_app_control_review。工程师手动修改后用 record_app_control_feedback 读取最新设备配置并生成可下载差异日志；此流程只读，不得把优化候选转交 edit_device 下发。device_id 可填写档案 ID、名称、IP 或 URL。优先使用已注册 operation_id；未注册能力调用 lookup_api_endpoint 查找手册端点，必要时 get_api_endpoint 核对参数，然后把唯一 endpoint_id 交给 query_device，或作为 edit_device change 的 endpoint_id。不得把 URL 交给执行工具。单项配置变更先调用 edit_device(action=prepare)，逐项向用户询问返回的必答问题；不得代替用户选择新增/修改、同名对象处理方式或策略启用状态。收集答案后才可 plan。GET 端点可直接查询；POST/PUT/PATCH/DELETE 必须 plan，向用户呈现对象、变更、影响和回滚，确认后 apply，再读取设备验证。普通实时查询不先调用 framework_probe。Session not found 属于客户端 MCP 会话失效，应提示重新连接，不得绕过 MCP。最终答复采用厂商运维口径：先给结论，只列与问题相关的配置和异常；区分设备实测、手册定义和推断；省略工具调用过程、PID、Session ID、原始 JSON、无关对象及调试产物。查询成功通常控制在一张小表或 3-6 个要点。""",
    )

    def describe_operations(operation_ids: list[str], kind: str) -> dict:
        if not operation_ids or len(operation_ids) > 8:
            raise ValueError("Supply 1-8 operation_ids returned by discover")
        items = []
        for operation_id in operation_ids:
            op = service.devices.capabilities.get(operation_id, kind)
            item = op.public()
            item["path"] = op.path
            item["call_shape"] = {"operation_id": operation_id,
                "path_parameters": {key: "<resource identity>" for key in op.required_path_parameters},
                "query_parameters": {}, "body": {} if kind == "edit" and op.method != "DELETE" else None}
            if op.endpoint_id:
                item["request_definition"] = service.knowledge.get_api_endpoint(op.endpoint_id, "request", 0, 20)
                item["examples"] = service.knowledge.get_api_endpoint(op.endpoint_id, "examples", 0, 2)
            if operation_id.startswith("virline."):
                item["workflow_note"] = "先查询 zone.list/interface.list/virline.list。接口须先解除不兼容区域成员关系，再改 VIRTUALLINE，建立网线组，再加入 VIRTUAL 区域。不要推断启用接口或区域方向；计划应列出全部步骤。"
            items.append(item)
        return {"ok": True, "transport": "AF HTTP API", "items": items,
                "next_action": "plan" if kind == "edit" else "execute"}

    @server.tool()
    async def framework_probe(message: Annotated[str, Field(min_length=1, max_length=200)] = "hello AF", request_id: str = "") -> ProbeResult:
        """Test the AF framework and SQLite connection. Echoes a short message; no device or PDF operations."""
        trace = traces.get(request_id)
        if trace:
            trace.emit("MCP", "running", "执行 framework_probe")
            trace.emit("SQLite", "running", "读取数据库结构版本")
        try:
            result = service.run(message)
        except Exception:
            if trace:
                trace.emit("SQLite", "failed", "数据库检查失败")
                trace.emit("MCP", "failed", "工具执行失败")
            raise
        if trace:
            trace.emit("SQLite", "completed", "数据库连接正常")
            trace.emit("MCP", "completed", "测试工具已返回")
        return result

    @server.tool()
    async def search_knowledge(
        query: Annotated[str, Field(min_length=1, max_length=500)],
        top_k: Annotated[int, Field(ge=1, le=20)] = 5,
        doc_type: str = "",
        version: str = "",
    ) -> KnowledgeSearchResponse:
        """Search the local Sangfor AF manuals. Returns source chunks with PDF page numbers; it does not generate an answer. Empty version searches all imported manual versions instead of assuming one."""
        return service.search_knowledge(KnowledgeSearchRequest(
            query=query, top_k=top_k, doc_type=doc_type or None, version=version or None))

    @server.tool()
    async def get_source_chunk(chunk_id: str) -> dict:
        """Read one complete knowledge chunk by its stable chunk_id, including source pages and neighboring chunk IDs."""
        chunk = service.knowledge.get_chunk(chunk_id)
        if chunk is None:
            raise ValueError(f"Unknown chunk_id: {chunk_id}")
        return chunk

    @server.tool()
    async def lookup_api_endpoint(
        query: Annotated[str, Field(max_length=500)] = "",
        path: Annotated[str, Field(max_length=500)] = "",
        method: str = "",
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> dict:
        """Find compact AF API summaries by natural-language query or URL. `path` remains as a compatibility alias. Empty auth/headers mean unknown; never infer them."""
        search_text = query.strip() or path.strip()
        if not search_text:
            raise ValueError("query or path is required")
        return {"query": {"text": search_text, "method": method or None},
                "items": service.knowledge.lookup_api(search_text, method or None, limit)}

    @server.tool()
    async def get_api_endpoint(
        endpoint_id: str,
        section: Annotated[str, Field(pattern="^(overview|request|response|examples)$")] = "overview",
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=80)] = 40,
    ) -> dict:
        """Read one AF API endpoint in bounded sections. Fields are paginated, examples are length-limited, and source pages are included."""
        endpoint = service.knowledge.get_api_endpoint(endpoint_id, section, offset, limit)
        if endpoint is None:
            raise ValueError(f"Unknown endpoint_id: {endpoint_id}")
        return endpoint

    @server.tool()
    async def lookup_cli_command(command: str, limit: Annotated[int, Field(ge=1, le=20)] = 10) -> dict:
        """Look up structured AF CLI command definitions and syntax."""
        return {"query": command, "items": service.knowledge.lookup_cli(command, limit)}

    @server.tool()
    async def query_device(
        request: Annotated[str, Field(max_length=500)] = "",
        device_id: str = "",
        operation_ids: list[str] | None = None,
        endpoint_ids: list[str] | None = None,
        path_parameters: dict[str, str] | None = None,
        query_parameters: dict[str, Any] | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        action: Literal["execute", "discover", "describe", "devices"] = "execute",
    ) -> dict:
        """默认通过 AF API 查询实时设备状态。已注册能力用 operation_ids；未注册能力先调用 lookup_api_endpoint，选定唯一 GET endpoint_id 后以 endpoint_ids 执行。path_parameters 填 URL 占位符，query_parameters 只填文档声明参数。不要编写脚本。回答时只保留相关对象和结论。"""
        try:
            if action == "devices":
                return {"ok": True, "items": [p.model_dump() for p in service.devices.repository.list()]}
            if action == "discover":
                return service.devices.discover(request, "query")
            if action == "describe":
                return describe_operations(operation_ids or [], "query")
            if not device_id and operation_ids != ["device.profiles.list"]:
                device_id = service.devices.resolve_device("")
            return await service.devices.query_device(
                device_id, request, operation_ids, endpoint_ids, path_parameters, query_parameters, limit)
        except KeyError:
            return {"ok": False, "status": "device_not_found", "message": "Device profile not found"}
        except (ValueError, DeviceAPIError) as exc:
            detail = exc.public_detail() if isinstance(exc, DeviceAPIError) else {"message": str(exc)}
            return {"ok": False, "status": "request_failed", "error": detail}

    @server.tool()
    async def edit_device(
        action: Literal["discover", "describe", "prepare", "plan", "status", "apply"],
        request: Annotated[str, Field(max_length=500)] = "",
        device_id: str = "",
        changes: list[DeviceChange] | None = None,
        plan_id: str = "",
        operation_ids: list[str] | None = None,
        endpoint_ids: list[str] | None = None,
    ) -> dict:
        """默认通过 AF API 新增、修改或删除设备配置。先用 prepare 获取操作前必答问题，例如新增/修改、同名对象处理和策略初始启用状态；收集答案后再 plan。注册能力用 operation_id；未注册能力使用 endpoint_id。每项 change 只填一种 ID，并使用 path_parameters/query_parameters/body/decisions。plan 只生成计划，用户确认后 apply，再查询验证。"""
        try:
            if action == "discover":
                return service.devices.discover(request, "edit")
            if action == "describe":
                return describe_operations(operation_ids or [], "edit")
            if action == "prepare":
                return service.devices.prepare_change_questions(request, operation_ids, endpoint_ids)
            if action == "plan":
                if not device_id:
                    raise ValueError("device_id is required for plan")
                return await service.devices.create_change_plan(device_id, request, [item.model_dump() for item in changes or []], "edit")
            if action == "status":
                if not plan_id:
                    raise ValueError("plan_id is required for status")
                return service.devices.plans.get(plan_id)
            if not plan_id:
                raise ValueError("plan_id is required for apply")
            return await service.devices.apply_change_plan(plan_id, "edit")
        except KeyError:
            return {"ok": False, "status": "not_found", "message": "Device profile or plan not found"}
        except (ValueError, DeviceAPIError) as exc:
            detail = exc.public_detail() if isinstance(exc, DeviceAPIError) else {"message": str(exc)}
            return {"ok": False, "status": "request_failed", "error": detail}

    @server.tool()
    async def initialize_device(
        action: Literal["design", "case", "inspect", "prepare", "plan", "status", "apply", "verify", "resolve",
                        "feedback_review", "feedback_submit", "events"],
        device_id: str,
        request: Annotated[str, Field(max_length=500)] = "",
        changes: list[DeviceChange] | None = None,
        plan_id: str = "",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        stage: Literal["", "management", "topology", "objects", "routing", "nat", "security", "application"] = "",
        case_id: str = "",
        design: dict[str, Any] | None = None,
        confirm_design: bool = False,
        feedback_csv: str = "",
    ) -> dict:
        """AF initialization trial in one MCP tool. Collect a design over multiple turns with design(case_id optional), confirm its revision, inspect and prepare each stage, then plan one change, obtain user confirmation, apply and verify. Bind changes with case_id. resolve records an engineer's manual review after uncertain or mismatched results. feedback_review reads live configuration and returns a difference table plus CSV; feedback_submit accepts the completed CSV; events returns metadata-only audit events. Registered operation_id and documented endpoint_id are both supported."""
        try:
            result = await service.devices.initialize_device(
                action, service.devices.resolve_device(device_id), request,
                [item.model_dump() for item in changes or []], plan_id, limit, stage,
                case_id, design, confirm_design, feedback_csv)
            if case_id and action in {"inspect", "prepare", "case", "status", "events"}:
                service.devices.initialization_cases.event(
                    case_id, action, str(result.get("status") or "completed"), stage, plan_id)
            return result
        except KeyError:
            return {"ok": False, "status": "not_found", "message": "Device profile or plan not found"}
        except (ValueError, DeviceAPIError) as exc:
            if case_id:
                try:
                    resolved = service.devices.resolve_device(device_id)
                    service.devices.initialization_cases.get(case_id, resolved)
                    service.devices.initialization_cases.event(case_id, action, "failed", stage, plan_id)
                except (ValueError, KeyError):
                    pass
            detail = exc.public_detail() if isinstance(exc, DeviceAPIError) else {"message": str(exc)}
            return {"ok": False, "status": "request_failed", "error": detail}

    @server.tool()
    async def analyze_app_control(request: OptimizationRequest) -> dict:
        """应用控制梳理：指定设备，默认分析并导出审核表，不修改设备。
        返回 job_id，使用 get_app_control_analysis 查询完成结果。
        内置程序负责采集和分析，不生成临时脚本。尚不支持的分析明确列出。
        """
        return await service.policy_optimizer.start(request)

    @server.tool()
    async def get_app_control_analysis(job_id: str, offset: Annotated[int, Field(ge=0)] = 0,
                                       limit: Annotated[int, Field(ge=1, le=50)] = 20) -> dict:
        """查询应用控制分析进度、摘要与审核表下载地址。"""
        result = service.policy_optimizer.status(job_id)
        if result.get("analysis_id"):
            analysis = service.policy_optimizer.store.get(result["analysis_id"])
            result["candidates"] = analysis["candidates"][offset:offset + limit]
            result["offset"] = offset
        return result

    @server.tool()
    async def review_app_control(analysis_id: str, proposal_hash: str, submission_key: str,
                                 decisions: list[ReviewDecision] | None = None,
                                 review_csv: Annotated[str, Field(max_length=2000000)] | None = None) -> dict:
        """记录用户按变更编号明确给出的审核决定。先展示审核表，不代替用户采纳。
        相同提交重试必须复用 submission_key。未指定项目保持待确认。
        本工具只记录审核，不生成或执行设备变更。
        """
        if (decisions is None) == (review_csv is None):
            raise ValueError("Supply either explicit decisions or the edited review CSV")
        store = service.policy_optimizer.store
        if store.get(analysis_id)["proposal_hash"] != proposal_hash:
            raise ValueError("Stale proposal version")
        if review_csv is not None:
            return store.review_csv(analysis_id, review_csv, submission_key)
        return store.submit(
            analysis_id, proposal_hash, [d.model_dump() for d in decisions], submission_key)

    @server.tool()
    async def verify_app_control_review(review_id: str) -> dict:
        """按已保存审核项重建完整策略表并复验组合效果，不修改设备。
        模型等价不等于设备适配已认证，未满足执行条件时不得调用 edit_device 绕过。
        """
        import asyncio
        return await asyncio.to_thread(service.policy_optimizer.verify_review, review_id)

    @server.tool()
    async def preflight_app_control_review(review_id: str) -> dict:
        """只读执行前检查：重新采集设备配置，核对审核快照并报告执行阻断项。
        不创建可执行计划、不写设备；快照变化必须重新分析和审核。
        """
        return await service.policy_optimizer.preflight_review(review_id)

    @server.tool()
    async def record_app_control_feedback(request: FeedbackRequest) -> dict:
        """工程师完成手动变更后，只读采集当前应用控制配置，与审核版本比较。
        保存不可变差异日志并返回下载链接。工程师标签、工单和原因属于自述，
        配置差异不证明修改者身份或流量行为。相同提交重试复用 submission_key。
        """
        return await service.policy_optimizer.record_feedback(request)

    @server.tool()
    async def get_app_control_feedback(feedback_id: str) -> dict:
        """按反馈 ID 查询已保存差异日志的摘要与下载链接，不重新读取设备。"""
        return service.policy_optimizer.get_feedback(feedback_id)

    @server.tool()
    async def list_app_control_feedback(review_id: str, offset: Annotated[int, Field(ge=0)] = 0,
                                        limit: Annotated[int, Field(ge=1, le=50)] = 20) -> dict:
        """列出某次审核关联的工程师反馈日志，用于将日志交还给需求方。"""
        return service.policy_optimizer.list_feedback(review_id, offset, limit)

    return server

