"""News-only delivery: exact retained observations, explicit choices and real Brief baselines.

No source collection, economic inference, human authentication or research execution.
Documents use the existing artifact owner; a baseline is read one step, not by a
recursive replay of every previous delivery. Source windows are always rebuilt.
"""
from __future__ import annotations

import base64
from collections import deque
from copy import deepcopy
from dataclasses import asdict
import json
import re
from urllib.parse import urlsplit

from decision_kernel.application.news_rolling import clock, reference
from decision_kernel.application.news_rolling_index import open_index, restore_index
from decision_kernel.domain.records import RecordError, digest, encoded, require
from decision_kernel.domain.research_artifacts import artifact_reference

SCHEMA = "dos2-news-edition-v1"
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_CHOICES = 128
ACTIONS = ("focus", "defer", "dismiss", "reset")
KINDS = {"morning": "晨间新闻简报", "evening": "晚间新闻简报", "adhoc": "即时新闻简报"}
CLASSIFICATIONS = {"baseline-not-supplied", "repeat-explicitly-requested",
                   "previously-available-not-included", "late-to-delivery", "new-to-window"}
POLICY = "explicit-focus-then-source-round-robin-v1"


def entry_id(entry):
    row = entry["observation"]
    return digest(encoded([row[k] for k in ("provider", "source_id", "article_id", "version_id")]))


def _text(value, maximum, *, empty=False):
    require(isinstance(value, str) and len(value) <= maximum and (empty or bool(value.strip()))
            and all(ord(c) >= 32 and ord(c) != 127 for c in value), "NEWS_DESK_TEXT_INVALID")
    return value


def _choice(value):
    require(isinstance(value, dict) and set(value) == {"entry_id", "action", "reason", "question", "review_at"},
            "NEWS_DESK_CHOICE_SHAPE")
    require(isinstance(value["entry_id"], str) and re.fullmatch(r"[a-f0-9]{64}", value["entry_id"])
            and isinstance(value["action"], str) and value["action"] in ACTIONS, "NEWS_DESK_CHOICE_INVALID")
    _text(value["reason"], 600); _text(value["question"], 400, empty=True)
    require(not value["question"] or value["action"] == "focus", "NEWS_DESK_QUESTION_REQUIRES_FOCUS")
    if value["review_at"] is not None:
        require(value["action"] == "defer", "NEWS_DESK_REVIEW_REQUIRES_DEFER")
        clock(value["review_at"])
    return deepcopy(value)


def plan_input(plan):
    require(isinstance(plan, dict) and {"window", "cutoff", "kind"} <= set(plan)
            and set(plan) <= {"window", "cutoff", "kind", "baseline", "attention", "max_items"},
            "NEWS_DESK_PLAN_REQUIRED")
    require(isinstance(plan["kind"], str) and plan["kind"] in KINDS, "NEWS_DESK_KIND_INVALID")
    clock(plan["cutoff"])
    limit = plan.get("max_items", 12)
    require(type(limit) is int and 1 <= limit <= 24, "NEWS_DESK_BRIEF_BOUND")
    choices = plan.get("attention", [])
    require(isinstance(choices, list) and len(choices) <= MAX_CHOICES, "NEWS_DESK_CHOICE_BOUND")
    choices = [_choice(c) for c in choices]
    require(len({c["entry_id"] for c in choices}) == len(choices), "NEWS_DESK_DUPLICATE_CHOICE")
    for c in choices:
        require(c["review_at"] is None or clock(c["review_at"]) > clock(plan["cutoff"]),
                "NEWS_DESK_REVIEW_NOT_FUTURE")
    return {"window": reference(plan["window"]), "cutoff": plan["cutoff"], "kind": plan["kind"],
            "baseline": asdict(artifact_reference(plan["baseline"])) if plan.get("baseline") is not None else None,
            "attention": choices, "max_items": limit}


def _window(reader, ref):
    index = open_index(reader, ref)
    return index, restore_index(reader, index)


def _summary(index, logical):
    return {"index_sha256": index["index_sha256"], "logical_sha256": index["logical_sha256"],
            **{k: deepcopy(logical[k]) for k in ("as_of", "window_start", "window_hours", "coverage",
               "source_outcomes", "losses", "rejected_references", "predecessor")}}


def _seal(doc):
    doc["edition_sha256"] = digest(encoded({k: v for k, v in doc.items() if k != "edition_sha256"}))
    require(len(encoded(doc)) <= MAX_DOCUMENT_BYTES, "NEWS_DESK_DOCUMENT_BOUND")
    return doc


