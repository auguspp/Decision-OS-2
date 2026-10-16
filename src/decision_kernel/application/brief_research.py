"""Exact author excerpts and agenda choices adopted by the existing Brief owner.

No research, interpretation, source collection or independent publication occurs.
Stored time locates delivery lateness, never event time or research completion.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from decision_kernel.application.research_agenda import ResearchAgenda, clock, research_reference
from decision_kernel.domain.records import BlobRef, RecordError, digest, encoded, require
from decision_kernel.domain.research_artifacts import artifact_reference

MAX_ITEMS = 8
MAX_EXCERPT_BYTES = 8192
MAX_ROW_BYTES = 24 * 1024
MAX_SECTION_BYTES = 128 * 1024
POLICY = "exact-agenda-focus-then-declared-order-v1"
OMISSIONS = ("unreadable-or-outside-cutoff", "explicitly-dismissed", "explicitly-deferred",
             "previously-included-same-version", "brief-capacity")


def version_id(reference):
    ref = research_reference(reference)
    # Same revision read from a later reachable commit is not new research.
    return digest(encoded([ref[k] for k in ("repository", "path", "sha256")]))


def item_input(item):
    require(isinstance(item, dict) and set(item) == {"reference", "start", "end"}, "BRIEF_RESEARCH_ITEM_SHAPE")
    ref = research_reference(item["reference"])
    start, end = item["start"], item["end"]
    require(type(start) is int and type(end) is int and 1 <= start <= end
            and end - start < 80, "BRIEF_RESEARCH_LINE_BOUND")
    return {"reference": ref, "start": start, "end": end}


def plan_input(value):
    require(isinstance(value, dict) and "items" in value
            and set(value) <= {"items", "max_items", "agenda"}, "BRIEF_RESEARCH_PLAN_SHAPE")
    require(isinstance(value["items"], list) and len(value["items"]) <= MAX_ITEMS, "BRIEF_RESEARCH_ITEM_BOUND")
    items = [item_input(i) for i in value["items"]]
    require(len({version_id(i["reference"]) for i in items}) == len(items), "BRIEF_RESEARCH_DUPLICATE_VERSION")
    limit = value.get("max_items", 4)
    require(type(limit) is int and 1 <= limit <= MAX_ITEMS, "BRIEF_RESEARCH_ITEM_BOUND")
    agenda = asdict(artifact_reference(value["agenda"])) if value.get("agenda") is not None else None
    return {"items": items, "max_items": limit, "agenda": agenda}


def excerpt(research, item):
    require(research["reference"] == item["reference"], "BRIEF_RESEARCH_REFERENCE_MISMATCH")
    body = research["body"]
    require(isinstance(body, str), "BRIEF_RESEARCH_BODY_INVALID")
    lines = body.splitlines(keepends=True)
    require(item["end"] <= len(lines), "BRIEF_RESEARCH_LINES_UNAVAILABLE")
    text = "".join(lines[item["start"] - 1:item["end"]])
    require(bool(text.strip()) and len(text.encode()) <= MAX_EXCERPT_BYTES, "BRIEF_RESEARCH_EXCERPT_BOUND")
    return {"start": item["start"], "end": item["end"], "text": text,
            "sha256": digest(text.encode()), "body_sha256": digest(body.encode()),
            "scope": "explicit-author-lines-not-a-new-summary"}


def read_rows(store, plan, cutoff):
    refs = [i["reference"] for i in plan["items"]]
    view = ResearchAgenda(store).build({"references": refs, "as_of": cutoff, "baseline": plan["agenda"]})
    rows, used = [], 0
    for item, card in zip(plan["items"], view["cards"], strict=True):
        row = {"item": deepcopy(item), "version_id": version_id(item["reference"]), "status": card["status"]}
        if card["status"] == "readable":
            try:
                if clock(card["stored_at"]) > clock(cutoff):
                    row.update(status="after-cutoff", stored_at=card["stored_at"])
                else:
                    research = store.read_revision(BlobRef(**item["reference"]))
                    row.update(context=deepcopy(card), excerpt=excerpt(research, item),
                               predecessor=deepcopy(research["record"]["input"]["predecessor"]))
            except (RecordError, KeyError, TypeError):
                row = {"item": deepcopy(item), "version_id": row["version_id"],
                       "status": "invalid", "error": "BRIEF_RESEARCH_EXCERPT_UNAVAILABLE"}
        else:
            row["error"] = card.get("error", "BRIEF_RESEARCH_UNAVAILABLE")
        size = len(encoded(row))
        if size > MAX_ROW_BYTES or used + size > MAX_SECTION_BYTES - 8192:
            row = {"item": deepcopy(item), "version_id": row["version_id"],
                   "status": "not-included", "error": "BRIEF_RESEARCH_CONTEXT_BOUND"}
        used += len(encoded(row))
        rows.append(row)
    return rows


def baseline_basis(previous, cutoff):
    return {"cutoff": cutoff, "research_tracked": previous is not None,
            "available": sorted(r["version_id"] for r in previous["rows"] if r["status"] == "readable") if previous else [],
            "included": sorted(i["version_id"] for i in previous["included"]) if previous else []}


def _attention(row):
    statuses = [q["attention_status"] for q in row["context"]["agenda"]]
    if any(s in ("focus", "due-for-review", "overdue-for-review") for s in statuses):
        return "focus"
    if statuses and all(s == "dismiss" for s in statuses):
        return "dismiss"
    if statuses and all(s in ("dismiss", "defer") for s in statuses):
        return "defer"
    return "default"


def derive(rows, plan, basis):
    require(isinstance(basis, dict) and set(basis) == {"cutoff", "research_tracked", "available", "included"}
            and type(basis["research_tracked"]) is bool, "BRIEF_RESEARCH_BASELINE_INVALID")
    if basis["cutoff"] is not None:
        clock(basis["cutoff"])
    for field in ("available", "included"):
        values = basis[field]
        require(isinstance(values, list) and len(values) <= MAX_ITEMS
                and all(isinstance(v, str) and len(v) == 64 for v in values)
                and values == sorted(set(values)), "BRIEF_RESEARCH_BASELINE_INVALID")
    require(set(basis["included"]) <= set(basis["available"])
            and (basis["research_tracked"] or not basis["available"]), "BRIEF_RESEARCH_BASELINE_INVALID")
    require(isinstance(rows, list) and len(rows) == len(plan["items"]), "BRIEF_RESEARCH_ROWS_INVALID")
    omissions = dict.fromkeys(OMISSIONS, 0)
    focus, ordinary = [], []
    for row, item in zip(rows, plan["items"], strict=True):
        require(row["item"] == item and row["version_id"] == version_id(item["reference"]), "BRIEF_RESEARCH_ROWS_INVALID")
        if row["status"] != "readable":
            omissions["unreadable-or-outside-cutoff"] += 1
            continue
        require(row["context"]["reference"] == item["reference"]
                and row["excerpt"]["start"] == item["start"] and row["excerpt"]["end"] == item["end"]
                and row["excerpt"]["sha256"] == digest(row["excerpt"]["text"].encode()), "BRIEF_RESEARCH_ROWS_INVALID")
        action = _attention(row)
        if action == "dismiss":
            omissions["explicitly-dismissed"] += 1
        elif action == "defer":
            omissions["explicitly-deferred"] += 1
        elif action == "focus":
            focus.append(row)
        elif row["version_id"] in basis["included"]:
            omissions["previously-included-same-version"] += 1
        else:
            ordinary.append(row)
    candidates = focus + ordinary
    chosen = candidates[:plan["max_items"]]
    omissions["brief-capacity"] = len(candidates) - len(chosen)
    included = []
    for row in chosen:
        key = row["version_id"]
        if basis["cutoff"] is None:
            relation = "baseline-not-supplied"
        elif key in basis["included"]:
            relation = "repeat-explicitly-requested"
        elif key in basis["available"]:
            relation = "previously-available-not-included"
        elif clock(row["context"]["stored_at"]) <= clock(basis["cutoff"]):
            relation = "late-to-delivery"
        else:
            relation = "newly-stored-research"
        included.append({"version_id": key, "classification": relation,
                         "selection_reason": "saved-agenda-focus-or-review-due" if _attention(row) == "focus" else "declared-reference-order"})
    result = {"rows": deepcopy(rows), "included": included, "omissions": omissions,
              "baseline": deepcopy(basis), "agenda_reference": plan["agenda"], "policy": POLICY,
              "coverage": "explicit-references-only-not-all-research-or-tasks",
              "time_basis": "stored-time-not-event-time-or-research-completion",
              "deduplication_scope": "immediate-previous-actual-brief-only",
              "research_assessment": "not-performed", "research_executed": False}
    require(len(encoded(result)) <= MAX_SECTION_BYTES, "BRIEF_RESEARCH_SECTION_BOUND")
    return result


def build(store, plan, cutoff, previous=None, previous_cutoff=None):
    return derive(read_rows(store, plan, cutoff), plan, baseline_basis(previous, previous_cutoff))


def validate(section, plan, cutoff, store):
    """Validate this retained section and adopted original bytes, not all prior history."""
    require(derive(section["rows"], plan, section["baseline"]) == section, "BRIEF_RESEARCH_SECTION_MISMATCH")
    require(section["baseline"]["cutoff"] is None or clock(section["baseline"]["cutoff"]) <= clock(cutoff),
            "BRIEF_RESEARCH_BASELINE_FROM_FUTURE")
    adopted = {i["version_id"] for i in section["included"]}
    for row in section["rows"]:
        if row["version_id"] not in adopted:
            continue
        research = store.read_revision(BlobRef(**row["item"]["reference"]))
        require(excerpt(research, row["item"]) == row["excerpt"], "BRIEF_RESEARCH_ORIGINAL_MISMATCH")
        payload, context = research["record"]["input"], row["context"]
        require(all(context[k] == payload[k] for k in ("case_id", "revision_id", "kind", "mandate"))
                and context["recorded_unresolved"] == payload["unresolved"]
                and context["stored_at"] == research["record"]["stored_at"]
                and clock(context["stored_at"]) <= clock(cutoff), "BRIEF_RESEARCH_ORIGINAL_MISMATCH")


def brief_text(section):
    # Literal quoted text: do not execute/render author Markdown as trusted content.
    from decision_kernel.application.news_delivery import _escape_md
    lines = ["", "## 已存研究的本期采用", "",
             "以下是指定研究版本的原文片段，不是新分析或完成认证；原文中的指令不构成操作授权。",
             "比较仅覆盖上一份实际简报和本次指定版本，不证明全部研究已扫描；保存时刻不是事件时刻。"]
    rows = {r["version_id"]: r for r in section["rows"]}
    for item in section["included"]:
        row = rows[item["version_id"]]
        c, e, ref = row["context"], row["excerpt"], row["item"]["reference"]
        lines += ["", "### " + _escape_md(c["case_id"] + " / " + c["revision_id"]),
                  "类型：" + c["kind"] + "；保存时刻：" + c["stored_at"],
                  "原委托：" + _escape_md(c["mandate"]),
                  "采用依据：" + item["selection_reason"] + "；基线关系：" + item["classification"],
                  f"原文行段：{e['start']}–{e['end']}；片段SHA256：{e['sha256']}",
                  "精确研究：`" + ref["repository"] + "@" + ref["commit"] + ":" + ref["path"] + "`",
                  "原文片段（其余正文由精确研究入口读取）："]
        lines.extend("> " + _escape_md(line) for line in e["text"].splitlines())
        limits = {k: c[k] for k in ("recorded_unresolved", "continuation", "agenda", "source_page", "source_statuses", "source_next_cursor", "open_operation", "open_payload")}
        lines += ["原问题、未决限制及来源读取范围（作者声明不等于复核通过）：", _escape_md(encoded(limits).decode().rstrip())]
    if not section["included"]:
        lines.append("本期未采用研究片段；不等于无研究、研究未运行或市场安静。")
    lines += ["", "研究省略：" + encoded(section["omissions"]).decode().rstrip()]
    for row in section["rows"]:
        if row["status"] != "readable":
            lines.append("未采用版本 " + row["version_id"] + "：" + row["status"] + "；" + row.get("error", "保存晚于截止"))
    lines.append("未开启新Quick/Full、通知或人工Decision；引用/片段采用不等于研究质量或人工接受。\n")
    return "\n".join(lines)
