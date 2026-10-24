"""Read-only retained-source adoption for the trusted external Full contract.

The existing store verifies representations and selections. This module neither
fetches an origin nor authenticates an upstream source, grants rights, or accepts
the default Full workflow. Its caller supplies an already verified workpaper.
"""
from __future__ import annotations

from dataclasses import asdict

from decision_kernel.domain.records import RecordError
from decision_kernel.domain.research_work import ResearchProgress, ResearchRequest

EXTERNAL_AUDIT_SCHEMA = "research-external-source-use-audit-v1"
EXTERNAL_USE_DEMONSTRATED = "external-evidence-use-demonstrated"


def external_source_guidance() -> dict:
    return {
        "policy": "external-research-sources-v1", "kind": "full",
        "available_from_start": True, "gap_required": False,
        "instruction": "沿原委托和已有权限取得适用来源，以确切SourceRef保留获准表示；"
                       "区分provided、excerpt与external，保留定位、时间、转换与限制。"
                       "在findings/challenges中用evidence下标记录实际采用，保存后核查；"
                       "仅外部定位不能证明已保留原文，不为核查扩大取得、保管或公开权限。",
        "guide": "docs/research/source-use-audit.md", "operation": "audit-source-use",
        "execution_authority": "existing-user-mandate-and-host-permission-not-granted-by-preparation",
        "publication_permission": "not-granted",
        "acceptance": "retained-representation-and-author-adoption-only",
    }


def external_execution_contract(request: ResearchRequest, method: dict) -> dict:
    return {
        "schema": "external-full-execution-contract-v1",
        "method": {"id": method["id"], "sha256": method["sha256"]},
        "required_for_full": request.kind == "full",
        "required_actions": [
            {"action": "retain-authorized-external-evidence", "required": True,
             "instruction": "通过现有保管层保留有权保存的来源表示及限制；已有确切引用可直接复用。"},
            {"action": "record-author-evidence-adoption", "required": True,
             "instruction": "以findings/challenges的evidence下标指向实际采用的sources，保留未决问题和挑战。"},
            {"action": "verify-saved-external-evidence-use", "operation": "audit-source-use",
             "input": "reference", "required": True, "guide": "docs/research/source-use-audit.md"},
        ],
        "workflow_acceptance": "not-demonstrated", "automatic_research": False,
        "partial_research_save_allowed": True, "publication_permission": "not-granted",
        "default_full_acceptance": "not-assessed", "human_acceptance": "not-confirmed",
        "instruction": "保存后仅核确切来源表示可读及作者声明的采用；研究充分性、默认Full执行和人工接受分别待验。",
    }


def external_acceptance_pending(reference: dict, method: dict) -> dict:
    return {
        "status": "not-demonstrated", "operation": "audit-source-use", "reference": reference,
        "method": {"id": method["id"], "sha256": method["sha256"]},
        "reason": "saved-research-is-not-proof-of-external-evidence-adoption",
        "partial_research_save_allowed": True,
        "default_full_acceptance": "not-assessed", "human_acceptance": "not-confirmed",
    }


def audit_external_source_use(store, *, reference: dict, request: ResearchRequest,
                              progress: ResearchProgress, method: dict) -> dict:
    """Assess the exact saved workpaper selected and authenticated by SourceUseAudit."""
    adopted = {i for item in (*progress.findings, *progress.challenges) for i in item.evidence}
    sources = []
    for index, ref in enumerate(progress.sources):
        row = {"index": index, "reference": asdict(ref), "adopted_by_author": index in adopted,
               "original_verified": False}
        try:
            view = store.read_source(ref)
            row.update(custody=view["manifest"]["input"]["custody"], status=view["availability"],
                       limitation_count=len(view["limitations"]), limitations_operation="read-source")
        except RecordError:
            row.update(status="unavailable", error="SOURCE_USE_SOURCE_UNAVAILABLE")
        sources.append(row)
    gaps = []
    if not sources:
        gaps.append("NO_SOURCE_REFERENCES")
    if not adopted:
        gaps.append("NO_SOURCE_ADOPTED")
    if not any(row["status"] == "stored-representation" and row["adopted_by_author"] for row in sources):
        gaps.append("NO_READABLE_ADOPTED_SOURCE")
    if any(row["status"] == "unavailable" for row in sources):
        gaps.append("SOURCE_REFERENCE_UNAVAILABLE")
    return {
        "schema": EXTERNAL_AUDIT_SCHEMA,
        "status": EXTERNAL_USE_DEMONSTRATED if not gaps else "needs-action",
        "case_id": request.case_id, "reference": reference,
        "method": {"id": method["id"], "sha256": method["sha256"]},
        "sources": sources, "gaps": gaps,
        "acceptance_scope": "retained-representation-and-author-adoption-only",
        "adoption": "author-evidence-indices-and-exact-stored-version-not-economic-approval",
        "source_rights": "not-granted-by-audit", "publication": "not-authorized-by-audit",
        "persistence": "not-written", "research_assessment": "not-performed",
        "default_full_acceptance": "not-assessed", "full_product_acceptance": "not-performed",
        "human_acceptance": "not-confirmed",
    }
