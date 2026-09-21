"""Compare an immutable reviewed proposal with a later read-only device capture.

This is structural feedback about observed configuration, not attribution of an
engineer's actions and not a proof that traffic behavior is equivalent.
"""
from af_app.policy_combination import OBJECTS, POLICIES, identity
from af_app.policy_review import fingerprint


GROUPS = "policy.app_control.group.list"
COLLECTIONS = (POLICIES, OBJECTS, GROUPS)
POLICY_METADATA = {"position", "hits", "lastHitTime", "updateTime", "durationTime", "showname"}
OBJECT_METADATA = {"uuid", "updateTime", "durationTime", "__creator"}
GROUP_METADATA = {"updateTime", "durationTime", "__creator"}


def _key(row, collection):
    # Network-object references are by name, including newly proposed groups
    # which have no device UUID yet. Policies keep their stable UUID on rename.
    value = row.get("name") if collection == OBJECTS else identity(row)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing stable identity in {collection}")
    return value


def _index(config, collection):
    rows = config.get(collection)
    if not isinstance(rows, list):
        raise ValueError(f"Missing configuration collection: {collection}")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"Invalid record in {collection}")
        key = _key(row, collection)
        if key in result:
            raise ValueError(f"Ambiguous resource identity in {collection}: {key}")
        result[key] = row
    return result


def _view(row, collection):
    if row is None:
        return None
    omitted = POLICY_METADATA if collection == POLICIES else OBJECT_METADATA if collection == OBJECTS else GROUP_METADATA
    return {key: value for key, value in row.items() if key not in omitted}


def _field_differences(recommended, observed, prefix=""):
    if recommended == observed:
        return []
    if isinstance(recommended, dict) and isinstance(observed, dict):
        changes = []
        for key in sorted(set(recommended) | set(observed)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in recommended or key not in observed:
                changes.append({"field": path,
                                "recommended_present": key in recommended,
                                "recommended": recommended.get(key),
                                "observed_present": key in observed,
                                "observed": observed.get(key)})
            else:
                changes.extend(_field_differences(recommended[key], observed[key], path))
        return changes
    return [{"field": prefix or "$record", "recommended_present": recommended is not None,
             "recommended": recommended, "observed_present": observed is not None,
             "observed": observed}]


def compare_configurations(analysis, review, recommended, observed):
    """Return complete changed-record evidence plus per-selected-candidate status."""
    baseline = analysis["snapshot"]["configuration"]
    current = observed["configuration"]
    indexed = {collection: (_index(baseline, collection), _index(recommended, collection),
                             _index(current, collection)) for collection in COLLECTIONS}
    collections = {}
    proposal_differences = baseline_differences = 0
    for collection, (before, proposed, actual) in indexed.items():
        changes = []
        for key in sorted(set(before) | set(proposed) | set(actual)):
            old, wanted, live = (_view(rows.get(key), collection) for rows in (before, proposed, actual))
            if old == wanted == live:
                continue
            proposal_differences += wanted != live
            baseline_differences += old != live
            changes.append({"identity": key, "baseline": old, "recommended": wanted,
                            "observed": live,
                            "recommended_vs_observed": "match" if wanted == live else "different",
                            "baseline_vs_observed": "match" if old == live else "different",
                            "field_differences": _field_differences(wanted, live)})
        collections[collection] = changes

    before_order = [identity(row) for row in baseline[POLICIES]]
    proposed_order = [identity(row) for row in recommended[POLICIES]]
    actual_order = [identity(row) for row in current[POLICIES]]
    order = {"baseline": before_order, "recommended": proposed_order, "observed": actual_order,
             "recommended_vs_observed": "match" if proposed_order == actual_order else "different",
             "baseline_vs_observed": "match" if before_order == actual_order else "different"}

    by_candidate = {candidate["id"]: candidate for candidate in analysis["candidates"]}
    decisions = {item["candidate_id"]: item["decision"] for item in review["decisions"]}
    candidate_results = []
    for candidate_id in sorted(by_candidate):
        candidate = by_candidate[candidate_id]
        selected = candidate_id in review["selected_ids"]
        result = {"candidate_id": candidate_id, "kind": candidate["kind"],
                  "review_decision": decisions[candidate_id], "selected": selected,
                  "policy_ids": candidate["policies"]}
        if not selected:
            result["observed_alignment"] = "not_selected"
            candidate_results.append(result)
            continue
        checks = []
        policy_maps = indexed[POLICIES]
        for key in candidate["policies"]:
            views = [_view(rows.get(key), POLICIES) for rows in policy_maps]
            checks.append((key, POLICIES, views))
        new_objects = candidate.get("after", {}).get("new_objects", []) if candidate["kind"] == "重复地址集合对象化" else []
        for obj in new_objects:
            key = obj["name"]
            views = [_view(rows.get(key), OBJECTS) for rows in indexed[OBJECTS]]
            checks.append((key, OBJECTS, views))
        needs_order = candidate["kind"] in {"单维地址合并", "顺序意图修复"}
        matches_proposal = all(values[1] == values[2] for _, _, values in checks)
        matches_baseline = all(values[0] == values[2] for _, _, values in checks)
        if needs_order:
            matches_proposal &= proposed_order == actual_order
            matches_baseline &= before_order == actual_order
        result["observed_alignment"] = ("matches_recommendation" if matches_proposal else
                                        "unchanged_since_analysis" if matches_baseline else
                                        "differs_from_recommendation")
        result["checked_resources"] = [{"identity": key, "collection": collection,
                                        "recommended_vs_observed": "match" if values[1] == values[2] else "different"}
                                       for key, collection, values in checks]
        result["requires_full_order_match"] = needs_order
        candidate_results.append(result)

    matches_proposal = proposal_differences == 0 and proposed_order == actual_order
    matches_baseline = baseline_differences == 0 and before_order == actual_order
    return {"status": "matches_recommendation" if matches_proposal else
            "unchanged_since_analysis" if matches_baseline else "differs_from_recommendation",
            "candidate_results": candidate_results, "collections": collections, "policy_order": order,
            "counts": {"selected_candidates": len(review["selected_ids"]),
                       "recommended_vs_observed_changed_records": proposal_differences,
                       "baseline_vs_observed_changed_records": baseline_differences,
                       "candidate_matches": sum(item["observed_alignment"] == "matches_recommendation"
                                                for item in candidate_results),
                       "candidate_divergences": sum(item["observed_alignment"] == "differs_from_recommendation"
                                                  for item in candidate_results)},
            "comparison_hash": fingerprint({"collections": collections, "policy_order": order})}
