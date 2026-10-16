"""Synthetic industry/issuer inputs on real Git; no source collection or research."""
from copy import deepcopy
import csv
from dataclasses import asdict, replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from decision_kernel.application.industry_reading import COLUMNS, read_industry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    Entity, MissingObject, RecordError, Selection, SourceInput, SourceRef, digest, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore

SUBJECT = Entity("synthetic-copper", "commodity", "Synthetic copper")
PRODUCER = Entity("synthetic-producer", "company", "Synthetic producer")
BUYER = Entity("synthetic-buyer", "company", "Synthetic buyer")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return VersionedStore(GitStore.init_synthetic(tmp_path_factory.mktemp("industry") / "records.git"))


def put(store, source):
    return store.put_source("industry-" + digest(encoded(asdict(source))), source)


def packet(store, payload):
    return put(store, SourceInput("synthetic-industry-packet", "content", "Synthetic industry reading",
        "synthetic:industry-test", encoded(payload).decode(), entities=(SUBJECT, PRODUCER, BUYER)))


def prepare(store, *, row_changes=None, source_changes=None):
    common = dict(subject_id=SUBJECT.entity_id, metric="price", specification="cathode grade A",
        region="synthetic-region", currency="CNY", unit="CNY/tonne", price_kind="survey",
        market="spot", contract="not-applicable", tax_basis="tax-inclusive",
        frequency="monthly", period_kind="price-level", assessment="period-average")
    rows = [{**common, "period_start": "2026-08-01", "period_end": "2026-08-31", "value": "100"},
            {**common, "period_start": "2026-09-01", "period_end": "2026-09-30", "value": "110"}]
    for index, changes in (row_changes or {}).items():
        rows[index].update(changes)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    source = SourceInput("synthetic-industry-prices", "dataset", "Synthetic monthly price survey",
        "synthetic:industry-survey", stream.getvalue(), entities=(SUBJECT,),
        acquired_at="2026-10-10", published_at="2026-10-01")
    source = replace(source, **(source_changes or {}))
    price_ref = put(store, source)
    observations = [asdict(SourceRef(price_ref.manifest, Selection("rows", i, i, COLUMNS))) for i in (1, 2)]
    relations = []
    for company, role, sentence in (
        (PRODUCER, "producer", "We produce and sell copper; hedging can offset price changes."),
        (BUYER, "purchaser", "We purchase copper; fixed-price contracts delay cost pass-through.")):
        source = SourceInput("issuer-" + company.entity_id, "content", "Synthetic issuer filing",
            "synthetic:issuer-filing", "Synthetic filing, NOT a real issuer.\n" + sentence + "\n",
            entities=(company,), acquired_at="2026-10-10", published_at="2026-10-01",
            report_period="2026-H1")
        issuer_ref = put(store, source)
        selected = SourceRef(issuer_ref.manifest, Selection("lines", 2, 2))
        relations.append(dict(relation_id="link-" + company.entity_id, subject_id=SUBJECT.entity_id,
            company=asdict(company), role=role, business_scope="Synthetic copper business",
            report_period="2026-H1", applicable_start="2026-01-01", applicable_end="2026-06-30",
            evidence=dict(reference=asdict(selected), source_role="issuer-filing", quote=sentence,
                issuer_binding_note="Synthetic issuer identity is explicit in retained source metadata."),
            exposure_size_note="Share of business not established.",
            transmission={channel: dict(hypothesis="Higher copper prices may " +
                ("raise procurement costs." if role == "purchaser" else "increase sales receipts."),
                counterevidence="Contracts, hedging, inventory and integration can offset transmission.")
                for channel in ("revenue", "cost", "profit", "cash_flow")},
            unknowns=["Volumes, business share, pass-through, lag and net effect are unknown."]))
    payload = dict(schema="industry-reading-v1", prepared_at="2026-10-12T10:00:00+08:00",
        as_of="2026-10-15T10:00:00+08:00", coverage_note="One synthetic subject, not market coverage.",
        use="Synthetic monthly input-cost review; no actual investment research.",
        series=[dict(series_id="synthetic-copper-price", subject=asdict(SUBJECT), observations=observations)],
        relations=relations)
    return packet(store, payload), payload


