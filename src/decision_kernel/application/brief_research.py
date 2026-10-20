"""Exact author excerpts and agenda choices adopted by the existing Brief owner.

No research, interpretation, source collection or independent publication occurs.
Stored time locates delivery lateness, never event time or research completion.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json

from decision_kernel.application.research_agenda import ResearchAgenda, clock, research_reference
from decision_kernel.domain.records import BlobRef, RecordError, digest, encoded, require
from decision_kernel.domain.research_artifacts import artifact_reference

MAX_ITEMS = 8
MAX_EXCERPT_BYTES = 8192
MAX_ROW_BYTES = 24 * 1024
MAX_SECTION_BYTES = 128 * 1024
POLICY = "exact-agenda-focus-then-declared-order-v1"
WATCH_POLICY = "exact-agenda-with-explicit-watch-evidence-v2"
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


def _watch_rows(row):
    return row.get("context", {}).get("watch_evidence", row.get("watch_evidence", []))


def _watch_omissions(card, reason):
    """Keep the exact selected results visible when their research is omitted."""
    return [{k: deepcopy(w[k]) for k in ("selection", "selection_id", "open_request")} | {
                "status": "not-included", "error": reason}
            for w in card.get("watch_evidence", [])]


def read_rows(store, plan, cutoff):
    refs = [i["reference"] for i in plan["items"]]
    view = ResearchAgenda(store).build({"references": refs, "as_of": cutoff, "baseline": plan["agenda"]})
    rows, used = [], 0
    for item, card in zip(plan["items"], view["cards"], strict=True):
        if "watch_evidence" in view and "watch_evidence" not in card:
            # The agenda may have omitted a whole card at its own display bound.
            # Its retained selections still need exact Brief omission receipts.
            card = {**card, "watch_evidence": [w for w in view["watch_evidence"]
                                               if w["selection"]["reference"] == item["reference"]]}
        row = {"item": deepcopy(item), "version_id": version_id(item["reference"]), "status": card["status"]}
        if card["status"] == "readable":
            try:
                if clock(card["stored_at"]) > clock(cutoff):
                    row.update(status="after-cutoff", stored_at=card["stored_at"])
                else:
                    research = store.read_revision(BlobRef(**item["reference"]))
                    # Workpaper dataclasses contain tuple evidence collections.
                    # Normalize at the JSON artifact boundary, preserving wire bytes,
                    # so an independent reader compares the same data model.
                    row.update(context=json.loads(encoded(card)), excerpt=excerpt(research, item),
                               predecessor=deepcopy(research["record"]["input"]["predecessor"]))
            except (RecordError, KeyError, TypeError):
                row = {"item": deepcopy(item), "version_id": row["version_id"],
                       "status": "invalid", "error": "BRIEF_RESEARCH_EXCERPT_UNAVAILABLE"}
        else:
            row["error"] = card.get("error", "BRIEF_RESEARCH_UNAVAILABLE")
        if "watch_evidence" in card and "context" not in row:
            row["watch_evidence"] = _watch_omissions(card, row.get("error", "BRIEF_RESEARCH_AFTER_CUTOFF"))
        size = len(encoded(row))
        if size > MAX_ROW_BYTES or used + size > MAX_SECTION_BYTES - 8192:
            row = {"item": deepcopy(item), "version_id": row["version_id"],
                   "status": "not-included", "error": "BRIEF_RESEARCH_CONTEXT_BOUND"}
            if "watch_evidence" in card:
                row["watch_evidence"] = _watch_omissions(card, "BRIEF_RESEARCH_CONTEXT_BOUND")
        used += len(encoded(row))
        rows.append(row)
    return rows


def baseline_basis(previous, cutoff):
    result = {"cutoff": cutoff, "research_tracked": previous is not None,
              "available": sorted(r["version_id"] for r in previous["rows"] if r["status"] == "readable") if previous else [],
              "included": sorted(i["version_id"] for i in previous["included"]) if previous else []}
    if previous is not None and "watch_evidence_adoption" in previous:
        result["watch_included"] = sorted({row["selection_id"] for row in previous["watch_evidence_adoption"]
                                           if row["status"] == "included"})
    return result


def _attention(row):
    statuses = [q["attention_status"] for q in row["context"]["agenda"]]
    if any(s in ("focus", "due-for-review", "overdue-for-review") for s in statuses):
        return "focus"
    if statuses and all(s == "dismiss" for s in statuses):
        return "dismiss"
    if statuses and all(s in ("dismiss", "defer") for s in statuses):
        return "defer"
    return "default"


def _watch_attention(row, watch):
    """A selected question's defer/dismiss does not become a whole-case choice."""
    question = watch["selection"]["question_id"]
    if question is None:
        return _attention(row)
    item = next((q for q in row["context"]["agenda"] if q["question_id"] == question), None)
    require(item is not None, "BRIEF_WATCH_QUESTION_NOT_IN_RESEARCH")
    status = item["attention_status"]
    if status in ("focus", "due-for-review", "overdue-for-review"):
        return "focus"
    return status if status in ("defer", "dismiss") else "default"


