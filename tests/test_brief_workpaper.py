"""Structured Quick/Full replay preserves existing JSON bytes, not Python tuples."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application import brief_delivery as brief, versioned_store
from decision_kernel.application.research_agenda import ResearchAgenda
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import encoded, SourceInput, digest
from decision_kernel.domain.research_work import ResearchProgress, ResearchRequest
from decision_kernel.infrastructure.git_store import GitStore
from test_news_delivery import store, index_ref, plan, retain, END


def structured_revision(store, monkeypatch, kind='full'):
    monkeypatch.setattr(versioned_store, '_now', lambda: '2026-10-16T10:00:00Z')
    source = store.put_source('structured-source', SourceInput(
        'structured-source', 'content', '合成资料', 'synthetic:workpaper', '原有证据'))
    request = ResearchRequest.from_dict({'case_id':'structured', 'kind':kind,
        'mandate':'合成原委托', 'questions':[{'question_id':'q1','text':'已回答的原问题'},
                                        {'question_id':'q2','text':'仍待核验的原问题'}]})
    progress = ResearchProgress.from_dict({
        'body':'原作者的经营判断。\n仍须核验经营现金流。\n不是已完成Full。\n',
        'sources':[asdict(source)],
        'findings':[{'question_id':'q1','status':'answered','finding':'限定回答','evidence':[0]},
                    {'question_id':'q2','status':'open','finding':'待核','evidence':[0],
                     'remaining_kind':'acquired-unanalysed','next_action':'继续核算'}],
        'challenges':[{'question_id':'q1','argument':'另一解释','disposition':'unresolved',
                       'response':'待核验','evidence':[0]}],
        'stop':'continue','stop_reason':'仍有重要工作'})
    saved = ResearchWorkflow(store).checkpoint(request, progress,
        submission_id='structured-revision', revision_id='r1')
    return {'reference':saved['reference'], 'start':1, 'end':2}


@pytest.mark.parametrize('kind', ['quick', 'full'])
def test_workpaper_replay_keeps_original_bytes_and_strict_equality(store, monkeypatch, tmp_path, kind):
    item = structured_revision(store, monkeypatch, kind)
    card = ResearchAgenda(store).build({'references':[item['reference']], 'as_of':END})['cards'][0]
    continuation = card['continuation']
    assert continuation['retained_findings'][0]['finding']['evidence'] == (0,)
    assert continuation['unresolved_challenges'][0]['evidence'] == (0,)
    normalized = json.loads(encoded(card))
    assert normalized != card and encoded(normalized) == encoded(card)
    p = plan(index_ref(store), research={'items':[item]})
    doc, logical = brief.build_edition(store.read_source, ResearchArtifacts(store), p)
    assert doc['research']['rows'][0]['context'] == normalized
    assert card['continuation']['unresolved_challenges'][0]['evidence'] == (0,)  # Not mutated.
    old_shape = deepcopy(doc)
    old_shape['research']['rows'][0]['context'] = card
    assert old_shape != doc and encoded(old_shape) == encoded(doc)
    assert brief.brief_text(old_shape) == doc['brief_markdown']
    # Save the PRE-FIX Python shape. Its JSON must reopen without rewriting it.
    saved = retain(store, old_shape)
    head = store.backend.head()
    fresh = VersionedStore(GitStore(tmp_path/'news.git'))
    restored, rows = brief.open_edition(fresh.read_source, ResearchArtifacts(fresh), saved)
    assert restored == doc and encoded(restored) == encoded(old_shape) and rows == logical
    # Also validates tuple-bearing adopted context of an actual v2 predecessor.
    following, _ = brief.build_edition(fresh.read_source, ResearchArtifacts(fresh),
                                      {**p, 'baseline':asdict(saved)})
    assert following['research']['included'] == []
    assert following['research']['omissions']['previously-included-same-version'] == 1
    assert following['comparison']['baseline_body_sha256'] == digest(doc['brief_markdown'].encode())
    assert fresh.backend.head() == head
