"""The local console may only ever run reads, and only with a read-only credential."""
import json
from pathlib import Path

import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.github_store import GitHubBinding
from decision_kernel.interfaces import (
    auction_commands, auction_next_session_commands, close_structure_commands, delivery_commands,
    decision_preparation_commands, local_read, odds_commands, outcome_review_commands,
    research_commands,
)

BINDING = {"repository": "auguspp/Decision-OS-2", "repository_id": 1420041744, "branch": "records",
           "anchor": "0" * 40, "store_id": "decision-os-runtime", "visibility": "public",
           "write_enabled": False, "private_upgrade_allowed": True}


def upstream_reads() -> set:
    research = set(research_commands.COMMANDS) - {"checkpoint-research"}
    delivery = set(delivery_commands.COMMANDS) - set(delivery_commands.WRITE_COMMANDS)
    return (research | delivery | set(odds_commands.READ_COMMANDS)
            | {"resume-research", "current", "search", "read-source", "read-revision"}
            | {outcome_review_commands.COMMAND, close_structure_commands.COMMAND,
               auction_commands.COMMAND, auction_next_session_commands.COMMAND})


def writes() -> set:
    return (set(odds_commands.WRITE_COMMANDS) | set(odds_commands.RECONCILE_COMMANDS)
            | set(decision_preparation_commands.WRITES) | set(decision_preparation_commands.RECONCILES)
            | set(delivery_commands.WRITE_COMMANDS)
            | {"put-source", "submit-revision", "checkpoint-research"})


def binding(tmp_path, **changes) -> Path:
    data = dict(BINDING)
    data.update(changes)
    path = tmp_path / "store-binding.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_every_local_command_is_exactly_the_current_read_set():
    assert set(local_read.READ_ONLY) == upstream_reads()
    assert len(local_read.READ_ONLY) == len(set(local_read.READ_ONLY))


def test_no_local_command_can_write_or_reconcile():
    assert set(local_read.READ_ONLY) & writes() == set()


def test_private_decision_preparation_never_runs_locally():
    assert "prepare-decision" not in local_read.READ_ONLY
    assert "read-decision-preparation" not in local_read.READ_ONLY


def test_a_write_command_is_refused_before_any_binding_or_token_is_read(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("save-odds", binding(tmp_path), {local_read.READ_TOKEN: "x"})
    assert str(refused.value) == "LOCAL_READ_REFUSES_WRITE_COMMAND"


def test_private_custody_reads_are_refused_with_their_own_reason(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("prepare-decision", binding(tmp_path), {local_read.READ_TOKEN: "x"})
    assert str(refused.value) == "LOCAL_READ_PRIVATE_CUSTODY_REQUIRES_GITHUB"


def test_a_local_read_requires_a_binding(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("research-catalogue", None, {local_read.READ_TOKEN: "x"})
    assert str(refused.value) == "LOCAL_READ_REQUIRES_A_BINDING"


def test_a_local_read_requires_its_own_token(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("research-catalogue", binding(tmp_path), {})
    assert str(refused.value) == "LOCAL_READ_TOKEN_REQUIRED"


def test_a_store_token_in_the_environment_is_refused(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("research-catalogue", binding(tmp_path),
                         {local_read.READ_TOKEN: "read", local_read.STORE_TOKEN: "write"})
    assert str(refused.value) == "LOCAL_READ_REFUSES_STORE_TOKEN"


def test_a_write_enabled_binding_is_refused(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("research-catalogue", binding(tmp_path, write_enabled=True),
                         {local_read.READ_TOKEN: "read"})
    assert str(refused.value) == "LOCAL_READ_REQUIRES_READ_ONLY_BINDING"


def test_a_private_binding_is_refused(tmp_path):
    with pytest.raises(RecordError) as refused:
        local_read.check("research-catalogue",
                         binding(tmp_path, visibility="private", private_upgrade_allowed=False),
                         {local_read.READ_TOKEN: "read"})
    assert str(refused.value) == "LOCAL_READ_PUBLIC_STORE_ONLY"


def test_an_allowed_read_returns_the_binding_it_will_read(tmp_path):
    found = local_read.check("read-odds-book", binding(tmp_path), {local_read.READ_TOKEN: "read"})
    assert isinstance(found, GitHubBinding)
    assert found.branch == "records" and found.write_enabled is False


def test_the_receipt_states_the_store_and_that_no_write_was_requested():
    record = local_read.receipt("research-catalogue", GitHubBinding.from_dict(BINDING),
                                started="a", finished="b", duration_ms=12, code=0)
    assert record["schema"] == "local-read-receipt-v1"
    assert record["identity"] == {"token": "read-only-required", "write": "not-requested",
                                  "authority": "local-read-only-console"}
    assert record["store"]["branch"] == "records" and record["duration_ms"] == 12
    assert record["runner"] == "local"
