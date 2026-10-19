# Stateful market discovery

## Responsibility and prior art

Issue #10 restores a stateful observation responsibility for industry, concept
and independently selected stocks. The three surfaces retain separate original
catalogues, calendars, coverage counts, dates and failures. Industry membership
can describe a source classification; it cannot manufacture an independent
stock price series or prove that a stock is exposed to a business theme.

The useful Kernel prior art is sector_radar_state.py, sector_radar_events.py and
the path observation in theme_radar_probe, audited at
cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8. These supplied the responsibility for
persistent state, transitions and immutable first event evidence. The initial
OS2 policy is deliberately explicit: close-path20-positive5-accel5-v1. The
available retained close data does not implement Kernel's top-decile, turnover,
breadth or fundamental discovery criteria. This view carries no investment or
research authority and does not rank a supplied subset as the whole market.

## Admitted inputs

MarketDiscovery(store, registry=None, source_store=None) provides build(plan),
save(submission_id, plan) and read(reference). build is read-only. save uses
ResearchArtifacts and the original VersionedStore journal/CAS, then a fresh
source replay. read takes an exact Artifact reference and validates the
materialized view and original event evidence. No source adapter is called for
acquisition by any of these operations.

A plan contains only the versioned policy, an explicit declared observation
clock, exact whole-source references, an optional predecessor Artifact and an
optional stream identifier. Prices, caller state, scope counts, comparison flags
and precomputed transitions are not admitted.

~~~json
{
  "schema": "market-discovery-plan-v1",
  "policy": "close-path20-positive5-accel5-v1",
  "stream": "manual",
  "as_of": "2026-10-19T12:10:00+00:00",
  "surfaces": {
    "sector": {"reference": "<exact retained industry reading SourceRef>"},
    "concept": {"reference": "<exact retained concept reading SourceRef>"},
    "stock": {
      "reference": "<exact retained FTShare collection SourceRef>",
      "adjustment": "forward"
    }
  },
  "previous": null
}
~~~

The reference placeholders above stand for the existing structured SourceRef
and Artifact reference objects, not paths or mutable latest selectors. At least
one surface is required. Stock adjustment is forward by default; raw is also
admitted. A predecessor must have the same stream. The default stream is
manual. The daily writer uses daily-market.

FT inputs are restored through the existing FTShare collection and original
single/batch capture owners. Completion policy, request parameters, wire hashes,
capture clocks, listing dates, original security master and real exchange
calendars are checked. A collection source hold is preserved. Missing or
quarantined securities remain in the full source master scope even when no
prices were requested for them.

THS inputs are restored through restore_market_reading_inputs, extracted from
the original market custody reader without adding a store. The original A/I
industry or A/N concept catalogue, qualified open sessions and retained
ths_daily requests are verified. Member queries, when present, retain literal
source relationships. They do not become extra state inputs. Each surface
keeps its own actual cutoff. An industry data date does not silently become a
stock data date. This policy uses absolute paths, so incompatible benchmarks or
calendars are never joined to produce excess performance.

For a native GitHubStore, source restoration uses the existing source-only
GitMarketReader. Artifact reads remain on GitHubStore. Both use the same native
binding, repository and GitHubAPI budget. Supplying a GitMarketReader is also
supported without expanding its source-only ACL; the Artifact companion uses
the same original binding and API. An independent credential or API cannot be
introduced through source_store.

## Policy and transitions

The 20-session change at a session is its qualified close divided by the close
20 actual open sessions earlier, minus one. Every close in that 21-session
window must exist. A persistence run counts consecutive sessions with positive
20-session change. It must cover at least five sessions. The acceleration
observation is the current 20-session percentage change minus the same measure
five actual sessions earlier, in percentage points.

| State | Qualified observation |
| --- | --- |
| not-entered | Nonpositive 20-session change, or a known positive run shorter than five sessions |
| persistent | At least five consecutive positive 20-session changes, without positive/negative acceleration classification |
| accelerating | Persistent path, positive acceleration and positive 5-session change |
| weakening | Persistent path with negative acceleration or nonpositive 5-session change |
| exited | A previously active comparable observation becomes nonpositive over 20 sessions |
| unknown | Required dates/prices or the history needed to distinguish a young run from a long run are unavailable |

The first computable observation records a baseline event. It does not claim
that the original historical entry was detected prospectively. A positive run
touching the left boundary is labelled persistence_left_censored. A missing
price interrupts all dependent windows. The state can become unknown; absence
of a row or request cannot create an exit.

