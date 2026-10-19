"""Versioned close-path observations and transitions, without source or trade I/O.

This is an explicit OS2 observation policy, not Kernel's top-decile/turnover
detector. Inputs have already been qualified by their original source owners.
"""
from __future__ import annotations

from decimal import Context, Decimal, localcontext

from decision_kernel.domain.records import digest, encoded, require
from decision_kernel.observations.market_daily import (
    MarketInputError, _change, _normalized, daily_context,
)

POLICY = "close-path20-positive5-accel5-v1"
ACTIVE = frozenset({"persistent", "accelerating", "weakening"})
STATES = frozenset({*ACTIVE, "not-entered", "unknown", "exited"})


def observe_path(*, entity_id, series, market_sessions, as_of, calendar_version,
                 source_version, market, currency, adjustment):
    """Positive 20-session path for five consecutive sessions, with 5-step change.

    A gap invalidates the dependent rolling window and interrupts persistence.
    A positive run reaching the available left boundary is explicitly censored.
    The state concerns absolute price ratios, never inferred excess performance.
    """
    days = daily_context(as_of=as_of, market_sessions=market_sessions,
        calendar_version=calendar_version, source_version=source_version,
        market=market, currency=currency, adjustment=adjustment, horizons=(5, 20))
    require(isinstance(entity_id, str) and 0 < len(entity_id) <= 128,
            "DISCOVERY_ENTITY_INVALID")
    prices = _normalized(series, days)
    result = {"entity_id": entity_id, "as_of": as_of, "state": "unknown",
        "reason_code": None, "metrics": {"change_5_pct": None, "change_20_pct": None,
            "acceleration_5_of_20_pp": None, "positive_20_sessions": 0,
            "positive_run_started": None, "persistence_left_censored": False,
            "persistence_interrupted_by_gap": False},
        "window_start": days[max(0, len(days) - 26)].isoformat(),
        "history_start": days[0].isoformat(),
        "basis": "absolute-close-path-not-excess-rank-or-investment-signal"}
    if len(days) <= 20:
        result["reason_code"] = "DISCOVERY_INSUFFICIENT_CALENDAR_HISTORY"
        return result
    returns = {}
    for index in range(20, len(days)):
        window = days[index - 20:index + 1]
        if all(day in prices for day in window):
            returns[index] = _change(prices[days[index]], prices[days[index - 20]])
    end = len(days) - 1
    metrics = result["metrics"]
    if all(day in prices for day in days[-6:]):
        metrics["change_5_pct"] = str(_change(prices[days[-1]], prices[days[-6]]))
    if end not in returns:
        result["reason_code"] = "DISCOVERY_MISSING_PRICE_WINDOW"
        return result
    metrics["change_20_pct"] = str(returns[end])
    if end - 5 in returns:
        with localcontext(Context(prec=34)):
            metrics["acceleration_5_of_20_pp"] = str(returns[end] - returns[end - 5])
    index = end
    while index >= 20 and index in returns and returns[index] > 0:
        metrics["positive_20_sessions"] += 1
        metrics["positive_run_started"] = days[index].isoformat()
        index -= 1
    metrics["persistence_left_censored"] = index < 20 and metrics["positive_20_sessions"] > 0
    metrics["persistence_interrupted_by_gap"] = index >= 20 and index not in returns
    if returns[end] <= 0:
        result["state"] = "not-entered"
    elif metrics["positive_20_sessions"] < 5:
        # With a missing predecessor we cannot tell a young run from an old run.
        if metrics["persistence_interrupted_by_gap"] or metrics["persistence_left_censored"]:
            result["reason_code"] = ("DISCOVERY_PERSISTENCE_GAP" if metrics["persistence_interrupted_by_gap"]
                                     else "DISCOVERY_INSUFFICIENT_PERSISTENCE_HISTORY")
        else:
            result["state"] = "not-entered"
    elif metrics["acceleration_5_of_20_pp"] is None:
        result["state"] = "persistent"
    elif Decimal(metrics["acceleration_5_of_20_pp"]) > 0 and Decimal(metrics["change_5_pct"]) > 0:
        result["state"] = "accelerating"
    elif Decimal(metrics["acceleration_5_of_20_pp"]) < 0 or Decimal(metrics["change_5_pct"]) <= 0:
        result["state"] = "weakening"
    else:
        result["state"] = "persistent"
    return result


