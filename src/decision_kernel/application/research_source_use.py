"""Full's default work instructions and bounded, read-only source-use assessment.

This is not a research executor, a second ledger, or economic approval. Native
receipt authentication is injected by the host; user status declarations cannot
substitute for it. Partial research remains saveable with its original contract.
"""
from __future__ import annotations

from dataclasses import asdict

from decision_kernel.domain.records import BlobRef, RecordError, SourceRef, checked_text, digest, encoded, require
from decision_kernel.domain.research_work import ResearchProgress, ResearchRequest, read_workpaper


def execution_contract(request: ResearchRequest, evidence: dict | None = None) -> dict:
    evidence = evidence or {}
    queried = evidence.get("schema") == "research-evidence-page-v1"
    interface = evidence.get("interface_sources", {})
    actions = []
    if not queried:
        actions.append({"action": "execute-pool-plan", "operation": "prepare", "required": True,
                        "request": asdict(request), "input": "evidence_plan",
                        "instruction": "从原委托提取公司/代码/别名和主题，自主生成计划并实际调用；不要让用户逐项提供引用。"})
    elif not evidence.get("coverage", {}).get("query_plan_complete"):
        actions.append({"action": "continue-or-account-for-query-boundary", "required": True,
                        "payload": evidence.get("next_prepare_payload"),
                        "instruction": "沿原游标继续或说明真实限制；未返回不等于无资料，不循环重试拒绝。"})
    if interface.get("status") != "checked-not-executed":
        actions.append({"action": "review-relay-and-select-plan-or-reuse", "required": True,
                        "guide": "docs/research/source-use.md"})
    else:
        actions.append({"action": "capture-or-justify-reuse", "required": True,
                        "workflow": "relay-source-runtime.yml", "family": "research",
                        "source_plan": interface["plan"], "permission": "existing-mandate-and-public-rights-required"})
    actions.append({"action": "verify-adoption-before-workflow-acceptance", "operation": "audit-source-use",
                    "required": True, "guide": "docs/research/source-use-audit.md"})
    return {"schema": "full-execution-contract-v1", "required_for_full": request.kind == "full",
            "pool": "not-executed" if not queried else evidence.get("status"),
            "relay": interface.get("status", "not-reviewed"), "required_actions": actions,
            "workflow_acceptance": "not-demonstrated", "automatic_research": False,
            "partial_research_save_allowed": True, "publication_permission": "not-granted",
            "instruction": "先实际prepare取材，与Relay并列核验；仅读说明或生成报告不能签系统Full闭环。"}


def acceptance_pending(reference: dict) -> dict:
    return {"status": "not-demonstrated", "operation": "audit-source-use", "reference": reference,
            "reason": "saved-research-or-inventory-is-not-proof-of-pool-and-relay-use",
            "partial_research_save_allowed": True}


def scope(request: ResearchRequest) -> bytes:
    # Continuation may advance the predecessor without changing the user's scope.
    return encoded({k: v for k, v in asdict(request).items() if k not in ("predecessor", "scope_change_reason")})


