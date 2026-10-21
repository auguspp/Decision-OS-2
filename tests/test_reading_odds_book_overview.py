"""Normal Room/host and exact detail consumers of the bounded Book overview."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application import odds_book, reading_room
from decision_kernel.application.conditional_odds import ConditionalOdds, RESULT_RECORD
from decision_kernel.application.reading_room import ReadingRoom, plan_input
from decision_kernel.domain.records import RecordError, RevisionInput, StoreUnavailable, encoded
from decision_kernel.infrastructure.github_store import MAX_REQUESTS
from decision_kernel.interfaces import odds_commands, reading_commands, reading_page
from test_conditional_odds import context
from test_hosted_watch_runtime import public_host
from test_news_read_budget import transport
from test_odds_book import saved_pair
from test_odds_book_host import seed
from test_reading_odds_book import AT, html_model
from test_reading_odds_book_host import run, failed
from test_research_runtime import host


def request(query=None, *, page_limit=4, detail=None, **changes):
    return {"plan": {"as_of": AT, "odds_book_view": {
        "query": {"limit": 2} if query is None else query,
        "page_limit": page_limit, "detail": detail}, **changes}}


@pytest.mark.parametrize("value", [
    {"pages": []}, {"page_limit": True}, {"page_limit": 5},
    {"query": {"pages": []}}, {"detail": []},
    {"detail": {"operation": "save-odds", "payload": {}}},
    {"detail": {"operation": "calculate-odds", "payload": {}}},
])
def test_preflight_rejects_imported_receipts_or_side_effect_selectors_before_read(context, monkeypatch, value):
    store, _ = context
    monkeypatch.setattr(ReadingRoom, "_catalogue", lambda *a, **k: pytest.fail("read before preflight"))
    with pytest.raises(RecordError):
        reading_commands.perform(store, {"plan": {"as_of": AT, "odds_book_view": value}}, public=False)


def test_page_and_overview_are_mutually_exclusive_and_null_remains_no_read(context, monkeypatch):
    store, _ = context
    with pytest.raises(RecordError, match="ROOM_ODDS_BOOK_VIEW_CONFLICT"):
        plan_input({"as_of": AT, "odds_book": {}, "odds_book_view": {}})
    monkeypatch.setattr(odds_book, "read_book", lambda *a, **k: pytest.fail("null requested Book"))
    base = reading_commands.perform(store, {"plan": {"as_of": AT}}, public=False)
    omitted = reading_commands.perform(store, {"plan": {"as_of": AT, "odds_book_view": None}}, public=False)
    assert omitted == base and "odds_book_view" not in omitted


def test_normal_room_preserves_multi_page_originals_and_opens_only_explicit_exact_result(context, monkeypatch):
    store, research, spec, alias, first, second = saved_pair(context)
    newer = store.submit_revision("overview-next", RevisionInput("case", "r2", "full", "Later Full body",
        "Same commission", "synthetic", predecessor=research, unresolved=("Still unresolved",)))
    store.submit_revision("overview-other", RevisionInput("other", "r1", "full", "Separate Full body",
        "Other commission", "synthetic", unresolved=("No spec",)))
    before = store.backend.head()
    calls, perform = [], odds_commands.perform
    def observed(reader, operation, payload, *args, **kwargs):
        assert reader.backend is store.backend
        calls.append((operation, deepcopy(payload)))
        return perform(reader, operation, payload, *args, **kwargs)
    monkeypatch.setattr(odds_commands, "perform", observed)
    value = request(detail={"operation": "read-odds", "payload": {"reference": first}}, reference=asdict(research))
    result = reading_commands.perform(store, value, public=False)
    panel, view = result["odds_book_view"], result["odds_book_view"]["view"]
    assert calls == [("read-odds", {"reference": first})]
    assert len(view["pages"]) == 3 and view["scan"]["complete_from_start"] and view["content_complete"]
    assert view["snapshot"] == result["catalogue"]["snapshot"] == before
    rows = [row for page in view["pages"] for row in page["items"]]
    prices = [row for row in rows if row["kind"] == RESULT_RECORD]
    assert {row["recorded"]["price"]["price"] for row in prices} == {"80", "100"}
    assert all(row["research"]["at_snapshot"]["reference"] == asdict(newer) for row in prices)
    assert panel["detail"]["status"] == "readable" and panel["detail"]["result"]["recalculation"] == "matches"
    assert panel["detail"]["result"]["result"]["price"]["price"] == "100"
    assert panel["detail"]["payload"]["reference"] == first
    assert html_model(reading_page.render_page(result)) == result and store.backend.head() == before
    assert any(reading.get("body") == "Later Full body" for reading in result["readings"])


def test_next_request_is_actually_consumable_and_refresh_releases_both_cursors(context):
    store, research, *_ = saved_pair(context)
    initial = reading_commands.perform(store, request({"limit": 1}, page_limit=1), public=False)
    source = initial["odds_book_view"]
    store.submit_revision("overview-later", RevisionInput("later", "r1", "quick", "Later body", "New", "synthetic",
        unresolved=("Synthetic only",)))
    following = reading_commands.perform(store, source["next_request"]["payload"], public=False)
    view = following["odds_book_view"]["view"]
    assert following["catalogue"] == initial["catalogue"]
    assert view["scan"]["start"] == source["view"]["scan"]["end"]
    assert view["scan"]["unread_prefix"] > 0 and not view["scan"]["complete_from_start"]
    assert following["plan"]["odds_book_view"]["detail"] is None
    refresh = following["refresh_plan"]
    assert "cursor" not in refresh["catalogue"]
    assert set(refresh["odds_book_view"]["query"]) == {"limit", "scan_limit", "research_ref"}
    current = reading_commands.perform(store, {"plan": refresh}, public=False)
    assert current["catalogue"]["snapshot"] == current["odds_book_view"]["view"]["snapshot"] == store.backend.head()


def test_detail_not_in_actual_read_range_is_explicit_and_never_dispatched(context, monkeypatch):
    store, _, spec, *_ = saved_pair(context)
    initial = reading_commands.perform(store, request({"limit": 1}, page_limit=1), public=False)
    assert all(row["open_payload"] != {"reference": spec} for page in initial["odds_book_view"]["view"]["pages"] for row in page["items"])
    value = request({"limit": 1}, page_limit=1, detail={"operation": "read-valuation", "payload": {"reference": spec}})
    monkeypatch.setattr(odds_commands, "perform", lambda *a, **k: pytest.fail("unseen detail dispatched"))
    result = reading_commands.perform(store, value, public=False)
    assert result["odds_book_view"]["detail"]["error"] == "ROOM_ODDS_BOOK_DETAIL_NOT_IN_VIEW"
    assert result["odds_book_view"]["status"] == result["status"] == "partial"


def test_oversize_additional_overview_keeps_original_body_and_real_range(context, monkeypatch):
    store, research, *_ = saved_pair(context)
    before = store.backend.head()
    monkeypatch.setattr(reading_room, "MAX_PANEL_BYTES", 1800)
    result = reading_commands.perform(store, request(reference=asdict(research)), public=False)
    panel = result["odds_book_view"]
    assert panel["status"] == "not-included" and panel["scan"]["complete_from_start"]
    assert panel["counts"]["returned_items"] == 4
    assert panel["open_request"]["operation"] == "read-odds-book"
    assert any(reading.get("body") for reading in result["readings"])
    html = reading_page.render_page(result)
    assert html_model(html) == result and "function ovRenderGroupDetail" not in html
    assert store.backend.head() == before


def test_normal_public_host_defers_all_outputs_and_shares_original_api_for_pages_and_detail(public_host, tmp_path, monkeypatch):
    model, _, _ = public_host
    _, _, result_ref = seed(model)
    api, output = transport(model), tmp_path / "overview"
    original, scopes = odds_book.read_book, []
    def page(store, *args, **kwargs):
        assert store.backend.api is api and not (output / "inputs.json").exists()
        assert not (output / "reading-room.html").exists()
        scopes.append(store.backend)
        return original(store, *args, **kwargs)
    monkeypatch.setattr(odds_book, "read_book", page)
    before, writes = model.git.head(), model.writes
    value = request(detail={"operation": "read-odds", "payload": {"reference": result_ref["reference"]}})
    report = run(model, api, output, value)
    assert report["status"] == "completed" and report["write"] == "not-requested"
    assert len(scopes) == 2 and scopes[0] is scopes[1] and scopes[0].active is False
    assert report["result"]["odds_book_view"]["detail"]["status"] == "readable"
    assert html_model((output / "reading-room.html").read_text()) == report["result"]
    assert json.loads((output / "inputs.json").read_bytes())["operation"] == "reading-room"
    assert 0 < api._requests == len(model.requests) < MAX_REQUESTS == 512
    assert all(method == "GET" for method, _, _ in model.requests)
    assert model.git.head() == before and model.writes == writes


def test_new_overview_requires_composite_scope_before_any_room_read(public_host, tmp_path, monkeypatch):
    model, grant, path = public_host
    grant["scopes"].remove("market");path.write_bytes(encoded(grant))
    monkeypatch.setattr(ReadingRoom, "build", lambda *a, **k: pytest.fail("Room read before composite admission"))
    output = tmp_path / "denied"
    with pytest.raises(RecordError, match="ODDS_BOOK_PUBLICATION_NOT_CONFIRMED"):
        run(model, transport(model), output, request())
    assert not output.exists()


@pytest.mark.parametrize("phase", ["second-page", "detail"])
def test_late_original_budget_failure_releases_no_earlier_page_or_input(public_host, tmp_path, monkeypatch, phase):
    model, _, _ = public_host
    _, _, result_ref = seed(model)
    api, output = transport(model), tmp_path / phase
    original, count = odds_book.read_book, 0
    def page(*args, **kwargs):
        nonlocal count
        count += 1
        if phase == "second-page" and count == 2:
            api._requests = MAX_REQUESTS
        return original(*args, **kwargs)
    monkeypatch.setattr(odds_book, "read_book", page)
    perform = odds_commands.perform
    def detail(*args, **kwargs):
        if phase == "detail":
            api._requests = MAX_REQUESTS
        return perform(*args, **kwargs)
    monkeypatch.setattr(odds_commands, "perform", detail)
    value = request(detail={"operation": "read-odds", "payload": {"reference": result_ref["reference"]}})
    with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
        run(model, api, output, value)
    assert count == 2 and api._requests == MAX_REQUESTS + 1
    failed(output)


def test_revocation_after_final_html_render_releases_no_overview(public_host, tmp_path, monkeypatch):
    from decision_kernel.interfaces import research_runtime as runtime
    model, grant, path = public_host
    seed(model)
    original, render = runtime._perform, reading_page.render_page
    complete = False
    def perform(*args, **kwargs):
        nonlocal complete
        result = original(*args, **kwargs);complete = True;return result
    def revoke(*args, **kwargs):
        html = render(*args, **kwargs)
        if complete:
            grant["scopes"].remove("market");path.write_bytes(encoded(grant))
        return html
    monkeypatch.setattr(runtime, "_perform", perform)
    monkeypatch.setattr(reading_page, "render_page", revoke)
    output = tmp_path / "revoked"
    with pytest.raises(RecordError, match="ODDS_BOOK_PUBLICATION_NOT_CONFIRMED"):
        run(model, transport(model), output, request())
    failed(output)


def exercise_installed_overview(command, outside, research):
    """Use the original cold wheel/store and independent normal CLI processes."""
    path = outside / "installed-overview-input.json"
    value = request({"research_ref": research, "limit": 4}, page_limit=2, reference=research)["plan"]
    path.write_bytes(encoded(value))
    output = outside / "installed-overview.html"
    result = command("research-room", "--input", str(path), "--out", str(output))
    panel = result["odds_book_view"]
    assert panel["view"]["schema"] == "odds-book-overview-v1"
    assert html_model(output.read_text()) == result
    rows = [row for page in panel["view"]["pages"] for row in page["items"] if row["status"] == "readable"]
    selected = next(row for row in rows if row["open_operation"] == "read-odds")
    plan = deepcopy(result["plan"])
    plan["odds_book_view"]["detail"] = {"operation": selected["open_operation"], "payload": selected["open_payload"]}
    path.write_bytes(encoded(plan))
    reopened = command("research-room", "--input", str(path), "--out", str(outside / "installed-overview-exact.html"))
    assert reopened["odds_book_view"]["detail"]["result"]["recalculation"] == "matches"
    assert reopened["catalogue"] == result["catalogue"]
