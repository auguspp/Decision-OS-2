"""Exact research -> frozen output -> conditional result, on existing artifacts.

No source acquisition, current-research writes, probability invention, general
model execution or parallel result store. Historical results remain readable;
the current-use hint is deliberately outside the immutable calculation.
"""
from __future__ import annotations

import base64
from dataclasses import asdict
from datetime import datetime, timezone
import json

from decision_kernel.application.numerical_audit import _extract
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.conditional_odds import (
    RETAINED_VERSION, calculate, calculate_retained, day, number, shape, timestamp,
    validate_close_policy, validate_price, validate_spec,
)
from decision_kernel.domain.records import (
    BlobRef, RecordError, SourceRef, checked_text, digest, encoded, require,
)
from decision_kernel.domain.research_artifacts import ArtifactInput, artifact_reference
from decision_kernel.infrastructure.relay_http import ROOT

SPEC_RECORD = "frozen-valuation-record-v1"
RESULT_RECORD = "conditional-odds-record-v1"
RECORD_LAYOUTS = {
    # This custody envelope wraps either activity event version. Segment seals
    # are ordinary ResearchArtifacts and share its original store/journal.
    "odds-watch-activity-record-v1": ("odds-watch-activity-", "odds-watch-activity.json"),
    "frozen-odds-watch-record-v1": ("odds-watch-", "odds-watch.json"),
    "odds-watch-check-record-v1": ("odds-watch-check-", "odds-watch-check.json"),
    SPEC_RECORD: ("odds-spec-", "valuation-spec.json"),
    RESULT_RECORD: ("odds-result-", "conditional-odds.json"),
    "frozen-odds-assessment-record-v1": ("odds-assessment-", "odds-assessment.json"),
    "probability-odds-record-v1": ("odds-probability-result-", "probability-odds.json"),
}


def record_artifact(schema, value, rights_basis):
    """Same immutable record encoding, also usable in one owner-controlled CAS."""
    raw = encoded({"schema": schema, "value": value, "rights_basis": rights_basis})
    require(schema in RECORD_LAYOUTS, "ODDS_RECORD_SCHEMA")
    prefix, filename = RECORD_LAYOUTS[schema]
    return ArtifactInput.from_bytes(prefix + digest(raw), filename, "application/json", raw)


def publication(payload, key):
    shape(payload, {key, "rights_confirmed", "rights_basis"})
    require(payload["rights_confirmed"] is True, "ODDS_PUBLICATION_RIGHTS_REQUIRED")
    checked_text(payload["rights_basis"], limit=4096)
    return payload[key]


def validate_calculation(payload):
    if isinstance(payload, dict) and "market" in payload:
        shape(payload, {"spec_ref", "market", "market_policy", "evaluated_at"})
        validate_close_policy(payload["market_policy"])
        timestamp(payload["evaluated_at"])
    else:
        shape(payload, {"spec_ref", "price"})