def _state(value):
    require(isinstance(value, list) and len(value) <= MAX_CHOICES, "NEWS_DESK_CHOICE_BOUND")
    state = [_choice(c) for c in value]
    require(all(c["action"] != "reset" for c in state)
            and len({c["entry_id"] for c in state}) == len(state), "NEWS_DESK_STATE_INVALID")
    return state


def _item(entry, classification, reason):
    row = entry["observation"]
    return {"entry_id": entry_id(entry), "title": row["title"], "source": row["source_id"],
            "url": row["url"], "publication_claim": deepcopy(row["published_claim"]),
            "first_observed": entry["first_seen_in_window"], "last_observed": entry["last_seen_in_window"],
            "source_reference": reference(entry["last_reference"]),
            "classification": classification, "selection_reason": reason,
            "assessment": "new-observation-awaiting-explanation", "research_started": False}


def _escape_md(text):
    return re.sub(r"([\\`*{}\[\]<>()#!|_])", r"\\\1", text)


def brief_text(doc):
    summary = doc["window"]
    lines = ["# " + KINDS[doc["plan"]["kind"]], "", "交付截止：" + doc["plan"]["cutoff"],
             "新闻观察窗口：" + summary["window_start"] + " → " + summary["as_of"] + "（24小时）", "",
             "这是已保管观察的阅读简报，不是新的采集、已验证事件或投资建议。媒体发布时间仍为来源声明。",
             "选择规则：显式继续优先，再按来源轮选；不使用投资信心评分。",
             "比较基线：" + ("已读取上一份实际保存正文" if doc["plan"]["baseline"] else "未提供；不声称已经去重或没有迟到"),
             "", "## 本期采用"]
    for i, item in enumerate(doc["included"], 1):
        lines += ["", f"### {i}. " + _escape_md(item["title"]),
                  "来源：" + _escape_md(item["source"]) + "；观察时间：" + item["last_observed"],
                  "采用依据：" + item["selection_reason"] + "；基线关系：" + item["classification"],
                  "新观察，待解释；未启动研究。",
                  "原链接：" + (item["url"] or "未取得单篇原链接"),
                  "精确来源：`" + item["source_reference"]["manifest"]["commit"] + ":"
                  + item["source_reference"]["manifest"]["path"] + "`"]
    if not doc["included"]:
        lines += ["", "本期没有满足当前选择规则的条目；不代表市场没有新闻。"]
    lines += ["", "## 必要省略与运行缺口",
              "省略：" + json.dumps(doc["omissions"], ensure_ascii=False, sort_keys=True),
              "覆盖：" + json.dumps(summary["coverage"], ensure_ascii=False, sort_keys=True),
              "来源结果为整窗历史，后续成功不抹掉旧失败；没有连续24小时完整采集证明。"]
    for wrapped in summary["source_outcomes"]:
        outcome = wrapped["outcome"]
        lines.append("- " + _escape_md(outcome["source"]) + "：" + outcome["status"]
                     + "；" + _escape_md(json.dumps(outcome, ensure_ascii=False, sort_keys=True)))
    lines += ["", "## 研究与人工状态",
              f"显式选择记录 {len(doc['attention_state'])} 项；Quick候选 {len(doc['quick_candidates'])} 项。",
              "候选只是有原问题和来源的交接，不是已完成Quick、Full或人工投资决定。",
              "研究产物未接入本新闻简报；无启动回执，执行状态未确认。未发送通知，未推定已读或接受。", ""]
    return "\n".join(lines)


def _read_document(artifacts, ref):
    exact = asdict(artifact_reference(ref))
    found = artifacts.read(exact)
    require(found.get("bytes_verified") is True and found.get("reference") == exact
            and found.get("metadata", {}).get("media_type") == "application/json", "NEWS_DESK_ARTIFACT_INVALID")
    try:
        raw = base64.b64decode(found["data_base64"], validate=True)
        require(len(raw) <= MAX_DOCUMENT_BYTES, "NEWS_DESK_DOCUMENT_BOUND")
        doc = json.loads(raw)
        require(encoded(doc) == raw and isinstance(doc, dict) and doc.get("schema") == SCHEMA,
                "NEWS_DESK_DOCUMENT_INVALID")
        require(doc.get("edition_sha256") == digest(encoded({k: v for k, v in doc.items() if k != "edition_sha256"})),
                "NEWS_DESK_DIGEST_MISMATCH")
        return doc
    except (ValueError, UnicodeError, KeyError, TypeError):
        raise RecordError("NEWS_DESK_DOCUMENT_INVALID") from None


