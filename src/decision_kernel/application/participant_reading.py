"""Eight bounded participant views from pinned sources; no acquisition or writes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from decision_kernel.application.industry_reading import _clock, _document
from decision_kernel.application.industry_operations import industry_source_scope
from decision_kernel.application.research_sources import inspect_result
from decision_kernel.domain.records import (
    RecordError, Selection, SourceRef, StoreUnavailable, checked_text, digest, encoded, identifier, require,
)
from decision_kernel.observations import participant_observations as observations

SCHEMA = "participant-observations-v1"
MAX_INPUTS = 32
MAX_REFERENCES = 96
MAX_READ_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
FORMATS = ("registered-relay", "registered-relay-parts", "legacy-participant", "page-parts")
FATAL = {"PRIVATE_BINDING_MISMATCH", "PUBLIC_BINDING_MISMATCH", "PRIVATE_UPGRADE_REVERSED",
         "OPERATING_SOURCE_SCOPE_BOUND", "PARTICIPANT_READING_BOUND"}


def _shape(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields), "PARTICIPANT_PACKET_SHAPE")


def _items(value, limit, minimum=0):
    require(isinstance(value, list) and minimum <= len(value) <= limit, "PARTICIPANT_PACKET_BOUND")
    return value


def _whole(value):
    ref = value if isinstance(value, SourceRef) else SourceRef.from_dict(value)
    require(ref.selection == Selection(), "PARTICIPANT_WHOLE_SOURCE_REQUIRED")
    return ref


def packet_input(value):
    _shape(value, ("schema", "prepared_at", "as_of", "coverage_note", "inputs", "families"))
    require(value["schema"] == SCHEMA, "PARTICIPANT_PACKET_SCHEMA")
    require(_clock(value["prepared_at"]) <= _clock(value["as_of"]), "PARTICIPANT_CUTOFF_INVALID")
    checked_text(value["coverage_note"], limit=4096)
    inputs, reference_keys, reference_count = {}, set(), 0
    for item in _items(value["inputs"], MAX_INPUTS):
        _shape(item, ("input_id", "api", "format", "references"))
        key = identifier(item["input_id"])
        require(key not in inputs and isinstance(item["api"], str) and item["api"] in observations.CONTRACTS, "PARTICIPANT_INPUT_ID")
        require(isinstance(item["format"], str) and item["format"] in FORMATS, "PARTICIPANT_INPUT_FORMAT")
        refs = _items(item["references"], observations.MAX_PARTS if item["format"] in ("page-parts", "registered-relay-parts") else 1, 1)
        for original in refs:
            ref = _whole(original)
            fingerprint = encoded(asdict(ref))
            require(fingerprint not in reference_keys, "PARTICIPANT_DUPLICATE_REFERENCE")
            reference_keys.add(fingerprint)
        reference_count += len(refs)
        inputs[key] = item
    require(reference_count <= MAX_REFERENCES, "PARTICIPANT_REFERENCE_BOUND")
    seen, used = set(), set()
    for family in _items(value["families"], 8, 8):
        _shape(family, ("family", "title", "scope_note", "input_ids", "missing", "reopen_when"))
        key = family["family"]
        require(isinstance(key, str) and key in observations.FAMILIES and key not in seen, "PARTICIPANT_FAMILY_INVALID")
        seen.add(key)
        for field in ("title", "scope_note"):
            checked_text(family[field], limit=4096)
        ids = _items(family["input_ids"], MAX_INPUTS)
        require(all(isinstance(v, str) for v in ids) and len(set(ids)) == len(ids), "PARTICIPANT_DUPLICATE_INPUT")
        for input_id in ids:
            identifier(input_id)
            require(input_id in inputs and observations.CONTRACTS[inputs[input_id]["api"]][0] == key,
                    "PARTICIPANT_FAMILY_INPUT_MISMATCH")
            used.add(input_id)
        missing = _items(family["missing"], 16, 1)
        for note in missing:
            checked_text(note, limit=4096)
        if not ids:
            checked_text(family["reopen_when"], limit=4096)
        else:
            require(family["reopen_when"] is None or isinstance(family["reopen_when"], str),
                    "PARTICIPANT_REOPEN_INVALID")
            if family["reopen_when"] is not None:
                checked_text(family["reopen_when"], limit=4096)
    require(used == set(inputs), "PARTICIPANT_UNUSED_INPUT")
    return deepcopy(value)


def _registered_pages(value, registry):
    if registry is None:
        from pathlib import Path
        from decision_kernel.application.relay_registry import RelayRegistry
        registry = RelayRegistry(Path("docs/sources/relay-registry.json"))
    inspected = inspect_result(value, registry)
    require(inspected["api"] in observations.CONTRACTS, "PARTICIPANT_API_UNSUPPORTED")
    pages = []
    for page in inspected["pages"]:
        rows = [dict(zip(page["table"]["fields"], row)) for row in page["table"]["items"]]
        pages.append(observations.page_input({"schema": observations.PAGE_SCHEMA,
            "api": inspected["api"], "provider": "third-party-relay", "captured_at": page["captured_at"],
            "query": inspected["parameters"], "coverage": inspected["coverage"], "rows": rows,
            "physical_page": {"page_id": inspected["query_key"] + ":" + str(page["offset"]),
                "offset": page["offset"], "row_count": len(rows), "reported_total": page["table"]["reported_count"],
                "request_complete": inspected["complete"], "error": inspected["error"],
                "wire_sha256": page["transport"]["wire_sha256"]}}))
    # Preserve the original reference to the full capture, including attempts.
    # Only validated control and safe numbers are projected into the reader.
    original = value["capture"]
    groups = [p.get("attempts", []) for p in original["pages"]] + [original.get("attempts", [])]
    require(all(isinstance(group, list) and len(group) <= 20 for group in groups),
            "PARTICIPANT_ATTEMPTS_INVALID")
    attempts = sum(len(group) for group in groups)
    return pages, {key: deepcopy(inspected[key]) for key in
        ("query_key", "api", "parameters", "complete", "row_count", "page_count", "error", "coverage", "units")} | {
        "retained_attempt_count": attempts, "execution_history": "original-capture-reference",
        "capture_sha256": digest(encoded(value)), "source_requests": 0}


def _open_input(item, reader, prepared, cutoff, registry):
    documents = []
    for original in item["references"]:
        ref = _whole(original)
        source = reader.read_source(ref)
        meta = source["manifest"]["input"]
        require(meta["kind"] == "content" and isinstance(source["data"], str), "PARTICIPANT_CONTENT_REQUIRED")
        acquired = meta.get("acquired_at")
        require(acquired is not None and (acquired[:10] <= prepared.date().isoformat()
            if len(acquired) == 10 else _clock(acquired) <= prepared), "PARTICIPANT_SOURCE_AFTER_PREPARED")
        documents.append(_document(source["data"]))
    if item["format"] in ("registered-relay", "registered-relay-parts"):
        if item["format"] == "registered-relay-parts":
            capture = _document(observations.join_registered_capture(documents))
        else:
            capture = documents[0]
        pages, control = _registered_pages(capture, registry)
        require(control["api"] == item["api"], "PARTICIPANT_INPUT_SCOPE_MISMATCH")
    elif item["format"] == "legacy-participant":
        pages = [observations.legacy_page(documents[0])]
        control = {"complete": False, "error": None, "coverage": "LEGACY_EXCERPT_DOES_NOT_RETAIN_QUERY_TERMINATION"}
    else:
        pages = [observations.join_page(documents)]
        control = {"complete": pages[0]["physical_page"]["request_complete"],
                   "error": pages[0]["physical_page"]["error"], "coverage": pages[0]["coverage"]}
    require(all(page["api"] == item["api"] and _clock(page["captured_at"]) <= prepared
                for page in pages), "PARTICIPANT_INPUT_SCOPE_MISMATCH")
    views = [observations.normalize_rows(page) for page in pages]
    rows = []
    for page_index, view in enumerate(views):
        for row in view["rows"]:
            # Retain unqualified dates but do not expose future facts as of cutoff.
            require(row["reported_day"] is None or row["reported_day"] <= cutoff.date().isoformat(),
                    "PARTICIPANT_OBSERVATION_AFTER_CUTOFF")
            rows.append({**row, "input_id": item["input_id"], "page_index": page_index})
    return {**deepcopy(item), "status": "read" if control["complete"] else "incomplete",
            "control": control, "raw_rows": sum(v["raw_row_count"] for v in views),
            "accepted_rows": len(rows), "rejected_rows": [
                {"page_index": index, **entry} for index, view in enumerate(views) for entry in view["rejected_rows"]],
            "physical_pages": [deepcopy(v["physical_page"]) for v in views], "rows": rows}


def _history(rows):
    groups = {}
    for row in rows:
        if row["participant"] is None:
            continue
        key = (row["family"], row["company_id"], row["participant"]["participant_id"])
        groups.setdefault(key, []).append(row)
    history = []
    for (family, company_id, participant_id), items in sorted(groups.items()):
        items = sorted(items, key=lambda r: (r["reported_day"] or "", r["report_period"] or "",
                                             r["captured_at"], r["row_sha256"]))
        history.append({"family": family, "company_id": company_id, "participant_id": participant_id,
            "observations": [{"input_id": r["input_id"], "row_sha256": r["row_sha256"],
                "reported_day": r["reported_day"], "report_period": r["report_period"],
                "source_row": r["source_row"], "page_index": r["page_index"]} for r in items],
            "identity_qualification": "same-source-label-not-verified-same-person-or-position",
            "absence_means_exit": False, "summed_amount": None, "inferred_position": None})
    return history


def read_participants(reference, store, *, company_id=None, participant_id=None, registry=None):
    """Validate/read all declared families, then filter; access failures are fatal.

    Exact source refs preserve coverage/failures outside the visible selection.
    History is a list of reported facts, not a position reconstruction or forecast.
    """
    require(company_id is None or participant_id is None, "PARTICIPANT_ONE_QUERY_DIRECTION")
    for selector in (company_id, participant_id):
        if selector is not None:
            identifier(selector)
    ref = _whole(reference)
    with industry_source_scope(store) as scoped:
        cache, total_bytes, fatal_error = {}, 0, None
        class Reader:
            def read_source(self, source_ref):
                nonlocal total_bytes, fatal_error
                if fatal_error is not None:
                    raise fatal_error
                key = encoded(asdict(source_ref))
                if key not in cache:
                    try:
                        view = scoped.read_source(source_ref)
                    except RecordError as exc:
                        if isinstance(exc, StoreUnavailable) or str(exc) in FATAL:
                            fatal_error = exc
                        raise
                    total_bytes += len(encoded(view))
                    if total_bytes > MAX_READ_BYTES:
                        fatal_error = RecordError("PARTICIPANT_READING_BOUND")
                        raise fatal_error
                    cache[key] = view
                return deepcopy(cache[key])
        reader = Reader()
        root = reader.read_source(ref)
        require(root["manifest"]["input"]["kind"] == "content" and isinstance(root["data"], str),
                "PARTICIPANT_PACKET_REQUIRED")
        payload = packet_input(_document(root["data"]))
        prepared, cutoff = _clock(payload["prepared_at"]), _clock(payload["as_of"])
        outcomes, rows = [], []
        for item in payload["inputs"]:
            try:
                opened = _open_input(item, reader, prepared, cutoff, registry)
                rows.extend(opened.pop("rows"))
                outcomes.append(opened)
            except RecordError as exc:
                outcomes.append({**deepcopy(item), "status": "unavailable", "reason_code": str(exc)})
            if fatal_error is not None:
                raise fatal_error
        outcome_map = {o["input_id"]: o for o in outcomes}
        families = []
        for declared in payload["families"]:
            family_rows = [r for r in rows if r["family"] == declared["family"]]
            selected = [outcome_map[key] for key in declared["input_ids"]]
            bad = sum(o["status"] != "read" for o in selected)
            families.append({**deepcopy(declared), "status": "not-retained" if not selected else
                "partial" if bad else "read", "scope": {"declared_inputs": len(selected),
                    "unavailable_or_incomplete_inputs": bad, "rows": len(family_rows)}, "rows": family_rows})
        scope = {"declared_families": 8, "declared_inputs": len(outcomes), "declared_references":
                sum(len(i["references"]) for i in payload["inputs"]),
            "unavailable_inputs": sum(o["status"] == "unavailable" for o in outcomes),
            "incomplete_inputs": sum(o["status"] == "incomplete" for o in outcomes),
            "not_retained_families": sum(f["status"] == "not-retained" for f in families),
            "raw_rows": sum(o.get("raw_rows", 0) for o in outcomes), "accepted_rows": len(rows),
            "rejected_rows": sum(len(o.get("rejected_rows", [])) for o in outcomes),
            "distinct_row_versions": len({(r["api"], r["row_sha256"]) for r in rows})}
        result = {"schema": "participant-observations-result-v1", "reference": asdict(ref),
            "prepared_at": payload["prepared_at"], "as_of": payload["as_of"],
            "coverage_note": payload["coverage_note"], "families": families, "scope": scope,
            "input_outcomes": outcomes, "history": _history(rows), "estimate_changes": observations.estimate_changes(rows),
            "source_requests": 0, "writes": 0, "research_executed": False,
            "status": "partial" if any(f["status"] != "read" for f in families) else "read",
            "query": {"company_id": None, "participant_id": None},
            "limitations": ["DECLARED_RETAINED_SCOPE_NOT_ALL_PUBLIC_PARTICIPANTS_OR_CONTINUOUS_COVERAGE",
                "SAME_LABEL_NOT_AUTHENTICATED_PERSON_FUND_OR_CROSS_SOURCE_IDENTITY",
                "SOURCE_UNITS_UNKNOWN_EXCEPT_EXISTING_REVIEWED_FORECAST_FIELDS",
                "REPEATED_OR_OVERLAPPING_WINDOWS_NOT_SUMMED; ABSENCE_NOT_EXIT",
                "HOLDINGS_TURNOVER_VISITS_FORECASTS_PLANS_AND_EXECUTION_STAY_DISTINCT",
                "READING_NOT_RESEARCH_OR_BRIEF_ADOPTION_OR_HUMAN_ACCEPTANCE"]}
        result["view_version"] = digest(encoded(result))
        require(len(encoded(result)) <= MAX_RESULT_BYTES, "PARTICIPANT_RESULT_BOUND")
        def selected(row):
            return (company_id is None or row["company_id"] == company_id) and (participant_id is None or
                    row["participant"] is not None and row["participant"]["participant_id"] == participant_id)
        for family in families:
            family["rows"] = [r for r in family["rows"] if selected(r)]
        result["history"] = [h for h in result["history"] if (company_id is None or h["company_id"] == company_id)
                             and (participant_id is None or h["participant_id"] == participant_id)]
        result["estimate_changes"] = [c for c in result["estimate_changes"] if
            (company_id is None or c["company_id"] == company_id) and
            (participant_id is None or c["participant_id"] == participant_id)]
        result["query"] = {"company_id": company_id, "participant_id": participant_id}
    return result
