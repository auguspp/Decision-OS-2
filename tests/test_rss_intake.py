"""RSS parser/custody owner: actual feedparser and GitStore; only HTTP is synthetic."""
from copy import deepcopy
from dataclasses import asdict
import base64
import gzip
import json
from pathlib import Path
import subprocess
import sys
from xml.sax.saxutils import escape

import pytest

from decision_kernel.application import rss_intake as rss
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Conflict, MissingObject, RecordError, SourceInput, UnknownWrite, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure import rss_http
from decision_kernel.interfaces import rss_cli
from decision_kernel.observations import rss_news

URL = "https://www.stats.gov.cn/sj/zxfb/202610/t20261016_10001.html"
AT = "2026-10-16T10:00:00+00:00"
LATER = "2026-10-16T11:00:00+00:00"


def xml(title="合成统计观察", summary="合成摘要不得在元数据里公开", stamp="2026-10-16 12:00:00", url=URL, extra=""):
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>NBS synthetic</title>'
            '<link>https://www.stats.gov.cn/</link><description>synthetic test only</description><item>'
            '<guid>'+escape(url)+'</guid><link>'+escape(url)+'</link><title>'+escape(title)+'</title>'
            '<description>'+escape(summary)+'</description><pubDate>'+escape(stamp)+'</pubDate>'
            '</item>'+extra+'</channel></rss>').encode()


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / "rss.git"))


def capture(store, *, body=None, prior=None, at=AT, ident="capture-one", original=False):
    return rss.collect(store, {"feed_id": "nbs-releases", "bootstrap": prior is None,
                              "predecessor": asdict(prior) if prior is not None else None,
                              "retain_original": original,
                              "rights_basis": "SYNTHETIC_TEST_BYTES_CREATED_FOR_TEST" if original else ""},
                       batch_id=ident, fetch=lambda _: xml() if body is None else body, clock_fn=lambda: at)


def save(store, name, value):
    return store.put_source(name, rss.source_input(value, name))


def test_real_parser_preserves_unzoned_claim_without_leaking_summary(store):
    result = capture(store)
    row = result["window"]["entries"][0]
    assert row["payload"]["published_raw"] == "2026-10-16 12:00:00"
    view = rss.read_capture(result)
    assert view["rows"][0]["published_claim"]["instant"] is None
    assert view["rows"][0]["published_claim"]["publication_verified"] is False
    assert "合成摘要不得在元数据里公开" not in encoded(result).decode()
    assert rss.original_bytes(result) is None and result["delta"]["new_to_selected_predecessor"] == []
    assert result["delta"]["status"] == "baseline-not-new-publications"


@pytest.mark.parametrize("raw,instant", [
    ("2026-10-16T18:00:00+08:00", "2026-10-16T18:00:00+08:00"),
    ("Fri, 16 Oct 2026 10:00:00 GMT", "2026-10-16T10:00:00+00:00"),
    ("Thu, 16 Oct 2026 10:00:00 GMT", None),
    ("2026-10-16", None), ("2026-02-30T10:00:00Z", None), ("", None),
])
def test_publication_date_never_uses_lenient_correction(raw, instant):
    claim = rss_news.publication_claim(raw)
    assert claim["raw"] == raw and claim["instant"] == instant and claim["publication_verified"] is False


def test_exact_metadata_predecessor_replay_without_fetch(store, tmp_path):
    first = capture(store); a = save(store, "first", first)
    second = capture(store, prior=a, at=LATER, ident="capture-two"); b = save(store, "second", second)
    assert second["predecessor"]["recovery"] == "restored-metadata-only"
    assert second["observations"][0]["first_seen_at"] == AT
    assert second["observations"][0]["seen_capture_count"] == 2
    assert second["delta"]["status"] == "no-new-feed-versions" and second["delta"]["raw_bytes_unchanged"] is True
    fresh = VersionedStore(GitStore(tmp_path / "rss.git"))
    assert rss.open_capture(fresh, b) == second
    assert store.put_source("second", rss.source_input(second, "second")) == b


