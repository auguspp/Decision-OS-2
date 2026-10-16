"""News CLI consumers retain whole-input failures, receipts and exact identity."""
from copy import deepcopy
import json

import pytest

from decision_kernel.domain.records import RecordError, SourceInput, encoded
from decision_kernel.infrastructure.news_capture import capture_newsnow
from decision_kernel.interfaces import news_cli
from decision_kernel.observations.news_windows import (
    combine, merge_readings, relay_window, verify_reading,
)
from test_news_windows import NOW, newsnow, relay


def window(*, failed_only=False):
    def fetcher(source):
        if source not in ("cls", "jin10"):
            raise RecordError("NEWSNOW_HTTP_503")
        return newsnow(source)
    sources = ("wallstreetcn", "thepaper") if failed_only else ("cls", "jin10", "wallstreetcn")
    return capture_newsnow(sources=sources, observed_at=NOW, fetcher=fetcher)


@pytest.fixture(autouse=True)
def no_live_capture(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline reading must not invoke capture")
    monkeypatch.setattr(news_cli, "capture_newsnow", forbidden)
    monkeypatch.setattr(news_cli, "capture_relay_news", forbidden)


def save(tmp_path, value, name="window.json"):
    path = tmp_path / name
    path.write_bytes(encoded(value))
    return str(path)


def assert_context(output, reading):
    assert output["reading_sha256"] == reading["reading_sha256"]
    assert output["source_outcomes"] == reading["source_outcomes"]
    assert output["source_outcomes_scope"] == "whole-input-reading"
    assert output["complete_news_coverage"] is False
    assert any(item["status"] == "unavailable" for item in output["source_outcomes"])


@pytest.mark.parametrize("source,count", [(None, 2), ("cls", 1), ("no-match", 0)])
def test_read_filter_and_limit_never_hide_batch_failures(tmp_path, capsys, source, count):
    view = window()
    original = deepcopy(view)
    args = ["read", "--window", save(tmp_path, view), "--limit", "1"]
    if source:
        args += ["--source", source]
    assert news_cli.main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert_context(output, view)
    assert output["selected_count"] == count and output["visible"] == min(count, 1)
    assert len(output["rows"]) == min(count, 1)
    assert verify_reading(view) == original


def test_all_failed_zero_rows_is_not_no_news(tmp_path, capsys):
    view = window(failed_only=True)
    assert news_cli.main(["read", "--window", save(tmp_path, view)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert_context(output, view)
    assert output["rows"] == [] and output["selected_count"] == 0
    assert len(output["source_outcomes"]) == 2
    assert all(x["status"] == "unavailable" for x in output["source_outcomes"])


@pytest.mark.parametrize("failed_only", [False, True])
def test_locator_file_and_stdout_keep_failures_without_changing_source_inputs(tmp_path, capsys, failed_only):
    view = window(failed_only=failed_only)
    target = tmp_path / "locators.json"
    assert news_cli.main(["export-locators", "--window", save(tmp_path, view),
                          "--output", str(target)]) == 0
    output = json.loads(target.read_text())
    summary = json.loads(capsys.readouterr().out)
    assert_context(output, view)
    assert_context(summary, view)
    assert output["write_performed"] is False and output["rights_verified"] is False
    assert summary["source_locator_candidates"] == len(view["observations"])
    assert len(output["items"]) == len(view["observations"])
    for item in output["items"]:
        source = SourceInput.from_dict(item)
        assert source.custody == "external" and source.text is None and source.published_at is None


def test_merge_summary_keeps_full_merged_health_and_identity(tmp_path, capsys):
    first = window()
    second = combine([relay_window(relay(), "news", "cls", observed_at=NOW, request={})])
    target = tmp_path / "merged.json"
    assert news_cli.main(["merge", "--windows", save(tmp_path, first),
                          save(tmp_path, second, "relay.json"), "--output", str(target)]) == 0
    merged = verify_reading(json.loads(target.read_bytes()))
    assert merged == merge_readings([first, second])
    summary = json.loads(capsys.readouterr().out)
    assert_context(summary, merged)
    assert summary["observations"] == 3 and summary["source_windows"] == 2


@pytest.mark.parametrize("action", ["read", "export-locators", "merge"])
def test_tampered_input_still_rejected_before_output(tmp_path, capsys, action):
    view = window()
    view["source_outcomes"][0]["status"] = "unavailable"
    path = save(tmp_path, view)
    target = tmp_path / "must-not-exist.json"
    flag = "--windows" if action == "merge" else "--window"
    args = [action, flag, path]
    if action != "read":
        args += ["--output", str(target)]
    assert news_cli.main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and json.loads(captured.err)["status"] == "failed"
    assert not target.exists()
