"""Current external Full discipline with the retained external evidence contract."""
from decision_kernel.domain.records import digest, encoded
from decision_kernel.methods.external_full_v1 import INTAKE
from decision_kernel.methods.full_v3 import full_method

EXTERNAL_METHOD_ID = "decision-os-research-external-full-v2"


def external_full_method() -> dict:
    method = full_method()
    method.pop("sha256")
    method["id"] = EXTERNAL_METHOD_ID
    method["steps"] += [{"topic": topic, "instruction": text} for topic, text in INTAKE]
    method["basis"] += ["docs/research/workflow.md", "docs/research/source-use-audit.md"]
    method["source_use_contract"] = "research-external-source-use-audit-v1"
    method["acceptance_scope"] = "retained-representation-and-author-adoption-only"
    return {**method, "sha256": digest(encoded(method))}