def test_exact_original_roundtrip_and_predecessor_reparse(store):
    first = capture(store, original=True); a = save(store, "original", first)
    assert rss.original_bytes(rss.open_capture(store, a)) == xml()
    second = capture(store, prior=a, original=True, at=LATER, ident="second")
    assert second["predecessor"]["recovery"] == "restored-original"
    assert rss.original_bytes(second) == xml()


@pytest.mark.parametrize("change", [dict(title="改版标题"), dict(summary="更正摘要"), dict(stamp="2026-10-16T12:00:00+08:00")])
def test_changed_title_summary_or_time_claim_is_new_version_not_new_event(store, change):
    first = capture(store); prior = save(store, "prior", first)
    second = capture(store, prior=prior, body=xml(**change), at=LATER, ident="second")
    assert second["window"]["entries"][0]["document_id"] == first["window"]["entries"][0]["document_id"]
    assert len(second["delta"]["new_to_selected_predecessor"]) == 1
    assert len(second["delta"]["missing_from_current_window"]) == 1
    assert second["delta"]["missing_meaning"] == "NOT_RETRACTION_OR_DELETION"
    assert second["authority"]["event_verified"] is False


def test_bad_or_future_predecessor_fails_before_source_request(store):
    future = save(store, "future", capture(store, at=LATER))
    called = []
    with pytest.raises(RecordError):
        rss.collect(store, {"feed_id": "nbs-releases", "bootstrap": False, "predecessor": asdict(future)},
                    batch_id="child", fetch=lambda _: called.append(True), clock_fn=lambda: AT)
    assert not called
    with pytest.raises(RecordError):
        rss.collect(store, {"feed_id": "nbs-releases", "bootstrap": False}, batch_id="child", fetch=lambda _: called.append(True))
    assert not called


def test_wrong_feed_predecessor_fails_before_source_request(store):
    prior = save(store, "prior", capture(store))
    with pytest.raises(RecordError, match="RSS_PREDECESSOR_FEED_MISMATCH"):
        rss.collect(store, {"feed_id": "nbs-interpretations", "bootstrap": False, "predecessor": asdict(prior)},
                    batch_id="child", fetch=lambda _: pytest.fail("no HTTP"), clock_fn=lambda: LATER)


def test_failed_http_does_not_advance_store_or_erase_predecessor(store):
    prior = save(store, "prior", capture(store)); head = store.backend.head(); calls = []
    def fail(feed):
        calls.append(feed); raise RecordError("RSS_HTTP_UNAVAILABLE")
    with pytest.raises(RecordError, match="RSS_HTTP_UNAVAILABLE"):
        rss.collect(store, {"feed_id": "nbs-releases", "bootstrap": False, "predecessor": asdict(prior)},
                    batch_id="failed", fetch=fail, clock_fn=lambda: LATER)
    assert calls == ["nbs-releases"] and store.backend.head() == head
    assert rss.open_capture(store, prior)["batch_id"] == "capture-one"


@pytest.mark.parametrize("body", [b"", b"<html>not a feed</html>", b"<!DOCTYPE rss><rss/>", b"<!ENTITY x 'x'><rss/>",
                                   "<!DOCTYPE rss><rss/>".encode("utf-16")])
def test_invalid_or_external_entity_feed_rejected(body):
    with pytest.raises(RecordError): rss_news.parse_window(body, "nbs-releases")


def test_source_limits_and_link_domain_are_not_silently_relaxed(monkeypatch):
    with pytest.raises(RecordError): rss_news.parse_window(xml(url="https://evil.invalid/private"), "nbs-releases")
    monkeypatch.setattr(rss_news, "MAX_ITEMS", 0)
    with pytest.raises(RecordError, match="RSS_ITEM_BOUND"): rss_news.parse_window(xml(), "nbs-releases")


