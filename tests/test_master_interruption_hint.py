"""An interrupted master unit must say whether an operator has to act.

Two very different states used to produce the same receipt: a retained
checkpoint (the last page request may have reached the source without a
recorded response, so nothing retries it) and no checkpoint at all (the unit
simply starts over). The receipt now separates them.
"""

from decision_kernel.interfaces import daily_market_cli as cli
from decision_kernel.interfaces import daily_reading_cli as reading


def test_retained_checkpoint_requires_an_operator():
    hint = cli._master_interruption("SSE", {"checkpoint_reference": {"manifest": {}},
        "reason_code": "RELAY_CHECKPOINT_INFLIGHT"})
    assert hint == {"schema": "daily-master-interruption-v1", "venue": "SSE",
        "reason_code": "RELAY_CHECKPOINT_INFLIGHT", "checkpoint_retained": True,
        "automatic_retry": False, "next_action": "explicit-reconciliation"}


def test_missing_checkpoint_retries_by_itself():
    hint = cli._master_interruption("SZSE", {"checkpoint_reference": None, "reason_code": None})
    assert hint["checkpoint_retained"] is False and hint["automatic_retry"] is True
    assert hint["next_action"] == "retry-next-run" and hint["reason_code"] is None


def test_receipt_keeps_the_hint_for_readers():
    assert "master_interruption" in reading.UPDATE_FIELDS


def _model(hint):
    return {"daily_update": {"read_as_of": "2026-10-23T23:00:00+08:00", "market_cycle": None,
        "market_resolution": "resolved", "market_error": None,
        "source_receipt": {"status": "persistence-unconfirmed", "master_interruption": hint}},
        "brief": {"status": "not-supplied"},
        "catalogue": {"total_cases": 0, "returned": 0}}


def test_summary_explains_both_cases():
    acted = cli._master_interruption("SSE", {"checkpoint_reference": {"manifest": {}},
        "reason_code": "RELAY_CHECKPOINT_INFLIGHT"})
    retried = cli._master_interruption("SZSE", {"checkpoint_reference": None, "reason_code": None})
    waiting = " ".join(reading._summary_lines(_model(acted)))
    restarting = " ".join(reading._summary_lines(_model(retried)))
    assert "上交所证券主表" in waiting and "显式对账" in waiting
    assert "深交所证券主表" in restarting and "从头续抓" in restarting
