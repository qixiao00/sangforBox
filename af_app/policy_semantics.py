"""Conservative AF network-object semantics and address-space analysis.

Only static IP/ADDRGROUP objects are interpreted. Other predicates must be
byte-for-byte structurally equal before address-space relationships are used.
This does not assert device-wide equivalence for dynamic application semantics.
"""
import copy
import json

from af_app.policy_review import canonical, fingerprint
from af_app.policy_sets import Cube, Intervals, addresses, subtract_all, ComplexityLimit


class UnsupportedSemantics(ValueError):
    pass


class AddressObjects:
    def __init__(self, records):
        self.index = {}
        self.cache = {}
        for record in records:
            for key in (record.get("uuid"), record.get("name")):
                if key:
                    if key in self.index and self.index[key] is not record:
                        raise UnsupportedSemantics(f"Ambiguous object identity: {key}")
                    self.index[key] = record

    def resolve(self, key, stack=()):
        if key in self.cache:
            return self.cache[key]
        record = self.index.get(key)
        if record is None:
            raise UnsupportedSemantics(f"Missing referenced object: {key}")
        identity = record.get("uuid") or record["name"]
        if identity in stack:
            raise UnsupportedSemantics(f"Cyclic address group: {identity}")
        kind, family = record.get("businessType"), record.get("addressType")
        if kind not in {"IP", "ADDRGROUP"} or family not in {"IPV4", "IPV6"}:
            raise UnsupportedSemantics(f"Unsupported object semantics: {identity} ({kind}/{family})")
        result = Intervals(())
        if kind == "ADDRGROUP":
            refs = record.get("refIpGroup")
            if not isinstance(refs, list) or not refs or record.get("ipRanges"):
                raise UnsupportedSemantics(f"Empty or ambiguous group: {identity}")
            for ref in refs:
                if not isinstance(ref, str):
                    raise UnsupportedSemantics("Invalid address reference")
                child = self.index.get(ref)
                if child and child.get("addressType") != family:
                    raise UnsupportedSemantics("Cross-family address group")
                result = result.union(self.resolve(ref, (*stack, identity)))
        else:
            ranges = record.get("ipRanges")
            if not isinstance(ranges, list) or not ranges or record.get("refIpGroup"):
                raise UnsupportedSemantics(f"Empty or ambiguous IP object: {identity}")
            for item in ranges:
                if not isinstance(item, dict) or set(item) - {"start", "end", "bits"}:
                    raise UnsupportedSemantics("Unsupported IP range fields")
                start = item.get("start")
                if not isinstance(start, str) or (":" in start) != (family == "IPV6"):
                    raise UnsupportedSemantics("Missing start address or address-family mismatch")
                end, bits = item.get("end"), item.get("bits")
                if end and bits is not None:
                    # Do not guess precedence of two range representations.
                    raise UnsupportedSemantics("Both range end and prefix supplied")
                if bits is not None and (type(bits) is not int or not 0 <= bits <= (32 if family == "IPV4" else 128)):
                    raise UnsupportedSemantics("Invalid prefix length")
                literal = f"{start}-{end}" if end else f"{start}/{bits}" if bits is not None else start
                try:
                    result = result.union(addresses([literal]))
                except ValueError as exc:
                    raise UnsupportedSemantics(f"Invalid IP range in {identity}") from exc
        self.cache[key] = result
        return result

    def collection(self, refs):
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r for r in refs):
            raise UnsupportedSemantics("Empty or invalid address selector")
        result = Intervals(())
        for ref in refs:
            result = result.union(self.resolve(ref))
        return result


