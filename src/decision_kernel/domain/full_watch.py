"""Full Watch declarations reuse v2 comparisons and name their retained basis."""
from copy import deepcopy

from decision_kernel.domain.conditional_odds import shape
from decision_kernel.domain.records import checked_text, encoded, identifier, require
from decision_kernel.domain.research_artifacts import artifact_reference
from decision_kernel.domain.watch_conditions import MAX_CONDITIONS, validate_watch as validate_conditions

SCHEMA = "odds-watch-v3"
FROZEN_SCHEMA = "frozen-odds-watch-v3"
CHECK_SCHEMA = "odds-watch-check-v3"
AFFECTED_INPUTS = {"cash", "capital", "shares", "world-value", "horizon", "probability", "risk-policy"}


def validate_watch(payload, validate_scalar):
    shape(payload, {"schema", "watch_id", "basis", "conditions", "combination", "activation", "window",
                    "data_policy", "effect", "limitations", "tracking"})
    require(payload["schema"] == SCHEMA and len(encoded(payload)) <= 64 * 1024, "WATCH_SIZE_LIMIT")
    previous = {key: deepcopy(value) for key, value in payload.items() if key != "tracking"}
    previous["schema"] = "odds-watch-v2"
    validate_conditions(previous, validate_scalar)
    require(payload["window"]["end_after_trigger"] is False, "WATCH_FULL_CONTINUOUS_REQUIRED")
    require(all(c["metric"] != "research_version" for c in payload["conditions"]),
            "WATCH_FULL_QUESTION_CHANGE_REQUIRED")
    tracking = payload["tracking"]
    shape(tracking, {"odds_result", "question_links"})
    odds = tracking["odds_result"]
    mode = payload["basis"]["kind"]
    if mode == "research":
        require(odds is None, "WATCH_FULL_RESEARCH_ONLY_WITHOUT_ODDS")
    else:
        require(isinstance(odds, dict), "WATCH_FULL_ODDS_RESULT_REQUIRED")
        shape(odds, {"kind", "reference"})
        require(odds["kind"] in ("conditional", "probability") and odds["kind"] == mode,
                "WATCH_FULL_ODDS_KIND_MISMATCH")
        artifact_reference(odds["reference"])
    links = tracking["question_links"]
    require(isinstance(links, list) and len(links) <= MAX_CONDITIONS, "WATCH_FULL_QUESTION_LINK_LIMIT")
    evidence_ids = {c["condition_id"] for c in payload["conditions"] if c["metric"] == "evidence"}
    seen = set()
    for link in links:
        shape(link, {"condition_id", "question_id", "affected_inputs", "world_ids", "impact_note"})
        identifier(link["condition_id"]); identifier(link["question_id"])
        require(link["condition_id"] in evidence_ids and link["condition_id"] not in seen,
                "WATCH_FULL_QUESTION_CONDITION_MISMATCH")
        seen.add(link["condition_id"])
        affected = link["affected_inputs"]
        require(isinstance(affected, list) and 1 <= len(affected) <= len(AFFECTED_INPUTS)
                and all(isinstance(value, str) and value in AFFECTED_INPUTS for value in affected)
                and len(set(affected)) == len(affected), "WATCH_FULL_AFFECTED_INPUTS_REQUIRED")
        worlds = link["world_ids"]
        require(isinstance(worlds, list) and len(worlds) <= 12, "WATCH_FULL_WORLD_LIMIT")
        for world in worlds: identifier(world)
        require(len(set(worlds)) == len(worlds), "WATCH_FULL_DUPLICATE_WORLD")
        require(mode != "research" or not worlds, "WATCH_FULL_RESEARCH_ONLY_WITHOUT_WORLDS")
        checked_text(link["impact_note"], limit=4096)
    require(seen == evidence_ids, "WATCH_FULL_EVIDENCE_QUESTION_LINK_REQUIRED")
    return deepcopy(payload)
