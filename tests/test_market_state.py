"""Synthetic market sessions exercise the actual close-path state policy."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

import pytest

from decision_kernel.observations.market_daily import MarketInputError
from decision_kernel.observations.market_state import event_for, observe_path, transition

DAYS = [(date(2026, 1, 1) + timedelta(days=n)).isoformat() for n in range(100)]


def path(values, *, missing=(), contract="same-qualified-universe"):
    sessions = DAYS[:len(values)]
    rows = [{"trade_date": day, "close": str(value)} for day, value in zip(sessions, values)
            if day not in missing]
    observation = observe_path(entity_id="synthetic:one", series=rows, market_sessions=sessions,
        as_of=sessions[-1], calendar_version="synthetic-calendar", source_version="synthetic-prices",
        market="synthetic", currency="XXX", adjustment="raw")
    return {"entity_id": "synthetic:one", "as_of": sessions[-1], "contract": contract,
        "sessions": sessions, "prices": {row["trade_date"]: row["close"] for row in rows},
        "observation": observation}


def rising(n):
    return [100] * 21 + list(range(101, 101 + n - 21))


def test_first_entry_continuation_acceleration_and_original_event_identity():
    previous, current = path(rising(25)), path(rising(26))
    assert previous["observation"]["state"] == "not-entered"
    out = transition(current, previous)
    assert out == {"state": "accelerating", "transition": "entered",
                   "comparison": "consecutive-session", "event_type": "entry"}
    event = event_for(kind="stock", entity_id="synthetic:one", current=current,
        previous=previous, current_proof="a", previous_proof="b")
    assert event["type"] == "entry" and event["metrics"]["positive_20_sessions"] == 5
    assert event == event_for(kind="stock", entity_id="synthetic:one", current=current,
        previous=previous, current_proof="a", previous_proof="b")
    assert transition(path(rising(27)), current)["transition"] == "continuing"
    assert event_for(kind="stock", entity_id="synthetic:one", current=current,
        previous=current, current_proof="a", previous_proof="a") is None


def test_weakening_exit_and_no_repeated_exit_on_flat_negative_path():
    before = path(rising(45))
    current = path(rising(45) + [121])
    assert current["observation"]["state"] == "weakening"
    assert Decimal(current["observation"]["metrics"]["change_20_pct"]) > 0
    exit_path = path(rising(45) + [121, 50])
    assert transition(exit_path, current)["event_type"] == "exit"
    assert transition(exit_path, current)["state"] == "exited"
    assert transition(path(rising(45) + [121, 50, 50]), exit_path)["event_type"] is None


def test_persistent_without_computable_acceleration_and_later_acceleration():
    values = [100 + n for n in range(25)]
    current = path(values)
    assert current["observation"]["state"] == "persistent"
    assert current["observation"]["metrics"]["acceleration_5_of_20_pp"] is None
    accelerated = path(values + [150])
    assert transition(accelerated, current)["event_type"] == "acceleration"


def test_baseline_is_not_historical_entry_and_left_censored_run_is_visible():
    current = path([100 + n for n in range(40)])
    assert transition(current)["event_type"] == "baseline"
    metrics = current["observation"]["metrics"]
    assert metrics["positive_20_sessions"] == 20
    assert metrics["positive_run_started"] == DAYS[20]
    assert metrics["persistence_left_censored"] is True
    short = path([100 + n for n in range(24)])
    assert short["observation"]["state"] == "unknown"
    assert short["observation"]["reason_code"] == "DISCOVERY_INSUFFICIENT_PERSISTENCE_HISTORY"


def test_missing_session_cannot_be_quiet_negative_or_exit():
    previous = path(rising(40))
    current = path(rising(41), missing=[DAYS[39]])
    changed = transition(current, previous)
    assert changed["state"] == "unknown" and changed["event_type"] is None
    recovered = path(rising(43), missing=[DAYS[21]])
    assert recovered["observation"]["metrics"]["persistence_interrupted_by_gap"] is True
    assert recovered["observation"]["state"] == "unknown"


@pytest.mark.parametrize("reason,change", [
    ("contract-changed", lambda x: x.update(contract="another-policy-or-universe")),
    ("historical-prices-changed", lambda x: x["prices"].update({DAYS[5]: "99"})),
    ("calendar-changed-or-missing", lambda x: x["sessions"].remove(DAYS[5])),
])
def test_revised_contract_forward_prices_or_calendar_make_new_baseline(reason, change):
    previous, current = path(rising(30)), path(rising(31))
    change(current)
    result = transition(current, previous)
    assert result["comparison"] == reason and result["event_type"] == "baseline"


def test_same_day_correction_and_unobserved_session_gap_are_not_new_entries():
    previous = path(rising(30))
    corrected = path(rising(29) + [170])
    assert transition(corrected, previous)["comparison"] == "historical-prices-changed"
    assert transition(corrected, previous)["event_type"] == "baseline"
    assert transition(path(rising(32)), previous)["comparison"] == "unobserved-session-gap"


def test_future_row_is_validated_but_does_not_create_state_or_hide_duplicates():
    current = path(rising(30))
    kwargs = dict(entity_id="synthetic:one", market_sessions=DAYS[:30], as_of=DAYS[29],
        calendar_version="synthetic", source_version="synthetic", market="synthetic",
        currency="XXX", adjustment="raw")
    rows = [{"trade_date": day, "close": value} for day, value in current["prices"].items()]
    future = {"trade_date": DAYS[31], "close": "100000000"}
    assert observe_path(series=rows + [future], **kwargs) == observe_path(series=rows, **kwargs)
    with pytest.raises(MarketInputError, match="DUPLICATE_SESSION"):
        observe_path(series=rows + [future, future], **kwargs)


@pytest.mark.parametrize("bad", [0, -1, True, "NaN", "Infinity", "1e100"])
def test_bad_numeric_prices_never_become_state_evidence(bad):
    with pytest.raises(MarketInputError):
        path(rising(29) + [bad])
