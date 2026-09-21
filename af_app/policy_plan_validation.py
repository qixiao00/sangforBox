"""Pure preflight gate for an explicit sequence of policy states.

No network access: a device/version adapter must derive these states from the
actual operations, including object changes, before this gate can accept them.
"""
from dataclasses import dataclass

from af_app.policy_sets import Cube, Rule, prove_equivalent


@dataclass(frozen=True)
class Transition:
    operation_id: str
    before: tuple[Rule, ...]
    after: tuple[Rule, ...]
    operation_is_atomic: bool
    # True only if the version adapter fully models all observable intermediate
    # effects of this one operation. Not an LLM/user-supplied safety assertion.
    adapter_verified: bool


def validate_sequence(baseline: tuple[Rule, ...], transitions: list[Transition],
                      universe: Cube, default_effect: tuple, *, complete: bool,
                      expected_snapshot: str, observed_snapshot: str,
                      expected_final: tuple[Rule, ...], limit=10000):
    if not expected_snapshot or expected_snapshot != observed_snapshot:
        return {"status": "rejected", "reason": "Snapshot changed; regenerate analysis and review"}
    if not complete:
        return {"status": "unknown", "reason": "Adapter semantics incomplete"}
    if not transitions:
        return {"status": "rejected", "reason": "No executable transitions"}
    previous, proofs = baseline, []
    for index, step in enumerate(transitions):
        if step.before != previous:
            return {"status": "rejected", "step": index + 1, "reason": "Discontinuous execution states"}
        if not step.operation_id or not step.operation_is_atomic or not step.adapter_verified:
            return {"status": "unknown", "step": index + 1,
                    "reason": "Operation intermediate effects are not fully verified"}
        proof = prove_equivalent(baseline, step.after, universe, default_effect,
                                 complete=complete, limit=limit)
        proofs.append({"step": index + 1, "operation_id": step.operation_id, "proof": proof})
        if proof["status"] != "equivalent":
            return {"status": "rejected" if proof["status"] == "different" else "unknown",
                    "reason": "Intermediate state failed equivalence", "proofs": proofs}
        previous = step.after
    if previous != expected_final:
        return {"status": "rejected", "reason": "Sequence does not produce the reviewed final state"}
    return {"status": "verified", "proofs": proofs,
            "scope": "Supplied complete model and verified atomic operations",
            "execution_authorized": False}
