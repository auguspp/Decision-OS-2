"""Selected retained industry series in the existing Brief, not new research.

The original readers own values, comparisons, roles and source qualification.
This owner selects bounded series, preserves their limitations and compares one
actual preceding Brief. Packet storage time is never economic event time.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json

from decision_kernel.application.industry_operations import industry_source_scope
from decision_kernel.application.industry_reading import read_industry
from decision_kernel.application.operating_cycle import operating_cycle
from decision_kernel.application.vehicle_flows import vehicle_flows
from decision_kernel.application.news_delivery import _escape_md, clock
from decision_kernel.domain.records import RecordError, Selection, SourceRef, digest, encoded, identifier, require

MAX_ITEMS = 8
MAX_PACKETS = 4
MAX_PACKET_BYTES = 2 * 1024 * 1024
MAX_ROW_BYTES = 24 * 1024
MAX_SECTION_BYTES = 96 * 1024
VIEWS = ("industry", "operating-cycle", "vehicle-flows")
POLICY = "exact-industry-series-in-declared-order-v1"
OMISSIONS = ("unreadable-or-outside-cutoff", "previously-included-same-selection", "brief-capacity")


def item_input(item):
    require(isinstance(item, dict) and set(item) == {"reference", "view", "series_id"},
            "BRIEF_INDUSTRY_ITEM_SHAPE")
    ref = SourceRef.from_dict(item["reference"])
    require(ref.selection == Selection(), "INDUSTRY_WHOLE_PACKET_REQUIRED")
    require(isinstance(item["view"], str) and item["view"] in VIEWS, "BRIEF_INDUSTRY_VIEW")
    identifier(item["series_id"])
    return {"reference": json.loads(encoded(asdict(ref))), "view": item["view"], "series_id": item["series_id"]}


def version_id(item):
    item = item_input(item)
    manifest = item["reference"]["manifest"]
    # An equivalent reachable commit is not a new series; a changed packet is.
    return digest(encoded([manifest[k] for k in ("repository", "path", "sha256")] +
                          [item["view"], item["series_id"]]))


def _packet_key(item):
    return encoded([item["reference"], item["view"]])


def plan_input(value):
    require(isinstance(value, dict) and "items" in value and set(value) <= {"items", "max_items"},
            "BRIEF_INDUSTRY_PLAN_SHAPE")
    require(isinstance(value["items"], list) and len(value["items"]) <= MAX_ITEMS, "BRIEF_INDUSTRY_ITEM_BOUND")
    items = [item_input(i) for i in value["items"]]
    require(len({version_id(i) for i in items}) == len(items), "BRIEF_INDUSTRY_DUPLICATE_SELECTION")
    require(len({_packet_key(i) for i in items}) <= MAX_PACKETS, "BRIEF_INDUSTRY_PACKET_BOUND")
    limit = value.get("max_items", 3)
    require(type(limit) is int and 1 <= limit <= MAX_ITEMS, "BRIEF_INDUSTRY_ITEM_BOUND")
    return {"items": items, "max_items": limit}


def _packet(store, item):
    ref = SourceRef.from_dict(item["reference"])
    # Keep the original immutable-source scopes and all exit permission checks.
    with industry_source_scope(store) as reader:
        root = reader.read_source(ref)
        view = read_industry(ref, reader)
        if item["view"] == "operating-cycle":
            view = operating_cycle(view)
        elif item["view"] == "vehicle-flows":
            view = vehicle_flows(view)
    view = json.loads(encoded(view))  # Normalize tuples at the artifact boundary only.
    require(view["reference"] == item["reference"], "BRIEF_INDUSTRY_REFERENCE_MISMATCH")
    require(len(encoded(view)) <= MAX_PACKET_BYTES, "BRIEF_INDUSTRY_PACKET_DISPLAY_BOUND")
    return {"stored_at": root["manifest"]["stored_at"], "view": view,
            "root_limitations": root["limitations"], "root_original_verified": root["original_verified"]}


def _point(point):
    out = deepcopy(point)
    if "source" in out:
        # Unrelated publisher-wide entity dictionaries are not adopted. All
        # selected basis, exact references, dates and limitations remain intact.
        out["source"].pop("entities", None)
    return out


def project(packet, item):
    view = packet["view"]
    retained = view.get("retained", view)
    if item["view"] == "industry":
        candidates = [{**s, "points": s["observations"]} for s in view["series"]]
    elif item["view"] == "vehicle-flows":
        candidates = view["entries"]
    else:
        candidates = [{**m, "subject": e["subject"], "metric": name,
                       "not_supplied_metrics": e["not_supplied_metrics"]}
                      for e in view["entries"] for name, m in e["metrics"].items()]
    matches = [s for s in candidates if s["series_id"] == item["series_id"]]
    require(len(matches) == 1, "BRIEF_INDUSTRY_SERIES_UNAVAILABLE")
    selected = deepcopy(matches[0])
    selected.pop("observations", None)
    selected["points"] = [_point(p) for p in selected["points"]]
    require(any(p["status"] == "read" for p in selected["points"]), "BRIEF_INDUSTRY_NO_READABLE_POINTS")
    subject = selected["subject"]["entity_id"]
    # Operating-cycle output deliberately has no company mapping.
    relations = [deepcopy(r) for r in view.get("relations", []) if r["subject_id"] == subject]
    return {"series": selected, "relations": relations,
            "packet_status": view["status"], "packet_scope": deepcopy(view["scope"]),
            "retained_scope": deepcopy(retained["scope"]),
            "relation_outcomes": deepcopy(retained["relation_outcomes"]),
            "as_of": view["as_of"], "prepared_at": retained["prepared_at"],
            "stored_at": packet["stored_at"], "result_sha256": digest(encoded(view)),
            "coverage_note": retained["coverage_note"], "use": retained["use"],
            "qualification": retained["qualification"], "source_authenticity": retained["source_authenticity"],
            "intraday_availability": retained["intraday_availability"],
            "root_limitations": deepcopy(packet["root_limitations"]),
            "root_original_verified": packet["root_original_verified"],
            "view_limitations": deepcopy(view.get("limitations", [])),
            "unclassified_series": deepcopy(view.get("unclassified_series", [])),
            "projection": "one-series-all-points-and-associated-roles; source-entity-dictionaries-omitted; no-derived-operating-snapshots"}


def read_rows(store, plan, cutoff):
    rows, packets, used = [], {}, 0
    for item in plan["items"]:
        row = {"item": deepcopy(item), "version_id": version_id(item)}
        key = _packet_key(item)
        if key not in packets:
            try:
                packets[key] = _packet(store, item)
            except (RecordError, KeyError, TypeError):
                # One explicit operation, not a retry queue or persistent cache.
                packets[key] = None
        packet = packets[key]
        try:
            require(packet is not None, "BRIEF_INDUSTRY_PACKET_UNAVAILABLE")
            if clock(packet["stored_at"]) > clock(cutoff) or clock(packet["view"]["as_of"]) > clock(cutoff):
                row.update(status="after-cutoff", stored_at=packet["stored_at"], as_of=packet["view"]["as_of"])
            else:
                row.update(status="readable", context=project(packet, item))
        except (RecordError, KeyError, TypeError):
            row.update(status="unavailable", error="BRIEF_INDUSTRY_SELECTION_UNAVAILABLE")
        size = len(encoded(row))
        if size > MAX_ROW_BYTES or used + size > MAX_SECTION_BYTES - 8192:
            row = {"item": deepcopy(item), "version_id": row["version_id"],
                   "status": "not-included", "error": "BRIEF_INDUSTRY_CONTEXT_BOUND"}
        used += len(encoded(row)); rows.append(row)
    return rows


def baseline_basis(previous, cutoff):
    return {"cutoff": cutoff, "industry_tracked": previous is not None,
            "available": sorted(r["version_id"] for r in previous["rows"] if r["status"] == "readable") if previous else [],
            "included": sorted(i["version_id"] for i in previous["included"]) if previous else []}


def derive(rows, plan, basis):
    require(isinstance(basis, dict) and set(basis) == {"cutoff", "industry_tracked", "available", "included"}
            and type(basis["industry_tracked"]) is bool, "BRIEF_INDUSTRY_BASELINE_INVALID")
    if basis["cutoff"] is not None:
        clock(basis["cutoff"])
    for key in ("available", "included"):
        values = basis[key]
        require(isinstance(values, list) and len(values) <= MAX_ITEMS and
                all(isinstance(v, str) and len(v) == 64 and all(c in '0123456789abcdef' for c in v) for v in values)
                and values == sorted(set(values)), "BRIEF_INDUSTRY_BASELINE_INVALID")
    require(set(basis["included"]) <= set(basis["available"]) and
            (basis["industry_tracked"] or not basis["available"]), "BRIEF_INDUSTRY_BASELINE_INVALID")
    require(isinstance(rows, list) and len(rows) == len(plan["items"]), "BRIEF_INDUSTRY_ROWS_INVALID")
    omissions, included = dict.fromkeys(OMISSIONS, 0), []
    for row, item in zip(rows, plan["items"], strict=True):
        require(row["item"] == item and row["version_id"] == version_id(item), "BRIEF_INDUSTRY_ROWS_INVALID")
        key = row["version_id"]
        if row["status"] != "readable":
            omissions["unreadable-or-outside-cutoff"] += 1
        elif key in basis["included"]:
            omissions["previously-included-same-selection"] += 1
        elif len(included) >= plan["max_items"]:
            omissions["brief-capacity"] += 1
        else:
            classification = ("baseline-not-supplied" if basis["cutoff"] is None else
                "previously-available-not-included" if key in basis["available"] else
                "late-to-delivery" if clock(row["context"]["stored_at"]) <= clock(basis["cutoff"]) else
                "newly-stored-packet-selection")
            included.append({"version_id": key, "classification": classification,
                             "selection_reason": "declared-series-order-not-human-acceptance"})
    result = {"rows": deepcopy(rows), "included": included, "omissions": omissions, "baseline": deepcopy(basis),
              "no_longer_selected": sorted(set(basis["available"]) - {r["version_id"] for r in rows}),
              "policy": POLICY, "coverage": "explicit-packet-series-only-not-all-industry-or-research",
              "deduplication_scope": "immediate-previous-actual-brief-only",
              "time_basis": "packet-storage-not-economic-event-or-publication-time",
              "company_benefit": "not-assessed", "research_executed": False, "human_acceptance": "not-established"}
    require(len(encoded(result)) <= MAX_SECTION_BYTES, "BRIEF_INDUSTRY_SECTION_BOUND")
    return result


def build(store, plan, cutoff, previous=None, previous_cutoff=None):
    result = derive(read_rows(store, plan, cutoff), plan, baseline_basis(previous, previous_cutoff))
    store.backend.head()  # Revalidate access after all cached immutable packets.
    return result


def validate(section, plan, cutoff, store):
    require(derive(section["rows"], plan, section["baseline"]) == section, "BRIEF_INDUSTRY_SECTION_MISMATCH")
    require(section["baseline"]["cutoff"] is None or clock(section["baseline"]["cutoff"]) <= clock(cutoff),
            "BRIEF_INDUSTRY_BASELINE_FROM_FUTURE")
    chosen = {i["version_id"] for i in section["included"]}
    old = [r for r in section["rows"] if r["version_id"] in chosen]
    actual = read_rows(store, {**plan, "items": [r["item"] for r in old]}, cutoff)
    require(actual == old, "BRIEF_INDUSTRY_ORIGINAL_MISMATCH")
    store.backend.head()


def brief_text(section):
    def literal(value):
        return _escape_md(str(value) if not isinstance(value, (dict, list)) else encoded(value).decode().rstrip())
    lines = ["", "## 产业观察的本期采用", "",
             "指定已保管系列的观察，待解释；不是新采集、新研究或当前净受益判断。",
             "比较只覆盖上一份实际简报；新保管或迟到纳入不等于经济事实今天发生。"]
    rows = {r["version_id"]: r for r in section["rows"]}
    for selected in section["included"]:
        row = rows[selected["version_id"]]; c = row["context"]; series = c["series"]
        lines += ["", "### " + literal(series["subject"]["name"] + " / " + series["series_id"]),
                  "采用关系：" + selected["classification"],
                  "资料包截止：" + c["as_of"] + "；实际保管：" + c["stored_at"],
                  "完整范围与读取状态：" + literal({"scope": c["packet_scope"], "status": c["packet_status"], "coverage": c["coverage_note"]}),
                  "精确根与选择：" + literal(row["item"]),
                  "| 期间 | 值 | 单位 | 口径 / 规格 | 读取状态 |", "|---|---|---|---|---|"]
        for p in series["points"]:
            lines.append("| " + " | ".join(literal(v) for v in (
                p.get("period_start", "未知") + " / " + p.get("period_end", "未知"),
                p.get("value") if p.get("value") is not None else "未知",
                p.get("unit", "未知"), " / ".join(str(p.get(k, "未知")) for k in
                    ("metric", "period_kind", "assessment", "specification", "region", "price_kind", "tax_basis")),
                p["status"] + " / " + str(p.get("reason_code") or ""))) + " |")
        lines += ["原比较（拒绝项不补算、不以0替代）：", literal(series["changes"])]
        for r in c["relations"]:
            lines += ["经营角色：" + literal({k: r[k] for k in ("company", "role", "business_scope", "report_period",
                        "applicable_start", "applicable_end", "applicability_at_cutoff", "current_business_freshness",
                        "business_share", "elasticity", "pass_through", "lag", "net_benefit")}),
                      "传导假设与反证：" + literal(r["transmission"]), "未知：" + literal(r["unknowns"]),
                      "角色证据及原件限制：" + literal({"evidence": r["evidence"], "view": r["evidence_view"]})]
        if not c["relations"]:
            lines.append("本选择未提供公司角色；不是零暴露，也不按产业名猜公司。")
        limits = list(c["root_limitations"]) + list(c["view_limitations"])
        for p in series["points"]:
            limits += p.get("source_limitations", [])
            lines.append("观察来源：" + literal({k: p[k] for k in ("reference", "source", "original_verified") if k in p}))
        lines += ["来源与解释限制：" + literal(list(dict.fromkeys(limits))),
                  "全包关系失败记录：" + literal(c["relation_outcomes"]),
                  "原读取器结果SHA256：" + c["result_sha256"]]
    if not section["included"]:
        lines.append("本期未采用产业系列；不代表没有变化、资料为空或市场安静。")
    lines += ["", "产业省略：" + literal(section["omissions"]),
              "退出本次选择的前版条目（未删除原资料）：" + literal(section["no_longer_selected"])]
    for r in section["rows"]:
        if r["status"] != "readable":
            lines.append("未采用 " + literal({"selection": r["item"], "status": r["status"], "error": r.get("error", "晚于截止")}))
    lines.append("同页或同一简报不证明研究已经采用产业解释；没有启动Quick/Full、通知或人工Decision。\n")
    return "\n".join(lines)