def comparable(current, previous):
    """Compare complete qualified identities and the actual common price history.

    The source owner supplies dates/prices here, never a caller's comparison flag.
    Revisions to overlapping forward prices are not stitched into one trend.
    """
    if previous is None:
        return "baseline"
    if current["contract"] != previous["contract"]:
        return "contract-changed"
    now, before = current["as_of"], previous["as_of"]
    if now < before:
        return "date-reversed"
    days, old_days = current["sessions"], previous["sessions"]
    overlap = [day for day in days if old_days[0] <= day <= before]
    old_overlap = [day for day in old_days if days[0] <= day <= before]
    if not overlap or overlap != old_overlap:
        return "calendar-changed-or-missing"
    a, b = current["prices"], previous["prices"]
    if any(a.get(day) != b.get(day) for day in overlap):
        return "historical-prices-changed"
    if now == before:
        return "same-session"
    subsequent = [day for day in days if before < day <= now]
    if subsequent != [now]:
        return "unobserved-session-gap"
    return "consecutive-session"


def transition(current, previous=None):
    """Only qualified consecutive state changes are new entry/exit events.

    Baselines, corrections, missing data and cross-contract observations do not
    manufacture historical prospective entries. Same-session reads stay quiet.
    """
    relation = comparable(current, previous)
    state = current["observation"]["state"]
    prior = None if previous is None else previous["observation"]["state"]
    result = {"state": state, "transition": "unknown" if state == "unknown" else "baseline",
              "comparison": relation, "event_type": None}
    if state == "unknown":
        return result
    if relation not in ("consecutive-session", "same-session"):
        result["event_type"] = "baseline"
        return result
    if relation == "same-session":
        result["transition"] = "unchanged-session"
        return result
    if prior == "unknown":
        result.update(transition="baseline-after-unknown", event_type="baseline")
    elif state in ACTIVE and prior not in ACTIVE:
        result.update(transition="entered", event_type="entry")
    elif prior in ACTIVE and state == "not-entered":
        # Loss of computable persistence through a gap is unknown upstream.
        # A positive young run cannot normally follow an active state in one
        # consecutive session; only a nonpositive 20-day ratio proves exit.
        value = current["observation"]["metrics"]["change_20_pct"]
        if value is not None and Decimal(value) <= 0:
            result.update(state="exited", transition="exited", event_type="exit")
        else:
            result.update(transition="baseline", event_type="baseline")
    elif state == "accelerating" and prior != "accelerating":
        result.update(transition="acceleration-entered", event_type="acceleration")
    elif state == "weakening" and prior != "weakening":
        result.update(transition="weakened", event_type="weakening")
    else:
        result["transition"] = "continuing" if state in ACTIVE else "not-entered"
    return result


def event_for(*, kind, entity_id, current, previous, current_proof, previous_proof):
    """An event is entirely recomputable from its original source proof pair."""
    change = transition(current, previous)
    if change["event_type"] is None:
        return None
    event = {"policy": POLICY, "kind": kind, "entity_id": entity_id,
        "type": change["event_type"], "at": current["as_of"],
        "current_proof": current_proof, "previous_proof": previous_proof,
        "state": change["state"], "comparison": change["comparison"],
        "metrics": current["observation"]["metrics"],
        "evidence_mode": "retained-source-replay", "timely_discovery_claim": False,
        "semantics": "first-observed-baseline-not-historical-discovery" if change["event_type"] == "baseline"
            else "observed-state-transition-not-trade-or-fundamental-change"}
    return {"event_id": digest(encoded(event)), **event}
