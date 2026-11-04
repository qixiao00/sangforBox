import asyncio
import copy

from af_app.app import create_app
from af_app.config import Settings
from af_app.policy_optimizer import OptimizationRequest, propose
from af_app.policy_review import fingerprint

from test_policy_combination import config


def snapshot(configuration, *, device="lab", namespace="public"):
    data = copy.deepcopy(configuration)
    return {"device_id": device, "namespace": namespace, "configuration": data,
            "configuration_hash": fingerprint({"device_id": device, "namespace": namespace,
                                               "configuration": data})}


def reviewed_optimizer(tmp_path):
    optimizer = create_app(Settings(tmp_path / "app.db")).state.policy_optimizer
    base = snapshot(config())
    candidates, findings = propose(base, OptimizationRequest(device_id="lab", objectives=["merge"]))
    analysis = optimizer.store.create("lab", base, candidates, findings)
    review = optimizer.store.submit(analysis["id"], analysis["proposal_hash"],
                                    [{"candidate_id": candidates[0]["id"], "decision": "采纳"}], "approval-1")
    return optimizer, base, review


def test_read_only_preflight_reports_device_qualification_blockers(tmp_path):
    optimizer, base, review = reviewed_optimizer(tmp_path)
    reads = []

    async def capture(device):
        reads.append(device)
        return copy.deepcopy(base)

    optimizer.collector.capture = capture
    result = asyncio.run(optimizer.preflight_review(review["review_id"]))
    assert reads == ["lab"]
    assert result["snapshot_unchanged"]
    assert result["model_proof"]["status"] == "model_equivalent"
    assert result["device_writes"] == 0
    assert not result["execution_ready"] and not result["execution_authorized"]
    assert any("设备版本" in item for item in result["blockers"])


def test_read_only_preflight_detects_configuration_drift(tmp_path):
    optimizer, base, review = reviewed_optimizer(tmp_path)
    changed = copy.deepcopy(base["configuration"])
    changed["policy.app_control.list"][0]["action"] = 0

    async def capture(_device):
        return snapshot(changed)

    optimizer.collector.capture = capture
    result = asyncio.run(optimizer.preflight_review(review["review_id"]))
    assert not result["snapshot_unchanged"]
    assert result["next_action"] == "reanalyze"
    assert any("重新分析" in item for item in result["blockers"])
