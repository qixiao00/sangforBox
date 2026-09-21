import copy

import pytest

from af_app.policy_semantics import AddressObjects, UnsupportedSemantics, analyze_addresses


def obj(name, start, end=None):
    return {"name": name, "businessType": "IP", "addressType": "IPV4",
            "ipRanges": [{"start": start, **({"end": end} if end else {})}]}


def rule(name, src, dst="all", **extra):
    return {"name": name, "uuid": name, "enable": True, "action": 1,
            "schedule": "all-week", "advanceOption": {"logEnable": False},
            "src": {"srcAddrs": {"srcAddrType": "NETOBJECT", "srcIpGroups": [src]}},
            "dst": {"dstAddrs": {"dstAddrType": "NETOBJECT", "dstIpGroups": [dst]}}, **extra}


def config(rules):
    return {"policy.app_control.list": rules, "network_object.list": [
        obj("a", "10.0.0.0", "10.0.0.127"), obj("b", "10.0.0.128", "10.0.0.255"),
        obj("c", "10.0.0.0", "10.0.0.255"), obj("all", "0.0.0.0", "255.255.255.255")]}


def test_real_address_union_and_multirule_coverage():
    candidates, findings = analyze_addresses(config([rule("r1", "a"), rule("r2", "b"), rule("r3", "c")]),
                                            {"shadowing", "merge"})
    r3 = next(f for f in findings if f["policy_id"] == "r3")
    assert r3["status"] == "covered_by_equal_context"
    assert r3["blockers"] == ["r1", "r2"]
    merged = candidates[0]
    assert merged["after"][0]["src"]["srcAddrs"]["srcIpGroups"] == ["a", "b"]
    assert merged["proof"]["status"] == "unknown"  # no unsupported global claim


def test_no_multidimensional_merge_and_preserve_unknown_fields():
    candidates, _ = analyze_addresses(config([rule("r1", "a", "a"), rule("r2", "b", "b")]), {"merge"})
    assert not candidates
    candidates, _ = analyze_addresses(config([rule("r1", "a", undocumented=True),
                                             rule("r2", "b", undocumented=False)]), {"merge"})
    assert not candidates


def test_protected_and_disabled_rules_not_merged():
    candidates, _ = analyze_addresses(config([rule("r1", "a"), rule("r2", "b")]), {"merge"}, ["r1"])
    assert not candidates
    candidates, _ = analyze_addresses(config([rule("r1", "a", enable=False), rule("r2", "b")]), {"merge"})
    assert not candidates


def test_missing_dynamic_and_empty_objects_never_universal():
    records = [obj("empty", "10.0.0.1"), obj("dynamic", "10.0.0.1")]
    records[0]["ipRanges"] = []
    records[1]["businessType"] = "BUSINESS"
    index = AddressObjects(records)
    for key in ("All", "empty", "dynamic"):
        with pytest.raises(UnsupportedSemantics):
            index.resolve(key)


def test_nested_group_cycles_and_families():
    a = {"name": "a", "businessType": "ADDRGROUP", "addressType": "IPV4", "refIpGroup": ["b"]}
    b = {**a, "name": "b", "refIpGroup": ["a"]}
    with pytest.raises(UnsupportedSemantics, match="Cyclic"):
        AddressObjects([a, b]).resolve("a")
    b = obj("b", "::1")
    b["addressType"] = "IPV6"
    with pytest.raises(UnsupportedSemantics, match="Cross-family"):
        AddressObjects([a, b]).resolve("a")


def test_log_and_schedule_difference_prevents_equal_context_analysis():
    a, b = rule("r1", "a"), rule("r2", "b")
    for field, value in (("schedule", "office-hours"), ("advanceOption", {"logEnable": True})):
        altered = copy.deepcopy(b)
        altered[field] = value
        candidates, _ = analyze_addresses(config([a, altered]), {"merge"})
        assert not candidates


def test_objectize_preserves_exact_members_and_outputs_complete_changes():
    a, b = rule("r1", "a"), rule("r2", "b")
    for r in (a, b):
        r["src"]["srcAddrs"]["srcIpGroups"] = ["a", "b"]
    candidates, _ = analyze_addresses(config([a, b]), {"objectize"})
    assert len(candidates) == 1
    after = candidates[0]["after"]
    assert after["new_objects"][0]["refIpGroup"] == ["a", "b"]
    assert len(after["policies"]) == 2
    assert candidates[0]["proof"]["status"] == "unknown"
    assert a["src"]["srcAddrs"]["srcIpGroups"] == ["a", "b"]
