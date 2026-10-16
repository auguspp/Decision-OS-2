"""RSS failed attempts through the real store/host and normal news consumers.

Only publisher responses and GitHub wire responses are injected. No artificial
fault option is exposed in the production workflow and no live outage is claimed.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from decision_kernel.application import rss_intake as rss, news_rolling as roll, news_rolling_index as index
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MissingObject, RecordError, digest, encoded
from decision_kernel.infrastructure import rss_http
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import rss_cli as host
from decision_kernel.observations import rss_failure as failure, rss_news, rss_reading
from decision_kernel.observations.news_windows import _reading, verify_reading
from test_github_store import GitAPI
from test_news_read_budget import transport

AT = "2026-10-16T10:00:00+00:00"
LATER = "2026-10-16T10:01:00+00:00"
END = "2026-10-16T12:00:00+00:00"
FEED = "nbs-releases"
PLAN = {"feed_id": FEED, "bootstrap": True}


def raising(error):
    def get(_):
        raise error
    return get


def attempt(store=None, *, plan=None, error=None, raw=None, clock_fn=lambda: AT, batch_id="test-attempt"):
    return rss.collect(store, plan or PLAN, batch_id=batch_id, clock_fn=clock_fn,
        fetch=raising(error or RecordError("RSS_HTTP_UNAVAILABLE")) if raw is None else lambda _: raw,
        retain_failure=True)


def save(store, value, name):
    return store.put_source(name, rss.source_input(value, name))


def baseline(*, at=AT, batch_id="baseline"):
    # A synthetic already-normalized metadata fixture, not a claimed parsed XML.
    entry = rss_news._entry("https://www.stats.gov.cn/sj/zxfb/202610/t20261016_10001.html", "id-1",
        {"title": "合成统计观察", "title_type": "text/plain", "summary_sha256": digest(b"synthetic"),
         "summary_type": "text/plain", "published_raw": "2026-10-16", "updated_raw": ""})
    window = rss_news.verify_window({"feed_id": FEED, "feed_url": rss_news.FEEDS[FEED],
        "parser_version": rss_news.PARSER, "format": "rss20", "raw_size": 1,
        "raw_sha256": digest(b"x"), "entries": [entry]})
    return rss.verify_capture(rss._assemble(window, batch_id=batch_id, requested_at=at, received_at=at,
        previous=None, predecessor=None, original={"status": "not-retained-rights-unconfirmed"}))


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / "rss-failure.git"))


def test_failed_http_is_one_request_with_exact_clock_and_no_raw_error(monkeypatch):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            raise HTTPError(request.full_url, 503, "private-secret-token and body", {}, None)
    monkeypatch.setattr(rss_http, "build_opener", lambda *args: Opener())
    clocks = iter([AT, LATER, END])
    value = rss.collect(None, PLAN, batch_id="http-error", fetch=rss_http.fetch_feed,
                        clock_fn=lambda: next(clocks), retain_failure=True)
    assert calls == [rss_news.FEEDS[FEED]]
    assert value["stage"] == "fetch" and value["error_code"] == "RSS_HTTP_STATUS_503"
    assert value["started_at"] == AT and value["requested_at"] == LATER and value["finished_at"] == END
    assert value["received_at"] is value["response"] is value["observations"] is None
    assert "private-secret-token" not in encoded(value).decode()


@pytest.mark.parametrize("error", [OSError("raw upstream body"), RecordError("SECRET_CAN_LOOK_UPPERCASE"),
                                    ValueError("private exception text")])
def test_unknown_errors_are_not_reflected_even_when_they_look_like_codes(error):
    value = attempt(error=error)
    assert value["error_code"] == "RSS_ATTEMPT_FAILED"
    assert str(error) not in encoded(value).decode()


def test_missing_predecessor_is_preserved_and_does_not_issue_http(store):
    ref = rss.reference(save(store, baseline(), "prior"))
    reader = SimpleNamespace(read_source=raising(MissingObject("private path")))
    plan = {"feed_id": FEED, "bootstrap": False, "predecessor": ref}
    value = rss.collect(reader, plan, batch_id="missing", clock_fn=lambda: LATER,
        retain_failure=True, fetch=lambda _: pytest.fail("no publisher request before valid predecessor"))
    assert value["stage"] == "predecessor" and value["requested_at"] is None
    assert value["predecessor"] == {"reference": ref, "recovery": "unavailable-or-rejected", "capture_sha256": None}
    saved = save(store, value, "missing")
    # Independent stored read only needs this receipt, not the unavailable prior.
    exact = store.read_source(saved)
    count = []
    consumer = SimpleNamespace(read_source=lambda r: count.append(r) or exact)
    assert rss.open_capture(consumer, saved) == value and len(count) == 1


def test_parse_rejection_keeps_response_identity_but_not_xml_or_fake_delta():
    value = attempt(raw=b"")  # real first parser admission guard; no optional parser required
    assert value["stage"] == "parse" and value["error_code"] == "RSS_BODY_OR_FEED_REJECTED"
    assert value["response"] == {"bytes": 0, "sha256": digest(b"")}
    assert "delta" not in value and value["observations"] is None
    assert value["original"] == {"status": "not-retained-failed-attempt"}


def test_real_parser_success_is_unchanged_and_bad_xml_is_a_failed_attempt():
    # Runs under the pinned real RSS extra in native full CI, not a parser stub.
    from test_rss_intake import xml
    options = dict(batch_id="actual-parser", fetch=lambda _: xml(), clock_fn=lambda: AT)
    old = rss.collect(None, PLAN, **options)
    new = rss.collect(None, PLAN, **options, retain_failure=True)
    assert encoded(old) == encoded(new) and old["source_outcome"] == "captured"
    bad = attempt(raw=b"<rss><channel>broken private publisher body")
    assert bad["stage"] == "parse" and bad["error_code"] == "RSS_XML_REJECTED"
    assert bad["response"]["sha256"] == digest(b"<rss><channel>broken private publisher body")
    assert "private publisher body" not in encoded(bad).decode()


def test_original_collect_still_raises_and_plan_errors_never_become_source_failures():
    with pytest.raises(RecordError, match="RSS_HTTP_UNAVAILABLE"):
        rss.collect(None, PLAN, batch_id="strict", fetch=raising(RecordError("RSS_HTTP_UNAVAILABLE")), clock_fn=lambda: AT)
    for plan in ({**PLAN, "feed_id": "https://example.invalid/"}, {**PLAN, "bootstrap": False},
                 {**PLAN, "retain_original": True}, {**PLAN, "extra": "private"}):
        with pytest.raises(RecordError):
            rss.collect(None, plan, batch_id="bad-plan", fetch=lambda _: pytest.fail("no fetch"), retain_failure=True)


@pytest.mark.parametrize("change", [
    {"observations": []}, {"observations": 0}, {"source_outcome": "captured"},
    {"requested_at": None}, {"finished_at": "2026-10-15T00:00:00Z"},
    {"error_code": "ARBITRARY_UPPERCASE_SECRET"}, {"extra": "hidden body"},
    {"feed_url": "https://example.invalid/"}, {"started_at": "2026-10-16T10:00:00"},
])
def test_forged_receipt_is_rejected_even_with_recomputed_digest(change):
    value = {**attempt(), **change}
    value.pop("capture_sha256")
    value["capture_sha256"] = digest(encoded(value))
    with pytest.raises(RecordError): rss.verify_capture(value)


def test_damaged_digest_is_not_silently_resealed():
    value = attempt(); value["capture_sha256"] = "0" * 64
    with pytest.raises(RecordError, match="RSS_FAILURE_INTEGRITY"): rss.read_capture(value)


def test_failed_attempt_cannot_advance_successful_predecessor(store):
    first = baseline(); before = save(store, first, "baseline")
    plan = {"feed_id": FEED, "bootstrap": False, "predecessor": asdict(before)}
    failed = attempt(store, plan=plan, clock_fn=lambda: LATER)
    assert failed["predecessor"]["capture_sha256"] == first["capture_sha256"]
    failure_ref = save(store, failed, "failed")
    wrong = {**plan, "predecessor": asdict(failure_ref)}
    next_failure = rss.collect(store, wrong, batch_id="must-not-advance", clock_fn=lambda: END,
        fetch=lambda _: pytest.fail("failed predecessor cannot fetch"), retain_failure=True)
    assert next_failure["stage"] == "predecessor"
    assert next_failure["error_code"] == "RSS_PREDECESSOR_NOT_CAPTURED"
    assert rss.open_capture(store, before) == first  # unchanged successful history


def test_saved_failure_works_in_joint_news_zero_filter_and_does_not_add_versions(store):
    prior = save(store, baseline(), "good")
    original = index.rolling_index(store.read_source, [prior], as_of=LATER)
    old_ref = store.put_source("index-old", rss.SourceInput(source_id="index-old",kind="content",title="synthetic",
        origin="synthetic://old",text=encoded(original).decode(),custody="provided"))
    ref = save(store, attempt(clock_fn=lambda: LATER), "bad")
    value = index.rolling_index(store.read_source, [ref, ref], as_of=END, predecessor=old_ref)
    assert value["entry_count"] == 1 and value["entries_sha256"] == original["entries_sha256"]
    assert value["coverage"]["failed_source_windows"] == 1 and not value["losses"]
    view = index.read_index(store.read_source, value, query="nonexistent")
    assert view["rows"] == [] and len(view["source_outcomes"]) == 2
    # Order is deterministic but do not assume the failure is last.
    received = next(x["outcome"] for x in view["source_outcomes"] if x["outcome"]["status"] == "unavailable")
    assert received["rss"]["failure"]["error_code"] == "RSS_HTTP_UNAVAILABLE"
    batch = roll.read_news_batch(store.read_source, ref)
    assert verify_reading(batch) == batch and batch["observations"] == []
    assert rss.read_capture(attempt(), query="nothing")["observations"] is None


def test_failure_cannot_authorize_fabricated_rss_rows():
    value = attempt()
    with pytest.raises(RecordError, match="RSS_NEWS_OUTCOME_REQUIRED"):
        _reading(rss_reading.news_rows(baseline()), [rss_reading.news_outcome(value)])
    forged = rss_reading.news_outcome(value); forged["observations"] = 0
    with pytest.raises(RecordError): rss_reading.verify_news_outcome(forged)


@pytest.fixture
def model(tmp_path):
    api = GitAPI(tmp_path / "native-rss.git")
    api.binding = replace(api.binding, repository=host.REPOSITORY, repository_id=1420041744,
                          branch="records", visibility="public", write_enabled=False)
    anchor = api.git.commit({"store.json": api.binding.marker()}, expected=api.git.head())
    api.binding = replace(api.binding, anchor=anchor)
    api.meta.update(id=1420041744, full_name=host.REPOSITORY, private=False)
    api.git.repository = host.REPOSITORY
    return api


def native(monkeypatch, tmp_path, model, *, operation="capture", plan=None, submission="failed-native"):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/store-binding.json").write_bytes(encoded(asdict(model.binding)))
    inputs = {"operation": operation, "code_sha": "a"*40,
        "submission_id": submission if operation == "capture" else "",
        "expected_head": model.git.head() if operation == "capture" else "",
        "public_metadata_custody_confirmed": "true", "public_original_custody_confirmed": "false",
        "plan": encoded(plan or PLAN).decode()}
    path = tmp_path / "event.json";path.write_bytes(encoded({"inputs": inputs}))
    for key, value in {"GITHUB_REPOSITORY":host.REPOSITORY,"GITHUB_EVENT_NAME":"workflow_dispatch",
        "GITHUB_REF":"refs/heads/main","GITHUB_WORKFLOW_REF":host.WORKFLOW,"GITHUB_RUN_ATTEMPT":"1",
        "GITHUB_SHA":"a"*40,"GITHUB_RUN_ID":"1234","GITHUB_EVENT_PATH":str(path),"DOS2_RSS_MODE":operation}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    api = transport(model)
    monkeypatch.setattr(host.GitHubAPI, "from_environment", lambda: api)
    return inputs, api


def test_native_failure_is_saved_nonzero_and_independently_read_without_http(model, tmp_path, monkeypatch):
    calls=[]
    def fail(feed):
        calls.append(feed);raise RecordError("RSS_HTTP_STATUS_503")
    monkeypatch.setattr(host, "fetch_feed", fail)
    _, api = native(monkeypatch, tmp_path, model)
    out=tmp_path/"save"
    assert host.main(["run","--out",str(out)]) == 2
    report=json.loads((out/"report.json").read_bytes())
    assert report["status"] == "source-failed-receipt-saved" and report["write"] == "published"
    assert report["observations"] is None and model.writes == 1 and calls == [FEED] and api._requests < 512
    original=(out/"capture.json").read_bytes()
    native(monkeypatch,tmp_path,model,operation="read",plan={"reference":report["reference"]})
    monkeypatch.setattr(host,"fetch_feed",lambda _:pytest.fail("read never requests publisher"))
    cold=tmp_path/"read"
    assert host.main(["run","--out",str(cold)]) == 0
    assert (cold/"capture.json").read_bytes() == original
    assert json.loads((cold/"reading.json").read_bytes())["source_outcome"] == "unavailable"
    assert json.loads((cold/"report.json").read_bytes())["write"] == "not-requested" and model.writes == 1
    # Retrying a capture with an existing original submission never refetches.
    native(monkeypatch,tmp_path,model)
    again=tmp_path/"again"
    assert host.main(["run","--out",str(again)]) == 2
    assert json.loads((again/"report.json").read_bytes())["error_code"] == "RSS_SUBMISSION_EXISTS_RECONCILE_WITHOUT_FETCH"
    assert model.writes == 1


def test_unknown_write_keeps_original_failure_and_reconciles_without_fetch(model,tmp_path,monkeypatch):
    calls=[]
    def fail(feed):
        calls.append(feed);raise RecordError("RSS_HTTP_UNAVAILABLE")
    monkeypatch.setattr(host,"fetch_feed",fail)
    native(monkeypatch,tmp_path,model)
    model.after_write=lambda value: (_ for _ in ()).throw(OSError("lost ack"))
    out=tmp_path/"lost"
    assert host.main(["run","--out",str(out)]) == 2
    report=json.loads((out/"report.json").read_bytes())
    assert report["write"] == "unknown" and report["source_outcome"] == "unavailable"
    assert "reference" not in report and calls == [FEED] and model.writes == 1
    model.after_write=None
    source=host.SourceInput.from_dict(json.loads((out/"source.json").read_bytes()))
    cold=VersionedStore(GitHubStore(model.binding,transport(model)))
    ref=cold.put_source("failed-native",source)
    assert rss.open_capture(cold,ref) == json.loads((out/"capture.json").read_bytes())
    assert model.writes == 1 and calls == [FEED]


def test_record_write_conflict_preserves_source_failure_without_claiming_saved(model,tmp_path,monkeypatch):
    native(monkeypatch,tmp_path,model)
    def fail(feed):
        model.git.commit({"synthetic-concurrent.txt":b"parallel"},expected=model.git.head())
        raise RecordError("RSS_HTTP_UNAVAILABLE")
    monkeypatch.setattr(host,"fetch_feed",fail)
    out=tmp_path/"conflict"
    assert host.main(["run","--out",str(out)]) == 2
    report=json.loads((out/"report.json").read_bytes())
    assert report["source_outcome"] == "unavailable" and report["status"] == "failed"
    assert report["write"] == "not-attempted" and "reference" not in report and model.writes == 0
    assert (out/"source.json").is_file()
