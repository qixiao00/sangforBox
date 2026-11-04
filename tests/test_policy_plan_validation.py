from af_app.policy_sets import Cube, Intervals, Rule
from af_app.policy_plan_validation import Transition, validate_sequence


SPACE = Cube((Intervals(((0, 10),)),))
OLD = Rule("old", SPACE, ("allow",))
NEW = Rule("new", SPACE, ("allow",))


def validate(steps, **kwargs):
    args = dict(complete=True, expected_snapshot="same", observed_snapshot="same",
                expected_final=(NEW,))
    args.update(kwargs)
    return validate_sequence((OLD,), steps, SPACE, ("deny",), **args)


def test_delete_then_create_rejected_even_when_final_equivalent():
    steps = [Transition("delete", (OLD,), (), True, True),
             Transition("create", (), (NEW,), True, True)]
    result = validate(steps)
    assert result["status"] == "rejected"
    assert result["proofs"][0]["proof"]["status"] == "different"


def test_create_then_delete_requires_verified_operations():
    steps = [Transition("create", (OLD,), (OLD, NEW), True, True),
             Transition("delete", (OLD, NEW), (NEW,), True, True)]
    result = validate(steps)
    assert result["status"] == "verified"
    assert not result["execution_authorized"]
    assert validate(steps, observed_snapshot="changed")["status"] == "rejected"
    assert validate(steps, complete=False)["status"] == "unknown"
    assert validate([Transition("replace", (OLD,), (NEW,), False, True)])["status"] == "unknown"
    assert validate([Transition("replace", (OLD,), (NEW,), True, False)])["status"] == "unknown"


def test_reviewed_final_state_and_continuity_required():
    assert validate([Transition("create", (OLD,), (OLD, NEW), True, True)])["status"] == "rejected"
    assert validate([Transition("replace", (), (NEW,), True, True)])["status"] == "rejected"
