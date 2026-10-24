"""仅使用 fake API 验证来源采集在途核验；不联网、不取消任何运行。"""
from copy import deepcopy
import socket

import pytest

from decision_kernel.domain.records import Conflict, RecordError, StoreUnavailable
from decision_kernel.infrastructure import source_collection_guard as guard

SHA = "a" * 40
PATH = ".github/workflows/daily-market.yml"
CONTEXT = {"run_id": 41, "code_sha": SHA, "workflow_path": PATH}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("本测试禁止真实网络")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def run(ident=41, path=PATH, **changes):
    return {"id": ident, "path": path, "status": "in_progress",
            "run_attempt": 1, "head_sha": SHA, **changes}


def listing(rows, total=None):
    return {"total_count": len(rows) if total is None else total, "workflow_runs": rows}


class FakeAPI:
    def __init__(self, own=None, pages=None, final_own=None):
        self.own = run() if own is None else own
        self.final_own = final_own
        self.pages = [listing([run()])] if pages is None else pages
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        assert method == "GET"
        if path == guard.PREFIX + "/41":
            result = self.final_own if len(self.calls) > 1 and self.final_own is not None else self.own
        else:
            page = sum("?" in called_path for _, called_path in self.calls)
            assert path == guard.PREFIX + f"?status=in_progress&per_page=100&page={page}"
            assert 1 <= page <= len(self.pages)
            result = self.pages[page - 1]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


def detect(api, **changes):
    return guard.detect_peer_collections(api, **{**CONTEXT, **changes})


def test_fixed_allowlist():
    assert guard.SOURCE_COLLECTION_WORKFLOWS == frozenset(
        ".github/workflows/" + name for name in (
            "daily-market.yml", "ashare-data.yml", "market-source-check.yml",
            "relay-source-runtime.yml", "relay-capabilities.yml"))


@pytest.mark.parametrize("path", sorted(guard.SOURCE_COLLECTION_WORKFLOWS))
def test_all_source_entries_and_same_workflow_other_run_are_conflicts(path):
    api = FakeAPI(pages=[listing([run(), run(42, path)])])
    assert detect(api) == ({"id": 42, "path": path},)
    with pytest.raises(Conflict, match="SOURCE_COLLECTION_PEER_CONFLICT"):
        guard.require_no_peer_collection(FakeAPI(pages=api.pages), **CONTEXT)


@pytest.mark.parametrize("path", sorted(guard.SOURCE_COLLECTION_WORKFLOWS))
def test_each_allowed_own_workflow_is_accepted(path):
    api = FakeAPI(own=run(path=path), pages=[listing([run(path=path)])])
    assert detect(api, workflow_path=path) == ()


def test_only_own_id_is_excluded_and_other_workflows_do_not_conflict():
    api = FakeAPI(pages=[listing([run(), run(42, ".github/workflows/ci.yml"),
        run(43, ".github/workflows/research.yml"),
        run(44, ".github/workflows/not-daily-market.yml"),
        run(45, ".github/workflows/daily-market.yaml")])])
    assert detect(api, run_id="41") == ()
    assert len(api.calls) == 3
    assert all("queued" not in path for _, path in api.calls)
    assert guard.require_no_peer_collection(FakeAPI(pages=api.pages), **CONTEXT) is None


def test_second_page_is_checked_before_issuing_a_verdict():
    first = [run()] + [run(i, ".github/workflows/ci.yml") for i in range(100, 199)]
    api = FakeAPI(pages=[listing(first, 101), listing([run(42)], 101)])
    assert detect(api) == ({"id": 42, "path": PATH},)
    assert len(api.calls) == 4


def test_exact_page_boundary_needs_no_unbounded_extra_request():
    rows = [run()] + [run(i, ".github/workflows/ci.yml") for i in range(100, 199)]
    api = FakeAPI(pages=[listing(rows)])
    assert detect(api, max_pages=1) == ()
    assert len(api.calls) == 3


@pytest.mark.parametrize("page", [None, [], {}, {"workflow_runs": []},
    listing([], True), listing([], "1"), listing([], -1), listing([], 1001),
    listing([], 1), listing([run()], 0), listing([run()], 2),
    {"total_count": 1, "workflow_runs": None},
    {"total_count": 1, "workflow_runs": {}}, listing([run()] * 101, 101)])
def test_malformed_total_count_and_incomplete_pages_fail_closed(page):
    with pytest.raises(RecordError, match="SOURCE_COLLECTION_LIST_INCOMPLETE"):
        detect(FakeAPI(pages=[page]))


@pytest.mark.parametrize("second", [listing([], 101), listing([run(100)], 101),
    listing([run(42)], 102), listing([run(42), run(43)], 101)])
def test_missing_duplicate_changed_and_overfull_later_pages_fail_closed(second):
    first = [run()] + [run(i, ".github/workflows/ci.yml") for i in range(100, 199)]
    with pytest.raises(RecordError, match="SOURCE_COLLECTION_"):
        detect(FakeAPI(pages=[listing(first, 101), second]))


