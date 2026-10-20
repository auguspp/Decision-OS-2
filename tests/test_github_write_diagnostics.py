"""Actual conditional-write checks, with synthetic wire faults and real Git.

Diagnostics report the failed verification only. They do not prove publication,
authorize another write, or reveal an exception/response containing private data.
"""
from copy import deepcopy
import base64
from http.client import IncompleteRead
import json
import traceback
from urllib.error import HTTPError, URLError

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, StoreUnavailable, UnknownWrite, encoded
from decision_kernel.infrastructure.github_store import (
    API_ROOT, MAX_REQUESTS, GitHubAPI, GitHubStore, write_failure_diagnostic,
)
from test_github_store import GitAPI, source
from test_github_transport import Response


TOTAL = "REMOTE_WRITE_REQUIRES_RECONCILIATION"
SECRET = "synthetic-private-token-and-server-prose"
NEW_PATH = "submissions/new.json"
OLD_PATH = "submissions/retained.json"
NEW_BYTES = b'{"new":true}\n'


def assert_diagnostic(error, *, phase, classification):
    assert type(error) is UnknownWrite and error.args == (TOTAL,)
    assert str(error) == TOTAL
    projected = write_failure_diagnostic(error)
    assert projected == {"schema": "github-write-diagnostic-v1", "phase": phase,
                         "classification": classification}
    assert SECRET not in encoded(projected).decode()
    assert SECRET not in repr(error) and SECRET not in repr(vars(error))
    assert SECRET not in "".join(traceback.format_exception(error))
    assert error.__suppress_context__ is True


@pytest.mark.parametrize("fault,phase,classification", [
    ("parent", "commit", "parent-mismatch"),
    ("write-set", "tree", "write-set-changed"),
    ("unrelated", "tree", "unrelated-change"),
    ("readback", "readback", "readback-mismatch"),
    ("hash", "readback", "blob-integrity"),
    ("tree", "binding", "tree-invalid"),
    ("binding", "binding", "binding-mismatch"),
])
def test_post_response_checks_still_reject_and_classify(tmp_path, fault, phase, classification):
    model = GitAPI(tmp_path / "diagnostic.git")
    model.git.commit({OLD_PATH: b'{"retained":true}\n'}, expected=model.git.head())
    backend = GitHubStore(model.binding, model)
    initial, request = backend.head(), model.request

    def alter_write(method, path, payload=None):
        if method == "POST" and fault in {"write-set", "unrelated", "readback"}:
            payload = deepcopy(payload)
            additions = payload["variables"]["input"]["fileChanges"]["additions"]
            if fault == "readback":
                additions[0]["contents"] = base64.b64encode(b'{"new":false}\n').decode()
            else:
                additions.append({"path": OLD_PATH if fault == "unrelated" else "submissions/extra.json",
                                  "contents": base64.b64encode(b'{"changed":true}\n').decode()})
        return request(method, path, payload)

    def corrupt_response(path, value):
        if model.writes:
            if fault == "parent" and "/git/commits/" in path:
                value["parents"] = [{"sha": "0" * 40}]
            elif fault == "hash" and "/git/blobs/" in path:
                value["content"] = base64.b64encode(b"x" * value["size"]).decode()
            elif fault == "tree" and "/git/trees/" in path:
                value["truncated"] = True
            elif fault == "binding" and path == "/repos/" + model.binding.repository:
                value["private"] = False
        return value

    model.request, model.corrupt_response = alter_write, corrupt_response
    with pytest.raises(UnknownWrite) as caught:
        backend.commit({NEW_PATH: NEW_BYTES}, expected=initial)
    assert_diagnostic(caught.value, phase=phase, classification=classification)
    assert model.writes == 1 and model.git.head() != initial
    # Native publication and a failed verification remain distinct facts.
    assert model.git.read(model.git.head(), NEW_PATH)


