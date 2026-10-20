"""Fixed supplied public originals only: no network, old archives or real records."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from decision_kernel.application.global_public_inputs import (
    PUBLIC_READING_SCHEMA, read_global_public_payload, validate_public_input,
)
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.observations.global_public import (
    ATOM, CURRENCIES, DATA, LEGACY_VERSION, META, TENORS, VERSION, date_policy, plan,
)

STARTED = "2026-09-27T11:00:00Z"
AS_OF = "2026-09-26"


def stamp(day, hour=0):
    return int(datetime.fromisoformat(day + f"T{hour:02d}:00:00+00:00").timestamp())


def body(spec):
    end = date.fromisoformat(spec["as_of_date"])
    dates = [(end - timedelta(days=i)).isoformat() for i in (2, 1)]
    if spec["family"] == "treasury":
        rows = []
        for dt, value in zip(dates, ("4.00", "4.25"), strict=True):
            if dt.replace("-", "")[:6] != spec["id"]:
                continue
            fields = "".join(f"<d:BC_{tenor}>{value}</d:BC_{tenor}>" for tenor in TENORS)
            rows.append(f"<entry><content><m:properties><d:NEW_DATE>{dt}T00:00:00</d:NEW_DATE>{fields}</m:properties></content></entry>")
        return (f'<feed xmlns="{ATOM[1:-1]}" xmlns:d="{DATA[1:-1]}" xmlns:m="{META[1:-1]}">'
                + "".join(rows) + "</feed>")
    if spec["family"] == "fx":
        rows = [f'<Cube time="{dt}">' + "".join(f'<Cube currency="{c}" rate="{value}"/>' for c in CURRENCIES)
                + "</Cube>" for dt, value in zip(dates, ("1.4", "1.5"), strict=True)]
        return ('<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" '
                'xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"><Cube>'
                + "".join(rows) + '</Cube></gesmes:Envelope>')
    if spec["family"] == "crypto":
        return json.dumps([[stamp(spec["as_of_date"]), 90, 110, 100, 105, 1],
                           [stamp(dates[0]), 90, 110, 100, 100, 1]])
    return json.dumps({"chart": {"error": None, "result": [{"meta": {
        "symbol": spec["id"], "currency": "USD", "instrumentType": "FUTURE", "dataGranularity": "1d",
        "exchangeTimezoneName": "America/New_York"}, "timestamp": [stamp(dt, 13) for dt in dates],
        "indicators": {"quote": [{"close": [100, 101]}]}}]}})


def replace_body(payload, index, text):
    record = payload["records"][index]
    raw = text.encode("utf-8")
    record.update(body=text, bytes=len(raw), sha256=digest(raw))
    record["headers"]["content-length"] = str(len(raw))
    return payload


def public_input(family, *, version=VERSION, started=STARTED, as_of=AS_OF):
    payload = {"schema": PUBLIC_READING_SCHEMA, "capture_version": version, "family": family,
        "as_of_date": as_of, "started_at": started, "finished_at": started,
        "execution_complete": True, "records": []}
    if version == VERSION:
        payload["date_policy"] = date_policy(family, started)
    for i, spec in enumerate(plan(family, as_of)):
        payload["records"].append({"index": i, "spec": spec, "state": "HTTP_RESPONSE", "http_status": 200,
            "requested_at": started, "received_at": started, "headers": {}, "body": None,
            "bytes": None, "sha256": None, "error_type": None})
        replace_body(payload, i, body(spec))
    return payload


@pytest.mark.parametrize("family,count,unit", [
    ("treasury", 8, "ANNUAL_PERCENT"), ("fx", 4, "USD_PER_EUR"),
    ("commodities", 3, "USD_PER_TROY_OUNCE"), ("crypto", 2, "USD_PER_BTC"),
])
def test_original_family_identity_units_dates_and_normal_dispatch(family, count, unit):
    value = public_input(family)
    before = encoded(value)
    full = read_market_payload(value, None)
    assert full == read_global_public_payload(value)
    assert full["scope_count"] == len(full["entries"]) == full["available_values"] == count
    assert full["entries"][0]["unit"] == unit and full["status"] == "AVAILABLE"
    assert full["captured_from"] == full["captured_through"] == STARTED
    assert full["as_of"] == AS_OF and full["source_requests"] == 0
    assert full["authority"]["investment_authority"] == "NONE"
    assert full["authority"]["automatic_admission"] is False
    filtered = read_global_public_payload(value, entity_id=full["declared_entities"][-1], limit=1)
    assert filtered["scope_count"] == count and filtered["available_values"] == count
    assert filtered["view_version"] == full["view_version"]
    assert filtered["pagination"]["matched_count"] == 1
    assert encoded(value) == before


def test_original_v1_v2_gate_and_capture_clock_cannot_be_rewritten_by_reading():
    # A reference release opportunity is not an actual publication timestamp.
    started = "2026-09-25T22:00:00Z"
    v2 = public_input("treasury", started=started, as_of="2026-09-25")
    full = read_global_public_payload(v2)
    assert full["date_policy"]["max_as_of_date"] == "2026-09-25"
    assert all(row["publication_time"] is None for row in full["entries"])
    v1 = public_input("treasury", version=LEGACY_VERSION, started=started, as_of="2026-09-25")
    with pytest.raises(RecordError, match="GP_RECENT_DATE"):
        read_global_public_payload(v1)
    v1 = public_input("fx", version=LEGACY_VERSION)
    assert read_global_public_payload(v1)["date_policy"] is None
    v1["date_policy"] = None
    with pytest.raises(RecordError, match="GP_DATE_POLICY"):
        read_global_public_payload(v1)
    v2["date_policy"]["max_as_of_date"] = "2026-09-26"
    with pytest.raises(RecordError, match="GP_DATE_POLICY"):
        read_global_public_payload(v2)
    for family in ("commodities", "crypto"):
        value = public_input(family, started=started, as_of="2026-09-25")
        with pytest.raises(RecordError, match="GP_RECENT_DATE"):
            read_global_public_payload(value)


def test_latest_null_negative_rates_oil_zero_and_roll_identity_are_not_rewritten():
    value = public_input("treasury")
    text = value["records"][0]["body"].replace('<d:BC_1MONTH>4.25</d:BC_1MONTH>', '<d:BC_1MONTH m:null="true"/>')
    text = text.replace('<d:BC_3MONTH>4.00</d:BC_3MONTH>', '<d:BC_3MONTH>0</d:BC_3MONTH>')
    text = text.replace('<d:BC_3MONTH>4.25</d:BC_3MONTH>', '<d:BC_3MONTH>-0.25</d:BC_3MONTH>')
    rates = read_global_public_payload(replace_body(value, 0, text))
    latest, negative = rates["entries"][:2]
    assert latest["value"] is None and latest["source_date"] == "2026-09-25" and latest["previous_value"] == "4.00"
    assert latest["change"] is None and rates["available_values"] == 7 and rates["scope_count"] == 8
    assert negative["value"] == "-0.25" and negative["previous_value"] == "0" and negative["change"] == "-25.00"
    value = public_input("commodities")
    for index, closes in enumerate(([100, None], [0, -1], [-1, 0])):
        raw = json.loads(value["records"][index]["body"])
        raw["chart"]["result"][0]["indicators"]["quote"][0]["close"] = closes
        replace_body(value, index, json.dumps(raw))
    futures = read_global_public_payload(value)
    assert [row["value"] for row in futures["entries"]] == [None, "-1", "0"]
    assert all(row["change"] is None and row["change_unit"] is None for row in futures["entries"])
    assert all(row["change_scope"] == "NOT_COMPUTED_CONTRACT_ROLL_IDENTITY_UNKNOWN" for row in futures["entries"])


def test_exact_product_and_duplicate_dates_reject_without_promoting_another_source():
    value = public_input("commodities")
    raw = json.loads(value["records"][0]["body"])
    raw["chart"]["result"][0]["meta"]["symbol"] = "CL=F"
    replace_body(value, 0, json.dumps(raw))
    full = read_global_public_payload(value)
    assert full["outcomes"][0]["status"] == "SOURCE_TABLE_UNCONFIRMED"
    assert full["entries"][0]["value"] is None and full["available_values"] == 2
    coins = public_input("crypto")
    rows = json.loads(coins["records"][0]["body"])
    replace_body(coins, 0, json.dumps(rows + [rows[0]]))
    full = read_global_public_payload(coins)
    assert full["outcomes"][0]["status"] == "SOURCE_TABLE_UNCONFIRMED"
    assert full["entries"][0]["value"] is None and full["scope_count"] == 2
    assert full["entries"][1]["previous_date"] == "2026-09-24"
    assert full["entries"][1]["source_date"] == "2026-09-26"
    assert full["entries"][1]["change"] == "5.000000"


def test_source_failures_service_stop_and_uncompleted_capture_remain_explicit():
    value = public_input("crypto")
    value["records"][0]["http_status"] = 403
    with pytest.raises(RecordError, match="GP_REQUEST_STATE"):
        read_global_public_payload(value)

    second = value["records"][1]
    second.update({key: None for key in ("http_status", "requested_at", "received_at", "body", "bytes", "sha256", "error_type")})
    second.update(state="NOT_ATTEMPTED_SERVICE_STOP", headers={})
    full = read_global_public_payload(value)
    assert [row["status"] for row in full["outcomes"]] == ["HTTP_403", "NOT_ATTEMPTED_SERVICE_STOP"]
    assert full["status"] == "UNAVAILABLE" and full["missing_values"] == 2
    value["execution_complete"] = False
    first = value["records"][0]
    first.update(state="REQUEST_IN_PROGRESS", http_status=None, received_at=None, body=None, bytes=None,
                 sha256=None, headers={})
    second["state"] = "NOT_ATTEMPTED"
    assert read_global_public_payload(value)["execution_complete"] is False
    value["execution_complete"] = True
    with pytest.raises(RecordError, match="GP_IN_PROGRESS"):
        read_global_public_payload(value)


def test_small_json_number_cannot_expand_into_unbounded_decimal_output():
    value = public_input("commodities")
    for index in range(3):
        replace_body(value, index, value["records"][index]["body"].replace('[100, 101]', '[100, 1e-999999999]'))
    result = read_global_public_payload(value)
    assert len(encoded(result)) < 10000
    assert result["available_values"] == 0
    assert all(row["status"] == "SOURCE_TABLE_UNCONFIRMED" for row in result["outcomes"])


@pytest.mark.parametrize("mutation,code", [
    (lambda p: p["records"][0]["spec"].update(url="https://synthetic.invalid/unapproved"), "GP_RECORD_SCOPE"),
    (lambda p: p["records"][0].update(sha256="0" * 64), "GP_RAW_IDENTITY"),
    (lambda p: p["records"][0].update(received_at="2026-09-28T11:00:00Z"), "GP_RESPONSE_TIME"),
    (lambda p: p["records"][0]["headers"].update(authorization="synthetic-only"), "GP_HEADERS"),
    (lambda p: p.update(public_rights_confirmed=True), "GP_INPUT_SCHEMA"),
])
def test_supplied_envelope_cannot_change_source_identity_clock_or_grant_authority(mutation, code):
    value = public_input("fx")
    mutation(value)
    with pytest.raises(RecordError, match=code):
        validate_public_input(value)
