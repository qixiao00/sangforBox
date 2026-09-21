import asyncio
import json

from fastapi.testclient import TestClient
from mcp import Client

from af_app.app import create_app
from af_app.config import Settings


def payload(result):
    return result.structured_content or json.loads(result.content[0].text)


def test_mcp_analysis_download_and_explicit_review(tmp_path):
    app = create_app(Settings(tmp_path / "app.db"))
    optimizer = app.state.policy_optimizer
    optimizer.devices.resolve_device = lambda d: d
    optimizer.devices.repository.get = lambda *args, **kwargs: None
    calls = []

    async def capture(device):
        calls.append(device)
        return {"configuration": {"policy.app_control.list": [
            {"uuid": "r1", "name": "old", "position": 0, "enable": True,
             "src": {}, "dst": {}, "action": 1, "schedule": "all-week", "advanceOption": {}}
        ], "network_object.list": [], "policy.app_control.group.list": []}}

    optimizer.collector.capture = capture

    async def scenario(web):
        async with Client(app.state.mcp_server) as client:
            result = await client.call_tool("analyze_app_control", {"request": {"device_id": "lab"}})
            assert not result.is_error
            job_id = payload(result)["job_id"]
            task = optimizer.tasks.get(job_id)
            if task:
                await task
            result = await client.call_tool("get_app_control_analysis", {"job_id": job_id})
            report = payload(result)
            assert report["state"] == "completed"
            assert not report["execution_ready"]
            download = web.get(report["review_url"])
            assert download.status_code == 200
            assert "用户决定" in download.text and "待确认" in download.text
            findings = web.get(report["findings_url"])
            assert findings.status_code == 200 and "证据与限制" in findings.text
            analysis = optimizer.store.get(report["analysis_id"])
            result = await client.call_tool("review_app_control", {
                "analysis_id": report["analysis_id"], "proposal_hash": report["proposal_hash"],
                "decisions": [{"candidate_id": analysis["candidates"][0]["id"], "decision": "采纳"}],
                "submission_key": "user-message-1"})
            assert not result.is_error
            assert not payload(result)["execution_authorized"]
            assert payload(result)["status"] == "awaiting_combination_verification"
            bad = await client.call_tool("analyze_app_control", {"request": {"device_id": "lab", "execute": True}})
            assert bad.is_error
    with TestClient(app) as web:
        asyncio.run(scenario(web))
        assert web.get("/api/app-control/analyses/missing/review.csv").status_code == 404
    assert calls == ["lab"]