def address_projection(rule, objects):
    try:
        src, dst = rule["src"], rule["dst"]
        sa, da = src["srcAddrs"], dst["dstAddrs"]
        if sa["srcAddrType"] != "NETOBJECT" or da["dstAddrType"] != "NETOBJECT":
            raise UnsupportedSemantics("Only NETOBJECT selectors support static address analysis")
        if set(sa) - {"srcAddrType", "srcIpGroups"} or set(da) - {"dstAddrType", "dstIpGroups"}:
            raise UnsupportedSemantics("Additional address conditions require adapter support")
        for key in ("schedule", "enable", "action", "advanceOption"):
            if key not in rule:
                raise UnsupportedSemantics(f"Missing policy field: {key}")
        if type(rule["enable"]) is not bool:
            raise UnsupportedSemantics("Unknown enable state")
        source = objects.collection(sa["srcIpGroups"])
        destination = objects.collection(da["dstIpGroups"])
        # Entire non-address configuration must match. This deliberately keeps
        # unknown fields and action/log settings, rather than ignoring them.
        context = copy.deepcopy(rule)
        context["src"]["srcAddrs"]["srcIpGroups"] = []
        context["dst"]["dstAddrs"]["dstIpGroups"] = []
        for key in ("uuid", "name", "position", "description", "updateTime", "durationTime",
                    "hits", "lastHitTime", "showname"):
            context.pop(key, None)
        return Cube((source, destination)), canonical(context)
    except (KeyError, TypeError) as exc:
        raise UnsupportedSemantics("Missing or unsupported source/destination structure") from exc


