"""Safe title diagnostics on the existing capture and normal reading path."""
import json
from hashlib import sha256

import pytest

from decision_kernel.domain.records import encoded
from decision_kernel.infrastructure.news_capture import _shape, capture_newsnow
from decision_kernel.interfaces import news_cli
from decision_kernel.observations.news_windows import verify_reading


def wire(source="wallstreetcn", title="合成标题", **extra):
    domain = {"wallstreetcn": "wallstreetcn.com", "thepaper": "thepaper.cn", "cls": "cls.cn"}[source]
    return json.dumps({"id": source, "status": "success", "items": [
        {"id": "synthetic", "url": "https://www." + domain + "/synthetic", "title": title}],
        **extra}, ensure_ascii=True).encode()


def test_profile_measures_controls_without_echoing_title_or_extra_fields():
    raw = wire(title="保密合成\n标题\t\r\x7f", error="DO_NOT_ECHO", token="DO_NOT_ECHO")
    result = _shape(raw)
    profile = result["headline_profile"]
    assert profile["rows"] == 1 and profile["titles_with_controls"] == 1
    assert profile["control_codepoints"] == {"U+000A": 1, "U+0009": 1, "U+000D": 1, "U+007F": 1}
    assert profile["max_characters"] == len("保密合成\n标题\t\r\x7f")
    assert profile["max_utf8_bytes"] == len("保密合成\n标题\t\r\x7f".encode())
    assert "保密" not in encoded(result).decode() and "DO_NOT_ECHO" not in encoded(result).decode()
    assert "synthetic" not in encoded(result).decode()


@pytest.mark.parametrize("title", [None, 0, True, [], {}])
def test_non_string_title_diagnostic_is_numeric_only(title):
    profile = _shape(wire(title=title))["headline_profile"]
    assert profile["non_string_titles"] == 1 and profile["max_characters"] == 0
    assert profile["control_codepoints"] == {} and profile["invalid_utf8_titles"] == 0


@pytest.mark.parametrize("title", ["", " \t\n", "\u3000"])
def test_blank_title_measurements_do_not_rewrite_source(title):
    profile = _shape(wire(title=title))["headline_profile"]
    assert profile["blank_titles"] == 1 and profile["max_stripped_characters"] == 0
    assert profile["max_characters"] == len(title)


def test_lengths_are_measured_not_clipped_and_bad_unicode_is_safe():
    title = "合" * 4097
    profile = _shape(wire(title=title))["headline_profile"]
    assert profile["max_characters"] == 4097 and profile["max_utf8_bytes"] == 12291
    profile = _shape(wire(title="\ud800"))["headline_profile"]
    assert profile["invalid_utf8_titles"] == 1 and profile["max_utf8_bytes"] == 0
    assert profile["max_characters"] == 1


def test_profile_stays_bounded_and_does_not_inspect_relay_cells():
    assert "headline_profile" not in _shape(json.dumps({"items": [{}] * 31}).encode())
    assert _shape(json.dumps({"data": {"fields": ["title"], "items": [["DO_NOT_ECHO"]]}}).encode()) == {
        "object": True, "top_field_count": 1, "field_count": 1, "item_count": 1}
    assert _shape(None) == {} and _shape(b"not json") == {"valid_json": False}
    assert _shape(b"[]") == {"object": False}
    profile = _shape(b'{"items":[null,{}, {"title":"ok"}]}')["headline_profile"]
    assert profile["non_object_rows"] == 1 and profile["non_string_titles"] == 1


def test_every_forbidden_control_is_reported_as_a_fixed_codepoint():
    controls = "".join(chr(n) for n in range(32)) + chr(127)
    profile = _shape(wire(title="synthetic" + controls))["headline_profile"]
    assert profile["control_codepoints"] == {f"U+{n:04X}": 1 for n in [*range(32), 127]}
    assert profile["titles_with_controls"] == 1


def test_failed_capture_still_rejects_and_normal_zero_read_retains_diagnostics(tmp_path, capsys):
    calls = []
    bodies = {"wallstreetcn": wire(title="合成\x00标题"), "thepaper": wire("thepaper", title="")}
    def fetch(source):
        calls.append(source)
        return bodies[source]
    result = capture_newsnow(sources=["wallstreetcn", "thepaper"], fetcher=fetch,
                             observed_at="2026-10-16T07:00:00Z")
    assert calls == ["wallstreetcn", "thepaper"] and result["observations"] == []
    assert verify_reading(result) == result
    for item in result["source_outcomes"]:
        receipt = item["capture"]
        assert item["status"] == "unavailable" and receipt["error_code"] == "NEWS_HEADLINE_INVALID"
        assert receipt["response_sha256"] == sha256(bodies[item["source"]]).hexdigest()
        assert receipt["source_body_retained"] is False
    path = tmp_path / "window.json"
    path.write_bytes(encoded(result))
    assert news_cli.main(["read", "--window", str(path), "--source", "cls"]) == 0
    page = json.loads(capsys.readouterr().out)
    assert page["source_outcomes"] == result["source_outcomes"]
    assert page["source_outcomes_scope"] == "whole-input-reading"


def test_valid_peer_and_transport_failure_are_not_reclassified():
    def fetch(source):
        if source == "thepaper":
            raise OSError("DO_NOT_ECHO")
        return wire("cls")
    result = capture_newsnow(sources=["cls", "thepaper"], fetcher=fetch,
                             observed_at="2026-10-16T07:00:00Z")
    assert len(result["observations"]) == 1
    good, bad = result["source_outcomes"]
    assert good["status"] == "captured" and "safe_structure" not in good["capture"]
    assert bad["capture"]["safe_structure"] == {}
    assert bad["capture"]["error_code"] == "NEWS_TRANSPORT_UNAVAILABLE"
    assert "DO_NOT_ECHO" not in encoded(result).decode()