A new entry, weakening, acceleration or exit requires comparable observations
on consecutive actual sessions. The complete source-qualified identity and
convention contract must agree. The overlapping calendars and normalized price
history must match. Revised forward-adjusted history, a same-day correction,
a changed source catalogue contract, reverse dates or a missed observation
session produce an explicit new baseline instead of a continuous entry claim.
A same-session repeat emits no new original event. Continuing state preserves
the original entry and latest material event.

A source scope departure is visible as departed_scope. It cannot prove an
economic exit. A previously observed but currently unrequested identity
remains an unknown state with its original event evidence retained.

## Exact event evidence and persistence

Artifact identifiers begin with market-discovery-. Their stable family digest
includes policy, stream and selected surfaces/conventions. Original source
versions and the immutable document digest distinguish editions through the
existing Artifact path schema. No mutable state pointer, new branch family,
database or second journal is introduced.

The materialized snapshot includes its plan, three surface summaries, detailed
states for supplied price series, gaps for the remaining identified source
scope, original events and bounded proof descriptors. Each event identifies its
original current and predecessor source proof, original session, policy,
metrics and comparison semantics. Original events are rebuilt with the pure
state function from those exact source versions.
Every event explicitly labels its evidence as retained-source-replay and sets
timely_discovery_claim to false; a replayed transition cannot impersonate a
historically timely discovery.

Checking a JSON digest is insufficient. On read, each retained proof reloads
and qualifies its original source objects and recomputes its source view hash.
Each event is recomputed from its original qualified pair. Current row metrics,
state, name, date and event links are checked. A computable row must retain
both its original baseline/entry and latest event, with ordered event dates.
Before build/save/read adopts a predecessor, that predecessor's own direct edge
is also replayed. The whole predecessor snapshot must match its reconstruction
from the next earlier materialized snapshot and exact original source proofs.
The current direct edge is then replayed and the whole current snapshot must
match. Removing original links, substituting an earlier valid baseline, or
changing a row, policy or event cannot pass merely by rehashing JSON and Artifact.

The current snapshot retains the original entry/baseline and most recent
material event needed by current rows. Superseded events remain in immutable
predecessor Artifacts. This bounds evidence replay without recursively loading
every daily ancestor: a build inspects at most two predecessor Artifacts, and an
exact read additionally inspects its target Artifact. Every inspected original
event still requires its exact retained source proof pair. Missing original
proof bytes cause an explicit failure;
current source rows are never substituted as proof of the earlier event.
The daily inventory separately validates the finite saved chain topology.

as_of is the declared calculation/source cutoff clock, and event at is the
actual session of the observation. Neither claims a provider uptime record or a
historically timely first discovery. The native Artifact manifest does not
contain a first-recorded clock, so read returns stored_at as null. The exact
native commit and immutable bytes establish persistence; callers cannot choose
a fictional wall clock and relabel it as native save time.

## Daily writer and normal reader

The existing manual market-custody workflow also exposes discovery-build,
discovery-save and discovery-read. It accepts a plan for build/save and an exact
Artifact reference for read. Build/read have read-only native permissions;
save is an explicit authorized operation through the original Artifact journal.
The runtime launches the source-free market_discovery_cli with the trusted
binding and registry, never provider credentials or source-supplied code.
Both jobs check the native private repository before releasing their exact
inputs/results. A failed or interrupted child retains an unknown/partial
receipt instead of claiming that no write happened.

The same custody workflow can retain a complete THS input without trimming its
catalogue to the price sample. It reuses research_payload's gzip-json-v1
transport: at most 60,000 UTF-8 transport bytes and the original 2 MiB decoded
limit, one gzip member and a verified canonical JSON digest. Decompression
does not grant rights; normal market projection and source qualification run
before an input artifact or source write. Execution diagnostics are removed
from retained public-fact projections by the original custody owner.

The standalone source-free command is:

~~~bash
python -m decision_kernel.interfaces.market_discovery_cli read \
  --binding config/store-binding.json --input exact-state-reference.json
~~~

An explicit save additionally uses the run owner's write-enabled binding,
submission identifier and `--custody-authorized` private custody confirmation.
The manual host forwards its independent `custody_authorized` input without
setting `public_rights_confirmed`; the latter retains its actual public-rights
meaning. Private retention does not confer public redistribution rights, and the
discovery writer continues to require the native private target. Source input is always an
exact SourceRef; the command cannot accept arbitrary caller prices or states.
Normal room plans accept discovery.reference or discovery.build, exactly one,
and an optional kind, entity_id, offset and limit (1 to 50). Filtering changes
the displayed rows only. All three full source scopes, unknown counts and
original event proofs remain visible. Reading an exact saved Artifact replays
its sources; a build preview is labelled not-written and has no saved reference.

