from itertools import product

import pytest

from af_app.policy_sets import Cube, Intervals, Rule, addresses, effective_regions, prove_equivalent


def interval(a, b):
    return Intervals(((a, b),))


def test_address_families_holes_and_range_equivalence():
    network = addresses(["10.0.0.0/24"])
    assert network.covers(addresses(["10.0.0.1"]))
    assert network == addresses(["10.0.0.0-10.0.0.255"])
    assert not addresses(["10.0.0.1", "10.0.0.3"]).covers(addresses(["10.0.0.2"]))
    assert not addresses(["0.0.0.1"]).intersection(addresses(["::1"]))
    assert addresses(["2021::/120"]).covers(addresses(["2021::88-2021::99"]))
    with pytest.raises(ValueError):
        addresses(["10.0.0.5-10.0.0.1"])


def test_cube_subtraction_against_exhaustive_small_universe():
    whole = Cube((interval(0, 3), interval(0, 3)))
    for x, y in product(range(4), repeat=2):
        removed = Cube((interval(x, 3), interval(0, y)))
        pieces = whole.subtract(removed)
        for a, b in product(range(4), repeat=2):
            point = Cube((interval(a, a), interval(b, b)))
            matches = sum(piece.intersection(point) is not None for piece in pieces)
            assert matches == int(not (a >= x and b <= y))


def test_joint_shadow_and_disabled_rule():
    whole = Cube((interval(0, 9),))
    rules = [Rule("a", Cube((interval(0, 4),)), ("deny",)),
             Rule("b", Cube((interval(5, 9),)), ("deny",)),
             Rule("c", whole, ("allow",)),
             Rule("disabled", whole, ("other",), False)]
    regions = effective_regions(rules, whole, ("default",))
    assert {r[2] for r in regions} == {"a", "b"}
    assert prove_equivalent(rules, rules[:2], whole, ("default",))["status"] == "equivalent"


def test_nonadjacent_merge_changes_first_match():
    whole = Cube((interval(0, 9),))
    rules = [Rule("a", Cube((interval(0, 3),)), ("allow",)),
             Rule("middle", Cube((interval(4, 7),)), ("deny",)),
             Rule("b", Cube((interval(4, 9),)), ("allow",))]
    result = prove_equivalent(rules, [Rule("merged", whole, ("allow",)), rules[1]], whole, ("deny",))
    assert result["status"] == "different"
    assert 4 <= result["witness"][0] <= 7


def test_cartesian_expansion_and_log_difference():
    whole = Cube((interval(0, 1), interval(0, 1)))
    diagonal = [Rule(str(i), Cube((interval(i, i), interval(i, i))), ("allow",)) for i in range(2)]
    assert prove_equivalent(diagonal, [Rule("merged", whole, ("allow",))], whole, ("deny",))["status"] == "different"
    assert prove_equivalent([Rule("a", whole, ("allow", "log-a"))],
                            [Rule("b", whole, ("allow", "log-b"))], whole, ("deny",))["status"] == "different"
    assert prove_equivalent([], [], whole, (), complete=False)["status"] == "unknown"


def test_partial_selection_must_be_reproved():
    whole = Cube((interval(0, 9),))
    original = [Rule("a", whole, ("allow",)), Rule("b", whole, ("allow",))]
    assert prove_equivalent(original, original[:1], whole, ("deny",))["status"] == "equivalent"
    assert prove_equivalent(original, original[1:], whole, ("deny",))["status"] == "equivalent"
    assert prove_equivalent(original, [], whole, ("deny",))["status"] == "different"