def read(store, ref, **kwargs):
    # New backend instance: no in-process input/source cache from preparation.
    return read_industry(ref, VersionedStore(GitStore(store.backend.root)), **kwargs)


def change(store, **kwargs):
    ref, _ = prepare(store, **kwargs)
    return read(store, ref)["series"][0]["latest_change"]


def test_both_directions_use_same_relations_and_keep_unknown_exposure(store):
    ref, _ = prepare(store)
    head = store.backend.head()
    forward = read(store, ref, subject_id=SUBJECT.entity_id)
    reverse = read(store, ref, company_id=BUYER.entity_id)
    buyer = next(r for r in forward["relations"] if r["company"]["entity_id"] == BUYER.entity_id)
    assert reverse["relations"] == [buyer]
    assert reverse["series"] == forward["series"]
    assert buyer["role"] == "purchaser"
    assert "procurement costs" in buyer["transmission"]["cost"]["hypothesis"]
    assert buyer["applicability_at_cutoff"] == "historical"
    assert all(buyer[k] is None for k in ("business_share", "elasticity", "pass_through", "lag", "net_benefit"))
    assert forward["series"][0]["latest_change"]["delta"] == "10"
    assert forward["series"][0]["latest_change"]["percent_change"] == "10.0"
    assert forward["scope"]["accepted_relations"] == 2
    assert forward["source_requests"] == forward["writes"] == 0
    assert store.backend.head() == head


@pytest.mark.parametrize("field,value", [
    ("specification", "different-grade"), ("region", "other-region"), ("currency", "USD"),
    ("unit", "CNY/kg"), ("price_kind", "quote"), ("tax_basis", "tax-exclusive"),
    ("assessment", "period-end"), ("frequency", "daily"), ("period_kind", "stock"),
])
def test_basis_changes_never_silently_mix(store, field, value):
    out = change(store, row_changes={1: {field: value}})
    assert out["reason_code"] == "INDUSTRY_BASIS_CHANGED"
    assert field in out["changed_fields"] and out["delta"] is None


def test_futures_and_different_contracts_not_spot(store):
    out = change(store, row_changes={1: {"market": "futures", "contract": "CU2610"}})
    assert out["reason_code"] == "INDUSTRY_BASIS_CHANGED"
    out = change(store, row_changes={0: {"market": "futures", "contract": "CU2610"},
                                    1: {"market": "futures", "contract": "CU2611"}})
    assert out["changed_fields"] == ["contract"]
    assert change(store, row_changes={1: {"market": "futures"}})["reason_code"] == "INDUSTRY_POINT_UNAVAILABLE"


def test_missing_zero_negative_and_unknown_basis_are_distinct(store):
    assert change(store, row_changes={1: {"value": ""}})["reason_code"] == "INDUSTRY_VALUE_MISSING"
    zero = change(store, row_changes={0: {"value": "0"}})
    assert zero["delta"] == "110" and zero["percent_change"] is None
    assert zero["reason_code"] == "INDUSTRY_ZERO_BASE"
    negative = change(store, row_changes={0: {"value": "-10"}, 1: {"value": "-12"}})
    assert negative["delta"] == "-2" and negative["percent_change"] == "-20.0"
    out = change(store, row_changes={0: {"region": "unknown"}, 1: {"region": "unknown"}})
    assert out["reason_code"] == "INDUSTRY_BASIS_UNKNOWN"
    assert change(store, row_changes={1: {"value": "NaN"}})["reason_code"] == "INDUSTRY_POINT_UNAVAILABLE"


def test_official_ten_day_periods_allow_variable_last_window_but_not_arbitrary_days(store):
    periods = {0: {"frequency": "ten-day", "period_start": "2026-02-11", "period_end": "2026-02-20"},
               1: {"frequency": "ten-day", "period_start": "2026-02-21", "period_end": "2026-02-28"}}
    assert change(store, row_changes=periods)["delta"] == "10"
    periods[0]["period_start"] = "2026-02-12"
    assert change(store, row_changes=periods)["reason_code"] == "INDUSTRY_PERIOD_WINDOWS_DIFFER"