def test_hard_bound_reads_at_most_ten_full_pages():
    rows = [run()] + [run(i, ".github/workflows/ci.yml") for i in range(100, 1099)]
    pages = [listing(rows[offset:offset + 100], 1000) for offset in range(0, 1000, 100)]
    api = FakeAPI(pages=pages)
    assert detect(api) == ()
    assert len(api.calls) == 12


def test_caller_can_lower_but_not_exceed_the_hard_page_bound():
    api = FakeAPI(pages=[listing([run()], 101)])
    with pytest.raises(RecordError, match="LIST_INCOMPLETE"):
        detect(api, max_pages=1)
    assert len(api.calls) == 2


@pytest.mark.parametrize("changes", [
    {"max_pages": value} for value in (0, -1, True, "2", 11)
] + [{"run_id": value} for value in (None, True, 0, -1, "0", "041", "41/attempts/2")]
  + [{"code_sha": value} for value in (None, "", "A" * 40, "a" * 39)]
  + [{"workflow_path": value} for value in (None, [], ".github/workflows/ci.yml", PATH + "@main")])
def test_invalid_caller_context_is_rejected_before_api_calls(changes):
    api = FakeAPI()
    with pytest.raises(RecordError):
        detect(api, **changes)
    assert api.calls == []


@pytest.mark.parametrize("changes", [{"id": True}, {"id": "41"}, {"id": 42},
    {"status": "queued"}, {"status": "completed"}, {"status": "waiting"},
    {"run_attempt": 2}, {"run_attempt": True}, {"run_attempt": "1"},
    {"run_attempt": None}, {"head_sha": "b" * 40}, {"head_sha": None},
    {"path": ".github/workflows/ashare-data.yml"}, {"path": None}])
def test_own_run_must_match_native_running_first_attempt_sha_and_path(changes):
    api = FakeAPI(own=run(**changes))
    with pytest.raises(RecordError, match="NATIVE_RUN_INVALID"):
        detect(api)
    assert len(api.calls) == 1


@pytest.mark.parametrize("own", [[], "run", False, {}])
def test_malformed_own_response_fails_closed(own):
    with pytest.raises(RecordError, match="NATIVE_RUN_INVALID"):
        detect(FakeAPI(own=own))


@pytest.mark.parametrize("key", ["id", "status", "path", "run_attempt", "head_sha"])
def test_missing_own_necessary_fields_fail_closed(key):
    own = run()
    del own[key]
    with pytest.raises(RecordError, match="NATIVE_RUN_INVALID"):
        detect(FakeAPI(own=own))


@pytest.mark.parametrize("row", [None, [], {}, {"id": 42, "status": "in_progress"},
    run(True), run("42"), run(0), run(-1), run(42, status="queued"),
    run(42, status="completed"), run(42, status=None)])
def test_malformed_peers_fail_closed_even_if_other_rows_are_irrelevant(row):
    with pytest.raises(RecordError, match="PEER_"):
        detect(FakeAPI(pages=[listing([run(), row])]))


@pytest.mark.parametrize("path", [None, [], {}, "", "daily-market.yml", "/" + PATH,
    PATH + "@refs/heads/main", PATH + "/extra", PATH + "\n", PATH + "?ref=main",
    ".github/workflows/../daily-market.yml", ".github/workflows//daily-market.yml",
    ".github/workflows/%64aily-market.yml", PATH.replace("/", "\\"),
    ".github/workflows/ci.yml@refs/heads/main"])
def test_peer_path_is_strict_and_never_normalized_into_safety(path):
    with pytest.raises(RecordError, match="PEER_PATH_INVALID"):
        detect(FakeAPI(pages=[listing([run(), run(42, path)])]))


@pytest.mark.parametrize("rows", [[], [run(42, ".github/workflows/ci.yml")]])
def test_own_run_disappearing_between_reads_is_rejected(rows):
    with pytest.raises(RecordError, match="NATIVE_RUN_MISSING"):
        detect(FakeAPI(pages=[listing(rows)]))


@pytest.mark.parametrize("changes", [{"head_sha": "b" * 40}, {"run_attempt": 2},
    {"path": ".github/workflows/ci.yml"}])
def test_own_list_entry_cannot_contradict_own_get(changes):
    with pytest.raises(RecordError, match="NATIVE_RUN_INVALID"):
        detect(FakeAPI(pages=[listing([run(**changes)])]))


@pytest.mark.parametrize("stage", ["own", "first", "second"])
def test_api_exceptions_never_grant_permission_or_leak_provider_text(stage):
    error = RuntimeError("不可输出的服务端文本")
    api = FakeAPI()
    if stage == "own":
        api.own = error
    elif stage == "first":
        api.pages = [error]
    else:
        first = [run()] + [run(i, ".github/workflows/ci.yml") for i in range(100, 199)]
        api.pages = [listing(first, 101), error]
    with pytest.raises(StoreUnavailable) as caught:
        detect(api)
    assert str(caught.value) == "SOURCE_COLLECTION_API_UNAVAILABLE"
    assert caught.value.__suppress_context__ is True