class SourceUseAudit:
    def __init__(self, store, *, receipt_reader=None, registry=None, payload_decoder=None):
        self.store, self.receipt_reader, self.registry = store, receipt_reader, registry
        self.payload_decoder = payload_decoder

    def audit(self, payload: dict) -> dict:
        require(isinstance(payload, dict) and set(payload) <= {
            "request", "progress", "reference", "receipts", "exceptions", "reuse_run_ids", "relay_recoveries"}, "SOURCE_USE_INPUT_INVALID")
        if "reference" in payload:
            require(not ({"request", "progress"} & set(payload)), "SOURCE_USE_ONE_RESEARCH_SELECTOR")
            ref = BlobRef(**payload["reference"])
            work = read_workpaper(self.store.read_revision(ref))
            require(work is not None, "SOURCE_USE_WORKPAPER_REQUIRED")
            request, progress = work
            reference = asdict(ref)
        else:
            require({"request", "progress"} <= set(payload), "SOURCE_USE_ONE_RESEARCH_SELECTOR")
            request = ResearchRequest.from_dict(payload["request"])
            progress = ResearchProgress.from_dict(payload["progress"])
            reference = None
        progress.validate_for(request)
        receipts, exceptions = payload.get("receipts", []), payload.get("exceptions", {})
        require(isinstance(receipts, list) and len(receipts) <= 8, "SOURCE_USE_RECEIPT_LIMIT")
        require(isinstance(exceptions, dict) and set(exceptions) <= {"pool", "relay", "custody"}, "SOURCE_USE_EXCEPTIONS_INVALID")
        for reason in exceptions.values():
            checked_text(reason, limit=4096)
        reuse = payload.get("reuse_run_ids", [])
        require(isinstance(reuse, list) and len(reuse) <= 8 and all(type(i) is int and i > 0 for i in reuse)
                and len(reuse) == len(set(reuse)) and (not reuse or "relay" in exceptions), "SOURCE_USE_REUSE_REASON_REQUIRED")
        require(len(encoded(payload)) <= 2 * 1024 * 1024, "SOURCE_USE_INPUT_TOO_LARGE")
        observed, rejected, pool_pages, captures, plans, relay_results = [], [], [], [], [], []
        seen = set()
        for proof in receipts:
            try:
                require(self.receipt_reader is not None, "SOURCE_USE_NATIVE_READER_REQUIRED")
                verified = self.receipt_reader.read(proof)
                ident = verified["identity"]
                key = (ident["run_id"], ident["run_attempt"], ident["artifact_id"])
                require(key not in seen, "SOURCE_USE_DUPLICATE_RECEIPT")
                seen.add(key)
                if verified["family"] == "pool":
                    page = self._pool(verified, request)
                    if page is not None:
                        pool_pages.append(page)
                    stage = "pool-query" if page is not None else "method-only-not-pool-query"
                else:
                    stage, checked, outcomes = self._relay(verified, request, ident["run_id"] in reuse)
                    relay_results.extend(outcomes)
                    plans.append(ident)
                    captures.extend(checked)
                observed.append({**ident, "stage": stage})
            except (RecordError, KeyError, TypeError, ValueError, AttributeError) as error:
                # No source prose, arbitrary IDs, or credential-bearing exception messages.
                code = str(error) if isinstance(error, RecordError) else "SOURCE_USE_RECEIPT_INVALID_SHAPE"
                safe = code.isascii() and code.replace("_", "").isalnum() and len(code) <= 96
                rejected.append({"index": len(observed) + len(rejected),
                                 "error": code if safe else "SOURCE_USE_RECEIPT_NOT_VERIFIED"})
        from decision_kernel.application.research_evidence import version_key
        pool_state, pool_history, current_pool = self._pool_recovery(pool_pages)
        matched = {version_key(SourceRef.from_dict(m["reference"])) for p in current_pool for m in p["matches"]}
        adopted = {i for item in (*progress.findings, *progress.challenges) for i in item.evidence}
        expected_versions = {digest(encoded(asdict(source))) for result in captures for source in self._exports(result)}
        sources = []
        for index, ref in enumerate(progress.sources):
            row = {"index": index, "reference": asdict(ref), "adopted_by_author": index in adopted,
                   "pool_match": version_key(ref) in matched, "relay_capture_match": False}
            try:
                view = self.store.read_source(ref)
                row.update(status=view["availability"], original_verified=False,
                           relay_capture_match=view["manifest"]["version_id"] in expected_versions,
                           limitation_count=len(view["limitations"]), limitations_operation="read-source")
            except RecordError:
                row.update(status="unavailable", error="SOURCE_USE_SOURCE_UNAVAILABLE")
            sources.append(row)
        from decision_kernel.application.research_source_recovery import relay_recovery
        recovery = relay_recovery(relay_results, payload.get("relay_recoveries", []))
        relay_state = recovery["state"]
        good = [s for s in sources if s["status"] == "stored-representation" and s["adopted_by_author"]]
        gaps = []
        if pool_state not in ("queried", "queried-empty"):
            gaps.append("POOL_USE_NOT_COMPLETE")
        if recovery["rejected_corrections"]:
            gaps.append("RELAY_RECOVERY_NOT_VERIFIED")
        if recovery["field_mapping_review_required"]:
            gaps.append("RELAY_FIELD_CORRECTION_REQUIRES_REVIEW")
        if relay_state not in ("captured", "captured-with-corrections"):
            gaps.append("RELAY_CAPTURE_OR_REUSE_NOT_DEMONSTRATED")
        if matched and not any(s["pool_match"] for s in good):
            gaps.append("POOL_MATCHES_NOT_ADOPTED")
        if expected_versions and not any(s["relay_capture_match"] for s in good):
            gaps.append("RELAY_CAPTURE_NOT_ADOPTED")
        if not good:
            gaps.append("NO_READABLE_ADOPTED_SOURCE")
        if any(s["status"] == "unavailable" for s in sources):
            gaps.append("SOURCE_REFERENCE_UNAVAILABLE")
        if rejected:
            gaps.append("UNVERIFIED_RECEIPTS_PRESENT")
        # Reasons are reviewable exceptions, never successful execution or automatic approval.
        status = "usage-demonstrated" if not gaps else "exceptions-require-review" if exceptions else "needs-action"
        return {"schema": "research-source-use-audit-v1", "status": status, "case_id": request.case_id,
                "reference": reference, "request_sha256": digest(scope(request)),
                "progress_sha256": digest(encoded(asdict(progress))), "pool": pool_state, "relay": relay_state,
                "receipts": observed, "rejected_receipts": rejected, "relay_results": relay_results,
                "sources": sources, "gaps": gaps, "relay_recovery": recovery, "pool_history":pool_history,
                "exceptions": exceptions, "exception_acceptance": "not-performed",
                "adoption": "author-evidence-indices-and-exact-stored-version-not-economic-approval",
                "publication": "not-authorized-by-audit", "persistence": "not-written",
                "research_assessment": "not-performed", "full_product_acceptance": "not-performed"}

    def _pool(self, verified, request):
        docs, ident = verified["documents"], verified["identity"]
        report, inputs = docs["report.json"], docs["inputs.json"]
        require(inputs["operation"] == report["operation"] == "prepare"
                and inputs["code_sha"] == report["code_sha"] == ident["code_sha"]
                and report["repository"] == ident["repository"] and report["branch"] == "records"
                and report["run_id"] == str(ident["run_id"]) and report["run_attempt"] == str(ident["run_attempt"])
                and report["status"] == "completed" and report["write"] == "not-requested"
                and verified["jobs"]["read"]["conclusion"] == "success", "SOURCE_USE_PREPARE_IDENTITY")
        require(self.payload_decoder is not None, "SOURCE_USE_PAYLOAD_DECODER_REQUIRED")
        original = self.payload_decoder(inputs["payload"])
        require(digest(encoded(original)) == report["payload_sha256"], "SOURCE_USE_PAYLOAD_MISMATCH")
        result = report["result"]
        require(scope(ResearchRequest.from_dict(result["request"])) == scope(request), "SOURCE_USE_CASE_MISMATCH")
        evidence = result.get("evidence", {})
        if evidence.get("schema") != "research-evidence-page-v1":
            return None
        from decision_kernel.application.research_evidence import validate_plan
        normalized = validate_plan(original["evidence_plan"], request)
        require(encoded(normalized) == encoded(evidence["plan"])
                and digest(encoded(normalized)) == evidence["plan_sha256"]
                and digest(encoded(asdict(ResearchRequest.from_dict(result["request"])))) == evidence["request_sha256"],
                "SOURCE_USE_PLAN_IDENTITY")
        require(evidence["repository"] == ident["repository"], "SOURCE_USE_POOL_REPOSITORY")
        return {**evidence, "input_cursor": original.get("evidence_cursor"), "receipt":ident}

    def _relay(self, verified, request, reuse):
        from decision_kernel.application.research_sources import inspect_result, validate_plan
        docs, ident = verified["documents"], verified["identity"]
        selected, result = docs["request.json"], docs["result.json"]
        require(selected.get("family") == "research" and selected["code_sha"] == result["code_sha"] == ident["code_sha"]
                and selected["source_plan"] == docs["source-plan.json"]
                and (reuse or selected["source_plan"]["case_id"] == request.case_id), "SOURCE_USE_RELAY_CASE_MISMATCH")
        registry = self._registry()
        checked = validate_plan(selected["source_plan"], registry)
        require(encoded(checked) == encoded(docs["plan.json"]), "SOURCE_USE_RELAY_PLAN_CHANGED")
        if selected["operation"] == "plan":
            require(result["status"] == "planned-not-captured", "SOURCE_USE_RELAY_PLAN_STATUS")
            return "relay-plan-only", [], [self._attempt(ident, selected, i, p, "planned-not-captured")
                                            for i,p in enumerate(checked["requests"])]
        require(selected["operation"] == "capture" and selected["public_rights_confirmed"] is True
                and result["status"] in ("capture-returned", "capture-incomplete"), "SOURCE_USE_RELAY_NOT_CAPTURED")
        step = [s for s in verified["jobs"]["source"].get("steps", [])
                if s["name"] == "Explicit source capture with the repository secret"]
        require(len(step) == 1 and step[0]["conclusion"] in ("success", "failure"), "SOURCE_USE_CAPTURE_NOT_EXECUTED")
        rows = docs.get("capture.jsonl", [])
        require(isinstance(rows, list) and len(rows) <= len(checked["requests"]), "SOURCE_USE_CAPTURE_SHAPE")
        usable, outcomes = [], []
        for i, planned in enumerate(checked["requests"]):
            outcome = self._attempt(ident, selected, i, planned, "not-returned")
            if i < len(rows):
                row = rows[i]
                require(row["case_id"] == selected["source_plan"]["case_id"]
                        and encoded(row["use"]) == encoded(planned), "SOURCE_USE_CAPTURE_SCOPE_CHANGED")
                if row.get("capture") is not None:
                    inspected = inspect_result(row, registry)
                    outcome.update(state="complete" if inspected["complete"] else "incomplete",
                                   row_count=inspected["row_count"], error=inspected["error"])
                    usable.append(row)
                else:
                    outcome["state"] = "not-attempted"
            outcomes.append(outcome)
        if result["status"] == "capture-returned":
            require(verified["jobs"]["source"]["conclusion"] == "success"
                    and all(o["state"] == "complete" for o in outcomes), "SOURCE_USE_CAPTURE_STATUS_MISMATCH")
        return ("relay-reused-native-capture" if reuse else "relay-capture"), usable, outcomes

    @staticmethod
    def _attempt(identity, selected, index, planned, state):
        return {"api": planned["api"], "query_key": planned["query_key"], "state": state,
                "run_id":identity["run_id"], "run_attempt":identity["run_attempt"], "query_index":index,
                "started_at":identity["started_at"], "case_id":selected["source_plan"]["case_id"],
                "parameters":planned["parameters"], "stage":planned["stage"], "purpose":planned["purpose"]}

    def _registry(self):
        if self.registry is None:
            from decision_kernel.application.relay_registry import RelayRegistry
            self.registry = RelayRegistry("docs/sources/relay-registry.json")
        return self.registry

    def _exports(self, result):
        if not result["capture"]["complete"]:
            return []
        from decision_kernel.application.research_sources import source_inputs
        return source_inputs(result, self._registry())

    @classmethod
    def _pool_recovery(cls, pages):
        """Keep history; evaluate the newest observed snapshot of each SAME plan.

        Each selected snapshot must still have a complete fixed cursor chain. A
        newer failed/unrooted scan cannot borrow completeness from an older one.
        Same-cursor retry results use native run start order, not caller list order.
        """
        from datetime import datetime
        def stamp(page):
            receipt = page["receipt"]
            return (datetime.fromisoformat(receipt["started_at"].replace("Z", "+00:00")),
                    receipt["run_id"], receipt["run_attempt"])
        groups = {}
        for page in pages:
            groups.setdefault((page["plan_sha256"],page["request_sha256"],page["snapshot"]), []).append(page)
        selected, history = {}, []
        for key, group in groups.items():
            cursors = {}
            for page in group:
                cursor = encoded(page["input_cursor"])
                if cursor not in cursors or stamp(page) > stamp(cursors[cursor]):
                    cursors[cursor] = page
            current = list(cursors.values())
            # The latest root identifies a new observation. Unrooted newer groups
            # stay unproven rather than borrowing earlier pages from another tree.
            roots = [p for p in current if p["input_cursor"] is None]
            epoch = max(map(stamp, roots or current))
            if key[0] not in selected or epoch > selected[key[0]][0]:
                selected[key[0]] = (epoch,current)
            history.extend({"run_id":p["receipt"]["run_id"], "run_attempt":p["receipt"]["run_attempt"],
                "snapshot":p["snapshot"], "plan_sha256":p["plan_sha256"], "status":p["status"],
                "query_plan_complete":p["coverage"]["query_plan_complete"]} for p in group)
        current = [p for _,group in selected.values() for p in group]
        return cls._pool_state(current), history, current

    @staticmethod
    def _pool_state(pages):
        if not pages:
            return "not-executed"
        # Each plan/snapshot must show the beginning and a terminal page. A terminal
        # page alone or a forged cursor cannot claim unseen earlier pages were read.
        groups = {}
        for page in pages:
            groups.setdefault((page["plan_sha256"], page["request_sha256"], page["snapshot"]), []).append(page)
        for group in groups.values():
            if any(p["status"] == "search-unavailable" for p in group):
                return "query-failed"
            # The input cursor comes from the authenticated original payload, never
            # from a caller claiming that the earlier pages were already read.
            if not any(p.get("input_cursor", "legacy") is None for p in group):
                return "query-coverage-unproven"
            cursor, visited = None, set()
            while True:
                candidates = [(i, p) for i, p in enumerate(group) if i not in visited
                              and encoded(p.get("input_cursor", "legacy")) == encoded(cursor)]
                if len(candidates) != 1:
                    return "query-partial"
                i, page = candidates[0]; visited.add(i)
                cursor = page["next_cursor"]
                if cursor is None:
                    if not page["coverage"]["query_plan_complete"] or len(visited) != len(group):
                        return "query-partial"
                    break
        return "queried" if any(p["matches"] for p in pages) else "queried-empty"
