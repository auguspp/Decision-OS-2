"""Installed-style batch consumer and HTTP fakes. No real provider traffic."""
from dataclasses import asdict
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

import pytest

from decision_kernel.domain.records import RecordError, SourceRef
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.relay_http import ROOT, RelayHTTP
from decision_kernel.interfaces.relay_financial_cli import main
from test_relay_financial_batch import query, body, capture


class Reply(BytesIO):
    status = 200
    def __init__(self, raw, url, headers=None):
        super().__init__(raw); self.url = url; self.headers = headers or {}
    def geturl(self): return self.url


class Opener:
    def __init__(self, replies): self.replies = iter(replies); self.calls = []
    def open(self, request, timeout):
        self.calls.append(request)
        value = next(self.replies)
        if isinstance(value, Exception): raise value
        return value


@pytest.mark.parametrize("api", ("income", "balancesheet", "cashflow", "fina_indicator", "forecast", "express", "fina_audit", "fina_mainbz"))
def test_actual_http_path_query_and_receipt_for_each_financial_endpoint(api):
    q = query(api); params = q.parameters(); url = ROOT + "/" + api + "?" + urlencode(params)
    raw = json.dumps(body(q)).encode()
    response = Reply(raw, url, {"Content-Length": str(len(raw)), "Content-Type": "application/json", "X-Cache": "HIT"})
    opener = Opener([response])
    client = RelayHTTP(opener=opener, credential="synthetic-secret")
    assert client.request(api, params)["code"] == 0
    assert opener.calls[0].get_header("X-api-key") == "synthetic-secret"
    assert client.last_response_meta["x_cache"] == "HIT" and response.closed


def test_shared_transport_has_no_single_operation_or_economic_request_quota():
    q = query(); params = q.parameters(); url = ROOT + "/income?" + urlencode(params)
    client = RelayHTTP(opener=Opener([Reply(json.dumps(body()).encode(), url) for _ in range(20)]), credential="synthetic-secret")
    for _ in range(20): client.request("income", params)
    assert client.requests == 20


@pytest.mark.parametrize("api,extra", [("p_save", {}), ("income", {"token": "x"}), ("income", {"url": "https://evil.example"}), ("forecast", {"report_type": "1"})])
def test_transport_cannot_be_repurposed_for_arbitrary_targets_or_writes(api, extra):
    opener = Opener([])
    with pytest.raises(RecordError):
        RelayHTTP(opener=opener, credential="synthetic-secret").request(api, {**query().parameters(), **extra})
    assert not opener.calls


@pytest.mark.parametrize("raw,headers", [(b'{"x":1,"x":2}', {}), (b'{"x":1e999}', {}),
    (b'{"x":"synthetic-secret"}', {}), (b'{"x":0}', {"Content-Length": "999"}),
    (b'{"x":0}', {"Content-Encoding": "gzip"})])
def test_bad_body_and_credential_reflection_are_never_persisted(raw, headers):
    params = query().parameters(); url = ROOT + "/income?" + urlencode(params)
    reply = Reply(raw, url, headers)
    with pytest.raises(RecordError):
        RelayHTTP(opener=Opener([reply]), credential="synthetic-secret").request("income", params)
    assert reply.closed


def test_http_error_closes_body_without_retries():
    fp = BytesIO(b"private")
    opener = Opener([HTTPError(ROOT, 429, "private", {}, fp)])
    with pytest.raises(RecordError, match="HTTP_429"):
        RelayHTTP(opener=opener, credential="synthetic-secret").request("income", query().parameters())
    assert fp.closed and len(opener.calls) == 1


def write_capture(path, queries=None):
    queries = queries or [query()]
    path.write_text("".join(json.dumps({"status": "captured", "query": asdict(q), "capture": capture(q)}) + "\n" for q in queries))


