"""Durable, immutable analysis and table-review records. No device writes.

Approving a review is deliberately not permission to execute a device plan.
The subsequent planner must re-prove the selected combination and bind an
explicit execution confirmation to that exact plan.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


HEADERS = ("分析ID", "快照指纹", "方案指纹", "变更编号", "类型", "涉及策略",
           "变更原因", "原配置", "建议配置", "验证结论", "证明范围与限制",
           "依赖项", "互斥项", "用户决定", "用户备注")
DECISIONS = {"待确认", "采纳", "不采纳"}


def safe_cell(value):
    """Prevent spreadsheet formula interpretation; retain exact display round-trip."""
    text = value if isinstance(value, str) else canonical(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n", "'")):
        return "'" + text
    return text


class ReviewStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS policy_analyses (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL, proposal_hash TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policy_reviews (
                    id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL,
                    submission_key TEXT NOT NULL, submission_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(analysis_id, submission_key),
                    FOREIGN KEY(analysis_id) REFERENCES policy_analyses(id)
                );
                CREATE TABLE IF NOT EXISTS policy_feedback (
                    id TEXT PRIMARY KEY, review_id TEXT NOT NULL,
                    submission_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(review_id, submission_key),
                    FOREIGN KEY(review_id) REFERENCES policy_reviews(id)
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create(self, device_id, snapshot, candidates, findings=None):
        # Canonical serialization severs references to caller-owned mutable data.
        candidates = json.loads(canonical(candidates))
        ids = [c["id"] for c in candidates]
        if len(ids) != len(set(ids)) or any(not isinstance(i, str) or not i for i in ids):
            raise ValueError("Candidate IDs must be unique nonempty strings")
        for c in candidates:
            for field in ("dependencies", "conflicts"):
                refs = c.get(field, [])
                if not isinstance(refs, list) or len(refs) != len(set(refs)) or set(refs) - set(ids) or c["id"] in refs:
                    raise ValueError(f"Invalid {field} for {c['id']}")
        visiting, visited = set(), set()
        by_id = {c["id"]: c for c in candidates}

        def visit(identity):
            if identity in visiting:
                raise ValueError("Cyclic candidate dependencies")
            if identity in visited:
                return
            visiting.add(identity)
            for dep in by_id[identity].get("dependencies", []):
                visit(dep)
            visiting.remove(identity)
            visited.add(identity)

        for identity in ids:
            visit(identity)
        payload = {"snapshot": snapshot, "candidates": candidates, "findings": findings or []}
        snapshot_hash = fingerprint({"device_id": device_id, "snapshot": snapshot})
        proposal_hash = fingerprint({"snapshot_hash": snapshot_hash, "candidates": candidates})
        identity = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("INSERT INTO policy_analyses VALUES (?,?,?,?,?,?)",
                       (identity, device_id, datetime.now(timezone.utc).isoformat(),
                        snapshot_hash, proposal_hash, canonical(payload)))
        return self.get(identity)

    def get(self, analysis_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM policy_analyses WHERE id=?", (analysis_id,)).fetchone()
        if row is None:
            raise KeyError("Analysis not found")
        result = dict(row)
        result.update(json.loads(result.pop("payload")))
        return result

    def get_review(self, review_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM policy_reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise KeyError("Review not found")
        return {"review_id": review_id, **json.loads(row["payload"])}

    def get_feedback(self, feedback_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM policy_feedback WHERE id=?", (feedback_id,)).fetchone()
        if row is None:
            raise KeyError("Feedback log not found")
        payload = json.loads(row["payload"])
        stored_hash = payload.get("feedback_hash")
        if not stored_hash or fingerprint({k: v for k, v in payload.items() if k != "feedback_hash"}) != stored_hash:
            raise ValueError("Feedback log integrity check failed")
        return payload

    def feedback_by_key(self, review_id, submission_key):
        with self.connect() as db:
            row = db.execute("SELECT id, request_hash FROM policy_feedback WHERE review_id=? AND submission_key=?",
                             (review_id, submission_key)).fetchone()
        return {"request_hash": row["request_hash"], "feedback": self.get_feedback(row["id"])} if row else None

    def save_feedback(self, review_id, submission_key, request_hash, payload):
        if not isinstance(submission_key, str) or not 1 <= len(submission_key) <= 128:
            raise ValueError("A stable feedback submission key is required")
        feedback_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        document = {**payload, "feedback_id": feedback_id, "review_id": review_id,
                    "created_at": created_at}
        document["feedback_hash"] = fingerprint(document)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT id, request_hash FROM policy_feedback WHERE review_id=? AND submission_key=?",
                               (review_id, submission_key)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise ValueError("Feedback key reused with different input")
                feedback_id = prior["id"]
            else:
                db.execute("INSERT INTO policy_feedback VALUES (?,?,?,?,?,?)",
                           (feedback_id, review_id, submission_key, request_hash, created_at, canonical(document)))
        return self.get_feedback(feedback_id)

    def list_feedback(self, review_id, offset=0, limit=50):
        self.get_review(review_id)
        with self.connect() as db:
            rows = db.execute("SELECT id FROM policy_feedback WHERE review_id=? ORDER BY created_at DESC, id DESC "
                              "LIMIT ? OFFSET ?", (review_id, limit, offset)).fetchall()
        return [self.get_feedback(row["id"]) for row in rows]

    def export_feedback_json(self, feedback_id):
        return json.dumps(self.get_feedback(feedback_id), ensure_ascii=False, indent=2, allow_nan=False) + "\n"

    @staticmethod
    def _rows(analysis):
        rows = []
        for c in analysis["candidates"]:
            values = [analysis["id"], analysis["snapshot_hash"], analysis["proposal_hash"],
                      c["id"], c.get("kind", ""), c.get("policies", []),
                      c.get("reason", ""), c.get("before", {}), c.get("after", {}),
                      c.get("proof", {}).get("status", "unknown"),
                      c.get("proof", {}), c.get("dependencies", []), c.get("conflicts", []),
                      "待确认", ""]
            rows.append(dict(zip(HEADERS, map(safe_cell, values))))
        return rows

    def export_csv(self, analysis_id):
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=HEADERS, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(self._rows(self.get(analysis_id)))
        return "\ufeff" + output.getvalue()

    def export_findings_csv(self, analysis_id):
        analysis = self.get(analysis_id)
        output = io.StringIO(newline="")
        columns = ("分析ID", "快照指纹", "策略ID", "问题类型", "状态", "说明", "证据与限制")
        writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\r\n")
        writer.writeheader()
        for finding in analysis["findings"]:
            values = [analysis_id, analysis["snapshot_hash"], finding.get("policy_id", ""),
                      finding.get("kind", ""), finding.get("status", ""),
                      finding.get("message", finding.get("scope", "")), finding]
            writer.writerow(dict(zip(columns, map(safe_cell, values))))
        return "\ufeff" + output.getvalue()

    def review_csv(self, analysis_id, text, submission_key):
        if len(text.encode("utf-8")) > 20_000_000:
            raise ValueError("Review file exceeds 20 MB")
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff"), newline=""), strict=True)
        if tuple(reader.fieldnames or ()) != HEADERS:
            raise ValueError("Review columns were changed")
        expected = {r["变更编号"]: r for r in self._rows(self.get(analysis_id))}
        decisions, seen = [], set()
        for row in reader:
            identity = row.get("变更编号")
            if identity not in expected or identity in seen:
                raise ValueError("Unknown or duplicate candidate ID")
            seen.add(identity)
            if None in row or any(row.get(k) != expected[identity][k] for k in HEADERS[:-2]):
                raise ValueError("Proposal content/version changed; regenerate and review the new proposal")
            decisions.append({"candidate_id": identity, "decision": row["用户决定"], "note": row["用户备注"]})
        if seen != set(expected):
            raise ValueError("Review rows are missing; use 不采纳 rather than deleting rows")
        analysis = self.get(analysis_id)
        return self.submit(analysis_id, analysis["proposal_hash"], decisions, submission_key)

    def submit(self, analysis_id, proposal_hash, decisions, submission_key):
        if not isinstance(submission_key, str) or not 1 <= len(submission_key) <= 128:
            raise ValueError("A stable submission key is required")
        analysis = self.get(analysis_id)
        if proposal_hash != analysis["proposal_hash"]:
            raise ValueError("Stale proposal version")
        known = {c["id"]: c for c in analysis["candidates"]}
        selected, seen, normalized = set(), set(), []
        for item in decisions:
            if set(item) - {"candidate_id", "decision", "note"}:
                raise ValueError("Unexpected review fields")
            identity, decision = item.get("candidate_id"), item.get("decision")
            if identity not in known or identity in seen or decision not in DECISIONS:
                raise ValueError("Unknown/duplicate candidate or invalid decision")
            note = item.get("note", "")
            if not isinstance(note, str) or len(note) > 4000:
                raise ValueError("Invalid review note")
            seen.add(identity)
            if decision == "采纳":
                selected.add(identity)
            normalized.append({"candidate_id": identity, "decision": decision, "note": note})
        # Chat can explicitly select a subset; omitted items remain pending.
        for identity in sorted(set(known) - seen):
            normalized.append({"candidate_id": identity, "decision": "待确认", "note": ""})
        for identity in selected:
            c = known[identity]
            if set(c.get("dependencies", [])) - selected:
                raise ValueError(f"Unselected dependencies for {identity}")
            if set(c.get("conflicts", [])) & selected:
                raise ValueError(f"Conflicting selections for {identity}")
        payload = {"analysis_id": analysis_id, "proposal_hash": proposal_hash,
                   "snapshot_hash": analysis["snapshot_hash"], "selected_ids": sorted(selected),
                   "decisions": sorted(normalized, key=lambda d: d["candidate_id"]),
                   "status": "awaiting_combination_verification", "execution_authorized": False}
        digest = fingerprint(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM policy_reviews WHERE analysis_id=? AND submission_key=?",
                               (analysis_id, submission_key)).fetchone()
            if prior:
                if prior["submission_hash"] != digest:
                    raise ValueError("Submission key reused with different decisions")
                return {"review_id": prior["id"], **json.loads(prior["payload"])}
            identity = str(uuid.uuid4())
            db.execute("INSERT INTO policy_reviews VALUES (?,?,?,?,?,?)",
                       (identity, analysis_id, submission_key, digest,
                        datetime.now(timezone.utc).isoformat(), canonical(payload)))
        return {"review_id": identity, **payload}