The existing daily-market workflow remains the sole daily source writer.
After its source runtime, daily_discovery_cli reads that same native run's
canonical result.json and control.json with only the existing job token. It
verifies current private visibility, native main identity and the successful
main CI before work, immediately before a new state write, and before release.
The original records anchor and permissions remain unchanged.

Only complete, already-complete or no-new-session source results can admit a
state write. The original daily cycle and exact current market reference must
match the source receipt/control. The actual FT collection must successfully
restore, have no source hold, and retain its selected complete original price
captures and actual calendar cutoffs. Caller assertions of success cannot
replace these checks. No provider credentials or source requests are added.

The daily-market stream is selected by a complete bounded inventory of legal
market-discovery-* Artifacts in research/artifacts/. All candidate headers must
be canonical and valid before the stream can be selected. Exactly one connected
chain with one root and one leaf is required. A damaged candidate, unknown
stream membership, missing predecessor, disconnected history, fork or future
clock stops selection. It never silently chooses an older healthy candidate.
The complete inventory uses the existing native reading_scope at one checked
snapshot, sharing the original immutable caches and API. It rechecks native
privacy and reachability before releasing the result. It does not repeat live
head checks for each manifest/content read or reset the 512-request budget.
A global transport, rate or request-limit failure stops immediately instead of
being collected as candidate damage while further requests continue.

Before saving, the original Artifact CAS must still use the inventory's
checked records head. A concurrent records change aborts the admission. This
reuses the existing transaction boundary rather than claiming a distributed
lock across manual writers. A manual operator adding another daily-market root
will produce an explicit fork that needs review.

The new plan carries previous industry/concept exact source references and
their actual dates. The daily FT update does not imply those sources refreshed.
With no saved daily state, the first qualified update creates a stock baseline.
A manual discovery save may seed a three-surface daily-market baseline first.
Once the same immutable FT version has been saved, another qualified run
revalidates the actual source and returns already-recorded without another
state write. Updating group inputs alone remains an explicit manual save.

Source failures leave prior persisted state intact and do not save a healthy
replacement. The original failure receipt remains in daily reading. A newer
failed FT collection can appear only as an explicitly unsaved current preview
with unknown/held observations. A saved state whose current market could not be
resolved is labelled unconfirmed against the current market.

daily_reading_cli stays read-only. The normal ReadingRoom receives either an
exact saved Artifact selector or an explicit build plan marked not-written. It
shows all three scope summaries, actual data dates, unknown counts and original
events through the shared discovery panel. Daily human summaries distinguish
saved state, unsaved preview and failed state persistence. Brief and research
reading retain their original ownership and do not execute new research.

## Bounds, verification and ownership

| Bound | Admission |
| --- | --- |
| Plan bytes | 16 KiB |
| Full identified source scope | 10,000 identities per surface |
| Qualified source sessions | 512 |
| Retained event source proofs | 64 |
| Retained original events | 20,000 |
| Immutable Artifact bytes | Existing 1 MiB limit |
| Daily legal Artifact inventory | 128 candidate editions, all inspected |
| Native API operations | Existing 512 requests per API instance |
| Reading panel | Existing shared projection bound, 768 KiB |

All bounds are checked before a new Artifact write. Detailed series/state is
stored only for actually supplied price identities; other source identities
remain explicit gaps. Exceeding a bound refuses the whole state write. There is
no hidden truncation, selected-subset denominator or automatic capacity increase.
The native transport and its existing rate/visibility failures remain final.

The pure tests cover consecutive entries, persistence, acceleration, weakening,
exits, left censoring, missing windows, corrections and contract changes.
Application tests use original synthetic-qualified custody and FT adoption,
the real Artifact journal, a temporary Git restore, rehashed tampering,
original source loss, capacity and the actual GitHubAPI/GitMarketReader shape.
Daily tests drive the existing FT daily Host through successful and failed
cycles, preserve all master quarantines, carry exact THS dates, reject forks and
stale CAS, check native identity/privacy and exercise the normal saved reader.
Synthetic provider results are test evidence only; real retained-source replay
and new native production acceptance are separate evidence.

This responsibility is owned by the market observation/application pair and
the existing daily host. Removing it removes its state reader/writer integration;
it does not replace or retire original source custody, VersionedStore, Brief,
research workflows, records identity, native privacy checks or shared CI gates.

