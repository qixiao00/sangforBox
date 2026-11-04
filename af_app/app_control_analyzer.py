#!/usr/bin/env python3
"""Offline application-control policy inventory and optimization candidates.

The tool intentionally stops at analysis and review-file generation.  Device
read/write adapters are not implemented until the project's existing toolchain
is identified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional


ANY = "__ANY__"
UNKNOWN = "__UNKNOWN__"

OBJECT_COLUMNS = {
    "name": "网络对象名称",
    "description": "描述（可留空）",
    "ranges": "IP范围（业务类型只支持多个单IP，用户组和IP组支持单IP、IP范围和网段，地址组不支持该功能，多个之间用“,”隔开，地址组可留空）",
    "kind": "网络对象类型（可留空）",
    "refs": "引用的网络对象名称（业务，IP组，用户组不支持该功能，多个之间用“,”隔开，可留空）",
    "protocol": "网络对象IP协议类型（可为IPV4或IPV6，地址组必填，其他类型可留空）",
}

POLICY_MATCH_COLUMNS = {
    "src_zone": "源区域",
    "src_object": "源网络对象",
    "src_mac": "源MAC",
    "user_group": "用户组",
    "user": "用户",
    "dst_zone": "目的区域",
    "dst_object": "目的网络对象",
    "dst_domain": "目的域名",
    "dst_mac": "目的MAC",
    "application": "应用",
    "service": "服务",
    "time": "生效时间",
}

POLICY_EFFECT_COLUMNS = {
    "action": "动作（y，n）",
    "ipv6_profile": "IPv6 Profile",
    "start_log": "会话开始时记录（y，n）",
    "end_log": "会话结束时记录（y，n）",
    "enabled": "启用状态（留空或y表示启用，n表示禁用）",
    "long_connection": "开启长连接（0表示不开启，其他表示开启，数值表示长连接时间）",
}


def clean(value: object) -> str:
    """Remove CSV whitespace and one leading Excel text apostrophe."""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) >= 1 and text.startswith("'"):
        text = text[1:]
    return text.strip()


def clean_header(value: object) -> str:
    text = str(value or "").strip()
    text = text.lstrip("#")
    return text.strip()


def split_values(value: str) -> list[str]:
    value = clean(value)
    if not value or value in {"/", "*", "any", "ANY", "全部", "All", "all"}:
        return [ANY]
    return [clean(item) for item in value.split(",") if clean(item)] or [ANY]


def is_any(value: str) -> bool:
    return value == ANY


@dataclass
class NetworkObject:
    name: str
    description: str = ""
    ranges: list[str] = field(default_factory=list)
    kind: str = ""
    refs: list[str] = field(default_factory=list)
    protocol: str = ""
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class Policy:
    position: int
    name: str
    fields: dict[str, str]
    raw: dict[str, str]

    @property
    def enabled(self) -> bool:
        return clean(self.fields.get("enabled", "")).lower() != "n"

    def match_value(self, key: str, objects: "ObjectIndex") -> str | frozenset[str]:
        value = self.fields.get(key, "")
        if key in {"src_object", "dst_object"}:
            return objects.resolve_value(value)
        values = split_values(value)
        return ANY if ANY in values else frozenset(values)

    def effect_signature(self) -> tuple[str, ...]:
        return tuple(clean(self.fields.get(key, "")) for key in POLICY_EFFECT_COLUMNS)


class ObjectIndex:
    def __init__(self, objects: Iterable[NetworkObject]):
        self.objects = {obj.name: obj for obj in objects}
        self._cache: dict[str, str | frozenset[str]] = {}

    def resolve_value(self, value: str) -> str | frozenset[str]:
        values = split_values(value)
        if ANY in values:
            return ANY
        result: set[str] = set()
        for item in values:
            resolved = self.resolve(item)
            if resolved == UNKNOWN:
                return UNKNOWN
            if resolved == ANY:
                return ANY
            result.update(resolved)
        return frozenset(result)

    def resolve(self, name: str, stack: Optional[set[str]] = None) -> str | frozenset[str]:
        name = clean(name)
        if not name:
            return ANY
        if name in self._cache:
            return self._cache[name]
        obj = self.objects.get(name)
        if obj is None:
            literal = parse_literal(name)
            result: str | frozenset[str] = frozenset({literal}) if literal else UNKNOWN
            self._cache[name] = result
            return result
        stack = set() if stack is None else set(stack)
        if name in stack:
            return UNKNOWN
        stack.add(name)
        result_set: set[str] = set(obj.ranges)
        for ref in obj.refs:
            resolved = self.resolve(ref, stack)
            if resolved == UNKNOWN:
                return UNKNOWN
            if resolved == ANY:
                return ANY
            result_set.update(resolved)
        result = frozenset(result_set)
        self._cache[name] = result
        return result


def parse_literal(value: str) -> Optional[str]:
    """Return a normalized address/range literal, or None for an unresolved name."""
    value = clean(value)
    if not value:
        return None
    if "-" in value:
        left, right = value.split("-", 1)
        try:
            first, last = ipaddress.ip_address(left.strip()), ipaddress.ip_address(right.strip())
            if first.version == last.version and int(first) <= int(last):
                return f"{first}-{last}"
        except ValueError:
            return None
    try:
        if "/" in value:
            return str(ipaddress.ip_network(value, strict=False))
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def csv_rows(line: str) -> list[str]:
    return next(csv.reader([line]))


def parse_objects(path: Path) -> list[NetworkObject]:
    lines = read_text(path).splitlines()
    header: Optional[list[str]] = None
    objects: list[NetworkObject] = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#") and not line.lstrip().startswith("#####网络对象名称"):
            if line.lstrip().startswith("#####网络对象名称"):
                header = [clean_header(item) for item in csv_rows(line)]
            continue
        if line.lstrip().startswith("#####网络对象名称"):
            header = [clean_header(item) for item in csv_rows(line)]
            continue
        if header is None:
            continue
        values = csv_rows(line)
        if not values:
            continue
        row = {header[i]: clean(values[i]) if i < len(values) else "" for i in range(len(header))}
        name = row.get(OBJECT_COLUMNS["name"], "")
        if not name:
            continue
        objects.append(
            NetworkObject(
                name=name,
                description=row.get(OBJECT_COLUMNS["description"], ""),
                ranges=[parse_literal(item) or clean(item) for item in split_values(row.get(OBJECT_COLUMNS["ranges"], "")) if item != ANY],
                kind=row.get(OBJECT_COLUMNS["kind"], ""),
                refs=[item for item in split_values(row.get(OBJECT_COLUMNS["refs"], "")) if item != ANY],
                protocol=row.get(OBJECT_COLUMNS["protocol"], ""),
                raw=row,
            )
        )
    return objects


def parse_policies(path: Path) -> list[Policy]:
    lines = read_text(path).splitlines()
    header: Optional[list[str]] = None
    policies: list[Policy] = []
    in_policy_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#名称"):
            header = [clean_header(item) for item in csv_rows(line)]
            in_policy_section = True
            continue
        if not in_policy_section or not stripped or stripped.startswith("#"):
            continue
        values = csv_rows(line)
        row = {header[i]: clean(values[i]) if i < len(values) else "" for i in range(len(header or []))}
        if not row:
            continue
        mapped: dict[str, str] = {}
        for key, label in {**POLICY_MATCH_COLUMNS, **POLICY_EFFECT_COLUMNS}.items():
            mapped[key] = row.get(label, "")
        name = mapped.get("name", "") or row.get("名称", "")
        if not name:
            # The name column is not part of the normalized maps above.
            name = row.get("名称", "")
        policies.append(Policy(position=len(policies) + 1, name=name, fields=mapped, raw=row))
    return policies


def relation(a: str | frozenset[str], b: str | frozenset[str]) -> Optional[bool]:
    """Whether dimension a covers b. None means unknown."""
    if a == UNKNOWN or b == UNKNOWN:
        return None
    if a == ANY:
        return True
    if b == ANY:
        return a == ANY
    return set(b).issubset(set(a))


def intersects(a: str | frozenset[str], b: str | frozenset[str]) -> Optional[bool]:
    if a == UNKNOWN or b == UNKNOWN:
        return None
    if a == ANY or b == ANY:
        return True
    return bool(set(a) & set(b))


def match_signature(policy: Policy, objects: ObjectIndex) -> tuple[object, ...]:
    return tuple(policy.match_value(key, objects) for key in POLICY_MATCH_COLUMNS)


def display_dimension(value: object) -> str:
    if value == ANY:
        return "任意"
    if value == UNKNOWN:
        return "未知"
    if isinstance(value, frozenset):
        return ",".join(sorted(value))
    return str(value)


def compare_policies(prior: Policy, later: Policy, objects: ObjectIndex) -> dict[str, object]:
    a = match_signature(prior, objects)
    b = match_signature(later, objects)
    coverage = [relation(x, y) for x, y in zip(a, b)]
    overlap = [intersects(x, y) for x, y in zip(a, b)]
    known_overlap = all(item is True for item in overlap)
    full = all(item is True for item in coverage)
    unknown = any(item is None for item in coverage + overlap)
    if full:
        kind = "完全遮蔽_同效果" if prior.effect_signature() == later.effect_signature() else "完全遮蔽_不同效果"
    elif known_overlap:
        kind = "部分重叠"
    elif unknown:
        kind = "无法判断重叠"
    else:
        kind = "无重叠"
    return {
        "prior_position": prior.position,
        "prior_name": prior.name,
        "later_position": later.position,
        "later_name": later.name,
        "relation": kind,
        "prior_effect": "|".join(prior.effect_signature()),
        "later_effect": "|".join(later.effect_signature()),
        "evidence": "；".join(
            f"{key}:{display_dimension(x)}→{display_dimension(y)}"
            for key, x, y in zip(POLICY_MATCH_COLUMNS, a, b)
            if x != y
        ),
    }


def safe_slug(value: str, limit: int = 24) -> str:
    value = clean(value)
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value).strip("-")
    return value[:limit] or "any"


def suggested_name(policy: Policy, objects: ObjectIndex) -> str:
    action = "允许" if clean(policy.fields.get("action", "")).lower() == "y" else "拒绝"
    app = safe_slug(policy.fields.get("application", "全部"))
    src = safe_slug(policy.fields.get("src_object", "全部"))
    dst = safe_slug(policy.fields.get("dst_object", "全部"))
    seed = json.dumps(policy.raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
    stable = hashlib.sha1(seed).hexdigest()[:8]
    return f"{action}_{app}_{src}_{dst}_{stable}"


def analyze(policies: list[Policy], objects: ObjectIndex) -> dict[str, list[dict[str, object]]]:
    overlaps: list[dict[str, object]] = []
    policy_rows: list[dict[str, object]] = []
    for policy in policies:
        incoming = [
            compare_policies(previous, policy, objects)
            for previous in policies[: policy.position - 1]
            if previous.enabled and policy.enabled
        ]
        exact_same = [item for item in incoming if item["relation"] == "完全遮蔽_同效果"]
        exact_diff = [item for item in incoming if item["relation"] == "完全遮蔽_不同效果"]
        partial = [item for item in incoming if item["relation"] == "部分重叠"]
        unknown = [item for item in incoming if item["relation"] == "无法判断重叠"]
        status = "正常"
        if exact_diff:
            status = "被前序不同效果策略完全遮蔽"
        elif exact_same:
            status = "被前序同效果策略完全遮蔽"
        elif partial:
            status = "部分被前序策略覆盖"
        elif unknown:
            status = "存在无法判断的前序关系"
        policy_rows.append(
            {
                "position": policy.position,
                "name": policy.name,
                "enabled": "是" if policy.enabled else "否",
                "status": status,
                "covering_same_effect": ",".join(str(item["prior_position"]) for item in exact_same),
                "covering_different_effect": ",".join(str(item["prior_position"]) for item in exact_diff),
                "partial_overlap": ",".join(str(item["prior_position"]) for item in partial),
                "unknown_overlap": ",".join(str(item["prior_position"]) for item in unknown),
                "suggested_name": suggested_name(policy, objects),
            }
        )
        overlaps.extend(incoming)

    merges: list[dict[str, object]] = []
    for i, left in enumerate(policies):
        if not left.enabled:
            continue
        for right in policies[i + 1 :]:
            if not right.enabled or left.effect_signature() != right.effect_signature():
                continue
            left_sig, right_sig = match_signature(left, objects), match_signature(right, objects)
            different = [key for key, a, b in zip(POLICY_MATCH_COLUMNS, left_sig, right_sig) if a != b]
            if len(different) != 1:
                continue
            dim = different[0]
            a, b = left_sig[list(POLICY_MATCH_COLUMNS).index(dim)], right_sig[list(POLICY_MATCH_COLUMNS).index(dim)]
            if a in {UNKNOWN, ANY} or b in {UNKNOWN, ANY}:
                verification = "无法证明：合并维度包含任意或未知值"
            else:
                verification = "候选等价：仅一个匹配维度不同，仍需整表组合复核"
            between = [p.position for p in policies if left.position < p.position < right.position and p.enabled]
            merges.append(
                {
                    "candidate_id": f"MERGE-{left.position:04d}-{right.position:04d}",
                    "left_position": left.position,
                    "left_name": left.name,
                    "right_position": right.position,
                    "right_name": right.name,
                    "different_dimension": dim,
                    "left_value": display_dimension(a),
                    "right_value": display_dimension(b),
                    "intervening_positions": ",".join(map(str, between)),
                    "verification": verification,
                    "recommended": "待确认" if verification.startswith("候选等价") else "不建议自动合并",
                }
            )

    object_candidates: list[dict[str, object]] = []
    by_signature: dict[tuple[str, ...], list[str]] = {}
    for obj in objects.objects.values():
        resolved = objects.resolve(obj.name)
        if resolved not in {ANY, UNKNOWN}:
            by_signature.setdefault(tuple(sorted(resolved)), []).append(obj.name)
    for signature, names in by_signature.items():
        if len(names) > 1:
            object_candidates.append(
                {
                    "candidate_id": f"OBJECT-REUSE-{len(object_candidates)+1:04d}",
                    "type": "已有对象精确重复",
                    "objects": ",".join(sorted(names)),
                    "members": ",".join(signature),
                    "recommendation": "优先复用一个对象并评估引用影响；不自动删除",
                    "verification": "集合精确相同",
                }
            )

    name_candidates = [
        {
            "candidate_id": f"NAME-{p.position:04d}",
            "position": p.position,
            "policy_name": p.name,
            "suggested_name": suggested_name(p, objects),
            "verification": "仅名称变化；需确认设备是否存在按名称引用",
        }
        for p in policies
        if p.name != suggested_name(p, objects)
    ]

    review: list[dict[str, object]] = []
    for row in name_candidates:
        review.append({"candidate_id": row["candidate_id"], "type": "命名规范化", "verification": row["verification"], "status": "待确认", "details": f"{row['policy_name']} → {row['suggested_name']}"})
    for row in merges:
        review.append({"candidate_id": row["candidate_id"], "type": "策略合并", "verification": row["verification"], "status": "待确认", "details": f"{row['left_name']} + {row['right_name']}"})
    for row in object_candidates:
        review.append({"candidate_id": row["candidate_id"], "type": "对象复用", "verification": row["verification"], "status": "待确认", "details": row["objects"]})
    return {"policy_analysis": policy_rows, "overlaps": overlaps, "merge_candidates": merges, "object_candidates": object_candidates, "name_candidates": name_candidates, "review": review}


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run(policy_path: Path, object_path: Path, output_dir: Path) -> dict[str, object]:
    objects = ObjectIndex(parse_objects(object_path))
    policies = parse_policies(policy_path)
    result = analyze(policies, objects)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in result.items():
        write_csv(output_dir / f"{name}.csv", rows)
    report = {
        "policy_file": str(policy_path),
        "object_file": str(object_path),
        "policy_count": len(policies),
        "object_count": len(objects.objects),
        "limitations": [
            "仅完成离线静态分析，未调用设备或项目固有工具链",
            "未建模设备特有的动态应用识别、继续匹配等语义",
            "未知字段和未知对象关系不会被当作任意或空集合",
        ],
        "outputs": sorted(path.name for path in output_dir.glob("*.csv")) + ["analysis_report.json"],
        "analysis": result,
    }
    (output_dir / "analysis_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="应用控制策略离线梳理与优化候选生成器")
    parser.add_argument("--policy", required=True, type=Path, help="应用控制策略 CSV")
    parser.add_argument("--objects", required=True, type=Path, help="网络对象 CSV")
    parser.add_argument("--out", required=True, type=Path, help="输出目录")
    args = parser.parse_args(argv)
    for path in (args.policy, args.objects):
        if not path.exists():
            parser.error(f"文件不存在: {path}")
    report = run(args.policy, args.objects, args.out)
    print(json.dumps({k: report[k] for k in ("policy_count", "object_count", "outputs")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
