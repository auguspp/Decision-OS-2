"""Same source/aggregation owner, compact custody and complete normal page replay."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys

import pytest

from decision_kernel.application import news_rolling as roll, news_rolling_index as idx, rss_intake as rss
from decision_kernel.domain.records import RecordError, SourceInput, digest, encoded
from decision_kernel.interfaces import news_rolling_cli as host
from decision_kernel.interfaces.research_payload import decode_payload, pack_payload
from test_rss_intake import AT, LATER, URL, capture, save, store, xml
from test_rss_news_reading import old_news, put_view

END = "2026-10-16T12:00:00Z"


def jref(ref):
    return json.loads(encoded(asdict(ref)))


def feed_body(size, *, prefix=10001):
    parts=[]
    for n in range(size):
        raw=xml(title="合成统计条目"+str(n), url=URL.replace("10001", str(prefix+n)), summary="合成摘要"+str(n))
        a=raw.index(b"<item>"); b=raw.index(b"</item>")+7
        parts.append(raw[a:b])
    template=xml(); a=template.index(b"<item>"); b=template.index(b"</item>")+7
    return template[:a]+b"".join(parts)+template[b:]


def feed_capture(store, body, feed, name, *, prior=None, at=AT):
    value=rss.collect(store, {"feed_id":feed,"bootstrap":prior is None,
                      "predecessor":jref(prior) if prior else None}, batch_id=name,
                      fetch=lambda _:body, clock_fn=lambda:at)
    return save(store, name, value)


def build(store, refs, **kw):
    return idx.rolling_index(store.read_source, refs, as_of=kw.pop("as_of",END), **kw)


def reseal(value):
    value.pop("index_sha256", None); value["index_sha256"]=digest(encoded(value)); return value


def test_large_joint_window_is_complete_under_existing_wire_limit_and_cold_page_read(store, tmp_path):
    old,_=old_news(store)
    a_body,b_body=feed_body(500),feed_body(500,prefix=20001)
    a=feed_capture(store,a_body,"nbs-releases","a")
    b=feed_capture(store,b_body,"nbs-interpretations","b")
    refs=[old,a,b]
    inline=roll.rolling_news(store.read_source,refs,as_of=END)
    assert len(inline["entries"])<1001 and any(x["kind"]=="BYTE_LIMIT" for x in inline["losses"])
    value=build(store,refs)
    assert value["entry_count"]==1001 and "entries" not in value
    assert len(encoded(value))<100*1024 and value["losses"]==[]
    logical=idx.restore_index(store.read_source,value)
    assert len(logical["entries"])==1001 and len(encoded(logical))>roll.MAX_BYTES
    source=SourceInput(source_id="joint",kind="content",title="合成紧凑窗口",origin="synthetic://index",
                       text=encoded(value).decode(),custody="provided")
    wire=pack_payload(encoded(asdict(source)))
    assert len(wire.encode())<=60*1024
    assert decode_payload(wire)["text"]==source.text
    saved=store.put_source("joint",source)
    assert idx.open_index(store.read_source,saved)==value
    # All filtered channels survive; older news is not displaced by an RSS baseline.
    seen=[]; cursor=None
    while True:
        page=idx.read_index(store.read_source,value,limit=200,cursor=cursor)
        seen.extend(e["observation"]["version_id"] for e in page["rows"])
        assert len(page["source_outcomes"])==4 and page["selected_count"]==1001
        cursor=page["next_cursor"]
        if cursor is None: break
    assert len(seen)==len(set(seen))==1001
    assert idx.read_index(store.read_source,value,source="cls")["selected_count"]==1
    assert idx.read_index(store.read_source,value,source="nbs-releases")["selected_count"]==500
    assert idx.read_index(store.read_source,value,source="nbs-interpretations")["selected_count"]==500
    a2=feed_capture(store,a_body,"nbs-releases","a2",prior=a,at=LATER)
    b2=feed_capture(store,b_body,"nbs-interpretations","b2",prior=b,at=LATER)
    later=build(store,[a2,b2],predecessor=saved)
    assert later["entry_count"]==1001 and later["predecessor"]["status"]=="restored-exact-reference"
    assert all(e["seen_batch_count_in_window"]==2 for e in idx.restore_index(store.read_source,later)["entries"] if e["observation"]["provider"]=="rss")
    exact=put_view(store,later,"later")
    ref_path=tmp_path/"ref.json";ref_path.write_bytes(encoded(jref(exact)))
    run=subprocess.run([sys.executable,"-I","-c",
        "import json,sys; from pathlib import Path; "
        "from decision_kernel.application.versioned_store import VersionedStore; "
        "from decision_kernel.infrastructure.git_store import GitStore; "
        "from decision_kernel.application.news_rolling_index import open_index,read_index; "
        "s=VersionedStore(GitStore(Path(sys.argv[1]))); v=open_index(s.read_source,json.loads(Path(sys.argv[2]).read_bytes())); "
        "print(json.dumps(read_index(s.read_source,v,source='cls')))",str(tmp_path/"rss.git"),str(ref_path)],
        cwd=tmp_path,capture_output=True,text=True,timeout=45)
    assert run.returncode==0,run.stderr
    cold=json.loads(run.stdout)
    assert cold["selected_count"]==1 and cold["rows"][0]["observation"]["provider"]=="newsnow"
    assert cold["coverage"]["retained_versions"]==1001 and cold["coverage"]["failed_source_windows"]==1
    assert cold["index_sha256"]==later["index_sha256"]


def test_compact_and_inline_share_identical_aggregation_when_inline_fits(store):
    old,_=old_news(store); a=save(store,"rss",capture(store))
    legacy=roll.rolling_news(store.read_source,[old,a],as_of=END)
    compact=build(store,[old,a]); restored=idx.restore_index(store.read_source,compact)
    assert restored==legacy
    for source in (None,"cls","nbs-releases","nonexistent"):
        actual=idx.read_index(store.read_source,compact,source=source)
        assert {k:v for k,v in actual.items() if k not in {"snapshot_schema","index_sha256","source_reconstruction"}}==roll.read_rolling(legacy,source=source)


def test_old_inline_predecessor_compact_successor_and_reverse_remain_readable(store):
    old,_=old_news(store); inline=roll.rolling_news(store.read_source,[old],as_of=END)
    prior=put_view(store,inline,"inline")
    a=save(store,"rss",capture(store))
    compact=build(store,[a],predecessor=prior)
    assert compact["entry_count"]==2 and compact["predecessor"]["status"]=="restored-exact-reference"
    saved=put_view(store,compact,"index")
    back=roll.rolling_news(store.read_source,[],as_of="2026-10-16T13:00:00Z",predecessor=saved)
    assert len(back["entries"])==2 and back["predecessor"]["status"]=="restored-exact-reference"
    assert roll.read_rolling(inline)["selected_count"]==1


@pytest.mark.parametrize("change",["entry_count","entries_sha256","logical_sha256","coverage","extra","hours"])
def test_resealed_bad_compact_metadata_cannot_be_read_as_a_valid_window(store,change):
    ref=save(store,"rss",capture(store));value=build(store,[ref]);bad=deepcopy(value)
    if change=="entry_count": bad["entry_count"]=True
    if change in ("entries_sha256","logical_sha256"): bad[change]="0"*64
    if change=="coverage": bad["coverage"]["retained_versions"]=0
    if change=="extra": bad["full_text"]="not a permitted snapshot field"
    if change=="hours": bad["window_hours"]=18
    reseal(bad)
    with pytest.raises(RecordError): idx.read_index(store.read_source,bad)


def test_plausible_resealed_row_count_still_needs_exact_source_replay(store):
    ref=save(store,"rss",capture(store));value=build(store,[ref])
    value["entry_count"]=0;value["coverage"]["retained_versions"]=0;reseal(value)
    idx.verify_index(value)  # Shape is not the semantic proof.
    with pytest.raises(RecordError,match="LOGICAL_REPLAY_MISMATCH"):
        idx.restore_index(store.read_source,value)


def test_missing_source_is_not_empty_page_and_bad_predecessor_does_not_erase_new_news(store):
    ref=save(store,"rss",capture(store));value=build(store,[ref]);prior=put_view(store,value,"prior")
    old,_=old_news(store)
    def reader(r):
        if r==ref: raise RecordError("SOURCE_UNAVAILABLE")
        return store.read_source(r)
    with pytest.raises(RecordError,match="SOURCE_RESTORE_FAILED"):
        idx.read_index(reader,value,query="no-match")
    later=idx.rolling_index(reader,[old],as_of=END,predecessor=prior)
    assert later["entry_count"]==1 and later["predecessor"]["status"]=="unavailable-or-rejected"
    assert any(x["kind"]=="PREDECESSOR_GAP" for x in later["losses"])


def test_compact_replay_accepts_retained_batches_over_new_input_limit_and_alias_is_not_exposure(store):
    refs=[save(store,"r"+str(n),capture(store,ident="r"+str(n),body=xml(url=URL.replace("10001",str(10001+n))))) for n in range(13)]
    first=put_view(store,build(store,refs[:12]),"first")
    value=build(store,[refs[-1]],predecessor=first)
    assert len(value["batch_references"])==13 and idx.read_index(store.read_source,value)["selected_count"]==13
    with pytest.raises(RecordError): build(store,refs)
    dup=build(store,[refs[0],refs[0]])
    assert idx.restore_index(store.read_source,dup)["entries"][0]["seen_batch_count_in_window"]==1


def test_expiration_uses_fixed_as_of_and_does_not_mutate_saved_reference(store):
    old=save(store,"old",capture(store,at="2026-10-15T13:00:00Z"))
    recent=save(store,"recent",capture(store,at=LATER,ident="recent"))
    value=build(store,[old,recent]);saved=put_view(store,value,"snapshot")
    later=build(store,[],as_of="2026-10-16T14:00:00Z",predecessor=saved)
    entry,=idx.restore_index(store.read_source,later)["entries"]
    assert entry["seen_batch_count_in_window"]==1 and entry["first_seen_in_window"]==LATER
    assert idx.open_index(store.read_source,saved)==value
    assert idx.restore_index(store.read_source,value)["entries"][0]["seen_batch_count_in_window"]==2


def test_pagination_binds_reconstructed_logical_snapshot_and_filter(store):
    a=feed_capture(store,feed_body(3),"nbs-releases","page")
    value=build(store,[a]);first=idx.read_index(store.read_source,value,limit=1)
    second=idx.read_index(store.read_source,value,limit=1,cursor=first["next_cursor"])
    assert first["rows"]!=second["rows"] and first["source_outcomes"]==second["source_outcomes"]
    other=build(store,[a],as_of="2026-10-16T13:00:00Z")
    with pytest.raises(RecordError): idx.read_index(store.read_source,other,cursor=first["next_cursor"])
    with pytest.raises(RecordError): idx.read_index(store.read_source,value,source="cls",cursor=first["next_cursor"])
    empty=idx.read_index(store.read_source,value,query="unmatched")
    assert not empty["rows"] and empty["source_outcomes"]==first["source_outcomes"]


def test_entry_and_byte_limits_are_still_explicit_not_silently_increased(store,monkeypatch):
    a=feed_capture(store,feed_body(3),"nbs-releases","budget")
    monkeypatch.setattr(roll,"MAX_OBSERVATIONS",2)
    value=build(store,[a]);assert value["entry_count"]==2
    assert any(x["kind"]=="OBSERVATION_LIMIT" and x["count"]==1 for x in value["losses"])
    assert idx.read_index(store.read_source,value)["coverage"]["retained_versions"]==2
    monkeypatch.setattr(idx,"MAX_REPLAY_BYTES",1000)
    with pytest.raises(RecordError): build(store,[a])
    monkeypatch.setattr(idx,"MAX_REPLAY_BYTES",8*1024*1024)
    monkeypatch.setattr(roll,"MAX_BYTES",len(encoded(value))-1)
    with pytest.raises(RecordError): idx.verify_index(value)


def setup_host(monkeypatch,tmp_path,store,inputs):
    root=Path(__file__).parents[1]
    binding=(root/"config/store-binding.json").read_bytes()
    monkeypatch.chdir(tmp_path);(tmp_path/"config").mkdir();(tmp_path/"config/store-binding.json").write_bytes(binding)
    event=tmp_path/"event.json";event.write_bytes(encoded({"inputs":inputs}))
    for key,value in {"GITHUB_REPOSITORY":host.REPOSITORY,"GITHUB_EVENT_NAME":"workflow_dispatch",
        "GITHUB_REF":"refs/heads/main","GITHUB_WORKFLOW_REF":host.WORKFLOW,"GITHUB_RUN_ATTEMPT":"1",
        "GITHUB_RUN_ID":"123","GITHUB_SHA":"a"*40,"GITHUB_EVENT_PATH":str(event),
        "DOS2_NEWS_MODE":"write" if inputs["save"]=="true" else "read"}.items(): monkeypatch.setenv(key,value)
    monkeypatch.setattr(host.GitHubAPI,"from_environment",lambda:object())
    monkeypatch.setattr(host,"GitHubStore",lambda *args:object())
    monkeypatch.setattr(host,"VersionedStore",lambda _:store)


def test_native_read_operation_replays_exact_store_without_calling_writer(store,tmp_path,monkeypatch,capsys):
    old,_=old_news(store);a=save(store,"rss",capture(store));value=build(store,[old,a]);ref=put_view(store,value,"index")
    inputs={"code_sha":"a"*40,"operation":"read","save":"false","plan":json.dumps({"reference":jref(ref),"source":"cls","limit":1})}
    setup_host(monkeypatch,tmp_path,store,inputs)
    monkeypatch.setattr(host,"run_command",lambda *a,**k:pytest.fail("read cannot write"))
    out=tmp_path/"out";assert host.main(["build","--out",str(out)])==0
    result=json.loads((out/"reading.json").read_bytes())
    assert result["selected_count"]==1 and result["coverage"]["retained_versions"]==2
    assert result["snapshot_reference"]==jref(ref) and result["source_reconstruction"]=="verified-exact-selected-batches"
    assert json.loads((out/"view.json").read_bytes())==value
    assert json.loads(capsys.readouterr().out)["saved_requested"] is False


@pytest.mark.parametrize("save_status,expected",[("published",0),("unknown",2)])
def test_native_build_passes_only_compact_metadata_to_existing_writer_and_honors_receipt(store,tmp_path,monkeypatch,save_status,expected):
    ref=save(store,"rss",capture(store))
    inputs={"code_sha":"a"*40,"operation":"build","save":"true","submission_id":"host-save",
            "expected_head":"b"*40,"public_metadata_custody_confirmed":"true",
            "plan":json.dumps({"as_of":END,"batch_references":[jref(ref)]})}
    setup_host(monkeypatch,tmp_path,store,inputs);calls=[]
    def writer(binding,**kw):
        calls.append(kw);payload=decode_payload(kw["inputs"]["payload"])
        SourceInput.from_dict(payload)
        value=json.loads(payload["text"])
        assert value["schema"]==idx.SCHEMA and "entries" not in value and value["entry_count"]==1
        assert len(kw["inputs"]["payload"].encode())<=60*1024 and kw["inputs"]["expected_head"]=="b"*40
        return {"status":"completed" if save_status=="published" else "failed","write":save_status}
    monkeypatch.setattr(host,"run_command",writer)
    assert host.main(["build","--out",str(tmp_path/"out")])==expected and len(calls)==1


@pytest.mark.parametrize("saving,plan",[("true","{}"),("false",'{"reference":{},"as_of":"2026-10-16T12:00:00Z"}')])
def test_native_read_cannot_become_save_or_invent_new_as_of(store,tmp_path,monkeypatch,saving,plan):
    inputs={"code_sha":"a"*40,"operation":"read","save":saving,"submission_id":"invalid" if saving=="true" else "",
            "public_metadata_custody_confirmed":"true","plan":plan}
    setup_host(monkeypatch,tmp_path,store,inputs)
    monkeypatch.setattr(host.GitHubAPI,"from_environment",lambda:pytest.fail("invalid operation must not reach backend"))
    out=tmp_path/"out";assert host.main(["build","--out",str(out)])==2 and not out.exists()


def test_offline_legacy_command_does_not_fake_empty_compact_read(store,tmp_path,capsys):
    ref=save(store,"rss",capture(store));value=build(store,[ref]);path=tmp_path/"index.json";path.write_bytes(encoded(value))
    assert host.main(["read","--window",str(path)])==2
    assert json.loads(capsys.readouterr().err)["error_code"]=="NEWS_INDEX_REQUIRES_EXACT_SOURCE_READER"


def test_manual_workflow_read_write_boundary_and_no_new_source_permissions():
    import yaml
    text=(Path(__file__).parents[1]/".github/workflows/news-rolling.yml").read_text()
    flow=yaml.load(text,Loader=yaml.BaseLoader)
    assert set(flow["on"])=={"workflow_dispatch"}
    assert flow["on"]["workflow_dispatch"]["inputs"]["operation"]["options"]==["build","read"]
    assert "inputs.operation != 'read'" in flow["jobs"]["save"]["if"]
    assert flow["jobs"]["build"]["permissions"]=={"contents":"read","actions":"read"}
    assert "schedule:" not in text and "TUSHARE" not in text
