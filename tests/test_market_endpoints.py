"""Explicit endpoint purpose over synthetic, calendar-qualified observations."""
from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest

from decision_kernel.observations.market_daily import MarketInputError, observe_daily, observe_endpoints


DAYS = [(date(2026, 7, 1) + timedelta(days=n)).isoformat() for n in range(100)
        if (date(2026, 7, 1) + timedelta(days=n)).weekday() < 5
        and n != 40][:61]  # Synthetic holiday on 10 August, not a provider calendar claim.
CONTEXT = {"entity_id": "600000.SH", "as_of": DAYS[-1], "market_sessions": DAYS,
    "calendar_version": "synthetic-calendar", "adjustment": "raw",
    "source_version": "synthetic-prices", "market": "SSE", "currency": "CNY"}


def test_endpoint_dates_zero_and_decimal_result_do_not_certify_missing_daily_path():
    rows = [{"trade_date": day, "close": close} for day, close in
            ((DAYS[-61], "20"), (DAYS[-21], "10"), (DAYS[-6], "10"), (DAYS[-1], "10"))]
    expected = observe_endpoints(**CONTEXT, series=rows)
    with localcontext() as context:
        context.prec = 2
        assert observe_endpoints(**CONTEXT, series=rows) == expected
    assert expected["status"] == "observed" and expected["comparison_basis"] == "calendar-endpoints-v1"
    assert [Decimal(w["change_pct"]) for w in expected["horizons"]] == [0, 0, -50]
    assert [w["required_dates"] for w in expected["horizons"]] == [
        [DAYS[-h - 1], DAYS[-1]] for h in (5, 20, 60)]
    strict = observe_daily(**CONTEXT, series=rows)
    assert all(w["status"] == "missing-market-sessions" for w in strict["horizons"])
    assert expected["return_semantics"] == strict["return_semantics"] == "price-ratio-not-total-return"


@pytest.mark.parametrize("missing", [DAYS[-6], DAYS[-1]])
def test_missing_endpoint_keeps_calendar_anchor_instead_of_using_neighbor_or_future(missing):
    rows = [{"trade_date": day, "close": "10"} for day in DAYS if day != missing]
    rows.append({"trade_date": "2027-01-04", "close": "20"})
    result = observe_endpoints(**CONTEXT, series=rows, horizons=(5,))
    window = result["horizons"][0]
    assert window["status"] == "missing-endpoint-observations" and window["missing_dates"] == [missing]
    assert window["baseline_date"] == DAYS[-6] and window["end_date"] == DAYS[-1]
    assert "change_pct" not in window
    short = observe_endpoints(**{**CONTEXT, "market_sessions": DAYS[-5:]}, series=rows, horizons=(5,))
    assert short["horizons"] == [{"sessions": 5, "end_date": DAYS[-1],
        "status": "insufficient-calendar-history", "available_sessions": 4}]


@pytest.mark.parametrize("rows,reason", [
    ([{"trade_date": DAYS[-1], "close": "0"}], "INVALID_CLOSE"),
    ([{"trade_date": "2026-08-10", "close": "10"}], "NON_MARKET_SESSION"),
    ([{"trade_date": "2027-01-04", "close": "10"}] * 2, "DUPLICATE_SESSION"),
    ([{"trade_date": DAYS[-1], "close": "10", "extra": "ignored?"}], "SERIES_ROW_INVALID"),
])
def test_endpoint_inputs_receive_the_original_daily_validation(rows, reason):
    for reader in (observe_daily, observe_endpoints):
        with pytest.raises(MarketInputError, match=reason):
            reader(**CONTEXT, series=rows)
