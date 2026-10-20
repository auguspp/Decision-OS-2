"""Segmented activity history uses native Git and complete original replay.

The large fixture appends already projected legal events through the original
CAS writer once. It does not repeatedly ask the production reader to reprove
every growing prefix; the boundary operation and independent readers do that.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import pytest

from decision_kernel.application import watch_history as history
from decision_kernel.application.conditional_odds import ConditionalOdds, record_artifact
from decision_kernel.application.odds_watch import OddsWatch, _Sources
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.store_audit import audit_store
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application.watch_activity import WatchActivity, pointer_path
from decision_kernel.domain.records import BlobRef, Conflict, MissingObject, RecordError, RevisionInput, SourceInput, UnknownWrite, encoded
from decision_kernel.domain.watch_activity import ACTIVITY_RECORD, validate_change
from decision_kernel.domain.watch_history import HISTORY_VERSION, position, validate_history, validate_segment
from decision_kernel.infrastructure.git_store import GitStore
from test_conditional_odds import authorized, price_payload, spec_payload
from test_odds_watch import case, watch_payload
from test_watch_activity import apply, change, query


def at(index):
    return (datetime(2026, 1, 3, 4, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat()


def setup_watch(store):
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00Z")
        source = store.put_source("s", SourceInput("s", "content", "Synthetic", "synthetic:history", "Synthetic research basis"))
        ref = store.submit_revision("r", RevisionInput("case", "r1", "full", "Synthetic research", "Synthetic", "synthetic", (source,)))
        spec = ConditionalOdds(store).freeze(authorized("spec", spec_payload(asdict(ref))), submission_id="s1")["reference"]
        return OddsWatch(store).freeze(authorized("watch", watch_payload(spec)), submission_id="w1")["reference"]


def append_projected(store, event, submission_id, *, segment=None):
    """Fixture construction only: original native event/pointer/journal CAS."""
    artifact = record_artifact(ACTIVITY_RECORD, event, "Synthetic engineering fixture")
    files = history.artifact_files(artifact)
    if segment:
        files.update(history.artifact_files(segment))
    files[pointer_path(event["watch_key"])] = encoded({"schema": "watch-activity-pointer-v1",
        "watch_key": event["watch_key"], "event_path": artifact.path()})
    kind = event["command"]["kind"]
    intent = {"operation": "watch-activity-" + kind, "input": authorized(kind, event["command"]["input"])}
    return asdict(store._save(event["snapshot"], submission_id, intent, artifact.path(), files))


def build_prefix(store, watch_ref, *, versioned=True, count=128):
    service, reader = WatchActivity(store), _Sources(store.backend)
    frozen = service._watch(watch_ref, reader)
    state, refs, states = None, [], []
    for i in range(count):
        if i < 2:
            value = change(watch_ref, refs[-1] if refs else None, "register" if i == 0 else "resume", at(i))
            if versioned and i == 0:
                value["history_version"] = HISTORY_VERSION
            command = {"kind": "change", "input": value}
        else:
            command = {"kind": "check", "input": query(store, watch_ref, refs[-1], at(i), price_payload())}
        event, state = service._project(frozen, command, store.backend.head(), state, reader,
                                        history=position(0, i) if versioned else None)
        ref = append_projected(store, event, "prefix-" + str(i))
        state["event_ref"] = ref
        if event["result"] is not None:
            state["last_check_ref"] = ref
        refs.append(ref)
        # Only the last two states are needed for the original v1 threshold.
        states = (states + [deepcopy(state)])[-2:]
    state["_history_tail"], state["_history_segments"] = refs, []
    return {"watch_ref": watch_ref, "frozen": frozen, "state": state, "refs": refs, "last_states": states}


@pytest.fixture(scope="module")
def native_prefix(tmp_path_factory):
    store = VersionedStore(GitStore.init_synthetic(tmp_path_factory.mktemp("watch-prefix") / "records.git"))
    return {"root": store.backend.root, **build_prefix(store, setup_watch(store))}


@pytest.fixture(scope="module")
def legacy_prefix(tmp_path_factory):
    store = VersionedStore(GitStore.init_synthetic(tmp_path_factory.mktemp("watch-legacy") / "records.git"))
    return {"root": store.backend.root, **build_prefix(store, setup_watch(store), versioned=False, count=127)}


def copy_prefix(value, tmp_path):
    root = tmp_path / "copy.git"
    shutil.copytree(value["root"], root)
    return VersionedStore(GitStore(root)), deepcopy({k: v for k, v in value.items() if k != "root"})


def extend_projected(store, data, through_count):
    """Append another legal native segment once; production readers reprove it."""
    service, reader, state = WatchActivity(store), _Sources(store.backend), data["state"]
    for index in range(len(data["refs"]), through_count):
        command = {"kind": "check", "input": query(store, data["watch_ref"], state["event_ref"], at(index), price_payload())}
        metadata, artifact = history.next_position(state, command, data["frozen"]["watch_sha256"])
        old_tail, seals = state["_history_tail"], state["_history_segments"]
        event, state = service._project(data["frozen"], command, store.backend.head(), state, reader, history=metadata)
        ref = append_projected(store, event, "prefix-" + str(index), segment=artifact)
        if artifact:
            seals = seals + [{"reference": asdict(store.backend.reference(ref["commit"], artifact.path())),
                              "segment": json.loads(artifact.raw())}]
            old_tail = []
        state.update(event_ref=ref, last_check_ref=ref, _history_tail=old_tail + [ref], _history_segments=seals)
        data["refs"].append(ref)
    data["state"] = state
    return data


def test_real_128_boundary_one_cas_continues_exact_check_and_fresh_segment_reader(native_prefix, tmp_path, monkeypatch):
    store, data = copy_prefix(native_prefix, tmp_path)
    service, w, tip = WatchActivity(store), data["watch_ref"], data["refs"][-1]
    command = query(store, w, tip, at(128), price_payload())
    expected = service.preview(command)["event"]
    original, writes = store.backend.commit, []
    def observe(files, *, expected):
        writes.append(deepcopy(files)); return original(files, expected=expected)
    monkeypatch.setattr(store.backend, "commit", observe)
    receipt = service.save_check(authorized("check", command), submission_id="rollover")
    assert len(writes) == 1 and len(writes[0]) == 6
    assert "submissions/rollover.json" in writes[0]
    assert len([p for p in writes[0] if p.endswith("/current.json")]) == 1
    fresh = WatchActivity(VersionedStore(GitStore(store.backend.root)))
    view = fresh.read(receipt["reference"])
    assert view["event"] == expected and view["summary"]["events"] == 129
    assert view["event"]["result"]["request"]["previous_check"] == tip
    assert view["event"]["result"]["status"] == "continued-trigger"
    assert view["event"]["result"]["trigger_continuity"] == "supplied-check-chain-only"
    assert len(view["history"]) == 1 and view["history_validation"]["verified_events"] == 129
    seal = view["sealed_segments"][0]["reference"]
    assert seal["commit"] == receipt["reference"]["commit"]
    segment = fresh.read_segment(seal)
    assert segment["segment"]["events"] == data["refs"]
    assert segment["summary"]["events"] == 128 and len(segment["history"]) == 128
    assert segment["history_validation"]["verified_events"] == 129 and not segment["history_validation"]["summary_trusted"]
    assert segment["effects"]["scheduler_created"] is False and segment["effects"]["notification_sent"] is False
    # Each original exact object is inherited byte-for-byte, including the v1
    # custody envelope around v2 event values. No alternate activity pointer.
    for ref in data["refs"]:
        assert store.backend.read(receipt["reference"]["commit"], ref["path"]) == store.backend.resolve(BlobRef(**ref))
    audit = audit_store(VersionedStore(GitStore(store.backend.root)), receipt["reference"]["commit"])
    assert seal in audit["artifacts"] and audit["watch_activities"][view["event"]["watch_key"]] == receipt["reference"]
    assert audit["watch_activity_replay"].startswith("not-performed")


def test_explicit_registration_old_read_shape_and_migration_preserve_original_state(case):
    store, _, _, _, w = case
    service = WatchActivity(store)
    registration = apply(service, change(w), "register")
    active = apply(service, change(w, registration, "resume", at(1)), "resume")
    checked = service.save_check(authorized("check", query(store, w, active, at(2), price_payload())), submission_id="first")["reference"]
    old = service.read(checked)
    assert old["event"]["schema"] == "watch-activity-event-v1" and "history_validation" not in old
    migrate = {**change(w, checked, "migrate", at(3)), "history_version": HISTORY_VERSION}
    adopted = apply(service, migrate, "adopt")
    view = WatchActivity(VersionedStore(GitStore(store.backend.root))).read(adopted)
    assert view["summary"]["events"] == 4 and view["event"]["state"] == "active"
    assert view["sealed_segments"][0]["kind"] == "legacy-v1"
    again = service.save_check(authorized("check", query(store, w, adopted, at(4), price_payload())), submission_id="next")
    result = service.read(again["reference"])["event"]["result"]
    assert result["request"]["previous_check"] == checked
    assert result["status"] == "continued-trigger" and result["trigger_continuity"] == "supplied-check-chain-only"
    assert service.read(checked, include_current_use=False)["event"] == old["event"]
    with pytest.raises(RecordError, match="USE_ACTIVITY_ENTRY"):
        case[3].inspect({"watch_ref": w, "checked_at": at(5), "records_snapshot": store.backend.head(),
                         "previous_check": None, "input": None, "review_reasons": []})


def test_original_127_boundary_cannot_be_migrated_and_128_is_retire_only(legacy_prefix, tmp_path):
    store, data = copy_prefix(legacy_prefix, tmp_path)
    service, w, tip = WatchActivity(store), data["watch_ref"], data["refs"][-1]
    head = store.backend.head()
    with pytest.raises(RecordError, match="LEGACY_LIMIT"):
        apply(service, {**change(w, tip, "migrate", at(127)), "history_version": HISTORY_VERSION}, "no-rescue")
    with pytest.raises(RecordError, match="LAST_SLOT_RESERVED_FOR_RETIRE"):
        apply(service, change(w, tip, "pause", at(127)), "no-extra-execution")
    assert store.backend.head() == head
    retired = apply(service, change(w, tip, "retire", at(127)), "close")
    assert service.read(retired)["summary"]["events"] == 128
    with pytest.raises(RecordError, match="LEGACY_LIMIT|RETIRED"):
        apply(service, {**change(w, retired, "migrate", at(128)), "history_version": HISTORY_VERSION}, "no-revive")


def test_complete_126_event_legacy_prefix_can_be_adopted_before_the_old_boundary(legacy_prefix, tmp_path):
    store, data = copy_prefix(legacy_prefix, tmp_path)
    # Select a known original fixture commit in this disposable synthetic copy.
    # Production uses no ref reset: migration CAS remains on the selected head.
    original_tip = data["refs"][-2]
    store.backend._run("update-ref", "refs/heads/records", original_tip["commit"], store.backend.head())
    service, w = WatchActivity(store), data["watch_ref"]
    adopted = apply(service, {**change(w, original_tip, "migrate", at(126)), "history_version": HISTORY_VERSION}, "adopt")
    view = WatchActivity(VersionedStore(GitStore(store.backend.root))).read(adopted)
    assert view["summary"]["events"] == 127 and view["summary"]["checks"] == 124
    assert view["sealed_segments"][0]["event_count"] == 126 and view["event"]["state"] == "active"
    saved = service.save_check(authorized("check", query(store, w, adopted, at(127), price_payload())), submission_id="continue")
    event = service.read(saved["reference"])["event"]
    assert event["summary"]["events"] == 128 and event["result"]["request"]["previous_check"] == original_tip
    assert event["result"]["status"] == "continued-trigger"


def test_second_real_segment_inherits_exact_seal_and_replays_the_first(native_prefix, tmp_path):
    store, data = copy_prefix(native_prefix, tmp_path)
    extend_projected(store, data, 256)
    service, w, tip = WatchActivity(store), data["watch_ref"], data["refs"][-1]
    receipt = service.save_check(authorized("check", query(store, w, tip, at(256), price_payload())), submission_id="second-rollover")
    fresh = WatchActivity(VersionedStore(GitStore(store.backend.root)))
    view = fresh.read(receipt["reference"])
    assert view["history_validation"]["verified_events"] == 257 and len(view["sealed_segments"]) == 2
    first, second = view["sealed_segments"]
    segment = fresh.read_segment(second["reference"])
    assert segment["segment"]["previous_segment"] == first["reference"]
    assert segment["segment"]["entry_event"] == data["refs"][127]
    assert segment["segment"]["events"] == data["refs"][128:256]
    assert segment["summary"]["events"] == 256 and segment["history_validation"]["verified_events"] == 257
    assert view["event"]["result"]["request"]["previous_check"] == tip
    assert view["event"]["result"]["status"] == "continued-trigger"


def test_unknown_rollover_write_reconciles_same_original_journal(native_prefix, tmp_path, monkeypatch):
    store, data = copy_prefix(native_prefix, tmp_path)
    service, w, tip = WatchActivity(store), data["watch_ref"], data["refs"][-1]
    payload = authorized("check", query(store, w, tip, at(128), price_payload()))
    original = store.backend.commit
    def lose(files, *, expected):
        original(files, expected=expected)
        raise UnknownWrite("SYNTHETIC_LOST_RECEIPT")
    monkeypatch.setattr(store.backend, "commit", lose)
    with pytest.raises(UnknownWrite): service.save_check(payload, submission_id="unknown")
    head = store.backend.head()
    monkeypatch.setattr(store.backend, "commit", original)
    receipt = service.save_check(payload, submission_id="unknown", reconcile=True)
    assert receipt["reference"]["commit"] == head and store.backend.head() == head
    assert service.current(w)["reference"] == receipt["reference"]
    changed = deepcopy(payload); changed["check"]["review_reasons"] = ["Different intent"]
    with pytest.raises(Conflict, match="CONTENT_CONFLICT"):
        service.save_check(changed, submission_id="unknown", reconcile=True)
    with pytest.raises(MissingObject, match="NO_WRITE"):
        service.save_check(payload, submission_id="missing", reconcile=True)
    assert store.backend.head() == head


def test_seal_published_alone_cannot_be_adopted_as_an_atomic_rollover(native_prefix, tmp_path):
    store, data = copy_prefix(native_prefix, tmp_path)
    service, w, tip = WatchActivity(store), data["watch_ref"], data["refs"][-1]
    command = {"kind": "check", "input": query(store, w, tip, at(128), price_payload())}
    _, artifact = history.next_position(data["state"], command, data["frozen"]["watch_sha256"])
    ref = ResearchArtifacts(store).put("unselected-seal", artifact)
    with pytest.raises(RecordError, match="SEAL_NOT_ATOMIC"): service.read_segment(ref)
    head = store.backend.head()
    with pytest.raises(RecordError, match="SEAL_ALREADY_PRESENT"):
        service.save_check(authorized("check", query(store, w, tip, at(128), price_payload())), submission_id="no-reuse")
    assert store.backend.head() == head and service.current(w)["reference"] == tip


@pytest.mark.parametrize("mode", ["retired", "expired", "fixed-horizon", "end-after-trigger", "disabled"])
def test_migration_does_not_reenable_or_extend_the_original_watch(case, mode):
    store, _, spec, old, w = case
    values = watch_payload(spec)
    if mode == "disabled": values["activation"]["enabled"] = False
    if mode == "expired": values["window"]["ends_at"] = at(1)
    if mode == "end-after-trigger": values["window"]["end_after_trigger"] = True
    if mode in ("expired", "disabled", "end-after-trigger"):
        w = old.freeze(authorized("watch", values), submission_id="other")["reference"]
    service = WatchActivity(store)
    seed = None
    if mode == "end-after-trigger":
        from test_odds_watch import request
        seed = old.save(authorized("check", request(store, w, at=at(0), price=price_payload())), submission_id="seed")["reference"]
    ref = apply(service, change(w, at=at(0), seed=seed), "register")
    if mode == "retired": ref = apply(service, change(w, ref, "retire", at(1)), "retire")
    migrate_at = "2029-01-04T00:00:00Z" if mode == "fixed-horizon" else at(2)
    service.clock = lambda: datetime(2029, 2, 1, tzinfo=timezone.utc)
    before = store.backend.head()
    with pytest.raises(RecordError, match="RETIRED|WINDOW_ENDED|FIXED_HORIZON_ENDED|DISABLED"):
        apply(service, {**change(w, ref, "migrate", migrate_at), "history_version": HISTORY_VERSION}, "forbidden")
    assert store.backend.head() == before


@pytest.mark.parametrize("damage", ["reordered", "omitted", "duplicate", "entry", "foreign-watch"])
def test_self_consistent_seal_does_not_replace_original_prefix_semantics(native_prefix, tmp_path, damage):
    store, data = copy_prefix(native_prefix, tmp_path)
    service = WatchActivity(store)
    command = {"kind": "check", "input": query(store, data["watch_ref"], data["refs"][-1], at(128), price_payload())}
    _, original = history.next_position(data["state"], command, data["frozen"]["watch_sha256"])
    value = json.loads(original.raw())
    if damage == "reordered": value["events"][1:3] = list(reversed(value["events"][1:3]))
    elif damage == "omitted": value["events"] = value["events"][1:]
    elif damage == "duplicate": value["events"][1] = value["events"][0]
    elif damage == "entry": value["entry_event"] = data["refs"][0]
    else: value["watch_key"] = "f" * 64
    if damage in ("omitted", "entry"):
        with pytest.raises(RecordError): history.segment_artifact(value)
        return
    artifact = history.segment_artifact(value)
    event, _ = service._project(data["frozen"], command, store.backend.head(), data["state"], _Sources(store.backend),
                                history=position(1, 0, history.local_edge(artifact)))
    forged = append_projected(store, event, "forged", segment=artifact)
    with pytest.raises(RecordError): WatchActivity(VersionedStore(GitStore(store.backend.root))).read(forged)
    seal = store.backend.reference(forged["commit"], artifact.path())
    with pytest.raises(RecordError): WatchActivity(store).read_segment(seal)


def test_old_snapshot_branch_cannot_hide_a_later_retirement(case):
    store, _, _, _, w = case
    service = WatchActivity(store)
    first = apply(service, {**change(w), "history_version": HISTORY_VERSION}, "register")
    active = apply(service, change(w, first, "resume", at(1)), "resume")
    old_head = store.backend.head()
    _, state, _ = service._replay(active, _Sources(store.backend))
    command = {"kind": "check", "input": query(store, w, active, at(2), price_payload())}
    planned, _ = service._project(service._watch(w, _Sources(store.backend)), command, old_head, state,
                                  _Sources(store.backend), history=position(0, 2))
    retired = apply(service, change(w, active, "retire", at(2)), "retire")
    # Forge coherent bytes and the current pointer on the later native head,
    # while falsely claiming the old pre-retirement CAS snapshot.
    artifact = record_artifact(ACTIVITY_RECORD, planned, "Synthetic engineering fixture")
    files = history.artifact_files(artifact)
    files[pointer_path(planned["watch_key"])] = encoded({"schema": "watch-activity-pointer-v1",
        "watch_key": planned["watch_key"], "event_path": artifact.path()})
    commit = store.backend.commit(files, expected=retired["commit"])
    forged = store.backend.reference(commit, artifact.path())
    with pytest.raises(RecordError, match="EVENT_PARENT_MISMATCH"):
        WatchActivity(VersionedStore(GitStore(store.backend.root))).read(forged)


def test_real_side_event_merged_after_mainline_retirement_is_not_current_or_replayable(case, tmp_path):
    store, _, _, _, w = case
    data = build_prefix(store, w, count=2)
    active = data["refs"][-1]
    side_path = tmp_path / "side.git"
    shutil.copytree(store.backend.root, side_path)
    retired = apply(WatchActivity(store), change(w, active, "retire", at(2)), "retire")
    side_store = VersionedStore(GitStore(side_path))
    side_ref = WatchActivity(side_store).save_check(authorized("check",
        query(side_store, w, active, at(3), price_payload())), submission_id="old-branch-check")["reference"]
    native = store.backend
    native._run("fetch", "--no-tags", str(side_path), "refs/heads/records")
    tree = native._run("rev-parse", side_ref["commit"] + "^{tree}").stdout.decode().strip()
    merged = native._run("commit-tree", tree, "-p", retired["commit"], "-p", side_ref["commit"],
                         data=b"Synthetic adversarial merge\n").stdout.decode().strip()
    native._advance(merged, retired["commit"])
    fresh = WatchActivity(VersionedStore(GitStore(native.root)))
    # Both parents and every exact blob are genuinely reachable. Default Git
    # tree simplification picks the side event, but it is not records' lineage.
    for operation in (lambda: fresh.current(w),
                      lambda: fresh.current(w, snapshot=side_ref["commit"]),
                      lambda: fresh.read(side_ref, include_current_use=False),
                      lambda: fresh.preview(query(store, w, side_ref, at(4), price_payload()))):
        with pytest.raises(RecordError, match="FIRST_PARENT|SELECTION_LINEAGE"):
            operation()
    assert native.head() == merged
    assert fresh.read(retired, include_current_use=False)["event"]["state"] == "retired"


@pytest.mark.parametrize("remove_tree", [False, True])
@pytest.mark.parametrize("versioned", [False, True])
def test_first_registration_cannot_forget_a_deleted_original_pointer(case, remove_tree, versioned):
    store, _, _, _, w = case
    native, before_registration = store.backend, store.backend.head()
    data = build_prefix(store, w, versioned=versioned, count=2)
    retired = apply(WatchActivity(store), change(w, data["refs"][-1], "retire", at(2)), "retire")
    if remove_tree:
        tree = native._run("rev-parse", before_registration + "^{tree}").stdout.decode().strip()
    else:
        # Change only current.json; keep all original artifacts and journals.
        with TemporaryDirectory() as directory:
            index = str(Path(directory) / "index")
            native._run("read-tree", retired["commit"], index=index)
            deleted = "0 " + "0" * 40 + "\t" + pointer_path(data["frozen"]["watch_sha256"]) + "\n"
            native._run("update-index", "--index-info", data=deleted.encode("ascii"), index=index)
            tree = native._run("write-tree", index=index).stdout.decode().strip()
    missing = native._run("commit-tree", tree, "-p", retired["commit"],
                          data=b"Synthetic missing current pointer\n").stdout.decode().strip()
    native._advance(missing, retired["commit"])
    fresh = WatchActivity(VersionedStore(GitStore(native.root)))
    if versioned:
        with pytest.raises(RecordError, match="POINTER_MISSING"):
            fresh.current(w)
        standalone = {"watch_ref": w, "checked_at": at(3), "records_snapshot": missing,
                      "previous_check": None, "input": price_payload(), "review_reasons": []}
        for operation in (lambda: apply(fresh, change(w, action="register", at=at(3)), "default-v1-register"),
                          lambda: OddsWatch(store).inspect(standalone),
                          lambda: OddsWatch(store).save(authorized("check", standalone), submission_id="standalone")):
            with pytest.raises(RecordError, match="POINTER_MISSING"):
                operation()
    assert fresh.read(retired, include_current_use=False)["event"]["state"] == "retired"
    command = {"kind": "change", "input": {**change(w, action="register", at=at(3)),
                                             "history_version": HISTORY_VERSION}}
    with pytest.raises(RecordError, match="POINTER_MISSING"):
        apply(fresh, command["input"], "second-registration")
    assert native.head() == missing
    # Persist coherent bytes through the fixture-only constructor to prove
    # that the original registration replay enforces the same admission gate.
    forged, _ = fresh._project(data["frozen"], command, missing, None, _Sources(native), history=position(0, 0))
    ref = append_projected(store, forged, "forged-registration")
    with pytest.raises(RecordError, match="POINTER_MISSING"):
        WatchActivity(VersionedStore(GitStore(native.root))).read(ref, include_current_use=False)


def test_v1_pointer_reset_cannot_be_adopted_as_a_new_verified_prefix(case):
    store, _, _, _, w = case
    native, before_registration = store.backend, store.backend.head()
    data = build_prefix(store, w, versioned=False, count=2)
    retired = apply(WatchActivity(store), change(w, data["refs"][-1], "retire", at(2)), "retire")
    tree = native._run("rev-parse", before_registration + "^{tree}").stdout.decode().strip()
    missing = native._run("commit-tree", tree, "-p", retired["commit"],
                          data=b"Synthetic old v1 reset\n").stdout.decode().strip()
    native._advance(missing, retired["commit"])
    service, reader = WatchActivity(store), _Sources(native)
    command = {"kind": "change", "input": change(w, action="register", at=at(3))}
    event, state = service._project(data["frozen"], command, missing, None, reader)
    ref = append_projected(store, event, "reset-v1")
    state.update(event_ref=ref, _history_tail=[ref], _history_segments=[])
    migration = {"kind": "change", "input": {**change(w, ref, "migrate", at(4)), "history_version": HISTORY_VERSION}}
    head = native.head()
    with pytest.raises(RecordError, match="POINTER_MISSING"):
        apply(service, migration["input"], "adopt-reset")
    assert native.head() == head
    metadata, segment = history.next_position(state, migration, data["frozen"]["watch_sha256"])
    forged, _ = service._project(data["frozen"], migration, head, state, reader, history=metadata)
    adopted = append_projected(store, forged, "forged-adoption", segment=segment)
    with pytest.raises(RecordError, match="POINTER_MISSING"):
        WatchActivity(VersionedStore(GitStore(native.root))).read(adopted, include_current_use=False)


def test_raw_v1_downgrade_cannot_hide_a_previous_v2_retirement(case):
    store, _, _, _, w = case
    native, before_registration = store.backend, store.backend.head()
    data = build_prefix(store, w, count=2)
    retired = apply(WatchActivity(store), change(w, data["refs"][-1], "retire", at(2)), "retire")
    tree = native._run("rev-parse", before_registration + "^{tree}").stdout.decode().strip()
    missing = native._run("commit-tree", tree, "-p", retired["commit"],
                          data=b"Synthetic v2 selection erased\n").stdout.decode().strip()
    native._advance(missing, retired["commit"])
    service, reader, state, ref = WatchActivity(store), _Sources(native), None, None
    for offset, action in enumerate(("register", "resume"), 3):
        command = {"kind": "change", "input": change(w, ref, action, at(offset))}
        event, state = service._project(data["frozen"], command, native.head(), state, reader)
        ref = append_projected(store, event, "raw-v1-" + action)
        state["event_ref"] = ref
    fresh = WatchActivity(VersionedStore(GitStore(native.root)))
    # The old v1 exact reader keeps its former byte/replay contract. New use
    # must still refuse to downgrade the same previously v2 activity pointer.
    assert fresh.read(ref, include_current_use=False)["event"]["state"] == "active"
    head = native.head()
    standalone = {"watch_ref": w, "checked_at": at(5), "records_snapshot": head,
                  "previous_check": None, "input": price_payload(), "review_reasons": []}
    for operation in (lambda: fresh.current(w),
                      lambda: fresh.preview(query(store, w, ref, at(5), price_payload())),
                      lambda: apply(fresh, change(w, ref, "pause", at(5)), "pause-downgrade"),
                      lambda: OddsWatch(store).inspect(standalone)):
        with pytest.raises(RecordError, match="FORMAT_DOWNGRADE"):
            operation()
    assert native.head() == head


@pytest.mark.parametrize("damage", ["summary", "condition"])
def test_sealed_prefix_replays_original_semantics_instead_of_adoption_summary(case, damage):
    store, _, _, _, w = case
    service, reader = WatchActivity(store), _Sources(store.backend)
    first = apply(service, change(w), "register")
    active = apply(service, change(w, first, "resume", at(1)), "resume")
    frozen, before, rows = service._replay_v1(active, reader)
    command = {"kind": "check", "input": query(store, w, active, at(2), price_payload())}
    event, state = service._project(frozen, command, store.backend.head(), before, reader)
    if damage == "summary": event["summary"]["checks"] = 500
    else: event["result"]["condition_met"] = False
    damaged_ref = append_projected(store, event, "damaged-original")
    state.update(event_ref=damaged_ref, last_check_ref=damaged_ref,
                 _history_tail=[row["reference"] for row in rows] + [damaged_ref], _history_segments=[])
    command = {"kind": "change", "input": {**change(w, damaged_ref, "migrate", at(3)), "history_version": HISTORY_VERSION}}
    metadata, segment = history.next_position(state, command, frozen["watch_sha256"])
    adopted, _ = service._project(frozen, command, store.backend.head(), state, reader, history=metadata)
    ref = append_projected(store, adopted, "plausible-adoption", segment=segment)
    # Both digests and the CAS topology are coherent, including the adoption's
    # recomputed-looking summary. The corrupted exact earlier event still fails.
    for operation in (lambda: service.read(ref),
                      lambda: service.read_segment(store.backend.reference(ref["commit"], segment.path()))):
        with pytest.raises(RecordError, match="REPLAY_MISMATCH"): operation()


@pytest.mark.parametrize("component", ["manifest", "content", "changed-content"])
def test_missing_or_changed_seal_original_fails_without_tail_only_fallback(case, monkeypatch, component):
    store, _, _, _, w = case
    service = WatchActivity(store)
    first = apply(service, change(w), "register")
    active = apply(service, change(w, first, "resume", at(1)), "resume")
    original, removed = store.backend.commit, []
    def damage(files, *, expected):
        files = dict(files)
        suffix = "/manifest.json" if component == "manifest" else "/content.bin"
        path = next(p for p in files if "odds-watch-segment-" in p and p.endswith(suffix))
        removed.append(path)
        if component == "changed-content": files[path] = b"{}\n"
        else: files.pop(path)
        return original(files, expected=expected)
    monkeypatch.setattr(store.backend, "commit", damage)
    ref = apply(service, {**change(w, active, "migrate", at(2)), "history_version": HISTORY_VERSION}, "damaged-seal")
    assert len(removed) == 1
    with pytest.raises(RecordError): WatchActivity(VersionedStore(GitStore(store.backend.root))).read(ref)


@pytest.mark.parametrize("bad", ["unversioned-migrate", "wrong-version", "repeat-opt-in", "early-local", "extra-summary", "bool-index"])
def test_history_payloads_fail_closed(case, bad):
    store, _, _, _, w = case
    if bad in ("unversioned-migrate", "wrong-version", "repeat-opt-in"):
        value = change(w, action="migrate" if bad == "unversioned-migrate" else "register")
        if bad != "unversioned-migrate": value["history_version"] = "unknown" if bad == "wrong-version" else HISTORY_VERSION
        if bad == "repeat-opt-in": value["action"] = "resume"
        with pytest.raises(RecordError): validate_change(value)
    else:
        value = position(0, 0)
        if bad == "early-local": value["previous_segment"] = {"kind": "same-commit", "path": w["path"], "sha256": w["sha256"]}
        elif bad == "extra-summary": value["summary"] = {"events": 1000}
        else: value["segment_index"] = True
        with pytest.raises(RecordError): validate_history(value)


def test_last_structural_slot_retains_the_original_retire_reservation(case):
    store, _, _, _, w = case
    service, reader = WatchActivity(store), _Sources(store.backend)
    first = apply(service, {**change(w), "history_version": HISTORY_VERSION}, "register")
    active = apply(service, change(w, first, "resume", at(1)), "resume")
    frozen, state, _ = service._replay(active, reader)
    # Projection receives a structurally final coordinate only to exercise the
    # final-slot lifecycle rule. This is not a fabricated 4096-event acceptance.
    metadata = position(31, 127, {"kind": "exact", "reference": w})
    command = {"kind": "check", "input": query(store, w, active, at(2), price_payload())}
    with pytest.raises(RecordError, match="LAST_SLOT_RESERVED_FOR_RETIRE"):
        service._project(frozen, command, store.backend.head(), state, reader, history=metadata)
    event, _ = service._project(frozen, {"kind": "change", "input": change(w, active, "retire", at(2))},
                                store.backend.head(), state, reader, history=metadata)
    assert event["state"] == "retired"