def test_graphql_rejection_preserves_cas_winner_without_guessing_private_prose(tmp_path):
    model = GitAPI(tmp_path / "cas.git")
    backend = GitHubStore(model.binding, model)
    initial = backend.head()

    def race():
        model.git.commit({"submissions/winner.json": b"{}\n"}, expected=initial)

    original = model.request

    def sensitive_response(method, path, payload=None):
        value = original(method, path, payload)
        if method == "POST":
            assert value["data"]["createCommitOnBranch"] is None
            value["errors"] = [{"message": SECRET, "path": [SECRET], "extensions": {"secret": SECRET}}]
        return value

    model.before_write, model.request = race, sensitive_response
    with pytest.raises(UnknownWrite) as caught:
        backend.commit({NEW_PATH: NEW_BYTES}, expected=initial)
    assert_diagnostic(caught.value, phase="response", classification="graphql-errors")
    assert model.writes == 1
    assert model.git.read(model.git.head(), "submissions/winner.json") == b"{}\n"
    assert NEW_PATH not in model.git.paths(model.git.head())


@pytest.mark.parametrize("response", [
    [SECRET],
    {"data": None, "private": SECRET},
    {"data": {"createCommitOnBranch": {"commit": {"oid": SECRET}}}},
])
def test_unusable_mutation_response_stays_unknown_without_echo(tmp_path, response):
    model = GitAPI(tmp_path / "response.git")
    backend = GitHubStore(model.binding, model)
    initial = backend.head()
    model.after_write = lambda _: response
    with pytest.raises(UnknownWrite) as caught:
        backend.commit({NEW_PATH: NEW_BYTES}, expected=initial)
    assert_diagnostic(caught.value, phase="response", classification="response-invalid")
    assert model.writes == 1 and model.git.read(model.git.head(), NEW_PATH) == NEW_BYTES


class FaultWire:
    """The original GitHubAPI still owns transport sanitization and POST policy."""
    def __init__(self, model, fault, *, on_post=False):
        self.model, self.fault, self.on_post = model, fault, on_post
        self.post_attempts = 0

    def open(self, request, timeout):
        assert timeout == 30
        path = request.full_url.removeprefix(API_ROOT)
        if request.method == "POST":
            self.post_attempts += 1
            if self.on_post:
                raise self.fault
        elif self.model.writes and "/git/commits/" in path:
            raise self.fault
        value = self.model.request(request.method, path, json.loads(request.data) if request.data else None)
        return Response(encoded(value), request.full_url)


@pytest.mark.parametrize("kind,classification", [
    ("timeout", "response-unavailable"),
    ("disconnect", "response-unavailable"),
    ("url", "response-unavailable"),
    (401, "not-authorized"),
    (404, "not-found-or-not-authorized"),
    (429, "rate-limited"),
    (500, "http-failure"),
])
def test_actual_transport_read_failure_is_classified_without_claiming_a_root_cause(tmp_path, kind, classification):
    fault = {"timeout": TimeoutError(SECRET), "disconnect": IncompleteRead(SECRET.encode()),
             "url": URLError(SECRET)}.get(kind)
    if fault is None:
        fault = HTTPError("https://private.invalid/" + SECRET, kind, SECRET, {}, None)
    model = GitAPI(tmp_path / "transport.git")
    api = GitHubAPI(SECRET)
    wire = FaultWire(model, fault)
    api._opener = wire
    backend = GitHubStore(model.binding, api)
    initial = backend.head()
    with pytest.raises(UnknownWrite) as caught:
        backend.commit({NEW_PATH: NEW_BYTES}, expected=initial)
    assert_diagnostic(caught.value, phase="commit", classification=classification)
    assert wire.post_attempts == model.writes == 1
    assert model.git.read(model.git.head(), NEW_PATH) == NEW_BYTES
    assert api.rate_limits.throttled is (kind == 429)


