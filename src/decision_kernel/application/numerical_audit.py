"""Read-only numeric workpapers over exact SourceRefs; no acquisition or new store.

Only declared inputs/calculations and generated excerpts are checked. A matching
stored field does not authenticate the publisher or certify its economic meaning.
Text boundary limits: docs/research/numeric-text-boundaries.md.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import json
import re
import unicodedata
from typing import TYPE_CHECKING

from decision_kernel.application.numerical_evidence_time import (
    EVIDENCE_TIME_SCOPE, VERSION as TIME_VERSION, assess_source_time, time_bounds,
)
from decision_kernel.application.numerical_evidence_use import (
    EVIDENCE_USE_SCOPE, VERSION as USE_VERSION, calculation_use, input_use,
)
from decision_kernel.domain.records import (
    BlobRef, IntegrityError, MAX_BYTES, MissingObject, RecordError, SourceRef,
    StoreUnavailable, checked_text, digest, encoded, identifier, require,
)
from decision_kernel.domain.research_numbers import (
    CONTEXT_FIELDS, VERSION, Quantity, calculate, number, unit,
)

if TYPE_CHECKING:
    from decision_kernel.application.versioned_store import VersionedStore

START = "\n\n<!-- decision-os-numbers-v1:start -->\n"
INPUT = "\n<!-- decision-os-numbers-v1:input -->\n```json\n"
END = "```\n<!-- decision-os-numbers-v1:end -->\n"
NUMERICAL_SCHEMAS = (VERSION, TIME_VERSION, USE_VERSION)
_MARKERS = {
    VERSION: (START, INPUT, END),
    TIME_VERSION: ("\n\n<!-- decision-os-numbers-v2:start -->\n",
                   "\n<!-- decision-os-numbers-v2:input -->\n```json\n",
                   "```\n<!-- decision-os-numbers-v2:end -->\n"),
    USE_VERSION: ("\n\n<!-- decision-os-numbers-v3:start -->\n",
                  "\n<!-- decision-os-numbers-v3:input -->\n```json\n",
                  "```\n<!-- decision-os-numbers-v3:end -->\n"),
}
_MARKER_PREFIX = re.compile(r"<!--\s*decision-os-numbers-")
_STRUCTURAL_MARKER = re.compile(r"^[ \t]*<!--\s*decision-os-numbers-", re.MULTILINE)
PLACEHOLDER = re.compile(r"\{\{number:([a-z0-9][a-z0-9_-]{0,95})\}\}")
NOTICE = ("Declared numerical scope only; stored-value consistency is not source authenticity, "
          "economic sufficiency, qualified Market/Odds or human acceptance. Unmarked prose is not audited.")


def _shape(value: dict, required: set[str], optional: set[str] = frozenset()) -> None:
    require(isinstance(value, dict) and required <= set(value) <= required | optional,
            "INVALID_NUMERICAL_SHAPE")


def has_numerical_appendix(body: str) -> bool:
    """Recognize reserved numerical markers, including damaged/partial markers.

    This is detection, not validation. ``saved`` rejects incomplete, mixed and
    unsupported markers instead of presenting them as an absent workpaper.
    """
    return _MARKER_PREFIX.search(body) is not None


def validate_numerical_workpaper(payload: dict, *, with_sources: bool = False) -> None:
    """Validate the version envelope before delivery has source references.

    This function performs no source reads and grants no time-use qualification.
    Individual inputs and their retained source clocks are assessed by ``audit``.
    """
    require(isinstance(payload, dict), "INVALID_NUMERICAL_SHAPE")
    required = {"schema", "inputs", "calculations"}
    if with_sources:
        required.add("sources")
    timed = payload.get("schema") == TIME_VERSION or (payload.get("schema") == USE_VERSION
            and bool({"knowledge_cutoff", "evaluated_at"} & set(payload)))
    if timed:
        required.update(("knowledge_cutoff", "evaluated_at"))
    _shape(payload, required, {"template"})
    require(payload["schema"] in NUMERICAL_SCHEMAS and len(encoded(payload)) <= MAX_BYTES,
            "NUMERICAL_SCHEMA_OR_SIZE_LIMIT")
    if timed:
        time_bounds(payload)


def _context(item: dict) -> tuple[str, str, str]:
    return tuple(checked_text(item[key], limit=256) for key in CONTEXT_FIELDS)


def _check_numeric_span(data: str, start: int, end: int) -> None:
    """Reject cropped/ambiguous tokens without normalizing the selected source bytes."""
    selected = data[start:end]
    token = selected.strip()
    require(bool(token), "NUMERIC_SPAN_NOT_AVAILABLE")
    # Boundary checks must see past padding INSIDE the requested span as well.
    left = start + len(selected) - len(selected.lstrip())
    right = left + len(token)

    def folded(char: str) -> str:
        return unicodedata.normalize("NFKC", char)

    for index, punctuation in ((left - 1, ".,+-eE"), (right, ".,eE")):
        if 0 <= index < len(data):
            char = data[index]
            require(not char.isdigit() and folded(char) not in punctuation,
                    "PARTIAL_NUMERIC_TOKEN")

    def neighbor(index: int, step: int) -> str:
        # PDF/text representations can separate a sign using horizontal whitespace
        # or a formatting mark. Do not silently cross into another source line.
        while 0 <= index < len(data):
            char = data[index]
            if char in "\n\r\v\f\x85\u2028\u2029":
                return ""
            if not (char.isspace() or unicodedata.category(char) == "Cf"):
                return char
            index += step
        return ""

    before, after = neighbor(left - 1, -1), neighbor(right, 1)
    for char in (before, after):
        if char:
            require(folded(char) not in ("+", "-", "−", "±", "➖")
                    and unicodedata.category(char) != "Pd", "PARTIAL_NUMERIC_TOKEN")
    # Parentheses may encode an accounting negative. Refuse the ambiguity rather
    # than reinterpret '(50)' as +50 or auto-convert it to -50.
    require(folded(before) != "(" and folded(after) != ")", "PARTIAL_NUMERIC_TOKEN")


def _extract(data, locator: dict, *, numeric: bool = False) -> str:
    require(isinstance(locator, dict), "INVALID_NUMERIC_LOCATOR")
    if isinstance(data, list):
        _shape(locator, {"row", "column"})
        row, column = locator["row"], locator["column"]
        require(type(row) is int and 1 <= row <= len(data)
                and isinstance(column, str) and column in data[row - 1], "NUMERIC_CELL_NOT_AVAILABLE")
        value = data[row - 1][column]
    elif isinstance(data, str):
        _shape(locator, {"start", "end"})
        start, end = locator["start"], locator["end"]
        require(type(start) is int and type(end) is int and 0 <= start < end <= len(data),
                "NUMERIC_SPAN_NOT_AVAILABLE")
        if numeric:
            _check_numeric_span(data, start, end)
        value = data[start:end]
    else:
        raise RecordError("SOURCE_VALUE_NOT_STORED")
    return checked_text(value, limit=256).strip()


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace(
        "|", "&#124;").replace("`", "&#96;").replace("\n", " ").replace("\r", " ")


class NumericalAudit:
    def __init__(self, store: VersionedStore):
        self.store = store

    def audit(self, payload: dict) -> dict:
        validate_numerical_workpaper(payload, with_sources=True)
        semantic = payload["schema"] == USE_VERSION
        timed = payload["schema"] == TIME_VERSION or (semantic and "knowledge_cutoff" in payload)
        bounds = time_bounds(payload) if timed else None
        markers = _MARKERS[payload["schema"]]
        require(isinstance(payload["sources"], list) and len(payload["sources"]) <= 64,
                "NUMERICAL_SOURCE_LIMIT")
        sources = [SourceRef.from_dict(s) for s in payload["sources"]]
        require(len({encoded(asdict(s)) for s in sources}) == len(sources), "DUPLICATE_NUMERICAL_SOURCE")
        for name in ("inputs", "calculations"):
            require(isinstance(payload[name], list) and 1 <= len(payload[name]) <= 64
                    and all(isinstance(item, dict) and "id" in item for item in payload[name]),
                    "NUMERICAL_ITEM_LIMIT")
        ids = [identifier(item["id"]) for item in payload["inputs"] + payload["calculations"]]
        require(len(ids) == len(set(ids)), "DUPLICATE_NUMERICAL_ID")
        template = checked_text(payload.get("template", ""), empty=True)
        require(not any(mark.strip() in template for mark in (START, INPUT, END)), "RESERVED_NUMERICAL_MARKER")
        if payload["schema"] != VERSION:
            require(not has_numerical_appendix(template), "RESERVED_NUMERICAL_MARKER")
        require("{{number:" not in PLACEHOLDER.sub("", template), "INVALID_NUMERICAL_PLACEHOLDER")
        calc_ids = {item["id"] for item in payload["calculations"]}
        require(all(match in calc_ids for match in PLACEHOLDER.findall(template)), "UNKNOWN_NUMERICAL_PLACEHOLDER")
        # Canonical JSON detaches mutable input and is also the retained audit identity.
        payload = json.loads(encoded(payload))
        cache: dict[int, dict | RecordError] = {}
        source_notes: dict[int, dict] = {}
        quantities: dict[str, Quantity] = {}
        evidence_uses: dict[str, dict] = {}
        inputs, calculations = [], []

        def read(index: int) -> dict:
            require(type(index) is int and 0 <= index < len(sources), "NUMERICAL_SOURCE_INDEX")
            if index not in cache:
                try:
                    view = self.store.read_source(sources[index])
                    cache[index] = view
                    meta = view["manifest"]["input"]
                    source_notes[index] = {"index": index, "reference": asdict(sources[index]),
                                           "custody": meta["custody"], "origin": meta["origin"],
                                           "published_at": meta.get("published_at"),
                                           "acquired_at": meta.get("acquired_at"),
                                           "report_period": meta.get("report_period"),
                                           "original_verified": False,
                                           "limitations": view["limitations"]}
                    if timed:
                        time_use = assess_source_time(view["manifest"], *bounds)
                        source_notes[index].update(stored_at=time_use["stored_at"], evidence_time=time_use)
                except (RecordError, KeyError, TypeError) as error:
                    code = ("SOURCE_MISSING" if isinstance(error, MissingObject) else
                            "SOURCE_UNAVAILABLE" if isinstance(error, StoreUnavailable) else
                            "SOURCE_INTEGRITY_FAILED" if isinstance(error, IntegrityError) else
                            "SOURCE_VALIDATION_FAILED")
                    cache[index] = RecordError(code)
                    source_notes[index] = {"index": index, "reference": asdict(sources[index]), "error": code}
            view = cache[index]
            if isinstance(view, RecordError):
                raise view
            return view

        for item in payload["inputs"]:
            row = {"id": item["id"], "status": "blocked"}
            try:
                common = {"id", "value", "unit", *CONTEXT_FIELDS}
                if semantic:
                    common.add("evidence_use")
                view = None
                if "assumption" in item:
                    _shape(item, common | {"assumption"})
                    checked_text(item["assumption"], limit=4096)
                    warnings = ["EXPLICIT_ASSUMPTION"]
                    row["assumption"] = item["assumption"]
                    if timed or semantic:
                        row["evidence_time"] = {"status": "not-applicable",
                                                "reason": "explicit-assumption-is-not-source-evidence"}
                else:
                    _shape(item, common | {"source", "locator"},
                           {"context_bindings", "evidence_bindings"} if semantic else {"context_bindings"})
                    view = read(item["source"])
                    if timed:
                        row["evidence_time"] = source_notes[item["source"]]["evidence_time"]
                        require(row["evidence_time"]["status"] == "checked",
                                row["evidence_time"].get("error", "NUMERICAL_TIME_UNAVAILABLE"))
                    elif semantic:
                        row["evidence_time"] = {"status": "not-requested"}
                    raw = _extract(view["data"], item["locator"], numeric=True)
                    row["source_value"] = raw
                    require(number(raw) == number(item["value"]), "SOURCE_VALUE_MISMATCH")
                    bindings = item.get("context_bindings", {})
                    require(isinstance(bindings, dict) and set(bindings) <= {"unit", *CONTEXT_FIELDS},
                            "INVALID_CONTEXT_BINDINGS")
                    warnings = []
                    for key in ("unit", *CONTEXT_FIELDS):
                        if key not in bindings:
                            warnings.append("CONTEXT_DECLARED_NOT_SOURCE_CHECKED:" + key)
                            continue
                        if isinstance(view["data"], list):
                            require(isinstance(bindings[key], dict)
                                    and bindings[key].get("row") == item["locator"]["row"],
                                    "CONTEXT_ROW_MISMATCH")
                        actual = _extract(view["data"], bindings[key])
                        require((unit(actual) == unit(item[key]) if key == "unit" else actual == item[key]),
                                "SOURCE_CONTEXT_MISMATCH:" + key)
                    if isinstance(view["data"], str):
                        warnings.append("TEXT_CONTEXT_RELATION_REQUIRES_REVIEW")
                    if view["manifest"]["input"]["custody"] != "provided":
                        warnings.append("SOURCE_IS_EXCERPT_OR_LOCATOR")
                    if view["limitations"]:
                        warnings.append("SOURCE_HAS_LIMITATIONS")
                if semantic:
                    use, use_warnings = input_use(item, view, _extract)
                    row["evidence_use"] = use
                    warnings.extend(use_warnings)
                quantity = Quantity(number(item["value"]), unit(item["unit"]), _context(item), tuple(warnings))
                quantities[item["id"]] = quantity
                if semantic:
                    evidence_uses[item["id"]] = use
                row.update(status="conditional" if warnings else "checked", value=str(quantity.value),
                           unit=quantity.unit, warnings=warnings)
            except (RecordError, KeyError, TypeError) as error:
                row["error"] = str(error) if isinstance(error, RecordError) else "INVALID_NUMERICAL_SHAPE"
            inputs.append(row)

        for item in payload["calculations"]:
            row = {"id": item["id"], "status": "blocked"}
            try:
                required = {"id", "operation", "operands", "unit", *CONTEXT_FIELDS}
                optional = {"places", "claimed", "context_bridge"}
                if semantic:
                    required.add("evidence_use")
                    optional.add("evidence_bridge")
                _shape(item, required, optional)
                require(isinstance(item["operands"], list) and 1 <= len(item["operands"]) <= 16
                        and all(isinstance(i, str) and i in quantities for i in item["operands"]),
                        "OPERAND_NOT_AVAILABLE")
                values = [quantities[i] for i in item["operands"]]
                context = _context(item)
                bridges = item.get("context_bridge", {})
                require(isinstance(bridges, dict) and set(bridges) <= set(CONTEXT_FIELDS), "INVALID_CONTEXT_BRIDGE")
                for reason in bridges.values():
                    checked_text(reason, limit=4096)
                warnings = sorted({warning for value in values for warning in value.warnings})
                for index, key in enumerate(CONTEXT_FIELDS):
                    require(all(value.context[index] == context[index] for value in values) or key in bridges,
                            "CALCULATION_CONTEXT_MISMATCH:" + key)
                if bridges:
                    warnings.append("DECLARED_CONTEXT_BRIDGE_REQUIRES_REVIEW")
                if semantic:
                    use, use_warnings = calculation_use(item, [evidence_uses[i] for i in item["operands"]])
                    row["evidence_use"] = use
                    warnings.extend(use_warnings)
                value, displayed, rounded = calculate(item["operation"], values, item["unit"], item.get("places", 2))
                row.update(value=str(value), display_number=displayed, unit=unit(item["unit"]),
                           context=dict(zip(CONTEXT_FIELDS, context)), arithmetic_rounded=rounded)
                if "claimed" in item:
                    require(number(item["claimed"]) == Decimal(displayed), "CLAIMED_DISPLAY_MISMATCH")
                if rounded:
                    warnings.append("ARITHMETIC_ROUNDED_TO_80_DIGITS")
                row.update(status="conditional" if warnings else "checked", warnings=sorted(set(warnings)),
                           display=displayed + " " + unit(item["unit"]))
                quantities[item["id"]] = Quantity(value, unit(item["unit"]), context, tuple(row["warnings"]))
                if semantic:
                    evidence_uses[item["id"]] = use
            except (RecordError, KeyError, TypeError) as error:
                row["error"] = str(error) if isinstance(error, RecordError) else "INVALID_NUMERICAL_SHAPE"
            calculations.append(row)

        blocked = any(row["status"] == "blocked" for row in inputs + calculations)
        status = "blocked" if blocked else "conditional" if any(
            row["status"] == "conditional" for row in inputs + calculations) else "checked"
        by_id = {row["id"]: row for row in calculations}
        def display(ident):
            row = by_id[ident]
            return row["display"] if row["status"] != "blocked" else "[UNAVAILABLE:" + ident + ":" + row["error"] + "]"
        rendered = PLACEHOLDER.sub(lambda match: display(match[1]), template)
        lines = ["| ID | Computed display | Status |", "|---|---|---|"]
        lines += [f"| {row['id']} | {_escape(display(row['id']))} | {row['status']} |" for row in calculations]
        table = "\n".join(lines) + "\n"
        start_marker, input_marker, end_marker = markers
        appendix = start_marker + NOTICE + "\n\n" + table + ("\n" + rendered + "\n" if template else "") + input_marker + encoded(payload).decode() + end_marker
        require(len(appendix.encode("utf-8")) <= MAX_BYTES, "NUMERICAL_APPENDIX_TOO_LARGE")
        result = {"schema": "research-numerical-audit-v3" if semantic else
                           "research-numerical-audit-v2" if timed else "research-numerical-audit-v1", "status": status,
                "input_sha256": digest(encoded(payload)), "arithmetic": "Decimal-80-ROUND_HALF_EVEN",
                "sources": [source_notes[i] for i in sorted(source_notes)], "inputs": inputs,
                "calculations": calculations, "table_markdown": table, "rendered_excerpt": rendered,
                "appendix": appendix, "coverage": "declared-inputs-calculations-and-number-placeholders-only",
                "unmarked_prose": "not-audited", "research_assessment": "not-performed",
                "source_authenticity": "not-certified", "persistence": "not-written"}
        if timed:
            result.update(knowledge_cutoff=payload["knowledge_cutoff"], evaluated_at=payload["evaluated_at"],
                          evidence_time_scope=EVIDENCE_TIME_SCOPE)
        if semantic:
            result["evidence_use_scope"] = EVIDENCE_USE_SCOPE
            result.setdefault("evidence_time_scope", "not-requested")
        return result

    def saved(self, reference: BlobRef) -> dict:
        """Recompute an exact saved revision; never follow current or change old text."""
        research = self.store.read_revision(reference)
        body = research["body"]
        if not has_numerical_appendix(body):
            return {"status": "not-declared", "reference": asdict(reference), "research_assessment": "not-performed"}
        positions = [(body.index(markers[0]), version) for version, markers in _MARKERS.items()
                     if markers[0] in body]
        require(bool(positions), "INVALID_NUMERICAL_APPENDIX")
        # A historical v1 template may legitimately quote the then-unreserved v2
        # marker. The outer workpaper starts first; its rendered content and
        # canonical input are data, not another structural appendix.
        _, version = min(positions)
        start_marker, input_marker, end_marker = _MARKERS[version]
        require(body.count(start_marker) == body.count(input_marker) == body.count(end_marker) == 1,
                "INVALID_NUMERICAL_APPENDIX")
        start, split, end = body.index(start_marker), body.index(input_marker), body.index(end_marker)
        require(start < split < end, "INVALID_NUMERICAL_APPENDIX")
        outside = body[:start] + body[end + len(end_marker):]
        require(_STRUCTURAL_MARKER.search(outside) is None, "INVALID_NUMERICAL_APPENDIX")
        if version != VERSION:
            # Exclude JSON string values from structural detection. V2 reserves
            # markers in its rendered template; v1 keeps its original semantics.
            rendered_section = body[start:split + len(input_marker)] + body[end:end + len(end_marker)]
            require(len(_STRUCTURAL_MARKER.findall(rendered_section)) == 3, "INVALID_NUMERICAL_APPENDIX")
        raw = body[split + len(input_marker):end].encode("utf-8")
        require(len(raw) <= MAX_BYTES, "NUMERICAL_SCHEMA_OR_SIZE_LIMIT")
        try:
            payload = json.loads(raw)
            require(encoded(payload) == raw, "NONCANONICAL_NUMERICAL_APPENDIX")
            require(payload["schema"] == version, "INVALID_NUMERICAL_APPENDIX")
            sources = [SourceRef.from_dict(s) for s in payload["sources"]]
        except (TypeError, KeyError, ValueError, RecursionError):
            raise RecordError("INVALID_NUMERICAL_APPENDIX") from None
        retained = {encoded(asdict(SourceRef.from_dict(s))) for s in research["record"]["input"]["sources"]}
        require(all(encoded(asdict(s)) in retained for s in sources), "NUMERICAL_SOURCE_NOT_RETAINED")
        result = self.audit(payload)
        result["reference"] = asdict(reference)
        if result["status"] == "blocked":
            result["saved_render"] = "not-comparable-until-inputs-and-calculations-resolved"
        elif result["appendix"] != body[start:end + len(end_marker)]:
            result.update(status="blocked", saved_render="mismatch", error="SAVED_NUMERICAL_DISPLAY_MISMATCH")
        else:
            result["saved_render"] = "matches-recalculation"
        return result
