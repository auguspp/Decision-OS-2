"""Hosted handoffs through the real GitHubStore over the existing real-Git API model.

This tests the production adapter and host, not a more permissive local store.
No network, provider calls, model execution or financial truth is simulated.
"""
from copy import deepcopy
from dataclasses import replace

import pytest

from decision_kernel.domain.records import Conflict, MissingObject, RecordError, UnknownWrite, digest
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.methods.research import research_method
from test_research_runtime import host as host, invoke, request, source, work

AT = "2026-10-19T19:00:00+08:00"


@pytest.fixture
def private_host(host):
    # Preserve the original public marker and exercise the supported private upgrade.
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta["private"] = True
    return host


def document(api, root, kind):
    src = invoke(api, root/"source", "put-source", source(), "source")["result"]["reference"]
    paper = work(src, request=request(kind="quick"),
                 method_sha256=research_method("quick")["sha256"])
    ref = invoke(api, root/"research", "checkpoint", paper, "research")["result"]["reference"]
    restored = invoke(api, root/"resume", "resume", {"reference": ref})["result"]["research"]
    doc = {"schema": "hosted-ai-handoff-v1", "kind": kind,
           "execution_date": "2026-10-19", "cutoff": AT,
           "scope": "Synthetic adapter integration; not real research", "gaps": []}
    if kind == "quick":
        doc["research"] = ref
    else:
        doc.update(body="合成Brief：保留原研究未解决问题，不代签结论。", baseline=None,
                   adoptions=[{"reference": ref, "body_sha256": digest(restored["body"].encode()),
                               "used_change": "Keep the unresolved source question"}])
    return doc, restored


def payload(doc):
    return {"document": doc, "rights_confirmed": True}


@pytest.mark.parametrize("kind", ["quick", "brief"])
def test_native_host_save_exact_read_and_normal_latest(private_host, tmp_path, kind):
    doc, research = document(private_host, tmp_path, kind)
    saved = invoke(private_host, tmp_path/"save", "save-hosted-handoff", payload(doc), "handoff")
    ref = saved["result"]["reference"]
    assert saved["write"] == "published" and saved["result"]["status"] == "readable"
    count = private_host.writes
    reopened = invoke(private_host, tmp_path/"read", "read-hosted-handoff", {"reference": ref})
    assert reopened["write"] == "not-requested"
    assert reopened["result"]["document"] == doc
    assert reopened["result"]["research"] == [research]
    latest = invoke(private_host, tmp_path/"latest", "latest-hosted-handoff",
                    {"kind": kind, "as_of": AT})["result"]
    assert latest["status"] == "readable" and latest["inventory_count"] == 1
    assert latest["items"][0]["document"] == doc
    assert private_host.writes == count
    assert latest["items"][0]["human_acceptance"] == "not-confirmed"


@pytest.mark.parametrize("kind", ["quick", "brief"])
def test_native_same_intent_replays_but_other_same_day_intent_cannot_overwrite(private_host, tmp_path, kind):
    doc, _ = document(private_host, tmp_path, kind)
    first = invoke(private_host, tmp_path/"first", "save-hosted-handoff", payload(doc), "same")
    count, head = private_host.writes, private_host.git.head()
    replay = invoke(private_host, tmp_path/"replay", "save-hosted-handoff", payload(doc), "same")
    assert replay["write"] == "replayed" and replay["result"] == first["result"]
    changed = deepcopy(doc)
    changed["scope"] += "; changed content"
    with pytest.raises(Conflict, match="HOSTED_DAY_ALREADY_SAVED"):
        invoke(private_host, tmp_path/"conflict", "save-hosted-handoff", payload(changed), "other")
    assert private_host.writes == count and private_host.git.head() == head


@pytest.mark.parametrize("kind", ["quick", "brief"])
def test_native_missing_reconcile_cannot_publish(private_host, tmp_path, kind):
    doc, _ = document(private_host, tmp_path, kind)
    count, head = private_host.writes, private_host.git.head()
    with pytest.raises(MissingObject, match="SUBMISSION_NOT_FOUND_NO_WRITE"):
        invoke(private_host, tmp_path/"missing", "reconcile-hosted-handoff", payload(doc), "absent")
    assert private_host.writes == count and private_host.git.head() == head


@pytest.mark.parametrize("kind", ["quick", "brief"])
def test_native_lost_write_reply_reconciles_same_intent_without_duplicate(private_host, tmp_path, kind):
    doc, _ = document(private_host, tmp_path, kind)
    expected = private_host.git.head()
    def lose(_):
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    private_host.after_write = lose
    with pytest.raises(UnknownWrite, match="READONLY_RECONCILIATION"):
        invoke(private_host, tmp_path/"lost", "save-hosted-handoff", payload(doc), "original", expected)
    private_host.after_write = None
    count, head = private_host.writes, private_host.git.head()
    restored = invoke(private_host, tmp_path/"reconcile", "reconcile-hosted-handoff",
                      payload(doc), "original", expected)
    assert restored["write"] == "reconciled-without-write"
    assert restored["result"]["status"] == "readable"
    assert private_host.writes == count and private_host.git.head() == head


def test_native_store_still_rejects_arbitrary_subdirectory_inventory(private_host):
    backend = GitHubStore(private_host.binding, private_host)
    head = backend.head()
    assert backend.paths(head, "research/artifacts/") == []
    with pytest.raises(RecordError, match="PATH_OUTSIDE_STORE"):
        backend.paths(head, "research/artifacts/hosted-quick-20261019/")
