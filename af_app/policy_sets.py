"""Exact finite unions of intervals and rectangular first-match policy spaces.

Adapters must supply complete semantics; unknown values are never represented
as an empty set or universal set. This module makes no device assumptions.
"""
from dataclasses import dataclass
from ipaddress import ip_address, ip_network


class ComplexityLimit(ValueError):
    pass


@dataclass(frozen=True)
class Intervals:
    spans: tuple[tuple[int, int], ...]

    def __post_init__(self):
        merged = []
        for lo, hi in sorted(self.spans):
            if lo > hi:
                raise ValueError("Reversed interval")
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
            else:
                merged.append((lo, hi))
        object.__setattr__(self, "spans", tuple(merged))

    def __bool__(self):
        return bool(self.spans)

    def union(self, other):
        return Intervals(self.spans + other.spans)

    def intersection(self, other):
        result = []
        i = j = 0
        while i < len(self.spans) and j < len(other.spans):
            a, b = self.spans[i], other.spans[j]
            lo, hi = max(a[0], b[0]), min(a[1], b[1])
            if lo <= hi:
                result.append((lo, hi))
            if a[1] < b[1]:
                i += 1
            else:
                j += 1
        return Intervals(tuple(result))

    def difference(self, other):
        remaining = list(self.spans)
        for lo, hi in other.spans:
            pieces = []
            for start, end in remaining:
                if hi < start or lo > end:
                    pieces.append((start, end))
                else:
                    if start < lo:
                        pieces.append((start, lo - 1))
                    if end > hi:
                        pieces.append((hi + 1, end))
            remaining = pieces
        return Intervals(tuple(remaining))

    def covers(self, other):
        return not other.difference(self)


# Disjoint numeric universes prevent IPv4/IPv6 collisions.
V6_OFFSET = 1 << 32
ALL_IP = Intervals(((0, V6_OFFSET + (1 << 128) - 1),))


def addresses(values):
    result = Intervals(())
    for text in values:
        text = text.strip()
        if "-" in text:
            first, last = (ip_address(v.strip()) for v in text.split("-", 1))
        elif "/" in text:
            network = ip_network(text, strict=False)
            first, last = network.network_address, network.broadcast_address
        else:
            first = last = ip_address(text)
        if first.version != last.version:
            raise ValueError("Mixed address families in a range")
        offset = V6_OFFSET if first.version == 6 else 0
        result = result.union(Intervals(((int(first) + offset, int(last) + offset),)))
    return result


@dataclass(frozen=True)
class Cube:
    dimensions: tuple[Intervals, ...]

    def intersection(self, other):
        if len(self.dimensions) != len(other.dimensions):
            raise ValueError("Dimension count mismatch")
        result = tuple(a.intersection(b) for a, b in zip(self.dimensions, other.dimensions))
        return Cube(result) if all(result) else None

    def subtract(self, other):
        overlap = self.intersection(other)
        if overlap is None:
            return [self]
        result = []
        prefix = list(self.dimensions)
        for i, matched in enumerate(overlap.dimensions):
            outside = prefix[i].difference(matched)
            if outside:
                part = prefix.copy()
                part[i] = outside
                result.append(Cube(tuple(part)))
            prefix[i] = matched
        return result

    def witness(self):
        return tuple(d.spans[0][0] for d in self.dimensions)


def subtract_all(cubes, blockers, limit=10000):
    remaining = list(cubes)
    for blocker in blockers:
        remaining = [piece for cube in remaining for piece in cube.subtract(blocker)]
        if len(remaining) > limit:
            raise ComplexityLimit("Exact partition budget exceeded; equivalence is unknown")
        if not remaining:
            break
    return remaining


@dataclass(frozen=True)
class Rule:
    identity: str
    match: Cube
    effect: tuple
    enabled: bool = True


def effective_regions(rules, universe, default_effect, limit=10000):
    regions, prior = [], []
    for rule in rules:
        if not rule.enabled:
            continue
        bounded = rule.match.intersection(universe)
        if bounded is None:
            continue
        effective = subtract_all([bounded], prior, limit)
        regions.extend((cube, rule.effect, rule.identity) for cube in effective)
        prior.append(bounded)
        if len(regions) > limit:
            raise ComplexityLimit("Effective-region budget exceeded")
    regions.extend((cube, default_effect, "<default>")
                   for cube in subtract_all([universe], prior, limit))
    return regions


def prove_equivalent(before, after, universe, default_effect, *, complete=True, limit=10000):
    if not complete:
        return {"status": "unknown", "reason": "Incomplete device semantics or unresolved conditions"}
    try:
        left = effective_regions(before, universe, default_effect, limit)
        right = effective_regions(after, universe, default_effect, limit)
        comparisons = 0
        for a, effect_a, id_a in left:
            for b, effect_b, id_b in right:
                comparisons += 1
                if comparisons > limit * 10:
                    raise ComplexityLimit("Comparison budget exceeded")
                if effect_a == effect_b:
                    continue
                common = a.intersection(b)
                if common:
                    return {"status": "different", "witness": common.witness(),
                            "before_rule": id_a, "after_rule": id_b,
                            "before_effect": effect_a, "after_effect": effect_b}
        return {"status": "equivalent", "scope": "Complete supplied first-match model including default effect"}
    except ComplexityLimit as exc:
        return {"status": "unknown", "reason": str(exc)}
