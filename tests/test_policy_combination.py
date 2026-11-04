import copy

import pytest

from af_app.policy_combination import materialize, verify_combination
from af_app.policy_optimizer import OptimizationRequest, propose
from af_app.policy_review import ReviewStore


def config():
    objects = [{"name": n, "businessType": "IP", "addressType": "IPV4", "ipRanges": [{"start": ip}]}
               for n, ip in (("a", "10.0.0.1"), ("b", "10.0.0.2"))]
    rules = []
    for i, name in enumerate(("a", "b")):
        rules.append({"uuid": name, "name": name, "position": i, "enable": True,
                      "action": 1, "schedule": "all-week",
                      "advanceOption": {"logEnable": False, "sessionDestroyLogEnable": False},
                      "src": {"srcAddrs": {"srcAddrType": "NETOBJECT", "srcIpGroups": [name]}},
                      "dst": {"dstAddrs": {"dstAddrType": "NETOBJECT", "dstIpGroups": ["a", "b"]}}})
    return {"policy.app_control.list": rules, "network_object.list": objects, "policy.app_control.group.list": []}


def test_review_materialization_and_global_model_proof(tmp_path):
    snapshot = {"configuration": config()}
    candidates, findings = propose(snapshot, OptimizationRequest(device_id="d", objectives=["merge"]))
    store = ReviewStore(tmp_path / "r.db")
    analysis = store.create("d", snapshot, candidates, findings)
    review = store.submit(analysis["id"], analysis["proposal_hash"],
                          [{"candidate_id": candidates[0]["id"], "decision": "采纳"}], "key")
    assert store.get_review(review["review_id"]) == review
    after = materialize(analysis, review)
    assert len(after["policy.app_control.list"]) == 1
    assert len(snapshot["configuration"]["policy.app_control.list"]) == 2
    proof = verify_combination(snapshot["configuration"], after)
    assert proof["status"] == "model_equivalent"
    assert not proof["device_qualified"]


def test_crossing_conflicting_rule_fails_proof():
    before = config()
    middle = copy.deepcopy(before["policy.app_control.list"][1])
    middle.update(uuid="middle", name="middle", action=0)
    before["policy.app_control.list"].insert(1, middle)
    after = copy.deepcopy(before)
    after["policy.app_control.list"][0]["src"]["srcAddrs"]["srcIpGroups"] = ["a", "b"]
    after["policy.app_control.list"].pop()
    proof = verify_combination(before, after)
    assert proof["status"] == "unknown"
    assert "abstract_counterexample" in proof


def test_log_identity_is_observable_and_missing_fields_block_proof():
    before = config()
    for rule in before["policy.app_control.list"]:
        rule["advanceOption"]["logEnable"] = True
    after = copy.deepcopy(before)
    after["policy.app_control.list"][0]["name"] = "new"
    assert verify_combination(before, after)["status"] == "unknown"
    before["policy.app_control.list"][0]["advanceOption"].pop("logEnable")
    assert verify_combination(before, before)["status"] == "unknown"


def test_objectization_replays_exact_references(tmp_path):
    snapshot = {"configuration": config()}
    candidates, findings = propose(snapshot, OptimizationRequest(device_id="d", objectives=["objectize"]))
    store = ReviewStore(tmp_path / "r.db")
    analysis = store.create("d", snapshot, candidates, findings)
    review = store.submit(analysis["id"], analysis["proposal_hash"],
                          [{"candidate_id": candidates[0]["id"], "decision": "采纳"}], "key")
    after = materialize(analysis, review)
    assert len(after["network_object.list"]) == 3
    assert verify_combination(snapshot["configuration"], after)["status"] == "model_equivalent"
    review["snapshot_hash"] = "stale"
    with pytest.raises(ValueError, match="snapshot"):
        materialize(analysis, review)


def test_order_repair_is_reviewed_as_behavior_change(tmp_path):
    baseline = config()
    baseline["policy.app_control.list"][0]["src"]["srcAddrs"]["srcIpGroups"] = ["a", "b"]
    baseline["policy.app_control.list"][0]["action"] = 0
    snapshot = {"configuration": baseline}
    candidates, findings = propose(snapshot, OptimizationRequest(device_id="d", objectives=["order"]))
    assert len(candidates) == 1
    assert candidates[0]["change_class"] == "behavior_change"
    assert candidates[0]["proof"]["crossed_policy_ids"] == ["a"]
    store = ReviewStore(tmp_path / "r.db")
    analysis = store.create("d", snapshot, candidates, findings)
    review = store.submit(analysis["id"], analysis["proposal_hash"],
                          [{"candidate_id": candidates[0]["id"], "decision": "采纳"}], "key")
    after = materialize(analysis, review)
    assert [r["uuid"] for r in after["policy.app_control.list"]] == ["b", "a"]
    assert verify_combination(baseline, after)["status"] == "unknown"
    protected, _ = propose(snapshot, OptimizationRequest(device_id="d", objectives=["order"], protected_policy_ids=["a"]))
    assert not protected