def test_duplicate_feed_items_and_changed_parser_rejected(monkeypatch):
    body = xml(); item = body[body.index(b"<item>"):body.index(b"</item>")+7].decode()
    with pytest.raises(RecordError, match="RSS_DUPLICATE_DOCUMENT"):
        rss_news.parse_window(xml(extra=item), "nbs-releases")
    monkeypatch.setattr(rss_news, "version", lambda _: "different")
    with pytest.raises(RecordError, match="RSS_PARSER_VERSION_CHANGED"):
        rss_news.parse_window(xml(), "nbs-releases")


def test_original_rights_and_corruption_are_explicit(store):
    with pytest.raises(RecordError, match="RSS_ORIGINAL_RIGHTS_REQUIRED"):
        rss.collect(store, {"feed_id": "nbs-releases", "bootstrap": True, "retain_original": True},
                    batch_id="x", fetch=lambda _: pytest.fail("rights first"))
    original = capture(store, original=True)
    original["original"]["gzip_base64"] = base64.b64encode(gzip.compress(b"different bytes")).decode()
    with pytest.raises(RecordError): rss.verify_capture(rss._seal(original))


def test_bounded_original_decompression_rejects_trailing_and_oversized_data(store, monkeypatch):
    original = capture(store, original=True)
    bad = deepcopy(original)
    packed = base64.b64decode(bad["original"]["gzip_base64"]) + gzip.compress(b"extra")
    bad["original"]["gzip_base64"] = base64.b64encode(packed).decode()
    with pytest.raises(RecordError): rss.verify_capture(rss._seal(bad))
    monkeypatch.setattr(rss, "MAX_BODY", 8)
    with pytest.raises(RecordError): rss.verify_capture(original)


def test_transition_counter_tampering_rejected_against_saved_predecessor(store):
    first = save(store, "first", capture(store))
    second = capture(store, prior=first, at=LATER, ident="second")
    second["observations"][0]["seen_capture_count"] += 1
    bad = save(store, "bad", rss._seal(second))
    with pytest.raises(RecordError, match="RSS_TRANSITION_REPLAY_DIFFERS"):
        rss.open_capture(store, bad)