@pytest.mark.parametrize("kind,error_class,code", [
    ("timeout", UnknownWrite, "REMOTE_WRITE_UNCONFIRMED"),
    (429, UnknownWrite, "REMOTE_WRITE_UNCONFIRMED"),
    (403, StoreUnavailable, "REMOTE_NOT_AUTHORIZED"),
])
def test_post_transport_outcomes_keep_original_policy_without_post_response_diagnostic(tmp_path, kind, error_class, code):
    fault = (TimeoutError(SECRET) if kind == "timeout"
             else HTTPError("https://private.invalid/" + SECRET, kind, SECRET, {}, None))
    model = GitAPI(tmp_path / "send.git")
    api = GitHubAPI(SECRET)
    wire = FaultWire(model, fault, on_post=True)
    api._opener = wire
    backend = GitHubStore(model.binding, api)
    initial = backend.head()
    with pytest.raises(error_class) as caught:
        backend.commit({NEW_PATH: NEW_BYTES}, expected=initial)
    assert caught.value.args == (code,) and write_failure_diagnostic(caught.value) is None
    assert wire.post_attempts == 1 and model.writes == 0 and model.git.head() == initial


def test_actual_readback_budget_exhaustion_keeps_the_same_api_and_unknown_write(tmp_path):
    model = GitAPI(tmp_path / "budget.git")
    api = GitHubAPI(SECRET)
    wire = FaultWire(model, AssertionError("saturated readback must stop before the wire"))
    api._opener = wire
    backend = GitHubStore(model.binding, api)
    initial = backend.head()

    def saturate(value):
        api._requests = MAX_REQUESTS
        return value

    model.after_write = saturate
    with pytest.raises(UnknownWrite) as caught:
        backend.commit({NEW_PATH: NEW_BYTES}, expected=initial)
    assert_diagnostic(caught.value, phase="commit", classification="request-limit")
    assert api._requests == MAX_REQUESTS + 1 and wire.post_attempts == model.writes == 1
    assert model.git.read(model.git.head(), NEW_PATH) == NEW_BYTES


def test_unknown_error_does_not_get_a_fabricated_network_reason(tmp_path):
    class PrivateFailure(RecordError):
        def __str__(self):
            raise AssertionError("diagnostic must not format the private failure")

    model = GitAPI(tmp_path / "private.git")
    backend = GitHubStore(model.binding, model)
    initial = backend.head()

    def failure(path, value):
        if model.writes and "/git/commits/" in path:
            raise PrivateFailure(SECRET)
        return value

    model.corrupt_response = failure
    with pytest.raises(UnknownWrite) as caught:
        backend.commit({NEW_PATH: NEW_BYTES}, expected=initial)
    assert_diagnostic(caught.value, phase="commit", classification="validation-failed")
    assert model.writes == 1


def test_projection_rejects_unclassified_or_unsafe_attributes():
    assert write_failure_diagnostic(UnknownWrite(TOTAL)) is None
    assert write_failure_diagnostic(RecordError(SECRET)) is None
    error = UnknownWrite(TOTAL)
    error.write_phase, error.write_classification = "commit", "parent-mismatch"
    error.private_response = {"token": SECRET}
    assert write_failure_diagnostic(error) == {"schema": "github-write-diagnostic-v1",
        "phase": "commit", "classification": "parent-mismatch"}
    for phase, classification in ((SECRET, "parent-mismatch"), ("commit", SECRET),
                                   ([], "parent-mismatch"), ("commit", {})):
        error.write_phase, error.write_classification = phase, classification
        assert write_failure_diagnostic(error) is None


def test_success_and_original_journal_replay_keep_exact_bytes_and_one_write(tmp_path):
    model = GitAPI(tmp_path / "normal.git")
    backend = GitHubStore(model.binding, model)
    store = VersionedStore(backend)
    original = source("Original synthetic source bytes, with exact punctuation.\n")
    reference = store.put_source("same-intent", original)
    assert VersionedStore(GitHubStore(model.binding, model)).read_source(reference)["data"] == original.text
    assert VersionedStore(GitHubStore(model.binding, model)).put_source("same-intent", original) == reference
    assert model.writes == 1