def analyze_addresses(config, objectives, protected=()):
    findings, candidates, previous = [], [], []
    try:
        objects = AddressObjects(config["network_object.list"])
    except UnsupportedSemantics as exc:
        return [], [{"kind": "unknown_objects", "message": str(exc)}]
    rules = config["policy.app_control.list"]
    for raw_index, raw in enumerate(rules):
        identity = raw.get("uuid") or raw["name"]
        if raw.get("enable") is False:
            continue
        try:
            cube, context = address_projection(raw, objects)
        except UnsupportedSemantics as exc:
            findings.append({"policy_id": identity, "kind": "unsupported_semantics", "message": str(exc)})
            previous.append((raw, None, None))
            continue
        comparable = [(p, c) for p, c, ctx in previous if ctx == context]
        if "order" in objectives and identity not in protected and not raw.get("isdefault"):
            current_context = json.loads(context)
            current_context.pop("action", None)
            blockers = []
            for p, c, ctx in previous:
                if ctx is None or p.get("action") == raw.get("action"):
                    continue
                other_context = json.loads(ctx)
                other_context.pop("action", None)
                if current_context == other_context and c.intersection(cube):
                    blockers.append(p)
            if blockers:
                anchor = blockers[0]
                anchor_index = next(i for i, r in enumerate(rules) if r is anchor)
                span = rules[anchor_index:raw_index + 1]
                ids = [r.get("uuid") or r["name"] for r in span]
                if set(ids) & set(protected) or any(r.get("isdefault") for r in span):
                    findings.append({"policy_id": identity, "kind": "order_protected",
                                     "message": "调整会跨越受保护或默认策略，仅报告冲突，不生成移动方案"})
                else:
                    candidates.append({"id": "order-" + fingerprint(ids)[:16],
                                       "kind": "顺序意图修复", "change_class": "behavior_change",
                                       "policies": ids, "before": copy.deepcopy(span),
                                       "after": copy.deepcopy([raw, *span[:-1]]),
                                       "reason": "前序存在同条件不同动作的地址重叠；提议将该策略置于首个相关规则之前，需确认业务优先级",
                                       "proof": {"status": "unknown", "not_equivalent_optimization": True,
                                                 "reason": "该移动旨在改变优先级，需整表影响分析和用户明确确认",
                                                 "from_index": raw_index + 1, "to_index": anchor_index + 1,
                                                 "crossed_policy_ids": ids[:-1],
                                                 "blocking_policy_ids": [p.get("uuid") or p["name"] for p in blockers]},
                                       "dependencies": [], "conflicts": []})
        if "shadowing" in objectives:
            try:
                remaining = subtract_all([cube], [c for _, c in comparable])
                blockers = [p.get("uuid") or p["name"] for p, c in comparable if c.intersection(cube)]
                findings.append({"policy_id": identity, "kind": "address_coverage",
                                 "status": "covered_by_equal_context" if not remaining else "partially_covered" if blockers else "no_equal_context_coverage",
                                 "blockers": blockers, "remaining_address_regions": [
                                     [d.spans for d in c.dimensions] for c in remaining],
                                 "scope": "相同非地址配置的前序规则；跨条件关系尚未完成分析，不代表策略整体正常"})
            except ComplexityLimit as exc:
                findings.append({"policy_id": identity, "kind": "complexity_limit", "message": str(exc)})
        if "merge" in objectives and identity not in protected and not raw.get("isdefault"):
            for prior, pcube, pcontext in previous:
                pid = prior.get("uuid") or prior["name"]
                if pcontext != context or pid in protected or prior.get("isdefault"):
                    continue
                differences = [i for i in range(2) if pcube.dimensions[i] != cube.dimensions[i]]
                if len(differences) != 1:
                    continue
                dim = differences[0]
                section, addr, field = ("src", "srcAddrs", "srcIpGroups") if dim == 0 else ("dst", "dstAddrs", "dstIpGroups")
                merged = copy.deepcopy(prior)
                merged[section][addr][field] = list(dict.fromkeys(prior[section][addr][field] + raw[section][addr][field]))
                candidates.append({"id": "merge-" + fingerprint([pid, identity])[:16], "kind": "单维地址合并",
                                   "policies": [pid, identity], "before": [prior, raw], "after": [merged],
                                   "reason": "非地址配置完全相同，仅一个地址维度不同；保持精确地址并集",
                                   "proof": {"status": "unknown", "address_union": "exact",
                                             "reason": "尚需整表跨策略关系、日志身份与执行中间状态验证"},
                                   "dependencies": [], "conflicts": []})
        previous.append((raw, cube, context))
    if "objectize" in objectives:
        repeated = {}
        for raw in rules:
            identity = raw.get("uuid") or raw["name"]
            if identity in protected or raw.get("isdefault"):
                continue
            for section, addr, field in (("src", "srcAddrs", "srcIpGroups"), ("dst", "dstAddrs", "dstIpGroups")):
                try:
                    selector = raw[section][addr]
                    type_key = "srcAddrType" if section == "src" else "dstAddrType"
                    if selector.get(type_key) != "NETOBJECT" or set(selector) - {type_key, field}:
                        continue
                    refs = selector[field]
                    if not isinstance(refs, list) or len(set(refs)) < 2:
                        continue
                    resolved = objects.collection(refs)
                    families = {objects.index[r]["addressType"] for r in refs}
                    if len(families) != 1:
                        continue
                    key = (tuple(sorted(set(refs))), next(iter(families)))
                    repeated.setdefault(key, []).append((raw, section, addr, field, resolved))
                except (KeyError, TypeError, UnsupportedSemantics):
                    continue
        for (refs, family), occurrences in repeated.items():
            if len(occurrences) < 2:
                continue
            stable_id = fingerprint({"refs": refs, "family": family})[:16]
            name = "AC_GROUP_" + stable_id
            if name in objects.index:
                findings.append({"kind": "object_name_conflict", "name": name,
                                 "message": "建议对象名称已存在，需生成新版本处理同名冲突"})
                continue
            before, after = {}, {}
            for raw, section, addr, field, _ in occurrences:
                identity = raw.get("uuid") or raw["name"]
                before[identity] = raw
                updated = after.setdefault(identity, copy.deepcopy(raw))
                updated[section][addr][field] = [name]
            proposed_object = {"name": name, "businessType": "ADDRGROUP", "addressType": family,
                               "refIpGroup": list(refs), "description": "应用控制重复地址集合"}
            candidates.append({"id": "objectize-" + stable_id, "kind": "重复地址集合对象化",
                               "policies": list(before), "before": {"policies": list(before.values())},
                               "after": {"new_objects": [proposed_object], "policies": list(after.values())},
                               "reason": "同一精确地址引用集合重复出现，提议统一为地址组；保留原对象",
                               "proof": {"status": "unknown", "address_union": "exact",
                                         "reason": "需验证目标版本地址组引用、对象创建与策略更新的中间状态"},
                               "dependencies": [], "conflicts": []})
    # Bind candidates that operate on a common rule as explicit alternatives.
    for c in candidates:
        c["conflicts"] = [o["id"] for o in candidates if o is not c and set(c["policies"]) & set(o["policies"])]
    return candidates, findings
