"""Synthetic NBS contract tests; live source evidence belongs outside main."""
from copy import deepcopy
from dataclasses import asdict
import json
from unittest.mock import patch

import pytest

from decision_kernel.application.industry_reading import COLUMNS, read_industry
from decision_kernel.application.material_prices import dataset_inputs, replay, seal, source_input
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, Selection, SourceInput, SourceRef, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.material_prices_http import capture, fetch
from decision_kernel.interfaces.material_prices_cli import main, restore
from decision_kernel.observations.material_prices import ROOT, discover, monitor, parse_release, period

NOW = "2026-10-05T12:00:00+08:00"
TITLES = ["2026年9月" + part + "旬流通领域重要生产资料市场价格变动情况" for part in ("上", "中", "下")]
URLS = [ROOT + "202610/t20261001_" + str(i) + ".html" for i in range(3)]


def html(index, *, price=None, count=50, duplicate=True, prior=None):
    price = price or str((100, 102, 104)[index])
    prior = prior or ("2026年8月下旬", "2026年9月上旬", "2026年9月中旬")[index]
    table = ("<table><tr>" + "".join("<th>" + s + "</th>" for s in (
        "产品名称", "单位", "本期价格（元）", "比上期价格涨跌（元）", "涨跌幅（%）")) + "</tr>"
        "<tr><td>一、黑色金属</td><td></td><td></td><td></td><td></td></tr>"
        + "".join("<tr><td>合成产品" + str(i) + "（一级）</td><td>吨</td><td>" + price +
                  "</td><td>2</td><td>2.0</td></tr>" for i in range(count))
        + "<tr><td>注：上期为" + prior + "。</td></tr></table>")
    return ('<html><meta name="ArticleTitle" content="' + TITLES[index] +
            '"><meta name="PubDate" content="2026/10/01 09:30">'
            "<p>50种；流通费用、利润和税费；上期为" + prior + "。</p>" +
            table * (2 if duplicate else 1) + "</html>").encode()


def fixture_fetch(url):
    if url in URLS:
        return html(URLS.index(url))
    return "".join('<a href="' + u + '">' + t + "</a>" for u, t in zip(URLS, TITLES)).encode()


def collect(fetcher=fixture_fetch, *, predecessor=None, now=NOW):
    report, raw = capture(fetcher=fetcher, clock=lambda: now,
                          predecessor=predecessor["report"] if predecessor else None)
    return seal(report, predecessor=predecessor), raw


def reseal(value):
    value["capture_sha256"] = digest(encoded({k: v for k, v in value.items() if k != "capture_sha256"}))
    return value


def test_real_three_period_pipeline_deduplicates_mobile_and_detects_sustained_total():
    value, raw = collect()
    report = replay(value, raw)
    assert len(raw) == 6 and len(report["rows"]) == 50
    assert report["coverage"]["comparable_products"] == 50
    row = report["rows"][0]
    assert row["sustained_direction"] == "up" and row["triggered"]
    assert row["trigger_reason"] == "sustained-total-threshold"
    assert row["sustained_comparison"]["delta"] == "4"
    assert row["sustained_comparison"]["from_period"] == "2026-09-10"
    assert row["comparison"]["delta"] == "2"
    assert report["transition"] == "bootstrap-existing-release" and not report["new_to_capture"]


@pytest.mark.parametrize("price,direction", [("110", "up"), ("90", "down")])
def test_detects_both_directions_from_observed_prices_not_reported_deltas(price, direction):
    def get(url):
        return html(2, price=price) if url == URLS[2] else fixture_fetch(url)
    value, raw = collect(get)
    row = replay(value, raw)["rows"][0]
    assert row["direction"] == direction and row["triggered"]
    assert row["trigger_reason"] == "absolute-percent-threshold"
    assert row["reported_percent_change"] == "2.0"


def test_no_trigger_only_for_complete_successful_fresh_scan():
    def flat(url):
        return html(URLS.index(url), price="100") if url in URLS else fixture_fetch(url)
    report = collect(flat)[0]["report"]
    assert report["status"] == "no-trigger"
    assert not any(r["triggered"] for r in report["rows"])
    assert collect(flat, now="2026-11-01T12:00:00+08:00")[0]["report"]["status"] == "partial-or-unavailable"