def _check_content(doc, index, logical):
    """Validate a prior edition's own source/body, not its entire predecessor chain."""
    required = {"schema", "plan", "window", "attention_state", "included", "omissions", "comparison",
                "quick_candidates", "policy", "human_acceptance", "brief_markdown", "edition_sha256"}
    require(set(doc) == required and plan_input(doc["plan"]) == doc["plan"]
            and doc["window"] == _summary(index, logical) and doc["policy"] == POLICY
            and doc["human_acceptance"] == "not-established-by-artifact", "NEWS_DESK_CONTENT_MISMATCH")
    require(clock(logical["as_of"]) <= clock(doc["plan"]["cutoff"]), "NEWS_DESK_WINDOW_AFTER_CUTOFF")
    state = {c["entry_id"]: c for c in _state(doc["attention_state"])}
    omissions = doc["omissions"]
    require(isinstance(omissions, dict) and set(omissions) == {
        "previously-included-same-version", "explicitly-deferred", "explicitly-dismissed", "brief-capacity"}
        and all(type(n) is int and n >= 0 for n in omissions.values()), "NEWS_DESK_OMISSIONS_INVALID")
    comparison = doc["comparison"]
    require(isinstance(comparison, dict) and set(comparison) == {
        "status", "baseline", "baseline_body_sha256", "new_to_window", "no_longer_in_window", "late_to_delivery"}
        and comparison["baseline"] == doc["plan"]["baseline"], "NEWS_DESK_COMPARISON_INVALID")
    has_baseline = doc["plan"]["baseline"] is not None
    require(comparison["status"] == ("read-actual-previous-body" if has_baseline else "not-supplied"),
            "NEWS_DESK_COMPARISON_INVALID")
    for name in ("new_to_window", "no_longer_in_window", "late_to_delivery"):
        number = comparison[name]
        require(type(number) is int and number >= 0 if has_baseline else number is None,
                "NEWS_DESK_COMPARISON_INVALID")
    body_hash = comparison["baseline_body_sha256"]
    require(isinstance(body_hash, str) and re.fullmatch(r"[a-f0-9]{64}", body_hash) if has_baseline else body_hash is None,
            "NEWS_DESK_COMPARISON_INVALID")
    available = {entry_id(e): e for e in logical["entries"]}
    require(isinstance(doc["included"], list) and len(doc["included"]) <= doc["plan"]["max_items"],
            "NEWS_DESK_BRIEF_BOUND")
    seen = set()
    for item in doc["included"]:
        require(isinstance(item, dict) and item.get("entry_id") in available
                and item["entry_id"] not in seen and item.get("classification") in CLASSIFICATIONS
                and item.get("selection_reason") in ("显式继续", "暂缓到期待复核", "来源轮选"),
                "NEWS_DESK_ITEM_INVALID")
        seen.add(item["entry_id"])
        require(item == _item(available[item["entry_id"]], item["classification"], item["selection_reason"]),
                "NEWS_DESK_ITEM_NOT_FROM_WINDOW")
    require(len(seen) + sum(omissions.values()) == len(available), "NEWS_DESK_OMISSIONS_INVALID")
    require(doc["quick_candidates"] == _candidates(state, available, doc["plan"]["window"]),
            "NEWS_DESK_CANDIDATES_INVALID")
    require(doc["brief_markdown"] == brief_text(doc), "NEWS_DESK_BODY_MISMATCH")


def _candidates(state, entries, window_reference):
    candidates = []
    for key, choice in sorted(state.items()):
        if key in entries and choice["action"] == "focus" and choice["question"]:
            candidates.append({"entry_id": key, "question": choice["question"], "reason": choice["reason"],
                               "source_reference": reference(entries[key]["last_reference"]),
                               "window_reference": window_reference, "status": "candidate-not-executed",
                               "research_started": False, "requires_research_scope_check": True})
    return candidates


