import asyncio
import copy
import json

from fastapi.testclient import TestClient
from mcp import Client

from af_app.app import create_app
from af_app.config import Settings
from af_app.policy_combination import materialize
from af_app.policy_optimizer import OptimizationRequest, propose
from af_app.policy_review import fingerprint

from test_policy_combination import config


def snapshot(configuration):
    data = copy.deepcopy(configuration)
    return {"device_id": "lab", "namespace": "public", "configuration": data,
            "configuration_hash": fingerprint({"device_id": "lab", "namespace": "public", "configuration": data}),
            "consistency": "two_matching_reads_not_atomic"}


def payload(result):
    return result.structured_content or json.loads(result.content[0].text)


def setup_review(app, objective="merge"):
    optimizer = app.state.policy_optimizer
    baseline = snapshot(config())
    candidates, findings = propose(baseline, OptimizationRequest(device_id="lab", objectives=[objective]))
    analysis = optimizer.store.create("lab", baseline, candidates, findings)
    review = optimizer.store.submit(analysis["id"], analysis["proposal_hash"],
                                    [{"candidate_id": candidates[0]["id"], "decision": "采纳"}], "review-1")
    return optimizer, analysis, review, materialize(analysis, review)


def test_engineer_feedback_log_round_trip_and_idempotency(tmp_path):
    app = create_app(Settings(tmp_path / "app.db"))
    optimizer, analysis, review, recommended = setup_review(app)
    calls = []
    live = recommended

    async def capture(device):
        calls.append(device)
        return snapshot(live)

    optimizer.collector.capture = capture
    candidate_id = review["selected_ids"][0]
    request = {"review_id": review["review_id"], "submission_key": "manual-change-1",
               "engineer_label": "engineer-A", "change_ticket": "CHG-100",
               "feedback_items": [{"candidate_id": candidate_id, "assessment": "implemented",
                                   "reason": "按工单手动调整"}]}

    async def scenario(web):
        nonlocal live
        async with Client(app.state.mcp_server) as client:
            first = await client.call_tool("record_app_control_feedback", {"request": request})
            assert not first.is_error
            summary = payload(first)
            assert summary["status"] == "comparison_complete"
            assert summary["observed_alignment"] == "matches_recommendation"
            assert summary["candidate_results"][0]["observed_alignment"] == "matches_recommendation"
            assert summary["device_writes"] == 0
            downloaded = web.get(summary["log_url"])
            assert downloaded.status_code == 200
            assert downloaded.headers["content-disposition"].startswith("attachment;")
            log = downloaded.json()
            assert log["proposal_hash"] == analysis["proposal_hash"]
            assert log["engineer_declaration"]["source"] == "self_report_not_authenticated"
            assert log["comparison"]["policy_order"]["recommended_vs_observed"] == "match"
            assert log["feedback_hash"] == fingerprint({k: v for k, v in log.items() if k != "feedback_hash"})

            live = analysis["snapshot"]["configuration"]
            repeated = await client.call_tool("record_app_control_feedback", {"request": request})
            assert payload(repeated)["feedback_id"] == summary["feedback_id"]
            assert calls == ["lab"]
            changed_request = {**request, "submission_key": "manual-change-2"}
            second = payload(await client.call_tool("record_app_control_feedback", {"request": changed_request}))
            assert second["observed_alignment"] == "unchanged_since_analysis"
            assert second["feedback_id"] != summary["feedback_id"]
            assert calls == ["lab", "lab"]
            listed = payload(await client.call_tool("list_app_control_feedback", {"review_id": review["review_id"]}))
            assert {item["feedback_id"] for item in listed["items"]} == {
                summary["feedback_id"], second["feedback_id"]}
            found = payload(await client.call_tool("get_app_control_feedback", {"feedback_id": second["feedback_id"]}))
            assert found["log_url"] == second["log_url"]
            conflicting = {**request, "note": "changed input"}
            bad = await client.call_tool("record_app_control_feedback", {"request": conflicting})
            assert bad.is_error

    with TestClient(app) as web:
        asyncio.run(scenario(web))
        assert web.get("/api/app-control/feedback/missing.json").status_code == 404


def test_feedback_records_read_failure_without_claiming_a_comparison(tmp_path):
    app = create_app(Settings(tmp_path / "app.db"))
    optimizer, _, review, _ = setup_review(app)

    async def capture(_device):
        raise RuntimeError("private transport response")

    optimizer.collector.capture = capture
    from af_app.policy_optimizer import FeedbackRequest
    request = FeedbackRequest(review_id=review["review_id"], submission_key="failed-read-1",
                              engineer_label="engineer-B", change_ticket="CHG-101")
    result = asyncio.run(optimizer.record_feedback(request))
    assert result["status"] == "comparison_unavailable"
    assert result["observed_alignment"] == "unknown"
    log = optimizer.store.get_feedback(result["feedback_id"])
    assert "private transport response" not in json.dumps(log)
    assert "comparison" not in log


def test_new_object_device_metadata_does_not_hide_configuration_differences(tmp_path):
    app = create_app(Settings(tmp_path / "app.db"))
    optimizer, _, review, recommended = setup_review(app, objective="objectize")
    live = copy.deepcopy(recommended)
    live["network_object.list"][-1].update(uuid="device-generated-id", updateTime="now")

    async def capture(_device):
        return snapshot(live)

    optimizer.collector.capture = capture
    from af_app.policy_optimizer import FeedbackRequest
    request = FeedbackRequest(review_id=review["review_id"], submission_key="object-1",
                              engineer_label="engineer-C", change_ticket="CHG-102")
    result = asyncio.run(optimizer.record_feedback(request))
    assert result["observed_alignment"] == "matches_recommendation"
    live["network_object.list"][-1]["refIpGroup"] = ["a"]
    changed = asyncio.run(optimizer.record_feedback(request.model_copy(update={"submission_key": "object-2"})))
    assert changed["observed_alignment"] == "differs_from_recommendation"
    log = optimizer.store.get_feedback(changed["feedback_id"])
    assert any(item["identity"].startswith("AC_GROUP_") and
               any(field["field"] == "refIpGroup" for field in item["field_differences"])
               for item in log["comparison"]["collections"]["network_object.list"])


def test_feedback_separates_selected_alignment_from_unrelated_device_drift(tmp_path):
    app = create_app(Settings(tmp_path / "app.db"))
    optimizer, _, review, recommended = setup_review(app)
    live = copy.deepcopy(recommended)
    live["network_object.list"].append({"name": "unrelated", "businessType": "IP",
                                        "addressType": "IPV4", "ipRanges": [{"start": "192.0.2.1"}]})

    async def capture(_device):
        return snapshot(live)

    optimizer.collector.capture = capture
    from af_app.policy_optimizer import FeedbackRequest
    request = FeedbackRequest(review_id=review["review_id"], submission_key="drift-1",
                              engineer_label="engineer-D", change_ticket="CHG-103")
    result = asyncio.run(optimizer.record_feedback(request))
    assert result["observed_alignment"] == "differs_from_recommendation"
    assert result["candidate_results"][0]["observed_alignment"] == "matches_recommendation"
    log = optimizer.store.get_feedback(result["feedback_id"])
    assert any(item["identity"] == "unrelated" for item in
               log["comparison"]["collections"]["network_object.list"])
