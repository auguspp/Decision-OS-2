"""A Brief must declare exactly the stored slices its own adoptions cite.

Synthetic records only: no network, no provider call, no economic finding.
"""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application import hosted_handoff as handoff
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, SourceInput, digest
from decision_kernel.infrastructure.git_store import GitStore

AT = "2026-10-23T19:00:00+08:00"


class _Counting:
    """Delegates every store call and counts the ones a receipt might add."""
    def __init__(self, store):
        self._store = store
        self.revisions = 0
        self.sources = 0

    def read_revision(self, *args, **kwargs):
        self.revisions += 1
        return self._store.read_revision(*args, **kwargs)

    def read_source(self, *args, **kwargs):
        self.sources += 1
        return self._store.read_source(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._store, name)


@pytest.fixture
def case(tmp_path):
    store = VersionedStore(GitStore.init_synthetic(tmp_path/"records.git"))
    first = store.put_source("first", SourceInput("first", "content", "Synthetic quote",
                                                 "synthetic:first", "合成报价切片"))
    second = store.put_source("second", SourceInput("second", "content", "Synthetic calendar",
                                                   "synthetic:second", "合成日历切片"))
    uncited = store.put_source("uncited", SourceInput("uncited", "content", "Synthetic extraneous",
                                                     "synthetic:uncited", "未被任何研究引用"))
    a = store.submit_revision("a", RevisionInput("case-a", "r1", "quick", "合成研究A：增长不等于现金。",
                                                "Synthetic", "synthetic", (first,)))
    b = store.submit_revision("b", RevisionInput("case-b", "r1", "quick", "合成研究B：日历不证明成交。",
                                                "Synthetic", "synthetic", (second,)))
    return store, a, b, first, second, uncited


def document(store, refs, sources):
    """A v3 Brief adopting the given revisions and declaring the given citations."""
    return {"schema": handoff.BOUND_SCHEMA, "kind": "brief", "execution_date": "2026-10-23",
            "cutoff": AT, "scope": "合成来源绑定测试", "gaps": [], "baseline": None,
            "body": "合成Brief：采用两份研究的增长与日历分歧，未采信任何未引用的切片。",
            "watch_adoptions": [], "sources": [asdict(item) for item in sources],
            "adoptions": [{"reference": asdict(ref),
                           "body_sha256": digest(store.read_revision(ref)["body"].encode()),
                           "used_change": "Keep the specific challenge " + str(index)}
                          for index, ref in enumerate(refs)]}


def test_a_bound_brief_declares_the_exact_union_of_its_adoptions(case):
    store, a, b, first, second, _ = case
    doc = document(store, (a, b), (first, second))
    saved = handoff.save(store, doc, "bound")
    assert saved["source_binding"]["status"] == "bound"
    assert saved["source_binding"]["declared"] == 2
    assert saved["source_binding"]["adopted_union"] == 2
    assert saved["replay"] is False
    assert saved["reference_fixing"]["sources"] == 2
    opened = handoff.open_handoff(VersionedStore(GitStore(store.backend.root)), saved["reference"])
    assert opened["source_binding"]["status"] == "bound"
    # asdict() keeps Selection.columns as a tuple; the stored copy comes back as a
    # list, so compare the round-tripped form rather than Python's container types.
    assert opened["document"]["sources"] == json.loads(json.dumps(doc["sources"]))


def test_omitting_a_cited_source_is_refused_before_the_write(case):
    store, a, b, first, _, _ = case
    doc = document(store, (a, b), (first,))
    head = store.backend.head()
    with pytest.raises(RecordError, match="HOSTED_SOURCE_BINDING_INCOMPLETE"):
        handoff.save(store, doc, "incomplete")
    assert store.backend.head() == head


def test_claiming_a_slice_no_adoption_read_is_refused_before_the_write(case):
    store, a, b, first, second, uncited = case
    doc = document(store, (a, b), (first, second, uncited))
    head = store.backend.head()
    with pytest.raises(RecordError, match="HOSTED_SOURCE_BINDING_UNSUPPORTED"):
        handoff.save(store, doc, "unsupported")
    assert store.backend.head() == head
    # Reading an already existing document is checked the same way, so a
    # hand-edited stored copy cannot claim a slice no adoption read.
    with pytest.raises(RecordError, match="HOSTED_SOURCE_BINDING_UNSUPPORTED"):
        handoff.resolve(store, doc)


def test_duplicate_declarations_are_refused(case):
    store, a, b, first, _, _ = case
    doc = document(store, (a, b), (first, first))
    head = store.backend.head()
    with pytest.raises(RecordError, match="HOSTED_SOURCE_BINDING_DUPLICATE"):
        handoff.save(store, doc, "duplicate")
    assert store.backend.head() == head


def test_a_new_brief_must_be_bound_but_a_replay_keeps_its_own_version(case):
    store, a, b, first, second, _ = case
    legacy = document(store, (a, b), (first, second))
    legacy["schema"] = handoff.WATCH_SCHEMA
    legacy.pop("sources")
    head = store.backend.head()
    with pytest.raises(RecordError, match="HOSTED_SOURCE_BINDING_REQUIRED"):
        handoff.save(store, legacy, "legacy")
    assert store.backend.head() == head
    replayed = handoff.save(store, deepcopy(legacy), "legacy", replay=True)
    assert replayed["replay"] is True
    assert replayed["source_binding"]["status"] == "not-declared"
    # The adopted revisions still fix the bytes even without a declared list.
    assert replayed["source_binding"]["adopted_union"] == 2


def test_binding_costs_one_adoption_read_per_pass_and_no_source_read(case):
    store, a, b, first, second, _ = case
    doc = document(store, (a, b), (first, second))
    counting = _Counting(store)
    handoff.save(counting, doc, "counted")
    # Two passes over the two adoptions: the pre-write gate and the read-back
    # that proves the stored document is satisfiable as stored.
    assert counting.revisions == 2 * len(doc["adoptions"])
    # A slice is fixed by its content address, so binding never reads the bytes.
    assert counting.sources == 0
