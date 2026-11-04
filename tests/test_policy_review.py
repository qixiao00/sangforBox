import csv
import io
from concurrent.futures import ThreadPoolExecutor

import pytest

from af_app.policy_review import ReviewStore


def setup(tmp_path, candidates=None):
    store = ReviewStore(tmp_path / "reviews.db")
    analysis = store.create("device", {"policies": [1, 2]}, candidates or [
        {"id": "a", "kind": "rename", "policies": ["=BAD()"], "reason": "=BAD()",
         "before": {"name": "旧\n名称"}, "after": {"name": "新名称"}, "proof": {"status": "unknown"}},
        {"id": "b", "kind": "merge", "dependencies": ["a"]}])
    return store, analysis


def edit_csv(text, edit):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    rows = list(reader)
    edit(rows)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def test_review_round_trip_is_not_execution_authorization(tmp_path):
    store, a = setup(tmp_path)
    text = store.export_csv(a["id"])
    assert "'=BAD()" in text
    text = edit_csv(text, lambda rows: rows[0].update(用户决定="采纳", 用户备注="保留业务含义"))
    review = store.review_csv(a["id"], text, "request-1")
    assert review["selected_ids"] == ["a"]
    assert review["status"] == "awaiting_combination_verification"
    assert not review["execution_authorized"]
    assert ReviewStore(store.path).review_csv(a["id"], text, "request-1") == review


@pytest.mark.parametrize("field,value", [("建议配置", "changed"), ("快照指纹", "old"),
                                         ("方案指纹", "old"), ("用户决定", "执行全部")])
def test_tampered_review_is_rejected(tmp_path, field, value):
    store, a = setup(tmp_path)
    text = edit_csv(store.export_csv(a["id"]), lambda rows: rows[0].update({field: value}))
    with pytest.raises(ValueError):
        store.review_csv(a["id"], text, "request")


def test_dependencies_duplicates_missing_rows_and_conflicts(tmp_path):
    store, a = setup(tmp_path)
    with pytest.raises(ValueError, match="dependencies"):
        store.submit(a["id"], a["proposal_hash"], [{"candidate_id": "b", "decision": "采纳"}], "key")
    for edit in (lambda rows: rows.append(rows[0]), lambda rows: rows.pop()):
        with pytest.raises(ValueError):
            store.review_csv(a["id"], edit_csv(store.export_csv(a["id"]), edit), "key")
    a = store.create("device", {}, [{"id": "x", "conflicts": ["y"]}, {"id": "y"}])
    with pytest.raises(ValueError, match="Conflicting"):
        store.submit(a["id"], a["proposal_hash"],
                     [{"candidate_id": i, "decision": "采纳"} for i in ("x", "y")], "key")


def test_concurrent_retries_are_idempotent(tmp_path):
    store, a = setup(tmp_path)
    def submit(_):
        return store.submit(a["id"], a["proposal_hash"], [], "same")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(8)))
    assert len({r["review_id"] for r in results}) == 1
    with pytest.raises(ValueError, match="different decisions"):
        store.submit(a["id"], a["proposal_hash"], [{"candidate_id": "a", "decision": "采纳"}], "same")


def test_empty_report_has_headers_and_cycles_rejected(tmp_path):
    store = ReviewStore(tmp_path / "r.db")
    a = store.create("device", {}, [])
    assert "用户决定" in store.export_csv(a["id"])
    with pytest.raises(ValueError, match="Cyclic"):
        store.create("device", {}, [{"id": "a", "dependencies": ["b"]},
                                    {"id": "b", "dependencies": ["a"]}])
