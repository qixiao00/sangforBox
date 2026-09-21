"""Materialize reviewed proposals and prove a conservative first-match model.

Opaque non-address predicates are independent Boolean variables, so equality
over every valuation is a sufficient model proof. A counterexample may be
infeasible on a device and therefore returns unknown, never a claimed traffic
incident. Device adapter qualification remains a separate execution gate.
"""
import copy

from af_app.policy_review import canonical, fingerprint
from af_app.policy_semantics import AddressObjects, address_projection, UnsupportedSemantics
from af_app.policy_sets import ALL_IP, Cube, Intervals, Rule, prove_equivalent


POLICIES = "policy.app_control.list"
OBJECTS = "network_object.list"


def identity(rule):
    return rule.get("uuid") or rule["name"]


def materialize(analysis, review):
    if review["analysis_id"] != analysis["id"] or review["proposal_hash"] != analysis["proposal_hash"]:
        raise ValueError("Review does not match analysis version")
    if review["snapshot_hash"] != analysis["snapshot_hash"]:
        raise ValueError("Review does not match snapshot")
    selected = set(review["selected_ids"])
    candidates = {c["id"]: c for c in analysis["candidates"]}
    if selected - set(candidates):
        raise ValueError("Review contains unknown candidates")
    config = copy.deepcopy(analysis["snapshot"]["configuration"])
    originals = {identity(r): r for r in config[POLICIES]}
    if len(originals) != len(config[POLICIES]):
        raise ValueError("Duplicate policy identity")
    replacements, removed, new_objects, reorders = {}, set(), [], []
    for cid in sorted(selected):
        candidate = candidates[cid]
        if set(candidate.get("dependencies", [])) - selected or set(candidate.get("conflicts", [])) & selected:
            raise ValueError("Invalid candidate combination")
        before, after = candidate["before"], candidate["after"]
        kind = candidate["kind"]
        if kind == "命名规范化":
            before, after = [before], [after]
        elif kind == "单维地址合并":
            pass
        elif kind == "顺序意图修复":
            before_ids, after_ids = [identity(r) for r in before], [identity(r) for r in after]
            if len(after_ids) != len(before_ids) or set(after_ids) != set(before_ids):
                raise ValueError("Reorder must preserve all policies in its span")
            if any(originals.get(identity(r)) != r for r in after):
                raise ValueError("Reorder must not change policy content")
            reorders.append((before_ids, after_ids))
        elif kind == "重复地址集合对象化":
            new_objects.extend(after["new_objects"])
            before, after = before["policies"], after["policies"]
        else:
            raise ValueError(f"Unsupported candidate transformation: {kind}")
        source_ids = {identity(r) for r in before}
        if source_ids != set(candidate["policies"]):
            raise ValueError("Candidate policy references do not match its content")
        for rule in before:
            rid = identity(rule)
            if originals.get(rid) != rule or rid in replacements or rid in removed:
                raise ValueError("Stale or overlapping candidate transformation")
        targets = {identity(r): r for r in after}
        if len(targets) != len(after) or set(targets) - source_ids:
            raise ValueError("Candidate changed stable policy identity")
        replacements.update(targets)
        removed.update(source_ids - set(targets))
    config[POLICIES] = [copy.deepcopy(replacements.get(identity(r), r)) for r in config[POLICIES]
                        if identity(r) not in removed]
    for before_ids, after_ids in reorders:
        order = [identity(r) for r in config[POLICIES]]
        start = order.index(before_ids[0])
        if order[start:start + len(before_ids)] != before_ids:
            raise ValueError("Reorder span changed")
        by_id = {identity(r): r for r in config[POLICIES]}
        config[POLICIES][start:start + len(before_ids)] = [by_id[rid] for rid in after_ids]
    names = [r["name"] for r in config[POLICIES]]
    if len(names) != len(set(names)):
        raise ValueError("Resulting policy names collide")
    occupied = {r["name"] for r in config[OBJECTS]}
    for obj in new_objects:
        if obj["name"] in occupied:
            raise ValueError("Resulting object names collide")
        occupied.add(obj["name"])
        config[OBJECTS].append(copy.deepcopy(obj))
    # Array order is authoritative here; stale numeric positions are retained as
    # provenance. The future execution adapter must compute actual move calls.
    return config


KNOWN_FIELDS = {"uuid", "name", "position", "description", "updateTime", "durationTime",
                "hits", "lastHitTime", "showname", "enable", "action", "advanceOption",
                "profile", "src", "dst", "schedule", "group", "labels", "isdefault", "reason"}


def _project(config):
    objects = AddressObjects(config[OBJECTS])
    projected = []
    for raw in config[POLICIES]:
        if raw.get("enable") is False:
            continue
        if set(raw) - KNOWN_FIELDS:
            raise UnsupportedSemantics("Unknown policy fields prevent complete model comparison")
        cube, _ = address_projection(raw, objects)
        advance = raw["advanceOption"]
        if not isinstance(advance, dict):
            raise UnsupportedSemantics("Invalid advanced options")
        # Explicit logging booleans are required; absence is not treated as off.
        if any(type(advance.get(k)) is not bool for k in ("logEnable", "sessionDestroyLogEnable")):
            raise UnsupportedSemantics("Logging semantics are missing")
        if type(raw["action"]) is not int or raw["action"] not in (0, 1):
            raise UnsupportedSemantics("Unknown policy action")
        predicate = {"src": copy.deepcopy(raw["src"]), "dst": copy.deepcopy(raw["dst"]),
                     "schedule": raw["schedule"], "group": raw.get("group"),
                     "labels": raw.get("labels"), "isdefault": raw.get("isdefault")}
        predicate["src"]["srcAddrs"]["srcIpGroups"] = []
        predicate["dst"]["dstAddrs"]["dstIpGroups"] = []
        effect = {"action": raw["action"], "advanceOption": advance,
                  "profile": raw.get("profile"), "reason": raw.get("reason")}
        if advance["logEnable"] or advance["sessionDestroyLogEnable"]:
            effect["log_identity"] = {"uuid": raw.get("uuid"), "name": raw["name"]}
        projected.append((identity(raw), cube, canonical(predicate), (canonical(effect),)))
    return projected


def verify_combination(before, after, limit=10000):
    try:
        left, right = _project(before), _project(after)
        predicates = sorted({r[2] for r in left + right})
        if len(predicates) > 64:
            return {"status": "unknown", "reason": "Opaque predicate budget exceeded"}
        boolean = Intervals(((0, 1),))
        true = Intervals(((1, 1),))
        universe = Cube((ALL_IP, ALL_IP, *[boolean for _ in predicates]))

        def rules(rows):
            return [Rule(rid, Cube((*cube.dimensions, *[true if p == predicate else boolean for p in predicates])), effect)
                    for rid, cube, predicate, effect in rows]

        proof = prove_equivalent(rules(left), rules(right), universe, ("unchanged-device-default",), limit=limit)
        if proof["status"] == "different":
            return {"status": "unknown", "reason": "Potential behavior difference in conservative model",
                    "abstract_counterexample": proof, "concrete_traffic_verified": False}
        if proof["status"] == "equivalent":
            return {"status": "model_equivalent", "scope": "所有独立非地址谓词取值下的首条匹配模型",
                    "premises": ["静态地址对象解释正确", "非地址谓词在前后保持同一含义",
                                 "设备采用首条匹配且默认行为不变", "模型所列效果覆盖设备实际执行效果"],
                    "device_qualified": False, "execution_authorized": False}
        return proof
    except (UnsupportedSemantics, KeyError, TypeError, ValueError) as exc:
        return {"status": "unknown", "reason": str(exc)}
