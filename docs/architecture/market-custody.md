# Market source custody and exact reading recovery

This capability closes the persistence gap between the existing Relay capture
results and the existing market readers. It does not acquire sources, select
winners, grant publication rights or turn synthetic tests into market evidence.

Durable SH/SZ daily acquisition, execution checkpoints, incremental collections,
private native reading and their explicit runtime are described in
[A-share data continuity](ashare-data-continuity.md). The existing save/read
entry remains supported; its normal read dispatcher also accepts exact collection
roots without concatenating multi-day data into the old payload envelope.

Collection custody reads both `market-collection-custody-v1` and the explicit
`market-collection-custody-v2` used for the single bounded terminal repair in
the [continuity contract](ashare-data-continuity.md#terminal-repair). The v2 root
retains the original failed units and an immutable grant, with separate repair
checkpoint/query references. Grant creation binds the exact predecessor and
original terminal references; subsequent retention, restoration and unique-chain
inspection reject grant replacement, removal, downgrade and a second episode
across generations. The original capture checkpoint remains v1, and its terminal
restart guard is unchanged.

Repair pages and completed queries use the existing exporters, exact references,
qualification and independent readers. Only a fully restored repair query can
be selected for its original slot. Original failures remain available alongside
effective state and repair provenance; an additional budget is not presented as
unused original allowance. Normal readers neither create grants nor acquire
sources. Native transport-run identity and execution-code continuity are checked
by the trusted explicit runtime, not by a source document claiming verification.

## Reuse and ownership

`market_custody` uses `research_sources.source_inputs` as the **only page exporter**,
`VersionedStore.put_source/read_source` as the **only custody writer/reader**, and
the existing Git/GitHub backends for identity, CAS, immutable versions and same
submission reconciliation. No source HTTP client, alternate store, generic graph
engine, scheduler or dependency is introduced. Standard-library JSON and hashes
compose the existing contracts; another archive/database library would duplicate
the already delivered storage and source-qualification responsibilities.

The old four-schema CLI dispatcher is moved into
`market_reading.read_market_payload`, shared by the unchanged local reading
interface and this restored-reading consumer. The original raw, security, THS
and three-surface schemas remain accepted. Business qualification, calendars,
raw/qfq arithmetic, member time limits and pagination stay with their owners.

## Saved representation, not reconstructed originals

`retain_market_reading(payload, registry, store, fresh_reader=...,
custody_authorized=True, public_rights_confirmed=False)` accepts the **existing** reading payload, containing
full capture results, without adding a source schema to the provider. It preflights
all supplied captures and the reading before any write. Supply only complete
captures; incomplete capture diagnostics are not silently turned into zero rows
or a successful archive. Omitted series remain omitted. Complete empty captures
are archived as complete empty sources, distinct from absence.

The immutable dependency chain is:

```
reading SourceRef -> query SourceRefs -> existing per-page excerpt SourceRefs
```

Page text is exactly the current `source_inputs` export, with its existing custody
level, source identity, query parameters, acquisition clock, projection and
transport hash. Query manifests retain original case/use/snapshot metadata,
inspected-view hash and exact page references with SourceInput digests; reading
manifests contain the existing reading configuration with query slots. Neither
manifest copies table rows. Reused source pages and shared queries are retained
once through the same submission journal. All objects live under the existing
`source-views` layout; no new authoritative path or mutable current pointer exists.

Manifests themselves are generated provided content. They are not original Relay
responses. Restoring creates an explicitly labelled **reading envelope**; original
wire bytes, unrequested columns and execution/attempt diagnostics are not restored.
The envelope's complete flag is independently rechecked by the existing inspector,
not accepted on the authority of a saved boolean. Its inspected-view hash must
match the archived one, and regenerated SourceInputs must exactly match the saved
page metadata/text. SourceRef integrity is not publisher authentication, semantic
qualification, historical PIT, a complete market or investment approval.

## Failure, consistency and permissions

Each source write uses a stable ID derived from the exact SourceInput, through the
existing journal and CAS backend. A failed or unknown write stops the operation;
there is no automatic write retry, new submission key, rollback deletion or
claim that all steps are atomic. Earlier saved pages can remain. Reinvoke the
identical retained payload only after reconciling the original operation; the
existing writer replays confirmed submissions instead of overwriting history.

Queries are checked by a newly instantiated reader before the final reading
manifest is saved. The final manifest and every dependency are then read again
through another new reader, and the resulting default-page view is compared with
the original reading. Only that outcome is `stored-and-independently-restored`.
A final-read failure does not prove the preceding write did not happen.

`read_stored_market` follows exact SourceRefs, requiring whole stored
representations, the same repository and compatible recorded registry snapshot.
It rechecks query association, per-page source metadata, digest, ordering,
completeness and inspected-view identity. It never substitutes the current
registry, latest data or a new provider request to repair a missing old version.
The input manifest is required; a corrupt root rejects the read.

A missing or refused **query** becomes an explicit unavailable-source outcome.
Its input slot remains present and rejected, not omitted or accepted empty. The
existing three-surface reader isolates the dependent surface; a lost industry
catalogue does not block a valid concept or stock surface. A single reader whose
shared calendar is missing returns `view=null` and a separate reading error.
Partial restoration returns nonzero from the CLI even when peer surfaces remain
readable. Paging never changes the unpaged source/market denominators.

The caller must establish source-specific access and custody authorization.
`custody_authorized` permits retention only in the natively checked private
store (or an explicitly synthetic test store). It does not assert public
redistribution rights. The separate `public_rights_confirmed` flag remains false
unless those rights have actually been established; a public target still
requires that declaration. Both use the existing `authorize_market_custody`
checks, and a source-supplied visibility value cannot choose a target.
Engineering permission, an enabled Relay endpoint or purchasing access alone
does not establish publication rights. The same repository's `records` branch
and shared credentials do not provide role isolation. Credentials, private
execution diagnostics and unlicensed full text are not admitted by this adapter.
The runtime strips unretained capture diagnostics **before writing input artifacts**.

## Explicit consumers

The module CLI uses an existing synthetic local store or a trusted GitHub binding:

```sh
python -m decision_kernel.interfaces.market_custody_cli save \
  --binding authorized-write-binding.json --registry docs/sources/relay-registry.json \
  --input retained-market-reading.json --custody-authorized

python -m decision_kernel.interfaces.market_custody_cli read \
  --binding config/store-binding.json --registry docs/sources/relay-registry.json \
  --input saved-root-sourceref.json --limit 50
```

Save returns `reference` (the root SourceRef), counts and independent readback.
The read input is that exact `reference`, not the complete save response. The
capture file can disappear after successful custody; it is not a recovery input.
For `--path`, use only an already initialized **synthetic** bare GitStore. The
CLI never initializes or selects a remote store from source text. Remote writes
also require the run-owner binding's write-enabled state; no fallback engineering
token is used. The read path uses a read-only binding.

The manual `market-custody.yml` host supports the same save/read operations at an
exact current main SHA. It reuses the existing trusted repository binding,
`dos2-store-runtime` concurrency group, pinned installation action and separate
contents:read/contents:write jobs. It receives no Relay secret, has no cron,
validates JSON rather than interpolating input into shell, and preserves actual
results/unknown attempts. A green workflow without the expected successful
operation result is not a custody receipt. Run receipts and temporary artifacts
are retained for seven days; immutable records are the source of recovery, not
those temporary artifacts. No guarantee of permanent disaster recovery is added.

The manual host accepts independent `custody_authorized` and
`public_rights_confirmed` inputs and forwards their actual values to the source
or discovery CLI. It does not fill publication permission on behalf of a private
save. Read/build jobs remain read-only, and neither provider's credentials are
passed to these source-free consumers. Native private visibility is checked again
before releasing the operation artifacts.

The source-only read factory reuses `GitMarketReader` with the same `GitHubAPI`
instance as the original writer. Query checks and the final reading each use
independently pinned Git snapshots. The final root and all dependencies share
one bounded read scope, followed by its native reachability/private check; this
does not repeat live-head requests for every immutable blob. Before releasing
the successful result, the original writer checks native visibility again,
preserving this operation's observed private upgrade across new reader scopes.
The original 512
API limit and Git download/capacity bounds remain unchanged. Global access or
request-budget failures stop the operation immediately; genuinely missing query
objects still retain the original dependent-surface isolation.

## Source acquisition and remaining production acceptance

Acquisition remains `relay-source-runtime.yml`, `family=research`, and its existing
`research-source-plan-v1`, followed by `relay_research_cli inspect` when useful.
Use actual returned query results in the appropriate existing market payload; do
not copy sample securities, calendars or invented transport hashes from tests.
Actual capture and public retention each require their applicable authorization.
No new market source endpoint is admitted by this custody feature.

This group can be installed and tested without a live market request. Real
source calls, upstream field availability, source-specific public rights,
production SourceRefs, independent remote read adoption and subsequent #7/#13
use still require their own actual receipts. CLI or workflow existence is not
proof that any of those operations has occurred. Historical members, broader
classification, independent discovery, corporate actions and global background
remain separate #10 responsibilities.

## Bounds, maintenance and exit

The direct reading input remains 2 MiB. This initial custody unit supports up to
32 distinct supplied queries and 100 pages per query, with bounded query and
restoration memory. The manual Actions JSON field is further bounded to 60,000
UTF-8 bytes. These are explicit limits of this reading/retention unit, not a
claim of full-market coverage. An oversized capture is refused; do not relabel a
truncated scope as complete. Larger ingestion is a successor tied to the existing
store/API limits, not permission to add a second database. References may need
multiple reads and consume the existing backend's finite request budget.

`tests/test_market_custody.py` owns complete/empty/missing sources, immutable old
versions, deduplication, query/page substitutions, unknown writes and same-intent
recovery, independent local Git/new-process reading after removing the capture
file, and the manual host's permission/identity/diagnostic boundaries. Its
fault-injectable in-memory backend is only a test fixture. Real registry, capture,
SourceInput and VersionedStore code is exercised with synthetic source data.

Retire custody module/CLI/workflow, exclusive tests and this document together
when the capability exits. Keep the shared market dispatcher/local readers,
source exporter, storage protections and shared test opener while consumed.
No cleanup is introduced: nested query/page references must remain retained,
even when they are not directly listed in a ResearchRevision. Existing snapshot
storage covers the same paths; transitive source restoration and long-term backup
acceptance must not be inferred merely from a generic backup job's green result.
