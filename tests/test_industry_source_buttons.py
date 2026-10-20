"""Actual offline industry source buttons through the existing native read host."""
from dataclasses import replace
import json
import shutil
import subprocess

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import SourceRef, encoded
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import reading_commands, research_runtime
from test_industry_room import plan, root_for
from test_news_read_budget import transport
from test_research_runtime import CODE, host, inputs


def source_buttons(page):
    """Execute the component actually included in the native HTML, using a DOM sink."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("offline button execution requires the existing optional Node runtime")
    payload = page.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    script = page.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    start = script.index("function renderIndustryUses(")
    component = script[start:script.index("renderIndustry();", start) + len("renderIndustry();")]
    prefix = r'''
const D=JSON.parse(require('fs').readFileSync(0,'utf8'));
class E{constructor(tag,text,cls){this.tag=tag;this.textContent=text===undefined?'':String(text);this.className=cls;this.value='';this.children=[];}
append(...n){this.children.push(...n);}replaceChildren(...n){this.children=n;}get firstChild(){return this.children[0];}addEventListener(){}}
const pane=new E('div'),buttons=[],exports=[],$=id=>id==='industry-content'?pane:null;
const el=(tag,text,cls)=>new E(tag,text,cls),details=(title,value)=>el('details',JSON.stringify(value));
const button=(text,run)=>{const b=el('button',text);b.run=run;buttons.push(b);return b;};
const request=(name,value)=>exports.push({name,...value}),label=x=>x,save=()=>{};
'''
    suffix = r'''
for(const b of buttons)if(b.textContent==='导出此来源读取请求'&&!b.disabled)b.run();
process.stdout.write(JSON.stringify(exports));
'''
    run = subprocess.run([node, "-e", prefix + component + suffix], input=payload,
                         text=True, capture_output=True, timeout=20)
    assert run.returncode == 0 and run.stderr == ""
    return json.loads(run.stdout)


def original_references(view, kind):
    groups = view["uses"] if kind == "industry-uses" else [view]
    refs = []
    for group in groups:
        refs.extend(e["reference"] for e in group.get("evidence", []))
        refs.extend(p["reference"] for series in group["series"] for p in series["observations"])
        refs.extend(r["evidence"]["reference"] for r in group["relations"])
    return refs


def test_original_industry_and_six_use_source_buttons_reopen_exact_native_rows_and_lines(host, tmp_path):
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    roots = {kind: root_for(writer, kind) for kind in ("industry", "industry-uses")}
    before, writes = host.git.head(), host.writes
    for private in (False, True):
        if private:
            host.binding = replace(host.binding, private_upgrade_allowed=True)
            host.meta["private"] = True
        for kind, root in roots.items():
            destination = tmp_path / (("private-" if private else "public-") + kind)
            value = {"plan": plan(root, kind)}
            if not private:
                value.update(public_research_custody_confirmed=True, public_sources_custody_confirmed=True)
            reading_commands.preflight("reading-room", value, public=not private)
            wire = transport(host)
            report = research_runtime.run_command(host.binding, api=wire, inputs=inputs("reading-room", value),
                                                 code_sha=CODE, output=destination)
            assert report["status"] == "completed" and report["write"] == "not-requested"
            assert report["repository_visibility"] == ("private" if private else "public")
            assert 0 < wire._requests < 512
            model = report["result"]
            assert model["industry"]["view"]["source_requests"] == model["industry"]["view"]["writes"] == 0
            page = (destination / "reading-room.html").read_text()
            embedded = page.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
            assert json.loads(embedded) == model
            exports = source_buttons(page)
            expected = {encoded(ref) for ref in original_references(model["industry"]["view"], kind)}
            assert len(exports) == (21 if kind == "industry-uses" else 4)
            assert {e["name"] for e in exports} == {"industry-use-source.json" if kind == "industry-uses" else "industry-source.json"}
            seen, values, selection_kinds = set(), set(), set()
            for index, export in enumerate(exports):
                assert export["operation"] == "read-source"
                identity = encoded(export["payload"])
                if identity in seen:
                    continue
                seen.add(identity)
                # run_command performs the existing dispatch/preflight/perform
                # chain. The former extra {reference: ...} fails here.
                read = research_runtime.run_command(host.binding, api=transport(host),
                    inputs=inputs(export["operation"], export["payload"]), code_sha=CODE,
                    output=destination / ("source-" + str(index)))
                assert read["status"] == "completed" and read["write"] == "not-requested"
                assert set(export["payload"]) == {"manifest", "selection"}
                ref = SourceRef.from_dict(export["payload"])
                expected_read = VersionedStore(GitHubStore(host.binding, transport(host))).read_source(ref)
                assert encoded(read["result"]) == encoded(expected_read)
                persisted = json.loads((destination / ("source-" + str(index)) / "report.json").read_bytes())["result"]
                assert encoded(persisted) == encoded(read["result"])
                selection_kinds.add(ref.selection.kind)
                if ref.selection.kind == "rows":
                    assert len(read["result"]["data"]) == 1
                    values.add(read["result"]["data"][0]["value"])
                else:
                    assert ref.selection.kind == "lines" and ref.selection.start == ref.selection.end
                    assert "Synthetic filing, NOT a real issuer." not in read["result"]["data"]
            assert seen == expected and selection_kinds == {"rows", "lines"} and values == {"100", "110"}
    assert host.writes == writes and host.git.head() == before
