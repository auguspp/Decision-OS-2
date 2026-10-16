"""The existing news/24h consumers use native RSS references, not a second pool."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys

import pytest

from decision_kernel.application import news_rolling as roll, rss_intake as rss
from decision_kernel.domain.records import RecordError, SourceInput, digest, encoded
from decision_kernel.observations import news_windows as news
from decision_kernel.interfaces import news_cli
from test_news_windows import newsnow
from test_rss_intake import AT, LATER, URL, capture, save, store, xml

END = "2026-10-16T12:00:00Z"


def old_news(store, name="news", at=AT):
    value = news.combine([news.newsnow_window(newsnow(), "cls", observed_at=at),
                          {"status": "source-unavailable", "source_id": "thepaper"}])
    ref = store.put_source(name, SourceInput(source_id=name, kind="content", title=name,
                          origin="synthetic://joint-news", text=encoded(value).decode(), custody="provided"))
    return ref, value


def build(store, references, **kw):
    return roll.rolling_news(store.read_source, references, as_of=kw.pop("as_of", END), **kw)


def put_view(store, value, name):
    return store.put_source(name, SourceInput(source_id=name, kind="content", title=name,
                          origin="synthetic://joint-news", text=encoded(value).decode(), custody="provided"))


def test_native_projection_retains_identity_claims_and_metadata_only(store):
    cap = capture(store)
    ref = save(store, "rss", cap)
    value = roll.read_news_batch(store.read_source, ref)
    row, = value["observations"]
    native = cap["window"]["entries"][0]
    assert row["provider"] == "rss" and row["source_id"] == "nbs-releases"
    assert row["article_id"] == native["document_id"]
    assert row["rss"]["version_id"] == native["version_id"]
    assert row["rss"]["summary_sha256"] == native["payload"]["summary_sha256"]
    assert row["published_claim"]["instant"] is None and row["published_claim"]["raw"] == "2026-10-16 12:00:00"
    assert row["observed_at"] == AT and row["event_verified"] is False
    assert "合成摘要不得在元数据里公开" not in encoded(value).decode()
    assert news.verify_reading(value) == value
    context = value["source_outcomes"][0]["rss"]
    assert context["delta"]["status"] == "baseline-not-new-publications"
    assert context["original_status"] == "not-retained-rights-unconfirmed"


def test_existing_cli_merge_read_and_locator_export_preserve_all_context(store, tmp_path, capsys):
    cap = capture(store); ref = save(store, "rss", cap)
    rss_reading = roll.read_news_batch(store.read_source, ref)
    _, legacy = old_news(store)
    a, b, mixed, loc = [tmp_path / n for n in ("a.json", "b.json", "mixed.json", "loc.json")]
    a.write_bytes(encoded(rss_reading)); b.write_bytes(encoded(legacy))
    assert news_cli.main(["merge", "--windows", str(a), str(b), "--output", str(mixed)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["observations"] == 2 and len(result["source_outcomes"]) == 3
    for selector in ("nbs-releases", "nonexistent"):
        assert news_cli.main(["read", "--window", str(mixed), "--source", selector]) == 0
        reading = json.loads(capsys.readouterr().out)
        assert len(reading["source_outcomes"]) == 3
        assert reading["selected_count"] == (1 if selector == "nbs-releases" else 0)
        assert any(o["source"] == "thepaper" and o["status"] == "unavailable" for o in reading["source_outcomes"])
    assert news_cli.main(["export-locators", "--window", str(mixed), "--output", str(loc)]) == 0
    capsys.readouterr()
    inputs = json.loads(loc.read_bytes())
    assert len(inputs["source_outcomes"]) == 3
    sources = [SourceInput.from_dict(item) for item in inputs["items"]]
    assert all(s.custody == "external" and s.text is None for s in sources)
    assert any("RSS_FEED_METADATA_NOT_LINKED_ARTICLE" in s.limitations for s in sources)


def test_old_rolling_predecessor_can_continue_with_rss_without_changing_old_entries(store):
    old, _ = old_news(store)
    original = build(store, [old])
    prior = put_view(store, original, "rolling-prior")
    native = capture(store); current = save(store, "current-rss", native)
    view = build(store, [current], predecessor=prior)
    assert view["predecessor"]["status"] == "restored-exact-reference"
    assert len(view["entries"]) == 2 and view["window_hours"] == 24
    assert [e for e in view["entries"] if e["observation"]["provider"] == "newsnow"] == original["entries"]
    assert view["coverage"]["failed_source_windows"] == 1 and view["losses"] == []


def test_selected_batch_counts_do_not_inherit_rss_chain_count_or_alias_exposure(store):
    first = capture(store); a = save(store, "first", first)
    child = capture(store, prior=a, at=LATER, ident="child"); b = save(store, "child", child)
    alias = save(store, "alias", child)
    current_only = build(store, [b, b, alias])
    row, = current_only["entries"]
    assert row["seen_batch_count_in_window"] == 1 and len(current_only["batch_references"]) == 1
    assert row["first_seen_in_window"] == LATER  # Not inherited AT.
    both = build(store, [a, b, alias, a])
    entry, = both["entries"]
    assert entry["first_seen_in_window"] == AT and entry["last_seen_in_window"] == LATER
    assert entry["seen_batch_count_in_window"] == 2
    later = build(store, [], predecessor=put_view(store, both, "rolling"), as_of="2026-10-17T10:30:00Z")
    assert later["entries"][0]["seen_batch_count_in_window"] == 1
    assert later["entries"][0]["first_seen_in_window"] == LATER


@pytest.mark.parametrize("at,expected", [("2026-10-15T13:00:00Z", 1), ("2026-10-15T12:00:00Z", 1), ("2026-10-15T11:59:59Z", 0)])
def test_rss_uses_exact_24h_receipt_clock_not_media_publication(store, at, expected):
    ref = save(store, "boundary", capture(store, at=at))
    view = build(store, [ref])
    assert len(view["entries"]) == expected and view["window_hours"] == 24


def test_summary_only_revision_is_not_lost_and_cross_feed_identity_does_not_collide(store):
    original = capture(store); a = save(store, "first", original)
    changed = capture(store, body=xml(summary="合成更正摘要"), prior=a, at=LATER, ident="changed")
    b = save(store, "changed", changed)
    other = rss.collect(store, {"feed_id": "nbs-interpretations", "bootstrap": True},
                        batch_id="other", fetch=lambda _: xml(), clock_fn=lambda: AT)
    c = save(store, "other", other)
    value = build(store, [a, b, c])
    rows = [e["observation"] for e in value["entries"]]
    assert len(rows) == 3 and len({r["version_id"] for r in rows}) == 3
    assert len({r["article_id"] for r in rows}) == 1
    assert len({r["rss"]["version_id"] for r in rows}) == 2
    assert all(r["title"] == original["window"]["entries"][0]["payload"]["title"] for r in rows)


def test_native_predecessor_reconstruction_is_required_and_legacy_news_survives(store):
    parent = save(store, "parent", capture(store))
    value = capture(store, prior=parent, at=LATER, ident="child")
    # Individually well-shaped but not the actual selected transition.
    value["observations"][0]["seen_capture_count"] = 8
    bad = put_view(store, rss._seal(value), "bad")
    old, _ = old_news(store)
    result = build(store, [bad, old])
    assert len(result["entries"]) == 1 and result["entries"][0]["observation"]["provider"] == "newsnow"
    assert result["rejected_references"] == json.loads(encoded([asdict(bad)]))
    assert result["losses"][0]["kind"] == "BATCH_UNAVAILABLE_OR_REJECTED"


def test_predecessor_manifest_mismatch_is_a_gap_not_trusted_metadata(store):
    parent = save(store, "parent", capture(store))
    child = save(store, "child", capture(store, prior=parent, at=LATER, ident="child"))
    old, _ = old_news(store)
    def reader(ref):
        result = store.read_source(ref)
        if ref == parent:
            result = deepcopy(result); result["manifest"]["stored_at"] = "tampered"
        return result
    output = roll.rolling_news(reader, [old, child], as_of=END)
    assert len(output["entries"]) == 1 and output["rejected_references"] == json.loads(encoded([asdict(child)]))


@pytest.mark.parametrize("change", [{"full_text": "not permitted"}, {"event_verified": True},
    {"body_archived": True}, {"url": "https://evil.test/article"}, {"provider": "newsnow"},
    {"rss": {"version_id": "0" * 64}}, {"published_claim": {"raw": "2026-10-16", "instant": "2026-10-16T00:00:00Z"}}])
def test_resealed_rows_cannot_escape_rss_projection_or_claim_authority(store, change):
    ref = save(store, "rss", capture(store))
    value = build(store, [ref]); value["entries"][0]["observation"].update(change)
    roll._seal(value)
    with pytest.raises(RecordError): roll.read_rolling(value)


@pytest.mark.parametrize("change", ["original", "delta", "extra", "time"])
def test_resealed_rss_batch_context_is_validated_on_normal_read(store, change):
    ref = save(store, "rss", capture(store))
    value = build(store, [ref]); outcome = value["source_outcomes"][0]["outcome"]
    if change == "original": outcome["rss"]["original_status"] = "verified-news-body"
    if change == "delta": outcome["rss"]["delta"]["status"] = "new-publications"
    if change == "extra": outcome["rss"]["full_text"] = "forbidden"
    if change == "time": outcome["capture"]["requested_at"] = "no-clock"
    roll._seal(value)
    with pytest.raises(RecordError): roll.read_rolling(value)


def test_empty_rss_snapshot_keeps_capture_health_in_zero_row_page(store):
    body = xml(); begin = body.index(b"<item>"); end = body.index(b"</item>")+len(b"</item>")
    ref = save(store, "empty", capture(store, body=body[:begin]+body[end:]))
    value = build(store, [ref]); reading = roll.read_rolling(value, source="nonexistent")
    assert not reading["rows"] and len(reading["source_outcomes"]) == 1
    assert reading["source_outcomes"][0]["outcome"]["observations"] == 0
    assert reading["source_outcomes"][0]["outcome"]["rss"]["delta"]["baseline_versions"] == 0


def test_byte_limit_keeps_whole_batch_context_and_source_refs(store, monkeypatch):
    refs = [save(store, "row-"+str(i), capture(store, ident="capture-"+str(i),
            body=xml(url=URL.replace("10001", str(10001+i))))) for i in range(4)]
    full = build(store, refs)
    monkeypatch.setattr(roll, "MAX_BYTES", len(encoded(full))-1000)
    trimmed = build(store, refs)
    assert len(trimmed["entries"]) < len(full["entries"])
    assert trimmed["source_outcomes"] == full["source_outcomes"]
    assert trimmed["batch_references"] == full["batch_references"]
    page = roll.read_rolling(trimmed, query="no-match", limit=1)
    assert not page["rows"] and len(page["source_outcomes"]) == 4
    assert any(loss["kind"] == "BYTE_LIMIT" for loss in page["losses"])


def test_retained_xml_is_verified_but_never_forwarded_to_joint_metadata(store):
    value = capture(store, original=True); ref = save(store, "original", value)
    reading = roll.read_news_batch(store.read_source, ref)
    assert reading["source_outcomes"][0]["rss"]["original_status"] == "retained-with-explicit-public-rights"
    assert "gzip_base64" not in encoded(reading).decode() and "合成摘要不得在元数据里公开" not in encoded(reading).decode()
    assert not reading["observations"][0]["body_archived"]


def test_metadata_only_open_does_not_need_parser_or_invoke_transport(store, monkeypatch):
    ref = save(store, "metadata", capture(store))
    monkeypatch.setattr(rss, "parse_window", lambda *a, **k: pytest.fail("metadata must not reparse or fetch"))
    assert len(build(store, [ref])["entries"]) == 1


def test_saved_joint_view_reads_in_new_cli_process_and_pagination_keeps_context(store, tmp_path):
    old, _ = old_news(store)
    a = save(store, "a", capture(store))
    b = save(store, "b", capture(store, body=xml(url=URL.replace("10001", "10002"))))
    value = build(store, [old, a, b]); path = tmp_path / "joint.json"; path.write_bytes(encoded(value))
    run = subprocess.run([sys.executable, "-m", "decision_kernel.interfaces.news_rolling_cli", "read",
                          "--window", str(path), "--source", "nbs-releases", "--limit", "1"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    first = json.loads(run.stdout)
    second = roll.read_rolling(value, source="nbs-releases", limit=1, cursor=first["next_cursor"])
    assert first["rows"] != second["rows"] and first["source_outcomes"] == second["source_outcomes"]
    assert path.read_bytes() == encoded(value)
    with pytest.raises(RecordError): roll.read_rolling(value, source="cls", cursor=first["next_cursor"])


def test_existing_host_supplies_locked_optional_parser_without_new_source_permission():
    import yaml
    flow = yaml.load((Path(__file__).parents[1]/".github/workflows/news-rolling.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(flow["on"]) == {"workflow_dispatch"}
    for name in ("build", "save"):
        steps = flow["jobs"][name]["steps"]
        assert any(s.get("uses") == "./.github/actions/ci-python" and s["with"]["profile"] == "dev" for s in steps)
    assert flow["jobs"]["build"]["permissions"] == {"contents": "read", "actions": "read"}
    assert flow["jobs"]["save"]["permissions"] == {"contents": "write"}