def test_cli_plan_multiple_codes_periods_all_eight_and_refuses_overwrite(tmp_path, capsys):
    path = tmp_path / "plan.jsonl"
    args = ["plan", "--codes", "000001.SZ", "600000.SH", "--periods", "20251231", "20260630", "--output", str(path)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["queries"] == 32
    assert len(path.read_text().splitlines()) == 32
    original = path.read_bytes()
    assert main(args) == 2 and path.read_bytes() == original


def test_capture_without_credentials_does_not_create_output_or_call_network(tmp_path, capsys, monkeypatch):
    plan = tmp_path / "plan"
    plan.write_text(json.dumps(asdict(query())) + "\n")
    monkeypatch.delenv("TUSHARE_PROXY_API_KEY", raising=False)
    output = tmp_path / "result"
    assert main(["capture", "--plan", str(plan), "--live", "--output", str(output)]) == 2
    assert "CREDENTIAL_REQUIRED" in capsys.readouterr().err and not output.exists()


def test_cli_capture_batch_writes_successes_and_failure_reasons(tmp_path, capsys, monkeypatch):
    plan = tmp_path / "plan"
    plan.write_text("\n".join(json.dumps(asdict(q)) for q in [query(), query("balancesheet")]))
    monkeypatch.setenv("TUSHARE_PROXY_API_KEY", "synthetic-secret")
    monkeypatch.setattr("decision_kernel.interfaces.relay_financial_cli.capture_query", lambda q, **kw: capture(q))
    output = tmp_path / "captured"
    assert main(["capture", "--plan", str(plan), "--live", "--output", str(output)]) == 0
    assert len(output.read_text().splitlines()) == 2
    assert json.loads(capsys.readouterr().out)["failed_or_not_attempted"] == 0


def test_cli_inspect_and_save_are_offline_and_idempotent(tmp_path, capsys, monkeypatch):
    def forbidden(*a, **kw): raise AssertionError("unexpected source call")
    monkeypatch.setattr("decision_kernel.interfaces.relay_financial_cli.capture_query", forbidden)
    path = tmp_path / "capture.jsonl"; write_capture(path, [query(), query("cashflow")])
    assert main(["inspect", "--capture", str(path)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2
    local = tmp_path / "s.git"; GitStore.init_synthetic(local)
    receipts = tmp_path / "receipts"
    args = ["save", "--capture", str(path), "--batch-id", "finance-001", "--public-rights-confirmed", "--local-synthetic-store", str(local), "--receipts", str(receipts)]
    assert main(args) == 0
    saved = [json.loads(x) for x in receipts.read_text().splitlines()]
    assert len(saved) == 2 and all(x["complete"] for x in saved)
    store = VersionedStore(GitStore(local))
    old_head = store.backend.head()
    new = tmp_path / "replayed-receipts"
    assert main([*args[:-1], str(new)]) == 0
    assert store.backend.head() == old_head
    assert new.read_bytes() == receipts.read_bytes()
    reference = SourceRef.from_dict(saved[0]["saved"][0]["reference"])
    assert "000001.SZ" in store.read_source(reference)["data"]


def test_cli_pretty_printed_readonly_binding_rejected_before_any_store_or_source(tmp_path, capsys):
    path = tmp_path / "capture.jsonl"; write_capture(path)
    binding = {"repository": "auguspp/Decision-OS-2", "repository_id": 1420041744,
               "branch": "records", "anchor": "0" * 40, "store_id": "test", "visibility": "public", "write_enabled": False}
    config = tmp_path / "binding.json"; config.write_text(json.dumps(binding, indent=2))
    out = tmp_path / "receipts"
    assert main(["save", "--capture", str(path), "--batch-id", "a", "--public-rights-confirmed", "--binding", str(config), "--receipts", str(out)]) == 2
    assert "REMOTE_READ_ONLY" in capsys.readouterr().err and not out.exists()


def test_source_false_rights_flag_does_not_grant_public_save(tmp_path, capsys):
    path = tmp_path / "capture.jsonl"; write_capture(path)
    original = json.loads(path.read_text()); original["public_rights_confirmed"] = True
    path.write_text(json.dumps(original) + "\n")
    local = tmp_path / "s.git"; GitStore.init_synthetic(local)
    assert main(["save", "--capture", str(path), "--batch-id", "a", "--local-synthetic-store", str(local), "--receipts", str(tmp_path / "r")]) == 2
    assert "PUBLIC_RIGHTS_REQUIRED" in capsys.readouterr().err


def test_empty_capture_file_does_not_report_completed_batch(tmp_path, capsys):
    empty = tmp_path / "empty"; empty.write_text("")
    assert main(["inspect", "--capture", str(empty)]) == 2
    assert "EMPTY_CAPTURE_FILE" in capsys.readouterr().err


@pytest.mark.parametrize("where", ["value", "key", "nested"])
def test_escaped_credential_reflection_cannot_hide_in_json(where):
    key = 'synthetic-"secret\\value'
    value = {"echo": key} if where == "value" else {key: 1} if where == "key" else {"nested": [{"x": key}]}
    raw = json.dumps(value).encode()
    assert key.encode() not in raw
    params = query().parameters(); url = ROOT + "/income?" + urlencode(params)
    client = RelayHTTP(opener=Opener([Reply(raw, url)]), credential=key)
    with pytest.raises(RecordError, match="RELAY_SECRET_REFLECTED"):
        client.request("income", params)
    assert client.last_response_meta is None


def test_receipt_output_failure_retains_last_published_reference(tmp_path, capsys, monkeypatch):
    from contextlib import contextmanager
    from decision_kernel.interfaces import relay_financial_cli as cli
    path = tmp_path / "capture"; write_capture(path)
    local = tmp_path / "s.git"; GitStore.init_synthetic(local)
    class BrokenOutput:
        def write(self, text): raise OSError("full disk")
        def flush(self): pass
    @contextmanager
    def broken(path):
        yield BrokenOutput()
    monkeypatch.setattr(cli, "_new_file", broken)
    assert main(["save", "--capture", str(path), "--batch-id", "persisted-before-disk-error",
                 "--public-rights-confirmed", "--local-synthetic-store", str(local),
                 "--receipts", str(tmp_path / "receipts")]) == 2
    result = json.loads(capsys.readouterr().err)
    assert result["status"] == "receipt-output-failed"
    ref = SourceRef.from_dict(result["last_result"]["saved"][0]["reference"])
    assert VersionedStore(GitStore(local)).read_source(ref)["availability"] == "stored-representation"