@pytest.mark.parametrize("problem", ["missing-value", "missing-product", "missing-period", "different-unit"])
def test_incomplete_input_is_not_quiet_or_zero_opportunity(problem):
    def get(url):
        if url == URLS[2]:
            if problem == "missing-value":
                return html(2, price="—")
            if problem == "missing-product":
                return html(2, count=49)
            if problem == "different-unit":
                return html(2).replace("吨".encode(), "千克".encode())
        if url == URLS[1] and problem == "missing-period":
            raise RecordError("MATERIAL_HTTP_500")
        return fixture_fetch(url)
    value, raw = collect(get)
    report = replay(value, raw)
    assert report["status"] == "partial-or-unavailable"
    assert report["coverage"]["comparable_products"] < 50


def test_refusal_stops_actual_network_calls_and_keeps_unavailable_receipt():
    calls = []
    def refused(url):
        calls.append(url)
        raise RecordError("MATERIAL_HTTP_403")
    value, raw = collect(refused)
    assert len(calls) == 1 and not raw
    assert value["report"]["status"] == "partial-or-unavailable"
    assert value["report"]["receipts"][0]["error_code"] == "MATERIAL_HTTP_403"
    assert all(r["status"] == "not-attempted-budget-or-refusal" for r in value["report"]["receipts"][1:])


def test_off_scope_redirects_and_bodies_are_rejected_before_parsing():
    with pytest.raises(RecordError, match="MATERIAL_URL_OUTSIDE_SCOPE"):
        discover('<a href="https://example.org/">2026年9月下旬流通领域重要生产资料市场价格变动情况</a>'.encode(), ROOT)
    with patch("decision_kernel.infrastructure.material_prices_http.build_opener") as opener:
        with pytest.raises(RecordError, match="MATERIAL_URL_OUTSIDE_SCOPE"):
            fetch("https://example.org/")
        opener.assert_not_called()


@pytest.mark.parametrize("change", ["conflicting-mobile", "wrong-prior", "nonpositive", "title", "future"])
def test_shape_or_clock_changes_fail_closed(change):
    raw, title, received = html(2), TITLES[2], NOW
    if change == "conflicting-mobile":
        raw = raw.replace(b"104", b"105", 1)
    elif change == "wrong-prior":
        raw = html(2, prior="2026年9月上旬")
    elif change == "nonpositive":
        raw = html(2, price="-1")
    elif change == "title":
        title = TITLES[1]
    elif change == "future":
        received = "2026-09-30T12:00:00+08:00"
    with pytest.raises(RecordError):
        parse_release(raw, url=URLS[2], title=title, received_at=received)


def test_repeated_check_does_not_claim_new_event_and_page_change_is_not_price_revision():
    first, _ = collect()
    same, _ = collect(predecessor=first)
    assert same["report"]["transition"] == "unchanged-release"
    assert not same["report"]["new_to_capture"]
    def page_change(url):
        return fixture_fetch(url) + b"<!-- publication layout -->" if url in URLS else fixture_fetch(url)
    changed, raw = collect(page_change, predecessor=first)
    assert replay(changed, raw)["transition"] == "same-period-page-change"
    def revision(url):
        return html(2, price="110") if url == URLS[2] else fixture_fetch(url)
    revised, _ = collect(revision, predecessor=first)
    assert revised["report"]["transition"] == "same-period-revision"
    assert revised["report"]["new_to_capture"]


def test_regressed_source_never_triggers_current_observation():
    first, _ = collect()
    def regressed(url):
        if url not in URLS:
            return "".join('<a href="' + u + '">' + t + "</a>" for u, t in zip(URLS[:2], TITLES[:2])).encode()
        return fixture_fetch(url)
    value, raw = collect(regressed, predecessor=first)
    report = replay(value, raw)
    assert report["transition"] == "source-period-regressed"
    assert report["status"] == "partial-or-unavailable"
    assert not any(r["triggered"] for r in report["rows"])


