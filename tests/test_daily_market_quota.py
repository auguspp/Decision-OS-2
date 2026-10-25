"""One original daily run through bounded native Git/API; all responses are synthetic."""
from collections import Counter
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import base64
import hashlib
import json
import re
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import encoded
from decision_kernel.infrastructure import daily_execution as execution
from decision_kernel.infrastructure.github_store import GitHubAPI, MAX_REQUESTS
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.infrastructure.runtime_publication import ANCHOR, STORE_ID
from decision_kernel.interfaces import daily_market_cli as cli
from test_daily_market import Host, QuotaClock, quota_headers
from test_ftshare_collection_runtime import NativeAPI
from test_github_transport import Response
from test_market_snapshot import local_fetch, native


def test_native_1000_core_and_graphql_complete_two_calendars_twelve_prices_in_one_run(
        native, monkeypatch, tmp_path, record_property):
    model, _ = native
    model.binding = replace(model.binding, repository=cli.REPO, repository_id=1420041744)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.binding, model.git.repository = model.binding, model.binding.repository
    model.meta.update(id=1420041744, full_name=cli.REPO)
    local_fresh = lambda: VersionedStore(copy(model.git))
    host = Host(storage=(model.git, VersionedStore(model.git), local_fresh), count=6)
    host.approved.update(symbols=sorted(host.fixture["context"]["eligible_symbols"])[:6], max_price_requests=12)
    assert len(host.approved["symbols"]) == 6
    # The complete native/Git/source model uses explicitly synthetic time. It
    # proves same-run quota accounting, never an actual future scheduled run.
    admitted = host.now = "2026-10-20T10:35:00+00:00"
    clock = QuotaClock(datetime.fromisoformat(admitted).timestamp())
    native_model = NativeAPI()
    native_model.run.update(path=cli.WORKFLOW, name=cli.TITLE, created_at=admitted)
    native_model.ci.update(total_count=1)
    native_model.ci["workflow_runs"][0].update(name="decision-os-ci", run_attempt=1,
        repository={"id": 1420041744}, head_repository={"id": 1420041744})
    workflow_sha, execution_sha, execution_tree = "a" * 40, "b" * 40, "c" * 40
    requested = {"operation": "update", "code_sha": workflow_sha,
        "run_id": 8001, "run_attempt": 1, "event": "workflow_dispatch"}
    environment = {"GITHUB_REPOSITORY": cli.REPO, "GITHUB_REPOSITORY_ID": "1420041744",
        "GITHUB_REF": "refs/heads/main", "GITHUB_WORKFLOW_REF": cli.REPO + "/" + cli.WORKFLOW + "@refs/heads/main",
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "8001", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_SHA": workflow_sha, "GITHUB_WORKFLOW_SHA": workflow_sha}
    approved_version = {"schema": execution.POLICY_SCHEMA, "enabled": True, "mode": "pinned",
        "pinned": {"code_sha": execution_sha, "tree_sha": execution_tree, "main_ci_run": 6002,
            "approval_ref": "https://github.com/auguspp/Decision-OS-2/issues/221#issuecomment-1"}}
    publication = {"schema": "public-runtime-authorization-v1", "enabled": False,
        "repository": cli.REPO, "repository_id": 1420041744, "branch": "records", "anchor": ANCHOR,
        "store_id": STORE_ID, "authorization_ref": "https://github.com/auguspp/Decision-OS-2/issues/164#issuecomment-1",
        "scopes": []}
    # Code is a separate synthetic native model. The real local records Git,
    # private binding, original source rights and budgets remain unchanged.
    code_documents = {(execution_sha, execution.INTERFACE_PATH): encoded(execution.INTERFACE)}
    main_heads = []

    def advance_main():
        sha = hashlib.sha1(("synthetic-daily-main-" + str(len(main_heads))).encode("ascii")).hexdigest()
        main_heads.append(sha)
        native_model.main["object"]["sha"] = sha
        code_documents[sha, execution.POLICY_PATH] = encoded(approved_version)
        code_documents[sha, execution.PUBLIC_PATH] = encoded(publication)
        return sha

    code_commit_path = cli.PREFIX + "/git/commits/" + execution_sha
    counts, apis, fetches, progress, trace = {}, [], [], [], []
    immutable_responses, cache_hits = {}, Counter()
    blob_sizes, size_queries, native_size_queries = {}, Counter(), Counter()
    immutable_path = re.compile(re.escape(cli.PREFIX) +
        r"/git/(?:(?:commits|blobs)/[a-f0-9]{40}|trees/[a-f0-9]{40}(?:\?recursive=1)?)")

    def records_object(path):
        return path != code_commit_path and immutable_path.fullmatch(path) is not None

    class Wire:
        def __init__(self, unit):
            self.unit = unit

        def open(self, request, timeout):
            window = int(clock.at // 3600)
            reset = (window + 1) * 3600
            totals = counts.setdefault(window, Counter())
            resource = "graphql" if request.method == "POST" else "core"
            totals[resource] += 1
            path = request.full_url.removeprefix("https://api.github.com")
            trace.append({"window": window, "resource": resource, "unit": self.unit,
                "method": request.method, "path": path})
            headers = quota_headers(resource=resource, remaining=max(0, 1000 - totals[resource]), reset=reset)
            if totals[resource] > 1000:
                raise HTTPError(request.full_url, 429, "synthetic quota exhausted", headers, None)
            payload = json.loads(request.data) if request.data else None
            cacheable = request.method == "GET" and records_object(path)
            if cacheable and path in immutable_responses:
                # Only the synthetic server reuses the exact immutable object
                # body. Every wire call above still consumes quota and gets
                # current headers; all mutable/native/CAS paths remain live.
                model.requests.append((request.method, path, deepcopy(payload)))
                cache_hits[path] += 1
                return Response(immutable_responses[path], request.full_url, headers=headers)
            if path.endswith("/git/ref/heads/main"):
                value = deepcopy(native_model.main)
            elif path.startswith(cli.PREFIX + "/contents/"):
                parsed = urlsplit(path)
                file = parsed.path.removeprefix(cli.PREFIX + "/contents/")
                query = parse_qs(parsed.query)
                assert set(query) == {"ref"} and len(query["ref"]) == 1
                raw = code_documents[query["ref"][0], file]
                blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
                value = {"type": "file", "path": file, "encoding": "base64", "size": len(raw),
                    "content": base64.b64encode(raw).decode("ascii"), "sha": blob}
            elif path.startswith(cli.PREFIX + "/compare/") and path.split("/compare/", 1)[1][:40] in {
                    workflow_sha, execution_sha}:
                parsed = urlsplit(path)
                base, head = parsed.path.rsplit("/", 1)[1].split("...")
                assert head == native_model.main["object"]["sha"] and parse_qs(parsed.query) == {"per_page": ["1"]}
                value = {"status": "ahead", "merge_base_commit": {"sha": base}}
            elif path == code_commit_path:
                value = {"sha": execution_sha, "tree": {"sha": execution_tree}}
            elif path.endswith("/actions/runs/8001"):
                value = deepcopy(native_model.run)
            elif "/actions/runs?status=in_progress&" in path:
                value = {"total_count": 1, "workflow_runs": [deepcopy(native_model.run)]}
            elif "/actions/workflows/ci.yml/runs?" in path:
                value = deepcopy(native_model.ci)
                query = parse_qs(urlsplit(path).query)
                code = query["head_sha"][0]
                assert query == {"branch": ["main"], "event": ["push"], "head_sha": [code], "per_page": ["5"]}
                assert code in {workflow_sha, execution_sha}
                value["workflow_runs"][0].update(head_sha=code, id=6001 if code == workflow_sha else 6002)
            elif cacheable and path.startswith(cli.PREFIX + "/git/trees/"):
                # Keep the original server's native tree expansion. Blob size
                # belongs to its exact object SHA, so later trees can reuse the
                # first successful native size without another child process.
                model.requests.append((request.method, path, deepcopy(payload)))
                sha = path.rsplit("/", 1)[1].split("?", 1)[0]
                rows = []
                for raw in model.git._run("ls-tree", "-r", "-z", sha).stdout.split(b"\0"):
                    if not raw:
                        continue
                    meta, name = raw.split(b"\t")
                    mode, kind, blob = meta.decode().split()
                    if kind == "blob" and re.fullmatch(r"[a-f0-9]{40}", blob):
                        size_queries[blob] += 1
                        if blob not in blob_sizes:
                            blob_sizes[blob] = int(model.git._run("cat-file", "-s", blob).stdout)
                            native_size_queries[blob] += 1
                        size = blob_sizes[blob]
                    else:
                        size = int(model.git._run("cat-file", "-s", blob).stdout)
                    rows.append(dict(path=name.decode(), mode=mode, type=kind, sha=blob, size=size))
                value = dict(sha=sha, truncated=False, tree=rows)
                if model.corrupt_response:
                    value = model.corrupt_response(path, value)
            else:
                value = model.request(request.method, path, payload)
            raw = encoded(value)
            if cacheable:
                immutable_responses[path] = raw
            return Response(raw, request.full_url, headers=headers)

    class BudgetAPI(GitHubAPI):
        def __init__(self, token):
            super().__init__(token)
            # Each new unit sees a distinct main head, forcing the actual live
            # guard's six cold GETs; the approved execution stays pinned.
            self.main_sha = advance_main()
            self._opener = Wire(len(apis))
            apis.append(self)

    def sleep(seconds):
        clock.sleep(seconds)
        host.now = datetime.fromtimestamp(clock.at, timezone.utc).isoformat()

    monkeypatch.setattr(cli, "GitHubAPI", BudgetAPI)
    monkeypatch.setattr(cli, "_records_runtime_binding", lambda operation:
        replace(model.binding, write_enabled=operation == "sync"))
    monkeypatch.setattr(cli, "GitMarketReader", lambda binding, api:
        GitMarketReader(binding, api, fetch=local_fetch(model, fetches)))
    startup = BudgetAPI("synthetic-native-daily-only")
    selected = execution.select_execution(startup, requested, env=environment)
    frozen = deepcopy(selected)
    assert selected["workflow_sha"] == workflow_sha != selected["code_sha"] == execution_sha
    assert selected["runtime_version"] == {"schema": execution.SELECTION_SCHEMA, "mode": "pinned",
        "code_tree": execution_tree, "policy_commit": main_heads[0],
        "policy_sha256": hashlib.sha256(encoded(approved_version)).hexdigest(), "protocol": execution.PROTOCOL,
        "workflow_ci_run": 6001, "execution_ci_run": 6002,
        "approval_ref": approved_version["pinned"]["approval_ref"]}
    cli.native_ready(startup, selected)
    opened = cli.remote_units("synthetic-native-daily-only", selected, clock=lambda: clock.at,
        monotonic=lambda: clock.elapsed, sleep=sleep)
    def report(receipt, control):
        row = {"calendars": receipt["calendar_source_requests"], "prices": receipt["price_source_requests"],
            "waits": receipt["store_quota_wait"]["refresh_waits"], "status": receipt["status"]}
        if not progress or row != progress[-1]:
            progress.append(row)
    options = dict(open_unit=opened, clock=lambda: host.now, admission_at=admitted,
        # 与生产装配一致：单元与只读只核权限，并发核验只在发上游请求前一刻。
        guard=lambda api: cli.check_private(api, selected),
        read_guard=lambda api: cli.check_private(api, selected),
        probe=lambda api: cli.collection_guard(api, selected), capacity=cli.market_capacity,
        monotonic=lambda: clock.elapsed, sleep=sleep, progress=report)
    first = host.run(**options)
    # Match the runtime's final native release check, which has its own API
    # instance but consumes the same server resource allowance.
    cli.check_private(startup, selected)
    receipt = first["receipt"]
    immutable_requests = Counter(row["path"] for row in trace
        if row["method"] == "GET" and records_object(row["path"]))
    model_immutable_requests = Counter(path for method, path, _ in model.requests
        if method == "GET" and records_object(path))
    assert immutable_requests == model_immutable_requests
    assert set(immutable_responses) == set(immutable_requests)
    assert all(immutable_requests[path] == cache_hits[path] + 1 for path in immutable_requests)
    assert all(any("/git/" + kind + "/" in path and count > 0 for path, count in cache_hits.items())
        for kind in ("commits", "trees", "blobs"))
    assert len(trace) == sum(api._requests for api in apis) == sum(sum(row.values()) for row in counts.values())
    assert native_size_queries == Counter({sha: 1 for sha in blob_sizes})
    assert set(size_queries) == set(blob_sizes) and sum(size_queries.values()) > len(blob_sizes)
    assert selected == frozen and len(set(main_heads)) == len(apis) > 1
    native_static = [row for row in trace if "/actions/workflows/ci.yml/runs?" in row["path"]
        or row["path"] == code_commit_path or execution.INTERFACE_PATH in row["path"]]
    assert len(native_static) == 4 and {row["unit"] for row in native_static} == {0}
    for unit, api in enumerate(apis[1:], start=1):
        # Every unit admits the frozen identity through the actual live guard;
        # new units cannot reuse static CI or a different head's authorization.
        rows = [row for row in trace if row["unit"] == unit]
        assert [row["path"] for row in rows[:7]] == [cli.PREFIX,
            cli.PREFIX + "/git/ref/heads/main",
            cli.PREFIX + "/contents/" + execution.POLICY_PATH + "?ref=" + api.main_sha,
            cli.PREFIX + "/contents/" + execution.PUBLIC_PATH + "?ref=" + api.main_sha,
            cli.PREFIX + "/compare/" + workflow_sha + "..." + api.main_sha + "?per_page=1",
            cli.PREFIX + "/compare/" + execution_sha + "..." + api.main_sha + "?per_page=1",
            cli.PREFIX + "/actions/runs/8001"]
    posts = [index for index, row in enumerate(trace) if row["method"] == "POST"]
    assert len(posts) == model.writes > 0
    for index in posts:
        # A real GitHubStore CAS callback pays its own live repository/head
        # check immediately before the POST, even after all source/store reads.
        assert [row["path"] for row in trace[index - 2:index]] == [
            cli.PREFIX, cli.PREFIX + "/git/ref/heads/main"]
        assert all(row["method"] == "GET" and row["unit"] == trace[index]["unit"]
            for row in trace[index - 2:index])
    # An independent native batch read verifies every reused size and object
    # type once, without changing any client/server wire or quota accounting.
    batch = model.git._run("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        data=("\n".join(sorted(blob_sizes)) + "\n").encode("ascii")).stdout
    verified_sizes = {}
    for row in batch.decode("ascii").splitlines():
        sha, kind, size = row.split()
        assert kind == "blob" and sha not in verified_sizes
        verified_sizes[sha] = int(size)
    assert verified_sizes == blob_sizes
    evidence = {"schema": "synthetic-daily-quota-windows-v1", "live_source_requests": 0,
        "live_store_writes": 0, "per_resource_hourly_limit": 1000,
        "windows": [{"window": key, **value} for key, value in sorted(counts.items())],
        "synthetic_immutable_response_cache": {"paths": len(immutable_responses),
            "hits": sum(cache_hits.values()), "wire_requests": sum(immutable_requests.values()),
            "blob_size_queries": sum(size_queries.values()), "native_blob_size_queries": sum(native_size_queries.values()),
            "blob_size_cache_hits": sum(size_queries.values()) - sum(native_size_queries.values()),
            "independent_native_sizes_verified": len(verified_sizes)},
        "max_unit_requests": max(api._requests for api in apis), "progress": progress,
        "runtime_selection": frozen, "distinct_live_main_heads": len(main_heads),
        "static_admission_requests": len(native_static), "cas_live_guards": len(posts),
        "core_remaining_before_reading": 1000 - counts[int(clock.at // 3600)]["core"],
        "result": first}
    (tmp_path / "daily-quota-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    (tmp_path / "daily-quota-trace.json").write_text(json.dumps(trace, indent=2) + "\n")
    record_property("daily_quota_windows", json.dumps(evidence["windows"]))
    assert receipt["status"] == "complete", evidence
    assert (receipt["calendar_source_requests"], receipt["price_source_requests"], receipt["source_requests"]) == (2, 12, 14)
    assert receipt["cycle_status"] == "complete" and receipt["cycle_date"] == "20261020"
    assert len(host.calendar_clients) == len(host.price_clients) == 1
    assert selected == frozen
    assert host.calendar_clients[0].requests == 2 and host.price_clients[0].requests == 12
    assert receipt["store_quota_wait"]["refresh_waits"] == 5 and len(counts) == 6
    assert receipt["store_quota_wait"]["waited_seconds"] <= 5 * 3600
    assert clock.chunks and max(clock.chunks) <= 60
    assert max(api._requests for api in apis) <= MAX_REQUESTS
    assert all(api.rate_limits is apis[1].rate_limits for api in apis[1:])
    assert all(value["core"] <= 1000 and value["graphql"] <= 1000 for value in counts.values())
    assert evidence["core_remaining_before_reading"] >= 260
    assert any(row["calendars"] == 2 and row["prices"] == 0 and row["waits"] == 1 for row in progress)
    assert any(row["prices"] == 12 and row["waits"] == 5 for row in progress)
    assert host.now[:10] == admitted[:10]  # The model finishes in the same local evening.
    assert all(not call["destination"].exists() for call in fetches)

    before = model.git.head()
    again = host.run(**options)
    assert again["receipt"]["status"] == "already-complete", again
    assert again["receipt"]["source_requests"] == 0 and model.git.head() == before
    assert len(host.calendar_clients) == len(host.price_clients) == 1
    assert len(trace) == sum(api._requests for api in apis) == sum(sum(row.values()) for row in counts.values())