def test_offline_read_zero_results_keeps_health_and_does_not_modify_bytes(store, tmp_path):
    path = tmp_path / "capture.json"; original = encoded(capture(store)); path.write_bytes(original)
    run = subprocess.run([sys.executable, "-m", "decision_kernel.interfaces.rss_cli", "read-local", "--input", str(path), "--query", "nonmatching"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["rows"] == [] and result["source_outcome"] == "captured"
    assert result["health_scope"] == "whole-selected-feed-capture" and path.read_bytes() == original
    assert result["original_status"] == "not-retained-rights-unconfirmed"


def test_planned_commit_boundary_uses_original_backend_cas(monkeypatch):
    backend = object.__new__(rss_cli._PlannedStore)
    backend.planned_head = "a" * 40; backend.commit_started = False; backend.published = None
    calls = []
    monkeypatch.setattr(rss_cli.GitHubStore, "commit", lambda self, files, *, expected: calls.append(expected) or "b" * 40)
    with pytest.raises(Conflict): backend.commit({}, expected="c" * 40)
    assert not calls and backend.commit_started is False
    assert backend.commit({}, expected="a" * 40) == "b" * 40 and calls == ["a" * 40]


def test_native_mode_needs_explicit_host_and_has_no_hidden_capture(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(rss_cli, "fetch_feed", lambda _: pytest.fail("no HTTP"))
    out = tmp_path / "out"
    assert rss_cli.main(["run", "--out", str(out)]) == 2 and not out.exists()


def native_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/store-binding.json").write_bytes(encoded({"repository": rss_cli.REPOSITORY,
        "repository_id": 1420041744, "branch": "records", "anchor": "a"*40, "store_id": "synthetic-rss", "visibility": "public", "write_enabled": False}))
    event = tmp_path / "event.json"
    inputs = {"operation": "capture", "code_sha": "a"*40, "submission_id": "native-test", "expected_head": "b"*40,
              "public_metadata_custody_confirmed": "true", "public_original_custody_confirmed": "false",
              "plan": json.dumps({"feed_id": "nbs-releases", "bootstrap": True})}
    event.write_bytes(encoded({"inputs": inputs}))
    for key, value in {"GITHUB_REPOSITORY": rss_cli.REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                       "GITHUB_WORKFLOW_REF": rss_cli.WORKFLOW, "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a"*40,
                       "GITHUB_RUN_ID": "1234", "GITHUB_EVENT_PATH": str(event), "DOS2_RSS_MODE": "capture"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(rss_cli.GitHubAPI, "from_environment", lambda: object())
    return inputs


def test_existing_submission_is_refused_before_recapture(tmp_path, monkeypatch):
    native_env(tmp_path, monkeypatch)
    class Backend:
        commit_started = False
        def head(self): return "b"*40
        def read(self, *args): return b"existing-intent"
    monkeypatch.setattr(rss_cli, "_PlannedStore", lambda *args: Backend())
    monkeypatch.setattr(rss_cli, "fetch_feed", lambda _: pytest.fail("must not recapture"))
    out = tmp_path / "out"
    assert rss_cli.main(["run", "--out", str(out)]) == 2
    report = json.loads((out / "report.json").read_text())
    assert report["error_code"] == "RSS_SUBMISSION_EXISTS_RECONCILE_WITHOUT_FETCH" and report["write"] == "not-attempted"


def test_unknown_write_retains_original_input_and_never_refetches(tmp_path, monkeypatch):
    native_env(tmp_path, monkeypatch)
    class Backend:
        commit_started = False
        published = None
        def head(self): return "b"*40
        def read(self, *args): raise MissingObject("OBJECT_MISSING")
    backend = Backend()
    class Store:
        def put_source(self, submission, source):
            backend.commit_started = True
            raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    calls = []
    monkeypatch.setattr(rss_cli, "_PlannedStore", lambda *args: backend)
    monkeypatch.setattr(rss_cli, "VersionedStore", lambda _: Store())
    monkeypatch.setattr(rss_cli, "fetch_feed", lambda feed: calls.append(feed) or xml())
    out = tmp_path / "out"
    assert rss_cli.main(["run", "--out", str(out)]) == 2 and calls == ["nbs-releases"]
    report = json.loads((out / "report.json").read_text())
    assert report["write"] == "unknown" and "reference" not in report and (out / "source.json").is_file()
    assert "合成摘要不得在元数据里公开" not in (out / "source.json").read_text()


def test_http_transport_is_fixed_bounded_and_without_credentials(monkeypatch):
    seen = []
    class Response:
        status = 200
        headers = {"Content-Length": str(len(xml()))}
        def geturl(self): return rss_news.FEEDS["nbs-releases"]
        def read(self, bound): assert bound == rss_news.MAX_BODY+1; return xml()
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            assert timeout == 30 and request.get_header("Authorization") is None
            return Response()
    monkeypatch.setattr(rss_http, "build_opener", lambda *args: Opener())
    assert rss_http.fetch_feed("nbs-releases") == xml() and len(seen) == 1
    with pytest.raises(RecordError): rss_http.fetch_feed("https://evil.invalid/")
    assert len(seen) == 1
    assert rss_http._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid/") is None


def test_workflow_and_optional_dependency_ownership():
    import tomllib
    import yaml
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/rss-intake.yml").read_text()
    flow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(flow["on"]) == {"workflow_dispatch"} and flow["permissions"] == {}
    assert flow["jobs"]["read"]["permissions"]["contents"] == "read"
    assert flow["jobs"]["capture"]["permissions"]["contents"] == "write"
    assert "TUSHARE_PROXY_API_KEY" not in text and "schedule:" not in text
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert project["project"]["dependencies"] == []
    assert project["project"]["optional-dependencies"]["rss"] == ["feedparser==6.0.14", "feedparser-sgmllib==2.1.0"]
    assert "-r rss.txt" in (root / "requirements/dev.txt").read_text()