def test_revision_regression_overlap_and_cumulative_not_monthly_flow(store):
    same = {"period_start": "2026-08-01", "period_end": "2026-08-31"}
    assert change(store, row_changes={1: same})["status"] == "same-period-revision"
    assert change(store, row_changes={1: {**same, "value": "100"}})["status"] == "unchanged"
    assert change(store, row_changes={1: {"period_start": "2026-07-01", "period_end": "2026-07-31"}})[
        "reason_code"] == "INDUSTRY_PERIOD_REGRESSED"
    assert change(store, row_changes={1: {"period_start": "2026-08-15"}})[
        "reason_code"] == "INDUSTRY_PERIODS_OVERLAP"
    out = change(store, row_changes={i: {"period_start": "2026-01-01", "period_kind": "ytd-flow"}
                                    for i in (0, 1)})
    assert out["reason_code"] == "INDUSTRY_CUMULATIVE_NOT_PERIOD_FLOW" and out["delta"] is None
    monthly = change(store, row_changes={i: {"metric": "output", "period_kind": "period-flow"}
                                        for i in (0, 1)})
    assert monthly["status"] == "changed"  # August and September have different calendar lengths.
    out = change(store, row_changes={0: {"period_kind": "period-flow", "frequency": "irregular"},
                                    1: {"period_kind": "period-flow", "frequency": "irregular"}})
    assert out["reason_code"] == "INDUSTRY_PERIOD_WINDOWS_DIFFER"


@pytest.mark.parametrize("mutation", ["quote", "identity", "period", "sellside", "locator", "channel", "missing"])
def test_unqualified_relation_never_enters_either_query(store, mutation):
    _, payload = prepare(store)
    relation = payload["relations"][0]
    if mutation == "quote":
        relation["evidence"]["quote"] = "Unsupported inferred beneficiary claim."
    elif mutation == "identity":
        relation["company"]["entity_id"] = BUYER.entity_id
    elif mutation == "period":
        relation["report_period"] = "2025"
    elif mutation == "sellside":
        relation["evidence"]["source_role"] = "sellside"
    elif mutation == "locator":
        relation["evidence"]["reference"]["selection"] = asdict(Selection("locator"))
    elif mutation == "channel":
        del relation["transmission"]["cash_flow"]
    else:
        del relation["evidence"]
    ref = packet(store, payload)
    forward = read(store, ref, subject_id=SUBJECT.entity_id)
    reverse = read(store, ref, company_id=PRODUCER.entity_id)
    assert forward["status"] == reverse["status"] == "partial"
    assert forward["scope"]["accepted_relations"] == 1
    assert reverse["relations"] == reverse["series"] == []
    assert forward["relation_outcomes"][0]["status"] == "unavailable"


def test_missing_evidence_is_local_and_no_latest_substitution(store):
    ref, payload = prepare(store)
    manifest = payload["relations"][0]["evidence"]["reference"]["manifest"]
    original = VersionedStore.read_source
    def lose_source(self, source_ref):
        if asdict(source_ref.manifest) == manifest:
            raise MissingObject("OBJECT_MISSING")
        return original(self, source_ref)
    with patch.object(VersionedStore, "read_source", lose_source):
        out = read(store, ref)
    assert out["status"] == "partial"
    assert len(out["relations"]) == 1 and len(out["series"]) == 1
    assert out["series"][0]["latest_change"]["status"] == "changed"
    assert out["relation_outcomes"][0]["reason_code"] == "OBJECT_MISSING"


def test_source_dates_and_exact_single_row_are_required(store):
    for changes in ({"acquired_at": None}, {"published_at": "2026-10-14"},
                    {"acquired_at": "2026-10-12T11:00:00+08:00"}):
        assert change(store, source_changes=changes)["reason_code"] == "INDUSTRY_POINT_UNAVAILABLE"
    _, payload = prepare(store)
    payload["series"][0]["observations"][0]["selection"]["end"] = 2
    out = read(store, packet(store, payload))
    assert out["status"] == "partial"
    assert out["series"][0]["observations"][0]["reason_code"] == "INDUSTRY_ONE_EXACT_ROW_REQUIRED"
    assert read(store, prepare(store)[0])["intraday_availability"].startswith("not-established")


def test_new_source_version_does_not_replace_old_reading(store):
    old, _ = prepare(store)
    before = read(store, old)
    newer, _ = prepare(store, row_changes={1: {"value": "120"}})
    assert read(store, old) == before
    assert read(store, newer)["series"][0]["latest_change"]["delta"] == "20"