class ConditionalOdds:
    def __init__(self, store, registry=None, *, clock=None, allow_declared_only=False):
        require(type(allow_declared_only) is bool, "ODDS_DECLARED_ONLY_FLAG_INVALID")
        self.store = store
        self.artifacts = ResearchArtifacts(store)
        self.registry = registry
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.allow_declared_only = allow_declared_only

    def _frozen(self, payload):
        spec = validate_spec(payload)
        research = self.store.read_revision(BlobRef(**spec["research_ref"]))
        record = research["record"]
        sources = record["input"]["sources"]
        indices = sorted({i for world in spec["worlds"] for i in world["source_indices"]})
        evidence = []
        for index in indices:
            require(index < len(sources), "ODDS_SOURCE_OUTSIDE_FROZEN_RESEARCH")
            view = self.store.read_source(SourceRef.from_dict(sources[index]))
            evidence.append({"index": index, "reference": sources[index],
                             "availability": view["availability"],
                             "limitations": view["limitations"], "original_verified": False})
        return {"schema": "frozen-valuation-spec-v1", "spec": spec,
                "spec_sha256": digest(encoded(spec)),
                "research_body_sha256": record["body"]["sha256"],
                "research_method": record["input"]["method"],
                "research_unresolved": record["input"]["unresolved"],
                "research_sources": sources, "cited_source_readback": evidence,
                "economic_assessment": "not-performed"}

    def _put(self, submission_id, schema, value, rights_basis, *, before_new_write=None):
        return self.artifacts.put(submission_id, record_artifact(schema, value, rights_basis), before_new_write=before_new_write)

    def _open(self, reference, schema):
        ref = artifact_reference(reference)
        view = self.artifacts.read(ref)
        try:
            raw = base64.b64decode(view["data_base64"], validate=True)
            value = json.loads(raw)
            shape(value, {"schema", "value", "rights_basis"})
            require(value["schema"] == schema and encoded(value) == raw, "ODDS_RECORD_MISMATCH")
            checked_text(value["rights_basis"], limit=4096)
            require(schema in RECORD_LAYOUTS, "ODDS_RECORD_SCHEMA")
            prefix, _ = RECORD_LAYOUTS[schema]
            require(view["metadata"]["artifact_id"] == prefix + digest(raw)
                    and view["metadata"]["media_type"] == "application/json", "ODDS_ARTIFACT_IDENTITY_MISMATCH")
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise RecordError("ODDS_RECORD_INVALID") from None
        return value["value"]

    def freeze(self, payload, *, submission_id):
        spec = publication(payload, "spec")
        frozen = self._frozen(spec)
        ref = self._put(submission_id, SPEC_RECORD, frozen, payload["rights_basis"])
        return {"reference": asdict(ref), "spec_sha256": frozen["spec_sha256"],
                "research_ref": frozen["spec"]["research_ref"]}

    def read_spec(self, reference):
        frozen = self._open(reference, SPEC_RECORD)
        require(isinstance(frozen, dict) and "spec" in frozen, "ODDS_FROZEN_SPEC_INVALID")
        require(encoded(self._frozen(frozen["spec"])) == encoded(frozen), "ODDS_FROZEN_SPEC_CHANGED")
        return frozen

    def _price_evidence(self, price):
        evidence = price["evidence"]
        if evidence is None:
            return {"status": "declared-reference-only", "market_qualification": "not-established"}
        view = self.store.read_source(SourceRef.from_dict(evidence["source"]))
        if price["kind"] == "relay-observation":
            origin = view["manifest"]["input"]["origin"]
            require(origin.startswith(ROOT + "/"), "ODDS_RELAY_SOURCE_ORIGIN_MISMATCH")
        source_price = number(_extract(view["data"], evidence["value_locator"], numeric=True))
        code = _extract(view["data"], evidence["security_locator"])
        source_date = _extract(view["data"], evidence["date_locator"])
        if len(source_date) == 8 and source_date.isascii() and source_date.isdigit():
            source_date = source_date[:4] + "-" + source_date[4:6] + "-" + source_date[6:]
        require(source_price == number(price["price"]), "ODDS_SOURCE_PRICE_MISMATCH")
        require(code == price["security"]["code"], "ODDS_SOURCE_SECURITY_MISMATCH")
        require(day(source_date) == timestamp(price["as_of"]).date(), "ODDS_SOURCE_PRICE_DATE_MISMATCH")
        return {"status": "stored-price-security-date-match", "reference": evidence["source"],
                "custody": view["manifest"]["input"]["custody"], "limitations": view["limitations"],
                "currency_share_class_and_capital": "declared-not-source-certified",
                "original_verified": False, "market_qualification": "not-established"}

    def compute(self, payload):
        validate_calculation(payload)
        spec_ref = artifact_reference(payload["spec_ref"])
        frozen = self.read_spec(spec_ref)
        if "market" in payload:
            from pathlib import Path
            from decision_kernel.application.odds_market import FTSHARE_CLOSE_SCHEMA, qualify_close
            from decision_kernel.application.relay_registry import RelayRegistry
            require(isinstance(payload["market"], dict)
                and payload["market"].get("schema") == FTSHARE_CLOSE_SCHEMA, "ODDS_MARKET_ADAPTER_UNSUPPORTED")
            evaluated = timestamp(payload["evaluated_at"])
            policy = validate_close_policy(payload["market_policy"])
            require(evaluated <= self.clock(), "ODDS_EVALUATION_IN_FUTURE")
            require(timestamp(policy["as_of"]) <= evaluated, "ODDS_POLICY_AFTER_EVALUATION")
            registry = self.registry or RelayRegistry(Path("docs/sources/relay-registry.json"))
            market = qualify_close(payload["market"], frozen["spec"], policy,
                                   payload["evaluated_at"], registry, self.store)
            result = calculate_retained(frozen["spec"], market["price"])
            price_check = {"status": market["status"], "source_references": market["source_references"],
                "market_qualification": market["status"], "share_count_qualification":
                "frozen-research-assumption-not-source-certified", "economic_assessment": "not-performed"}
            result.update(market=market, market_policy=policy, market_qualification=market["status"],
                          calculation_input=payload)
        else:
            price = validate_price(payload["price"], frozen["spec"])
            price_check = self._price_evidence(price)
            result = calculate(frozen["spec"], price)
        result.update(spec_ref=asdict(spec_ref), spec_sha256=frozen["spec_sha256"],
                      research_ref=frozen["spec"]["research_ref"],
                      research_body_sha256=frozen["research_body_sha256"],
                      research_unresolved=frozen["research_unresolved"], price_check=price_check,
                      cited_source_readback=frozen["cited_source_readback"])
        if frozen["research_unresolved"]:
            result["limitations"].append("FROZEN_RESEARCH_HAS_UNRESOLVED_LIMITATIONS")
        result["limitations"].append("PRICE_CONTEXT_DOES_NOT_ESTABLISH_HISTORICAL_PIT")
        return json.loads(encoded(result)) if "market" in payload else result

    def save(self, payload, *, submission_id):
        calculation = publication(payload, "calculation")
        result = self.compute(calculation)
        # A price with no fixed slice cannot be re-derived later: the stored
        # bytes that justified it were never bound. New records must not take
        # that path silently. Historical ones stay readable because compute()
        # still reconstructs them; only this write is narrowed.
        declared_only = result["price_check"]["status"] == "declared-reference-only"
        require(not declared_only or self.allow_declared_only, "ODDS_PRICE_EVIDENCE_REQUIRED")
        ref = self._put(submission_id, RESULT_RECORD, result, payload["rights_basis"])
        return {"reference": asdict(ref), "calculation_sha256": digest(encoded(result)),
                "spec_ref": result["spec_ref"], "research_ref": result["research_ref"],
                "price_evidence": "declared-only-acknowledged" if declared_only else "fixed-slice-verified"}

    def read(self, reference):
        result = self._open(reference, RESULT_RECORD)
        require(isinstance(result, dict) and {"spec_ref", "price"} <= set(result), "ODDS_RESULT_INVALID")
        if result.get("schema") == RETAINED_VERSION:
            require("calculation_input" in result, "ODDS_RESULT_INVALID")
            rebuilt = self.compute(result["calculation_input"])
        else:
            rebuilt = self.compute({"spec_ref": result["spec_ref"], "price": result["price"]})
        require(encoded(rebuilt) == encoded(result), "ODDS_RECALCULATION_MISMATCH")
        # Mutable current information is not part of the result/hash or a rewrite.
        hint = {"status": "unknown", "current_research": None,
                "meaning": "pointer comparison only, not economic validity or acceptance"}
        try:
            research = self.store.read_revision(BlobRef(**result["research_ref"]))
            current = self.store.current(research["record"]["input"]["case_id"])
            hint.update(status="same-revision" if current == BlobRef(**result["research_ref"]) else
                        "historical-research-not-current", current_research=asdict(current) if current else None)
        except RecordError:
            hint["error"] = "CURRENT_RESEARCH_NOT_AVAILABLE"
        return {"reference": asdict(artifact_reference(reference)), "result": result,
                "recalculation": "matches", "current_use": hint}


def verify_fresh(receipt, operation, reader_factory, registry=None):
    """Preserve a real save receipt even if the separate readback is unavailable."""
    try:
        reader = ConditionalOdds(reader_factory(), registry)
        if operation in ("freeze-valuation", "reconcile-valuation"):
            frozen = reader.read_spec(receipt["reference"])
            require(frozen["spec_sha256"] == receipt["spec_sha256"], "ODDS_FRESH_READBACK_MISMATCH")
        else:
            view = reader.read(receipt["reference"])
            require(digest(encoded(view["result"])) == receipt["calculation_sha256"], "ODDS_FRESH_READBACK_MISMATCH")
        status = {"status": "verified", "new_reader": True, "economic_assessment": "not-performed"}
    except RecordError:
        status = {"status": "failed", "error": "ODDS_FRESH_READBACK_FAILED", "new_reader": True}
    return {**receipt, "readback": status}