def _derive(index, logical, plan, prior=None, prior_logical=None):
    cutoff = clock(plan["cutoff"])
    require(clock(logical["as_of"]) <= cutoff, "NEWS_DESK_WINDOW_AFTER_CUTOFF")
    entries = {entry_id(e): e for e in logical["entries"]}
    state = {c["entry_id"]: deepcopy(c) for c in (prior["attention_state"] if prior else [])}
    for c in plan["attention"]:
        require(c["entry_id"] in entries, "NEWS_DESK_CHOICE_NOT_IN_WINDOW")
        if c["action"] == "reset":
            state.pop(c["entry_id"], None)
        else:
            state[c["entry_id"]] = deepcopy(c)
    require(len(state) <= MAX_CHOICES, "NEWS_DESK_CHOICE_BOUND")
    previous_ids = {entry_id(e) for e in prior_logical["entries"]} if prior_logical is not None else set()
    previous_included = {e["entry_id"] for e in prior["included"]} if prior else set()
    omit = {"previously-included-same-version": 0, "explicitly-deferred": 0,
            "explicitly-dismissed": 0, "brief-capacity": 0}
    focus, groups, reasons = [], {}, {}
    for key, e in entries.items():
        choice = state.get(key)
        due = choice is not None and choice["action"] == "defer" and choice["review_at"] is not None and clock(choice["review_at"]) <= cutoff
        if choice and choice["action"] == "dismiss":
            omit["explicitly-dismissed"] += 1
        elif choice and choice["action"] == "defer" and not due:
            omit["explicitly-deferred"] += 1
        elif choice and choice["action"] == "focus" or due:
            focus.append(key); reasons[key] = "暂缓到期待复核" if due else "显式继续"
        elif key in previous_included:
            omit["previously-included-same-version"] += 1
        else:
            groups.setdefault(e["observation"]["source_id"], deque()).append(key)
            reasons[key] = "来源轮选"
    # Group insertion follows the existing most-recent-observation order, not a score.
    queue = list(focus)
    while any(groups.values()):
        for group in groups.values():
            if group:
                queue.append(group.popleft())
    chosen = queue[:plan["max_items"]]
    omit["brief-capacity"] = len(queue) - len(chosen)
    included = []
    for key in chosen:
        if prior is None:
            classification = "baseline-not-supplied"
        elif key in previous_included:
            classification = "repeat-explicitly-requested"
        elif key in previous_ids:
            classification = "previously-available-not-included"
        elif clock(entries[key]["first_seen_in_window"]) <= clock(prior["plan"]["cutoff"]):
            classification = "late-to-delivery"
        else:
            classification = "new-to-window"
        included.append(_item(entries[key], classification, reasons[key]))
    candidates = _candidates(state, entries, plan["window"])
    comparison = {"status": "read-actual-previous-body" if prior else "not-supplied",
                  "baseline": plan["baseline"], "baseline_body_sha256": digest(prior["brief_markdown"].encode()) if prior else None,
                  "new_to_window": len(set(entries) - previous_ids) if prior else None,
                  "no_longer_in_window": len(previous_ids - set(entries)) if prior else None,
                  "late_to_delivery": sum(clock(entries[k]["first_seen_in_window"]) <= clock(prior["plan"]["cutoff"])
                                          for k in set(entries) - previous_ids) if prior else None}
    doc = {"schema": SCHEMA, "plan": deepcopy(plan), "window": _summary(index, logical),
           "attention_state": [state[k] for k in sorted(state)], "included": included, "omissions": omit,
           "comparison": comparison, "quick_candidates": candidates, "policy": POLICY,
           "human_acceptance": "not-established-by-artifact"}
    doc["brief_markdown"] = brief_text(doc)
    return _seal(doc)


def build_edition(reader, artifacts, plan):
    normalized = plan_input(plan)
    index, logical = _window(reader, normalized["window"])
    prior = prior_logical = None
    if normalized["baseline"] is not None:
        prior = _read_document(artifacts, normalized["baseline"])
        prior_index, prior_logical = _window(reader, prior["plan"]["window"])
        _check_content(prior, prior_index, prior_logical)
        require(clock(prior["plan"]["cutoff"]) <= clock(normalized["cutoff"])
                and clock(prior_logical["as_of"]) <= clock(logical["as_of"]), "NEWS_DESK_BASELINE_FROM_FUTURE")
    return _derive(index, logical, normalized, prior, prior_logical), logical


def open_edition(reader, artifacts, ref):
    doc = _read_document(artifacts, ref)
    expected, logical = build_edition(reader, artifacts, doc["plan"])
    require(expected == doc, "NEWS_DESK_EDITION_REPLAY_MISMATCH")
    return doc, logical


def page_model(doc, logical, saved_reference=None):
    """All rows remain readable, including those omitted by this Brief policy."""
    return {"edition": doc, "saved_reference": asdict(artifact_reference(saved_reference)) if saved_reference else None,
            "articles": [{"entry_id": entry_id(e), "title": e["observation"]["title"],
                          "source": e["observation"]["source_id"], "url": safe_link(e["observation"]["url"]),
                          "published_claim": e["observation"]["published_claim"],
                          "first_observed": e["first_seen_in_window"], "last_observed": e["last_seen_in_window"],
                          "observations": e["seen_batch_count_in_window"],
                          "source_reference": reference(e["last_reference"])} for e in logical["entries"]]}


def safe_link(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        url = urlsplit(value)
        return value if url.scheme in ("http", "https") and url.hostname and not url.username and not url.password else None
    except ValueError:
        return None