def test_new_period_is_distinct_from_bootstrap_and_timezone_freshness_uses_china_day():
    def old_window(url):
        if url not in URLS:
            return "".join('<a href="' + u + '">' + t + "</a>" for u, t in zip(URLS[:2], TITLES[:2])).encode()
        return fixture_fetch(url)
    old, _ = collect(old_window)
    new, raw = collect(predecessor=old)
    assert replay(new, raw)["transition"] == "new-statistical-period"
    assert new["report"]["new_to_capture"]
    stale, _ = collect(now="2026-10-20T17:00:00+00:00")
    assert stale["report"]["freshness"]["period_age_days"] == 21
    assert stale["report"]["coverage"]["fresh_monitored_products"] == 0
    assert stale["report"]["status"] == "partial-or-unavailable"


def test_original_tamper_and_rehashed_fabricated_calculation_are_rejected():
    value, raw = collect()
    with pytest.raises(RecordError, match="MATERIAL_ORIGINAL_CHANGED"):
        replay(value, {**raw, URLS[2]: raw[URLS[2]] + b" "})
    forged = deepcopy(value)
    forged["report"]["rows"][0]["comparison"]["delta"] = "999"
    with pytest.raises(RecordError, match="MATERIAL_RESULT_DIFFERS"):
        replay(reseal(forged), raw)
    forged = deepcopy(value)
    forged["report"]["releases"][-1]["rows"][0]["value"] = "999"
    forged["report"] = monitor(forged["report"]["releases"], as_of=NOW,
                               receipts=forged["report"]["receipts"])
    with pytest.raises(RecordError, match="MATERIAL_REPLAY_DIFFERS"):
        replay(reseal(forged), raw)


def test_cli_retains_originals_and_replays_offline_before_next_capture(tmp_path, capsys):
    target = tmp_path / "first"
    def fake(**kwargs):
        return capture(fetcher=fixture_fetch, clock=lambda: NOW, **kwargs)
    with patch("decision_kernel.interfaces.material_prices_cli.capture", fake):
        assert main(["collect", "--out", str(target)]) == 0
    assert restore(target)["report"]["coverage"]["current_products"] == 50
    with patch("decision_kernel.interfaces.material_prices_cli.capture") as network:
        assert main(["read", "--capture", str(target)]) == 0
        network.assert_not_called()
    assert not SourceInput.from_dict(json.loads((target / "source-input.json").read_text(encoding="utf-8"))).entities
    (target / "response-3.body").write_bytes(b"tampered")
    with patch("decision_kernel.interfaces.material_prices_cli.capture") as network:
        assert main(["collect", "--out", str(tmp_path / "next"), "--predecessor", str(target)]) == 2
        network.assert_not_called()


def test_dataset_reaches_existing_exact_industry_reader_after_fresh_store_open(tmp_path):
    value, _ = collect()
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "records.git"))
    def put(source):
        return store.put_source("material-" + digest(encoded(asdict(source))), source)
    sources = dataset_inputs(value)
    refs = [put(source) for source in sources]
    observations = [asdict(SourceRef(ref.manifest, Selection("rows", 1, 1, COLUMNS))) for ref in refs]
    payload = {"schema": "industry-reading-v1", "prepared_at": NOW, "as_of": NOW,
               "coverage_note": "One synthetic product selected from 50 parsed synthetic rows.",
               "use": "Input cost investigation", "series": [{"series_id": "synthetic-price",
               "subject": asdict(sources[0].entities[0]), "observations": observations}], "relations": []}
    packet = put(SourceInput("synthetic-material-packet", "content", "Synthetic monitor consumer",
                             "synthetic:materials", encoded(payload).decode()))
    put(source_input(value, "synthetic-material-monitor"))
    fresh = VersionedStore(GitStore(store.backend.root))
    reading = read_industry(packet, fresh)
    assert reading["series"][0]["latest_change"]["delta"] == "2"
    assert reading["series"][0]["latest_change"]["status"] == "changed"
    assert sources[0].published_at != sources[0].report_period


@pytest.mark.parametrize("title,expected", [
    ("2024年2月下旬流通领域重要生产资料市场价格变动情况", ("2024-02-21", "2024-02-29")),
    ("2025年2月下旬流通领域重要生产资料市场价格变动情况", ("2025-02-21", "2025-02-28")),
    ("2026年1月下旬流通领域重要生产资料市场价格变动情况", ("2026-01-21", "2026-01-31")),
])
def test_ten_day_calendar_not_equal_duration(title, expected):
    assert period(title) == expected