def derive(rows, plan, basis):
    require(isinstance(basis, dict) and {"cutoff", "research_tracked", "available", "included"} <= set(basis)
            and set(basis) <= {"cutoff", "research_tracked", "available", "included", "watch_included"}
            and type(basis["research_tracked"]) is bool, "BRIEF_RESEARCH_BASELINE_INVALID")
    if basis["cutoff"] is not None:
        clock(basis["cutoff"])
    for field in ("available", "included", "watch_included"):
        if field not in basis:
            continue
        values = basis[field]
        require(isinstance(values, list) and len(values) <= MAX_ITEMS
                and all(isinstance(v, str) and len(v) == 64 for v in values)
                and values == sorted(set(values)), "BRIEF_RESEARCH_BASELINE_INVALID")
    require(set(basis["included"]) <= set(basis["available"])
            and (basis["research_tracked"] or not basis["available"]), "BRIEF_RESEARCH_BASELINE_INVALID")
    require(isinstance(rows, list) and len(rows) == len(plan["items"]), "BRIEF_RESEARCH_ROWS_INVALID")
    omissions = dict.fromkeys(OMISSIONS, 0)
    focus, ordinary, reasons, new_watch = [], [], {}, set()
    for row, item in zip(rows, plan["items"], strict=True):
        require(row["item"] == item and row["version_id"] == version_id(item["reference"]), "BRIEF_RESEARCH_ROWS_INVALID")
        if row["status"] != "readable":
            omissions["unreadable-or-outside-cutoff"] += 1
            reasons[row["version_id"]] = "research-unreadable-or-outside-cutoff"
            continue
        require(row["context"]["reference"] == item["reference"]
                and row["excerpt"]["start"] == item["start"] and row["excerpt"]["end"] == item["end"]
                and row["excerpt"]["sha256"] == digest(row["excerpt"]["text"].encode()), "BRIEF_RESEARCH_ROWS_INVALID")
        action = _attention(row)
        selected_checks = {w["selection_id"] for w in _watch_rows(row)
                           if w["status"] == "readable" and _watch_attention(row, w) not in ("defer", "dismiss")}
        if selected_checks - set(basis.get("watch_included", [])):
            new_watch.add(row["version_id"])
        if action == "dismiss":
            omissions["explicitly-dismissed"] += 1
            reasons[row["version_id"]] = "research-explicitly-dismissed"
        elif action == "defer":
            omissions["explicitly-deferred"] += 1
            reasons[row["version_id"]] = "research-explicitly-deferred"
        elif action == "focus":
            focus.append(row)
        elif row["version_id"] in basis["included"] and row["version_id"] not in new_watch:
            omissions["previously-included-same-version"] += 1
            reasons[row["version_id"]] = "research-and-selected-checks-already-in-previous-brief"
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
            relation = "repeat-explicitly-requested" if _attention(row) == "focus" else "repeat-with-explicit-watch-evidence"
        elif key in basis["available"]:
            relation = "previously-available-not-included"
        elif clock(row["context"]["stored_at"]) <= clock(basis["cutoff"]):
            relation = "late-to-delivery"
        else:
            relation = "newly-stored-research"
        selected_reason = "saved-agenda-focus-or-review-due" if _attention(row) == "focus" else (
            "explicit-watch-evidence-not-in-previous-brief" if key in basis["included"] and key in new_watch
            else "declared-reference-order")
        included.append({"version_id": key, "classification": relation, "selection_reason": selected_reason})
    result = {"rows": deepcopy(rows), "included": included, "omissions": omissions,
              "baseline": deepcopy(basis), "agenda_reference": plan["agenda"], "policy": POLICY,
              "coverage": "explicit-references-only-not-all-research-or-tasks",
              "time_basis": "stored-time-not-event-time-or-research-completion",
              "deduplication_scope": "immediate-previous-actual-brief-only",
              "research_assessment": "not-performed", "research_executed": False}
    if any("watch_evidence" in row.get("context", {}) or "watch_evidence" in row for row in rows):
        chosen_ids = {row["version_id"] for row in chosen}
        adoption = []
        for row in rows:
            for watch in _watch_rows(row):
                action = _watch_attention(row, watch) if row["status"] == "readable" else "default"
                adopted = (row["version_id"] in chosen_ids and watch["status"] == "readable"
                           and action not in ("defer", "dismiss"))
                if adopted:
                    reason = "included-in-original-brief-body"
                elif action in ("defer", "dismiss"):
                    scope = "question" if watch["selection"]["question_id"] is not None else "research"
                    reason = scope + "-explicitly-" + ("deferred" if action == "defer" else "dismissed")
                elif watch["status"] != "readable":
                    reason = watch.get("error", "retained-check-unavailable-or-outside-clock")
                else:
                    reason = reasons.get(row["version_id"], "brief-capacity")
                adoption.append({"selection": deepcopy(watch["selection"]), "selection_id": watch["selection_id"],
                                 "status": "included" if adopted else "not-included", "reason": reason})
        result["watch_evidence_adoption"] = adoption
        result["policy"] = WATCH_POLICY
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
        # Rebuild the adopted context, not merely its quote/mandate. Otherwise a
        # resealed artifact could forge original challenges, source status or
        # agenda choices in the actual prior body. Nonadopted historical failures
        # remain historical observations; recovery may make them usable next time.
        current = read_rows(store, {**plan, "items": [row["item"]]}, cutoff)[0]
        require(current == row, "BRIEF_RESEARCH_ORIGINAL_MISMATCH")


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
        if c.get("watch_evidence"):
            lines += _watch_text(c["watch_evidence"], section["watch_evidence_adoption"])
    if not section["included"]:
        lines.append("本期未采用研究片段；不等于无研究、研究未运行或市场安静。")
    lines += ["", "研究省略：" + encoded(section["omissions"]).decode().rstrip()]
    for row in section["rows"]:
        if row["status"] != "readable":
            lines.append("未采用版本 " + row["version_id"] + "：" + row["status"] + "；" + row.get("error", "保存晚于截止"))
    if "watch_evidence_adoption" in section:
        receipts = section["watch_evidence_adoption"]
        count = sum(row["status"] == "included" for row in receipts)
        lines += ["", f"条件证据本期采用 {count} 项；声明范围共 {len(receipts)} 项。"]
        for row in receipts:
            if row["status"] != "included":
                ref = row["selection"]["check_ref"]
                lines.append("未采用条件证据：" + _escape_md(ref["path"]) + "；" + row["reason"])
    lines.append("未开启新Quick/Full、通知或人工Decision；引用/片段采用不等于研究质量或人工接受。\n")
    return "\n".join(lines)


