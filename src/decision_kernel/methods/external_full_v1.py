"""Trusted external Full intake contract; no source, model, or permission plugin."""
from decision_kernel.domain.records import digest, encoded
from decision_kernel.methods.full_v2 import full_method

EXTERNAL_METHOD_ID = "decision-os-research-external-full-v1"

INTAKE = (
    ("external-execution", """由获准外部研究宿主执行并提交实质成果；来源可以由合法连接器、公开取证或已有保管资料取得，不要求通过特定供应商或Pool/Relay入口。保留原委托、问题、事实与推断、最强挑战、取得但未分析的材料和下一行动；更换宿主或本合同不删除原研究的未解问题，也不把原默认方法的未完成执行改记为通过。以下研究纪律仍适用；接收一份成果不是证明这些纪律已经实际完成。"""),
    ("received-representation", """提交实际收到且获准保管的表示，以确切SourceRef固定；区分provided、excerpt与external，注明已知来源定位、取得与披露时间、期间、单位、转换和限制。加工JSON、卡片、摘录不冒充供应商原始响应；所存表示摘要只证明字节一致，不证明上游真实性。访问、保管和公开再分发权限分别成立，本合同不授予任何一项。重要用途的资料不足只限制相依结论，不要求为保存部分研究扩大权限或补造过去取得时间。"""),
    ("external-evidence-use", """findings/challenges的evidence下标指向实际采用的确切sources。audit-source-use对本合同只核已保存研究、可读的provided/excerpt表示及作者声明的采用；摘录保留限制，external仅提供定位和恢复限制。全为外部定位、没有可读被采用表示或来源不可得时，采用证据仍未展示，部分成果仍可保存，不能为过关取得超权全文。机械采用核查不认证正文所有事实、数值用途、经济充分性、默认Full的Pool/Relay执行或人工接受；这些资格分别保持明确待验。"""),
)


def external_full_method() -> dict:
    # Reuse the retained economic discipline without modifying its definition or
    # inheriting the default workflow's provider-specific execution assessment.
    method = full_method()
    method.pop("sha256")
    method["id"] = EXTERNAL_METHOD_ID
    method["steps"] += [{"topic": topic, "instruction": text} for topic, text in INTAKE]
    method["basis"] += ["docs/research/workflow.md", "docs/research/source-use-audit.md"]
    method["source_use_contract"] = "research-external-source-use-audit-v1"
    method["acceptance_scope"] = "retained-representation-and-author-adoption-only"
    return {**method, "sha256": digest(encoded(method))}
