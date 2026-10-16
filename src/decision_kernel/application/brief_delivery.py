"""Research extension of the existing news edition, same artifact and host.

Legacy v1 bytes and entry points stay intact. V2 shares the existing news
selection/replay functions; this module owns only mixed-body composition.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
import json

from decision_kernel.application import news_delivery as news, brief_research as research
from decision_kernel.domain.records import RecordError, digest, encoded, require
from decision_kernel.domain.research_artifacts import artifact_reference

SCHEMA = "dos2-news-edition-v2"


def plan_input(value):
    require(isinstance(value, dict), "NEWS_DESK_PLAN_REQUIRED")
    normalized = news.plan_input({k: v for k, v in value.items() if k != "research"})
    if "research" in value:
        normalized["research"] = research.plan_input(value["research"])
    return normalized


def _news_projection(doc):
    result = {k: deepcopy(v) for k, v in doc.items() if k != "research"}
    result["schema"] = news.SCHEMA
    result["plan"].pop("research", None)
    result["brief_markdown"] = news.brief_text(result)
    return result


def brief_text(doc):
    if doc["schema"] == news.SCHEMA:
        return news.brief_text(doc)
    text = news.brief_text(_news_projection(doc))
    text = text.replace("研究产物未接入本新闻简报；无启动回执，执行状态未确认。", "本版采用的既存研究及其限制见后文；没有据此启动新的研究。")
    return text + research.brief_text(doc["research"])


def _read_document(artifacts, ref):
    exact = asdict(artifact_reference(ref))
    found = artifacts.read(exact)
    require(found.get("reference") == exact and found.get("bytes_verified") is True
            and found.get("metadata", {}).get("media_type") == "application/json", "NEWS_DESK_ARTIFACT_INVALID")
    try:
        raw = base64.b64decode(found["data_base64"], validate=True)
        require(len(raw) <= news.MAX_DOCUMENT_BYTES, "NEWS_DESK_DOCUMENT_BOUND")
        doc = json.loads(raw)
        require(isinstance(doc, dict) and doc.get("schema") in (news.SCHEMA, SCHEMA)
                and encoded(doc) == raw, "NEWS_DESK_DOCUMENT_INVALID")
        require(doc.get("edition_sha256") == digest(encoded({k: v for k, v in doc.items() if k != "edition_sha256"})), "NEWS_DESK_DIGEST_MISMATCH")
        return doc
    except (ValueError, UnicodeError, KeyError, TypeError, RecursionError) as error:
        if isinstance(error, RecordError):
            raise
        raise RecordError("NEWS_DESK_DOCUMENT_INVALID") from None


def _check(doc, index, logical, artifacts):
    require(plan_input(doc["plan"]) == doc["plan"], "NEWS_DESK_PLAN_REQUIRED")
    mixed = doc["schema"] == SCHEMA
    require(("research" in doc) == mixed and ("research" in doc["plan"]) == mixed, "BRIEF_SCHEMA_MISMATCH")
    news._check_content(_news_projection(doc) if mixed else doc, index, logical)
    if mixed:
        research.validate(doc["research"], doc["plan"]["research"], doc["plan"]["cutoff"], artifacts.store)
        require(doc["brief_markdown"] == brief_text(doc), "NEWS_DESK_BODY_MISMATCH")


def build_edition(reader, artifacts, plan):
    normalized = plan_input(plan)
    index, logical = news._window(reader, normalized["window"])
    prior = prior_logical = None
    if normalized["baseline"] is not None:
        prior = _read_document(artifacts, normalized["baseline"])
        prior_index, prior_logical = news._window(reader, prior["plan"]["window"])
        _check(prior, prior_index, prior_logical, artifacts)
        require(news.clock(prior["plan"]["cutoff"]) <= news.clock(normalized["cutoff"])
                and news.clock(prior_logical["as_of"]) <= news.clock(logical["as_of"]), "NEWS_DESK_BASELINE_FROM_FUTURE")
        require(prior["schema"] != SCHEMA or "research" in normalized, "BRIEF_RESEARCH_TRACKING_CANNOT_DISAPPEAR")
    news_plan = {k: v for k, v in normalized.items() if k != "research"}
    # _derive reads only the predecessor's choices/IDs/cutoff and ACTUAL body hash.
    doc = news._derive(index, logical, news_plan, prior, prior_logical)
    if "research" in normalized:
        doc.update(schema=SCHEMA, plan=normalized, research=research.build(
            artifacts.store, normalized["research"], normalized["cutoff"],
            prior.get("research") if prior else None, prior["plan"]["cutoff"] if prior else None))
        doc["brief_markdown"] = brief_text(doc)
        news._seal(doc)
    return doc, logical


def open_edition(reader, artifacts, ref):
    doc = _read_document(artifacts, ref)
    expected, logical = build_edition(reader, artifacts, doc["plan"])
    require(expected == doc, "NEWS_DESK_EDITION_REPLAY_MISMATCH")
    return doc, logical


page_model = news.page_model