def test_empty_query_does_not_imply_zero_coverage_and_duplicate_id_rejected(store):
    ref, payload = prepare(store)
    empty = read(store, ref, company_id="not-covered")
    assert empty["relations"] == empty["series"] == []
    assert empty["scope"]["accepted_relations"] == 2
    with pytest.raises(RecordError, match="ONE_QUERY_DIRECTION"):
        read(store, ref, company_id=BUYER.entity_id, subject_id=SUBJECT.entity_id)
    payload["relations"].append(deepcopy(payload["relations"][0]))
    with pytest.raises(RecordError, match="DUPLICATE_RELATION"):
        read(store, packet(store, payload))


def test_cli_fresh_process_uses_only_exact_retained_inputs_and_writes_nothing(store, tmp_path):
    ref, _ = prepare(store)
    reference = tmp_path / "ref.json"
    reference.write_bytes(encoded(asdict(ref)))
    head = store.backend.head()
    env = {k: v for k, v in os.environ.items()
           if k not in ("GH_TOKEN", "GITHUB_TOKEN", "DECISION_OS_STORE_TOKEN", "TUSHARE_PROXY_API_KEY")}
    env["PYTHONPATH"] = str(ROOT / "src")
    cli = [sys.executable, "-m", "decision_kernel.interfaces.industry_cli",
           "--path", str(store.backend.root), "--ref", str(reference)]
    out = subprocess.run([*cli, "--company-id", BUYER.entity_id], cwd=tmp_path, env=env,
                         text=True, capture_output=True, timeout=30)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert result["relations"][0]["role"] == "purchaser"
    assert result["source_requests"] == result["writes"] == 0
    assert GitStore(store.backend.root).head() == head
    reference.write_text('{"manifest":{},"manifest":{}}', encoding="utf-8")
    out = subprocess.run(cli, cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)
    assert out.returncode == 2 and json.loads(out.stderr)["error"] == "DUPLICATE_JSON_KEY"


def test_provider_change_requires_explicit_new_baseline(store):
    _, payload = prepare(store)
    _, other = prepare(store, source_changes={"origin": "synthetic:different-survey"})
    payload["series"][0]["observations"][1] = other["series"][0]["observations"][1]
    out = read(store, packet(store, payload))["series"][0]["latest_change"]
    assert out["reason_code"] == "INDUSTRY_BASIS_CHANGED"
    assert out["changed_fields"] == ["source.origin"] and out["delta"] is None


def test_remote_cli_forces_readonly_binding(store, tmp_path, capsys):
    from decision_kernel.interfaces import industry_cli
    ref, _ = prepare(store)
    reference, binding = tmp_path / "ref.json", tmp_path / "binding.json"
    reference.write_bytes(encoded(asdict(ref)))
    configuration = json.loads((ROOT / "config/store-binding.json").read_text())
    configuration["write_enabled"] = True
    binding.write_bytes(encoded(configuration))
    with patch.object(industry_cli.GitHubAPI, "from_environment", return_value=object()), \
            patch.object(industry_cli, "GitHubStore", return_value=store.backend) as backend:
        assert industry_cli.main(["--binding", str(binding), "--ref", str(reference)]) == 0
    assert backend.call_args.args[0].write_enabled is False
    assert json.loads(capsys.readouterr().out)["writes"] == 0


def test_observation_cannot_be_future_to_its_source_and_average_windows_must_match(store):
    out = change(store, row_changes={1: {"period_start": "2026-10-02", "period_end": "2026-10-05"}})
    assert out["reason_code"] == "INDUSTRY_POINT_UNAVAILABLE"
    out = change(store, row_changes={1: {"period_start": "2026-09-15"}})
    assert out["reason_code"] == "INDUSTRY_PERIOD_WINDOWS_DIFFER"


def test_supported_decimal_extremes_keep_exact_delta(store):
    from decimal import Decimal, localcontext
    before, after = "9" * 63, "0." + "0" * 60 + "1"
    out = change(store, row_changes={0: {"value": before}, 1: {"value": after}})
    with localcontext() as context:
        context.prec = 128
        assert Decimal(out["delta"]) == Decimal(after) - Decimal(before)