def _watch_text(rows, adoption):
    """The adopted result appears in the same body as its original research."""
    from decision_kernel.application.news_delivery import _escape_md

    labels = {"first-trigger": "首次满足", "continued-trigger": "继续满足", "not-met": "未满足",
              "cannot-determine": "无法判断", "review-required": "需要复核", "ended": "已结束",
              "not-due": "尚未到检查时点", "not-enabled": "未启用"}
    lines = ["", "#### 关联的已存条件证据", "",
             "以下为确切已存检查的重演，没有执行新检查；条件满足不构成新的研究结论、关注选择或投资决定。",
             "检查记录时钟不是该检查的保管时间或历史可知性证明；原问题绑定只说明这次议程的用途。"]
    for row in rows:
        receipt = next(item for item in adoption if item["selection_id"] == row["selection_id"])
        if row["status"] == "readable" and receipt["status"] != "included":
            lines.append("未采用检查：" + receipt["reason"])
            continue
        if row["status"] != "readable":
            lines.append("未采用检查：" + row["status"] + "；" + row.get("error", "未知原因"))
            continue
        ref, result = row["selection"]["check_ref"], row["result"]
        question = row["selection"]["question_id"] or "整份原研究"
        lines += ["", "条件：" + _escape_md(row["watch_id"]) + "；关联：" + _escape_md(question),
                  "原记录时点：" + row["recorded_at"]]
        if result is not None:
            truth = "满足" if result["condition_met"] is True else "未满足" if result["condition_met"] is False else "未知"
            lines += ["原检查结果：" + labels.get(result["status"], result["status"]) + "；条件判定：" + truth,
                      "连续性：" + result["trigger_continuity"],
                      "原资料切面：`" + result["request"]["records_snapshot"] + "`"]
            if "condition_results" in result:
                for condition in result["condition_results"]:
                    value = condition.get("value")
                    truth = ("满足" if condition["condition_met"] is True else
                             "未满足" if condition["condition_met"] is False else "未知")
                    lines.append("条件 " + _escape_md(condition["condition_id"]) + "："
                                 + labels.get(condition["status"], condition["status"])
                                 + "；条件判定：" + truth + "；连续性：" + condition["trigger_continuity"]
                                 + ("；值 " + _escape_md(str(value)) if value is not None else "")
                                 + ("；" + _escape_md(condition["reason"]) if condition.get("reason") else ""))
                    if condition.get("evidence") is not None:
                        lines += _watch_assessment_text(condition["evidence"])
                lines.append("相邻边界：" + _escape_md(encoded(result["next_boundaries"]).decode().rstrip()))
            else:
                observation = result["observation"]
                if observation.get("value") is not None:
                    lines.append("原比较值：" + _escape_md(str(observation["value"])))
                if observation.get("reason"):
                    lines.append("原原因：" + _escape_md(observation["reason"]))
            lines.append("冻结条件：" + _escape_md(encoded(row["definition"]).decode().rstrip()))
        if row["activity"] is not None:
            lines.append("原活动操作：" + row["activity"]["action"] + "；状态：" + row["activity"]["state"]
                         + ("；本事件没有执行条件检查。" if result is None else ""))
        lines += ["确切检查：`" + ref["repository"] + "@" + ref["commit"] + ":" + ref["path"] + "`",
                  "原检查结果SHA256：" + row["result_sha256"]]
    return lines


def _watch_assessment_text(evidence):
    """Adopt the stated assessment and its exact source limits in the same body."""
    from decision_kernel.application.news_delivery import _escape_md

    lines = ["提交的经营判读：" + _escape_md(evidence["conclusion"])
             + "；依据：" + _escape_md(evidence["reason"]),
             "声明观察时点：" + evidence["observed_at"] + "；声明判读时点：" + evidence["assessed_at"],
             "这是提交者的经营判断；来源回读只验证留存选段，不证明原件真实性或证据足以支持判断。"]
    for source in evidence["source_reads"]:
        lines += ["依据来源：" + _escape_md(source.get("title", "未读取来源正文")) + "；读取状态：" + source["availability"],
                  "确切来源、选段、时间和原件限制：" + _escape_md(encoded(source).decode().rstrip())]
    if not evidence["source_reads"]:
        lines.append("没有可列出的来源回读；该证据缺口仍保留。")
    return lines
