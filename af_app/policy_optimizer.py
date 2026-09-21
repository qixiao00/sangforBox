"""Application-control review service; all device access stays server-side."""
import asyncio
import copy
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from af_app.policy_review import ReviewStore, fingerprint
from af_app.policy_snapshot import PolicySnapshotCollector
from af_app.policy_semantics import analyze_addresses
from af_app.policy_combination import materialize, verify_combination
from af_app.policy_feedback import compare_configurations


class OptimizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1, max_length=500)
    objectives: list[Literal["naming", "shadowing", "merge", "objectize", "order"]] = Field(
        default_factory=lambda: ["naming", "shadowing", "merge", "objectize", "order"], min_length=1, max_length=5)
    protected_policy_ids: list[str] = Field(default_factory=list, max_length=1000)
    naming_prefix: str = Field(default="AC", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,11}$")


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=128)
    decision: Literal["待确认", "采纳", "不采纳"]
    note: str = Field(default="", max_length=4000)


class EngineerFeedbackItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=128)
    assessment: Literal["implemented", "modified", "rejected", "deferred", "unsafe", "not_applicable"]
    reason: str = Field(min_length=1, max_length=4000)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: str = Field(min_length=1, max_length=128)
    submission_key: str = Field(min_length=1, max_length=128)
    engineer_label: str = Field(min_length=1, max_length=200)
    change_ticket: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=4000)
    feedback_items: list[EngineerFeedbackItem] = Field(default_factory=list, max_length=1000)


def propose(snapshot, request):
    """Structural suggestions, explicitly unproven until semantics are adapted.

    Retain whole raw before/after records, never build an executable request body
    from an incomplete projection. No wildcard/empty-field inference here.
    """
    config = snapshot["configuration"]
    policies = config["policy.app_control.list"]
    candidates, findings = [], []
    objectives = set(request.objectives)
    known_ids = {r.get("uuid") or r["name"] for r in policies}
    if set(request.protected_policy_ids) - known_ids:
        raise ValueError("Unknown protected policy IDs; use identifiers from this device")
    occupied = {r["name"] for r in policies}
    for rule in policies:
        identity = rule.get("uuid") or rule["name"]
        missing = [k for k in ("src", "dst", "action", "enable", "schedule", "advanceOption", "position") if k not in rule]
        if missing:
            findings.append({"policy_id": identity, "kind": "missing_fields", "fields": missing,
                             "message": "策略字段不完整，无法证明匹配语义"})
        if rule.get("enable") is False:
            findings.append({"policy_id": identity, "kind": "disabled", "message": "策略处于禁用状态"})
        if "naming" not in objectives or identity in request.protected_policy_ids or rule.get("isdefault"):
            continue
        # Numeric action meanings are not guessed from API examples.
        suffix = fingerprint({"identity": identity})[:12]
        name = f"{request.naming_prefix}_{suffix}"
        if name == rule["name"]:
            continue
        if name in occupied:
            findings.append({"policy_id": identity, "kind": "name_conflict", "name": name})
            continue
        occupied.add(name)
        after = copy.deepcopy(rule)
        after["name"] = name
        candidates.append({"id": f"name-{suffix}", "kind": "命名规范化", "policies": [identity],
                           "reason": "使用固定前缀和稳定标识；业务含义可通过新版本命名方案补充",
                           "before": rule, "after": after,
                           "proof": {"status": "unknown", "reason": "尚需验证名称引用与日志中的规则身份影响"},
                           "dependencies": [], "conflicts": []})
    address_candidates, address_findings = analyze_addresses(config, objectives, request.protected_policy_ids)
    candidates.extend(address_candidates)
    findings.extend(address_findings)
    for c in candidates:
        c["conflicts"] = [other["id"] for other in candidates if other is not c
                          and set(c["policies"]) & set(other["policies"])]
    for objective in sorted(objectives - {"naming", "shadowing", "merge", "objectize", "order"}):
        findings.append({"kind": "adapter_pending", "objective": objective,
                         "message": "该能力尚待完整设备语义适配；未执行分析，不能据此判断没有问题"})
    findings.append({"kind": "execution_gate", "message": "本结果为待审核建议，尚未具备设备执行条件"})
    return candidates, findings


