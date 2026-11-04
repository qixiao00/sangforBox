"""Local case evidence for the AF initialization trial. Device I/O stays in DeviceService."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any
from af_app.network_safety import NetworkContext


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


_SENSITIVE = {"password", "passwd", "secret", "token", "cookie", "authorization", "api_key", "apikey", "private_key"}
_DESIGN_FIELDS = {
    "deployment_mode", "topology", "interfaces", "zones", "address_plan", "routing",
    "nat", "security", "application", "preserve_existing", "default_choices",
    "business_subnets", "protection_scope", "management_recovery", "open_questions", "notes", "network_context",
}
_REQUIRED_DESIGN = (
    "deployment_mode", "topology", "interfaces", "zones", "routing",
    "preserve_existing", "business_subnets", "protection_scope", "default_choices", "network_context",
)
_CSV_FIELDS = (
    "case_id", "item_id", "review_hash", "stage", "configuration", "before", "planned",
    "implemented", "current", "difference", "difference_confirmed", "satisfaction",
    "reason", "comment",
)


def _reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _SENSITIVE:
                raise ValueError(f"Design must not contain credential field: {key}")
            _reject_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secrets(child)


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[redacted]" if str(key).lower() in _SENSITIVE else _safe(child))
                for key, child in value.items()}
    if isinstance(value, list):
        return [_safe(child) for child in value]
    return value


def _merge_design(previous: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(previous)
    for key, value in patch.items():
        merged[key] = (_merge_design(merged[key], value)
                       if isinstance(value, dict) and isinstance(merged.get(key), dict) else value)
    return merged


def _csv_cell(value: Any) -> str:
    text = value if isinstance(value, str) else _json(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


class InitializationCaseRepository:
    def __init__(self, database, output_directory=None):
        self.path = database.path
        self.output_directory = output_directory

    def persist_review_csv(self, case_id: str, content: str) -> str | None:
        if self.output_directory is None:
            return None
        directory = self.output_directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"af-initialization-feedback-{case_id}.csv"
        descriptor, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return str(target)

    def get(self, case_id: str, device_id: str) -> dict[str, Any]:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM initialization_cases WHERE case_id=? AND device_id=?",
                (case_id, device_id),
            ).fetchone()
        if row is None:
            raise ValueError("Initialization case not found for this device")
        result = dict(row)
        result["design"] = json.loads(result.pop("design_json"))
        result["design_confirmed"] = result["confirmed_revision"] == result["design_revision"]
        result["missing_context"] = self.design_gaps(result["design"])
        return result

    @staticmethod
    def design_gaps(design: dict[str, Any]) -> list[str]:
        gaps = [key for key in _REQUIRED_DESIGN if key not in design or
                design[key] is None or design[key] == "" or
                (key not in {"routing"} and design[key] == [])]
        choices = design.get("default_choices")
        if isinstance(choices, dict):
            gaps.extend(f"default_choices.{key}" for key in ("nat", "security", "application")
                        if not choices.get(key))
        elif "default_choices" not in gaps:
            gaps.append("default_choices")
        if design.get("open_questions"):
            gaps.append("open_questions")
        if "network_context" not in gaps:
            try:
                NetworkContext.model_validate(design["network_context"])
            except ValueError:
                gaps.append("network_context")
        return gaps

    def save_design(self, device_id: str, patch: dict[str, Any], case_id: str = "",
                    confirm: bool = False) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("design must be an object")
        unknown = set(patch) - _DESIGN_FIELDS
        if unknown:
            raise ValueError(f"Unknown design fields: {sorted(unknown)}")
        _reject_secrets(patch)
        if len(_json(patch).encode("utf-8")) > 65536:
            raise ValueError("Design update exceeds 64 KiB")
        now = _now()
        if case_id:
            previous = self.get(case_id, device_id)
            design = _merge_design(previous["design"], patch)
            revision = previous["design_revision"] + (1 if patch else 0)
            confirmed_revision = revision if confirm else (None if patch else previous["confirmed_revision"])
            if confirm and self.design_gaps(design):
                raise ValueError(f"Design is incomplete: {self.design_gaps(design)}")
            with sqlite3.connect(self.path) as connection:
                connection.execute(
                    "UPDATE initialization_cases SET design_json=?,design_revision=?,confirmed_revision=?,updated_at=? "
                    "WHERE case_id=? AND device_id=?",
                    (_json(design), revision, confirmed_revision, now, case_id, device_id),
                )
                if patch:
                    connection.execute(
                        "UPDATE operation_plans SET status='stale' WHERE status='planned' AND plan_id IN "
                        "(SELECT plan_id FROM initialization_case_plans WHERE case_id=?)",
                        (case_id,),
                    )
        else:
            case_id = str(uuid.uuid4())
            revision = 1 if patch else 0
            if confirm and self.design_gaps(patch):
                raise ValueError(f"Design is incomplete: {self.design_gaps(patch)}")
            with sqlite3.connect(self.path) as connection:
                connection.execute(
                    "INSERT INTO initialization_cases VALUES (?,?,?,?,?,?,?)",
                    (case_id, device_id, _json(patch), revision, revision if confirm else None, now, now),
                )
        self.event(case_id, "design_confirm" if confirm else "design_update", "recorded")
        return self.get(case_id, device_id)

    def event(self, case_id: str, action: str, status: str,
              stage: str = "", plan_id: str = "") -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO initialization_events (case_id,action,stage,plan_id,status,occurred_at) "
                "VALUES (?,?,?,?,?,?)", (case_id, action, stage or None, plan_id or None, status, _now()),
            )

    def events(self, case_id: str, device_id: str, limit: int = 100) -> list[dict[str, Any]]:
        self.get(case_id, device_id)
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT event_id,action,stage,plan_id,status,occurred_at FROM initialization_events "
                "WHERE case_id=? ORDER BY event_id DESC LIMIT ?", (case_id, min(max(limit, 1), 200)),
            ).fetchall()
        return [dict(row) for row in rows]

    def link_plan(self, case_id: str, device_id: str, plan_id: str, stage: str) -> str:
        case = self.get(case_id, device_id)
        if not case["design_confirmed"]:
            raise ValueError("Design revision must be confirmed before planning")
        item_id = str(uuid.uuid4())
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO initialization_case_plans (item_id,case_id,plan_id,stage,design_revision) "
                "VALUES (?,?,?,?,?)", (item_id, case_id, plan_id, stage, case["design_revision"]),
            )
        self.event(case_id, "plan", "planned", stage, plan_id)
        return item_id

    def linked_plan(self, case_id: str, device_id: str, plan_id: str) -> dict[str, Any]:
        self.get(case_id, device_id)
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM initialization_case_plans WHERE case_id=? AND plan_id=?",
                (case_id, plan_id),
            ).fetchone()
        if row is None:
            raise ValueError("Plan is not linked to this initialization case")
        return dict(row)

    def case_for_plan(self, plan_id: str) -> str:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT case_id FROM initialization_case_plans WHERE plan_id=?", (plan_id,),
            ).fetchone()
        return str(row[0]) if row else ""

    def record_implementation(self, case_id: str, device_id: str, plan_id: str,
                              item: Any, status: str) -> None:
        self.linked_plan(case_id, device_id, plan_id)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE initialization_case_plans SET implementation_json=?,implementation_status=?,verified_at=? "
                "WHERE case_id=? AND plan_id=? AND implementation_json IS NULL",
                (_json(_safe(item)), status, _now(), case_id, plan_id),
            )
        self.event(case_id, "verify", status, plan_id=plan_id)

    def plans(self, case_id: str, device_id: str) -> list[dict[str, Any]]:
        self.get(case_id, device_id)
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM initialization_case_plans WHERE case_id=? ORDER BY rowid",
                (case_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def feedback(self, case_id: str, device_id: str) -> list[dict[str, Any]]:
        self.get(case_id, device_id)
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM initialization_feedback WHERE case_id=? ORDER BY submitted_at,item_id",
                (case_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def review_hash(row: dict[str, Any]) -> str:
        return _hash({key: row.get(key) for key in
                      ("case_id", "item_id", "stage", "before", "planned", "implemented", "current", "difference")})

    @staticmethod
    def csv_template(rows: list[dict[str, Any]]) -> str:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_cell(row.get(key, "")) for key in _CSV_FIELDS})
        return "\ufeff" + output.getvalue()

    @staticmethod
    def parse_feedback_csv(value: str) -> list[dict[str, str]]:
        if len(value.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Feedback CSV exceeds 1 MiB")
        reader = csv.DictReader(io.StringIO(value.lstrip("\ufeff")))
        if reader.fieldnames is None or not set(_CSV_FIELDS).issubset(reader.fieldnames):
            raise ValueError("Feedback CSV is missing required columns")
        rows = list(reader)
        if not rows or len(rows) > 500:
            raise ValueError("Feedback CSV must contain 1-500 rows")
        return rows

    def submit_feedback(self, case_id: str, device_id: str, review_rows: list[dict[str, Any]],
                        feedback_csv: str) -> dict[str, Any]:
        self.get(case_id, device_id)
        expected = {row["item_id"]: row for row in review_rows}
        submitted = self.parse_feedback_csv(feedback_csv)
        seen: set[str] = set()
        validated = []
        for row in submitted:
            item_id = row.get("item_id", "")
            if item_id in seen or item_id not in expected or row.get("case_id") != case_id:
                raise ValueError("Feedback references a duplicate or unknown case item")
            seen.add(item_id)
            if row.get("review_hash") != expected[item_id]["review_hash"]:
                raise ValueError("Feedback snapshot changed; review differences again")
            satisfaction = (row.get("satisfaction") or "").strip().lower()
            confirmed = (row.get("difference_confirmed") or "").strip().lower()
            if satisfaction not in {"satisfied", "unsatisfied"} or confirmed not in {"yes", "no", "uncertain"}:
                raise ValueError("Each item needs satisfaction=satisfied/unsatisfied and difference_confirmed=yes/no/uncertain")
            reason = (row.get("reason") or "").strip()
            comment = (row.get("comment") or "").strip()
            if satisfaction == "unsatisfied" and not reason:
                raise ValueError("Unsatisfied items require a reason")
            if len(reason) > 120 or len(comment) > 2000:
                raise ValueError("Feedback text is too long")
            validated.append((item_id, case_id, row["review_hash"], satisfaction,
                              confirmed, reason, comment, _now()))
        if seen != set(expected):
            raise ValueError("Feedback CSV must rate every case item")
        with sqlite3.connect(self.path) as connection:
            connection.executemany(
                "INSERT INTO initialization_feedback VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(item_id) DO UPDATE SET review_hash=excluded.review_hash,"
                "satisfaction=excluded.satisfaction,difference_confirmed=excluded.difference_confirmed,"
                "reason=excluded.reason,comment=excluded.comment,submitted_at=excluded.submitted_at",
                validated,
            )
        self.event(case_id, "feedback_submit", "recorded")
        return {"ok": True, "case_id": case_id, "recorded": len(validated),
                "unsatisfied": sum(row[3] == "unsatisfied" for row in validated)}
