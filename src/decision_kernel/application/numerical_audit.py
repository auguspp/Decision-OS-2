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
PLACEHOLDER = re.compile(r"\{\{number:([a-z0-9][a-z0-9_-]{0,95})\}\}")
NOTICE = ("Declared numerical scope only; stored-value consistency is not source authenticity, "
          "economic sufficiency, qualified Market/Odds or human acceptance. Unmarked prose is not audited.")


def _shape(value: dict, required: set[str], optional: set[str] = frozenset()) -> None:
    require(isinstance(value, dict) and required <= set(value) <= required | optional,
            "INVALID_NUMERICAL_SHAPE")


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
        _shape(payload, {"schema", "sources", "inputs", "calculations"}, {"template"})
        require(payload["schema"] == VERSION and len(encoded(payload)) <= MAX_BYTES,
                "NUMERICAL_SCHEMA_OR_SIZE_LIMIT")
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
        require("{{number:" not in PLACEHOLDER.sub("", template), "INVALID_NUMERICAL_PLACEHOLDER")
        calc_ids = {item["id"] for item in payload["calculations"]}
        require(all(match in calc_ids for match in PLACEHOLDER.findall(template)), "UNKNOWN_NUMERICAL_PLACEHOLDER")
        # Canonical JSON detaches mutable input and is also the retained audit identity.
        payload = json.loads(encoded(payload))
        cache: dict[int, dict | RecordError] = {}
        source_notes: dict[int, dict] = {}
        quantities: dict[str, Quantity] = {}
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
                if "assumption" in item:
                    _shape(item, common | {"assumption"})
                    checked_text(item["assumption"], limit=4096)
                    warnings = ["EXPLICIT_ASSUMPTION"]
                    row["assumption"] = item["assumption"]
                else:
                    _shape(item, common | {"source", "locator"}, {"context_bindings"})
                    view = read(item["source"])
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
                quantity = Quantity(number(item["value"]), unit(item["unit"]), _context(item), tuple(warnings))
                quantities[item["id"]] = quantity
                row.update(status="conditional" if warnings else "checked", value=str(quantity.value),
                           unit=quantity.unit, warnings=warnings)
            except (RecordError, KeyError, TypeError) as error:
                row["error"] = str(error) if isinstance(error, RecordError) else "INVALID_NUMERICAL_SHAPE"
            inputs.append(row)

        for item in payload["calculations"]:
            row = {"id": item["id"], "status": "blocked"}
            try:
                _shape(item, {"id", "operation", "operands", "unit", *CONTEXT_FIELDS},
                       {"places", "claimed", "context_bridge"})
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
        appendix = START + NOTICE + "\n\n" + table + ("\n" + rendered + "\n" if template else "") + INPUT + encoded(payload).decode() + END
        require(len(appendix.encode("utf-8")) <= MAX_BYTES, "NUMERICAL_APPENDIX_TOO_LARGE")
        return {"schema": "research-numerical-audit-v1", "status": status,
                "input_sha256": digest(encoded(payload)), "arithmetic": "Decimal-80-ROUND_HALF_EVEN",
                "sources": [source_notes[i] for i in sorted(source_notes)], "inputs": inputs,
                "calculations": calculations, "table_markdown": table, "rendered_excerpt": rendered,
                "appendix": appendix, "coverage": "declared-inputs-calculations-and-number-placeholders-only",
                "unmarked_prose": "not-audited", "research_assessment": "not-performed",
                "source_authenticity": "not-certified", "persistence": "not-written"}

    def saved(self, reference: BlobRef) -> dict:
        """Recompute an exact saved revision; never follow current or change old text."""
        research = self.store.read_revision(reference)
        body = research["body"]
        if START not in body:
            return {"status": "not-declared", "reference": asdict(reference), "research_assessment": "not-performed"}
        require(body.count(START) == body.count(INPUT) == body.count(END) == 1, "INVALID_NUMERICAL_APPENDIX")
        start, split, end = body.index(START), body.index(INPUT), body.index(END)
        require(start < split < end, "INVALID_NUMERICAL_APPENDIX")
        raw = body[split + len(INPUT):end].encode("utf-8")
        require(len(raw) <= MAX_BYTES, "NUMERICAL_SCHEMA_OR_SIZE_LIMIT")
        try:
            payload = json.loads(raw)
            require(encoded(payload) == raw, "NONCANONICAL_NUMERICAL_APPENDIX")
            sources = [SourceRef.from_dict(s) for s in payload["sources"]]
        except (TypeError, KeyError, ValueError, RecursionError):
            raise RecordError("INVALID_NUMERICAL_APPENDIX") from None
        retained = {encoded(asdict(SourceRef.from_dict(s))) for s in research["record"]["input"]["sources"]}
        require(all(encoded(asdict(s)) in retained for s in sources), "NUMERICAL_SOURCE_NOT_RETAINED")
        result = self.audit(payload)
        result["reference"] = asdict(reference)
        if result["status"] == "blocked":
            result["saved_render"] = "not-comparable-until-inputs-and-calculations-resolved"
        elif result["appendix"] != body[start:end + len(END)]:
            result.update(status="blocked", saved_render="mismatch", error="SAVED_NUMERICAL_DISPLAY_MISMATCH")
        else:
            result["saved_render"] = "matches-recalculation"
        return result