class PolicyOptimizer:
    def __init__(self, devices, database_path):
        self.devices = devices
        self.store = ReviewStore(Path(database_path).with_name("policy-review.db"))
        self.collector = PolicySnapshotCollector(devices)
        self.tasks = {}
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS policy_analysis_jobs (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, analysis_id TEXT, error TEXT)""")

    async def start(self, request: OptimizationRequest):
        device_id = self.devices.resolve_device(request.device_id)
        self.devices.repository.get(device_id)
        job_id = str(uuid.uuid4())
        with self.store.connect() as db:
            db.execute("INSERT INTO policy_analysis_jobs VALUES (?,?,NULL,NULL)", (job_id, "running"))
        task = asyncio.create_task(self._run(job_id, request.model_copy(update={"device_id": device_id})))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))
        return {"job_id": job_id, "status": "running", "next_action": "get_app_control_analysis",
                "read_only": True}

    async def _run(self, job_id, request):
        try:
            async with asyncio.timeout(180):
                snapshot = await self.collector.capture(request.device_id)
                candidates, findings = await asyncio.to_thread(propose, snapshot, request)
                analysis = self.store.create(request.device_id, snapshot, candidates, findings)
            with self.store.connect() as db:
                db.execute("UPDATE policy_analysis_jobs SET state='completed',analysis_id=? WHERE id=?",
                           (analysis["id"], job_id))
        except asyncio.CancelledError:
            with self.store.connect() as db:
                db.execute("UPDATE policy_analysis_jobs SET state='interrupted',error=? WHERE id=?",
                           ("服务停止，分析未完成；未修改设备", job_id))
            raise
        except Exception as exc:
            # Do not leak credentials, device responses or transport internals.
            error = str(exc) if isinstance(exc, ValueError) else "采集或分析失败；未修改设备"
            with self.store.connect() as db:
                db.execute("UPDATE policy_analysis_jobs SET state='failed',error=? WHERE id=?", (error, job_id))

    def status(self, job_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM policy_analysis_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("Analysis job not found")
        result = dict(row)
        if result["state"] == "running" and job_id not in self.tasks:
            # A persisted job without a local owner may belong to another server
            # process. Do not restart or mark it terminal from this process.
            result["local_owner"] = False
        if result["analysis_id"]:
            result.update(self.summary(result["analysis_id"]))
        return result

    def summary(self, analysis_id):
        analysis = self.store.get(analysis_id)
        return {"analysis_id": analysis_id, "proposal_hash": analysis["proposal_hash"],
                "snapshot_hash": analysis["snapshot_hash"], "candidate_count": len(analysis["candidates"]),
                "findings": analysis["findings"], "execution_ready": False,
                "review_url": f"/api/app-control/analyses/{analysis_id}/review.csv",
                "findings_url": f"/api/app-control/analyses/{analysis_id}/findings.csv",
                "instructions": "先下载审核表，仅修改用户决定与用户备注，再提交审核。采纳不等于执行授权。"}

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def verify_review(self, review_id):
        review = self.store.get_review(review_id)
        analysis = self.store.get(review["analysis_id"])
        combined = materialize(analysis, review)
        proof = verify_combination(analysis["snapshot"]["configuration"], combined)
        return {"review_id": review_id, "analysis_id": analysis["id"],
                "selected_ids": review["selected_ids"], "proof": proof,
                "contains_behavior_change": any(c.get("change_class") == "behavior_change"
                                                for c in analysis["candidates"] if c["id"] in review["selected_ids"]),
                "result_configuration_hash": fingerprint(combined),
                "execution_authorized": False, "next_action": "device_adapter_qualification_required"}

    async def preflight_review(self, review_id):
        """Read-only, live preflight. Never converts a review into a write plan.

        The two-read capture detects changes during this read, but cannot lock the
        device against another administrator. A future executor must repeat this
        check immediately before every serialized write and verify every readback.
        """
        review = self.store.get_review(review_id)
        analysis = self.store.get(review["analysis_id"])
        combined = materialize(analysis, review)
        proof = verify_combination(analysis["snapshot"]["configuration"], combined)
        baseline = analysis["snapshot"]
        expected = baseline.get("configuration_hash")
        if not expected or baseline.get("device_id") != analysis["device_id"]:
            raise ValueError("Analysis has no qualified device snapshot fingerprint")
        recomputed = fingerprint({"device_id": analysis["device_id"],
                                  "namespace": baseline.get("namespace"),
                                  "configuration": baseline["configuration"]})
        if expected != recomputed:
            raise ValueError("Persisted analysis snapshot fingerprint is inconsistent")
        current = await self.collector.capture(analysis["device_id"])
        if current.get("device_id") != analysis["device_id"] or current.get("namespace") != baseline.get("namespace"):
            raise ValueError("Live capture belongs to another device or namespace")
        live_hash = current.get("configuration_hash")
        if live_hash != fingerprint({"device_id": current["device_id"],
                                     "namespace": current["namespace"],
                                     "configuration": current["configuration"]}):
            raise ValueError("Live snapshot fingerprint is inconsistent")
        unchanged = live_hash == expected
        blockers = []
        if not review["selected_ids"]:
            blockers.append("没有已采纳变更")
        if not unchanged:
            blockers.append("设备配置与审核快照不同；必须重新分析和审核")
        if proof["status"] != "model_equivalent":
            blockers.append("组合方案未获完整模型等价证明")
        if any(c.get("change_class") == "behavior_change" for c in analysis["candidates"]
               if c["id"] in review["selected_ids"]):
            blockers.append("包含改变访问行为的意图修复，需单独的业务确认和影响验证")
        blockers.extend([
            "尚未认证设备首条匹配、默认动作及可观察附加效果语义",
            "尚未验证本设备版本的对象创建、策略位置移动和读回接口行为",
            "尚未生成并逐步证明可安全执行的串行变更及恢复计划",
        ])
        return {"review_id": review_id, "analysis_id": analysis["id"],
                "device_id": analysis["device_id"], "snapshot_unchanged": unchanged,
                "reviewed_snapshot_hash": expected, "live_snapshot_hash": live_hash,
                "model_proof": proof, "blockers": blockers,
                "execution_ready": False, "execution_authorized": False,
                "device_writes": 0, "next_action": "qualify_device_adapter_or_reanalyze" if unchanged else "reanalyze"}

    @staticmethod
    def feedback_summary(document):
        comparison = document.get("comparison") or {}
        selected_results = [item for item in comparison.get("candidate_results", []) if item["selected"]]
        return {"feedback_id": document["feedback_id"], "review_id": document["review_id"],
                "analysis_id": document["analysis_id"], "device_id": document["device_id"],
                "created_at": document["created_at"], "status": document["status"],
                "engineer_label": document["engineer_declaration"]["engineer_label"],
                "change_ticket": document["engineer_declaration"]["change_ticket"],
                "baseline_configuration_hash": document["baseline_configuration_hash"],
                "recommended_configuration_hash": document["recommended_configuration_hash"],
                "observed_configuration_hash": document.get("observed", {}).get("configuration_hash"),
                "observed_alignment": comparison.get("status", "unknown"),
                "candidate_results": selected_results[:20],
                "candidate_results_total": len(selected_results),
                "candidate_results_truncated": len(selected_results) > 20,
                "counts": comparison.get("counts", {}),
                "feedback_hash": document["feedback_hash"], "device_writes": 0,
                "log_url": f"/api/app-control/feedback/{document['feedback_id']}.json"}

    async def record_feedback(self, request: FeedbackRequest):
        review = self.store.get_review(request.review_id)
        analysis = self.store.get(review["analysis_id"])
        candidates = {item["id"] for item in analysis["candidates"]}
        reported = [item.candidate_id for item in request.feedback_items]
        if len(reported) != len(set(reported)) or set(reported) - candidates:
            raise ValueError("Engineer feedback contains unknown or duplicate candidate IDs")
        request_hash = fingerprint(request.model_dump(exclude={"submission_key"}))
        prior = self.store.feedback_by_key(request.review_id, request.submission_key)
        if prior:
            if prior["request_hash"] != request_hash:
                raise ValueError("Feedback key reused with different input")
            return self.feedback_summary(prior["feedback"])
        recommended = materialize(analysis, review)
        baseline = analysis["snapshot"]
        expected_hash = fingerprint({"device_id": analysis["device_id"],
                                     "namespace": baseline.get("namespace"),
                                     "configuration": baseline["configuration"]})
        if (baseline.get("device_id") != analysis["device_id"] or not baseline.get("namespace")
                or baseline.get("configuration_hash") != expected_hash):
            raise ValueError("Analysis has no consistent device snapshot fingerprint")
        document = {"schema_version": 1, "analysis_id": analysis["id"],
                    "device_id": analysis["device_id"], "namespace": baseline["namespace"],
                    "analysis_snapshot_hash": analysis["snapshot_hash"],
                    "baseline_configuration_hash": expected_hash,
                    "proposal_hash": analysis["proposal_hash"],
                    "recommended_configuration_hash": fingerprint({"device_id": analysis["device_id"],
                                                                     "namespace": baseline["namespace"],
                                                                     "configuration": recommended}),
                    "reviewed_model_proof": verify_combination(baseline["configuration"], recommended),
                    "selected_ids": review["selected_ids"],
                    "engineer_declaration": {"source": "self_report_not_authenticated",
                                             "engineer_label": request.engineer_label,
                                             "change_ticket": request.change_ticket,
                                             "note": request.note,
                                             "feedback_items": [item.model_dump() for item in request.feedback_items]},
                    "read_only": True, "device_writes": 0,
                    "limitations": ["配置差异不能证明是谁修改了设备或修改原因",
                                    "结构一致不代表生产流量行为已获设备版本认证",
                                    "首次快照和本次快照之间的其他管理员变更也会体现在日志中"]}
        try:
            observed = await self.collector.capture(analysis["device_id"])
            if observed.get("device_id") != analysis["device_id"] or observed.get("namespace") != baseline["namespace"]:
                raise ValueError("Live capture belongs to another device or namespace")
            observed_hash = fingerprint({"device_id": observed["device_id"],
                                         "namespace": observed["namespace"],
                                         "configuration": observed["configuration"]})
            if observed.get("configuration_hash") != observed_hash:
                raise ValueError("Live snapshot fingerprint is inconsistent")
            document["observed"] = {"captured_at": observed.get("captured_at"),
                                    "configuration_hash": observed_hash,
                                    "consistency": observed.get("consistency", "not_reported")}
            document["comparison"] = compare_configurations(analysis, review, recommended, observed)
            document["status"] = "comparison_complete"
        except Exception as exc:
            # A failed capture/comparison is itself an auditable outcome. Do not
            # expose transport responses, credentials, or claim a successful diff.
            document["status"] = "comparison_unavailable"
            document["error"] = str(exc) if isinstance(exc, ValueError) else "设备读取或比较失败"
        saved = self.store.save_feedback(request.review_id, request.submission_key, request_hash, document)
        return self.feedback_summary(saved)

    def get_feedback(self, feedback_id):
        return self.feedback_summary(self.store.get_feedback(feedback_id))

    def list_feedback(self, review_id, offset=0, limit=50):
        return {"review_id": review_id, "offset": offset,
                "items": [self.feedback_summary(item) for item in self.store.list_feedback(review_id, offset, limit)]}
