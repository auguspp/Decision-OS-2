# THS industry/concept inputs and three independent market readers

## Scope and reuse

`application/market_group_inputs.py` consumes **retained complete results** from
`research_sources.capture_plan` with the existing `RelayRegistry`. It adds the
source-specific THS A-market `I` industry and `N` concept slices, reported member
rows and index-level reading. It does not collect data or certify discovery.
The [existing market adapters](market-relay-inputs.md), `inspect_result`, numeric
policy, calendar windows, pagination and `membership_overlap` remain the owners
of their existing contracts. No HTTP client, registry, source store, scheduler,
third-party dependency or shared CLI route is added.

Official parameter definitions were checked on 2026-10-16:
[ths_index](https://tushare.pro/document/2?doc_id=259),
[ths_member](https://tushare.pro/document/2?doc_id=261), and
[ths_daily](https://tushare.pro/document/2?doc_id=260). The existing
[Relay manual](../sources/vendor/tushare-relay-v0.5.61.md) and byte-bound registry
supply transport and admission, not business qualification or permission.
Published fields and an enabled catalogue entry do not prove a real invocation.
This engineering group uses only synthetic responses through the actual existing
HTTP/Retry/capture implementation; it makes no external market requests.

## Classification and identities

`qualified_ths_catalog(result, registry=..., kind="sector"|"concept")` requires an
entire zero-origin `ths_index` slice with exactly `exchange=A,type=I` or
`exchange=A,type=N`, plus `fields`, `limit` and optional zero `offset`.
Requested columns must include `ts_code,name,count,exchange,list_date,type`.
Extra filters, including a hand-selected code or name, are refused. All rows in
that slice become the denominator; missing index or member inputs do not remove
catalogue rows. The ordinary shared inspector verifies pagination and transport.

Each source code must be six digits plus `.TI`, unique in its slice, with the
matching exchange/type. Names are bounded printable strings; duplicate names are
allowed and never merged. A reported member count may be a nonnegative integer
or digit string, or an exactly integral finite JSON floating number in the same
0..999999999 range; null/empty is **unknown**, not zero. No fractions are rounded,
negative/nonfinite/boolean/over-range counts remain invalid, and decimal strings
are not newly admitted. Normalized counts are Python integers; original captures
and their lineage remain unchanged, so equivalent values with different source
representations retain different source versions. A supplied list date must be
canonical compact YYYYMMDD. The date is retained as reference metadata, not a
claim that any backfilled index history existed or was knowable at launch.

Reading IDs are scoped as `ths-a-sector:881001.TI` and
`ths-a-concept:885001.TI`. These are source-scoped reading keys, **not canonical
Entity or issuer IDs**. Source version, classification version, member version
and calendar version remain separate. This does not merge THS with SW, DC, TDX
or another classification, and does not infer type from the index name/prefix.
Regional, thematic and other THS types are outside this group. The catalogue is
observed at its capture clocks, not an atomic historical/full-market universe.

## Count representation compatibility

The non-publishing diagnostic run `38864296571/1` on 2026-10-16 localized both
THS directory rejections to `MARKET_RELAY_GROUP_COUNT`: all non-null counts were
integral floating-number representations. This is evidence of that representation
mismatch, not evidence that every other row qualification passes. The official
`ths_index` field remains an integer member count. Only exact, finite, nonnegative
integer values within the previous nine-digit bound are accepted; e.g. synthetic
`1.0` normalizes to `1`, but `1.5` is rejected. No blanket float-to-int coercion,
source-row filtering, missing-to-zero fill or changes to shared JSON parsing occur.

The original capture bytes are not rewritten. Version and transport lineage are
computed over the original inspected representation, not a fabricated int-only
capture. Existing member comparison keeps null counts unknown, and every valid
catalogue row remains in the same denominator. Synthetic integration tests cover
mixed integer-number/null/zero counts, paging, literal member comparison and
independent-surface failure. The earlier integer-float rejection case now tests
an illegal fractional value, with separate exact-integer acceptance and boundary
counterexamples. Actual post-fix source qualification must still be measured;
this change does not establish public redistribution rights, historical membership,
full-market discovery or retained SourceRefs.

## Undated reported members are not historical constituents

`qualified_ths_members(result, registry=..., ts_code=...)` requires a complete
zero-origin `ths_member` request for exactly that index, with requested
`ts_code,con_code,con_name`. Extra stock or date filters are refused. Rows must
match the index, have unique canonical six-digit SH/SZ/BJ security codes, and
bounded names. BJ codes can be retained as member metadata; the existing stock
price reader's SH/SZ scope is **not** expanded. A malformed supplied member set
is rejected locally, not silently deduplicated or partially dropped.

The official interface marks weight and entry/exit dates as unavailable. The
qualifier deliberately does not infer them, even if callers request optional
columns. It returns literal as-received rows, `effective_at=null`, the inspected
view version, and page-level real acquisition clocks/transport lineage. These
rows are not certified current, historical, contemporaneous with another group,
or continuously held throughout the price interval.

Consequently, the normal market surface's **dated** `membership`,
`membership_as_of` and `membership_version` remain null. Each selected entry has
an explicit `member_snapshot` instead. Captured-at is never relabelled as a
member-effective date or the reading cutoff. The index series comes from
`ths_daily`; it is not constructed using today's constituents or guessed weights.

The reader distinguishes absent input, rejected input and a checked empty set.
A catalogue member-count mismatch is exposed as `differs-between-captures`, not
silently repaired; equal counts also do not establish completeness or same-time
identity. Index observation remains independent of these member qualifications.

## Index reading and optional baseline

`read_ths_group_surface` takes `catalog_result`, `member_results`, `index_results`,
`calendar_result`, `registry`, `kind`, `exchange`, `start` and `end`.
Result maps are keyed by the source index code. Orphan keys not in the catalogue
are refused. `exchange` selects an explicit SSE/SZSE calendar; it does not relabel
the THS A-market catalogue as a single-exchange classification. Prices must be on
that calendar and the explicit end must be open. The calendar is caller-selected
and source-checked, not independently certified as the latest completed session.

Index requests use exactly one `.TI` code and start/end, requesting
`ts_code,trade_date,close`; no adjustment or extra date filter is accepted.
The existing `_daily` row/numeric checks and 5/20/60 calendar-session core are
reused. Missing prices are never carried forward. Local index failures do not
remove members or block other index series. `raw` means source index levels;
`index_unit=index-points-not-currency` and the reader's `XXX` no-currency marker
prevent representing points as CNY. Ratios are not independently certified total
returns, constituent portfolio returns, forecasts or signals.

Optional `benchmark={"ts_code": "...TI", "result": <retained result>}` selects
another THS index at the identical dates/calendar/convention. A bad or missing
baseline restricts only relative output. A checked baseline retains its own
lineage, and the core returns percentage-point excess, not compounded relative
wealth. A chosen `.TI` code is not automatically a broad-market or economically
appropriate benchmark. No raw/adjusted-stock cross-comparison is introduced.

`offset`, `limit` (1..100) and `entity_id` use the original reader. Global scope,
observation and input-qualification counts survive filtering/pagination. Member
snapshots are attached only to the returned groups. Empty scope and zero usable
windows do not mean zero opportunities.

## Existing CLI, single and composed reads

```sh
python -m decision_kernel.interfaces.market_relay_cli \
  --registry docs/sources/relay-registry.json --input retained-groups.json --limit 50
```

Single group input has `schema="relay-ths-group-reading-v1"`, `kind`, and the
required arguments above except `registry`. `horizons` and `benchmark` are optional.
The original raw-stock and source-master/dual-price routes remain intact.

Composed input has exactly `schema="relay-three-market-reading-v1"` and `surfaces`.
`surfaces` contains any supplied `sector`, `concept`, `stock` channels. Group
channels use the single group arguments **without schema or kind**; `stock` uses
the existing security-master reader's arguments without schema. Per-channel
`horizons` may be supplied; group channels may have a benchmark. An omitted
channel is not-supplied; malformed supplied input is rejected **only for that
channel**. Top-level unknown keys, invalid paging or untrusted registry still
refuse the operation. No callback, registry, kind or paging override can be
injected through a nested channel. A successful CLI exit means reading completed,
not that every channel succeeded or any opportunity was discovered.

The composed view includes sector/concept **selected-page-only** literal ID
intersections using the existing `membership_overlap`. It retains both page
contexts, price cutoffs, member versions and unknown member counts. It does not
materialize an unbounded all-pairs matrix. Missing/rejected membership and an
empty page cannot imply no overlap; `insufficient-reported-membership` is explicit.
Even available intersections describe only returned rows, not same-time holdings
or issuer/economic exposure. Use the single-surface routes for ID filtering;
combined `--entity-id` is refused rather than misapplied across namespaces.

All routes retain the original 2 MiB JSON read bound, duplicate/nonfinite guards,
stable stderr errors, no credential requirement and no input writes. The bound
is for this local reading entrypoint, not a claim of full-market capacity.

## Custody, validation ownership and successors

The **original retained capture results** still use `source_inputs` and the
existing authorized writer/SourceRef reader. A view hash or this member snapshot
is not a saved SourceRef, source authentication or original HTTP bytes. No new
custody representation, public records write, private-data permission, schedule,
notification, research mandate, deployment or legacy cutover is introduced.

`tests/test_market_group_inputs.py` owns the new classifications, as-returned
member interpretation, independent index/member/benchmark failures, versions,
paged intersections and three-channel isolation. It shares the existing synthetic
HTTP opener rather than replacing source contracts with invented envelopes.
Package-external subprocess CLI tests verify the input bytes remain unchanged.
These are engineering consumers, not actual #7/#13 research adoption.

The existing calendar/Decimal/set operations already provide the required
mechanics. A vendor SDK/dataframe pipeline would duplicate transport and cannot
supply unavailable member-effective dates. This narrow source adapter is therefore
local; it is not a generic new framework. Retire its module, both new CLI routes,
this document and exclusive tests together; keep the original raw/master routes,
shared capture fixture, numeric, custody and CI protections while still consumed.

Remaining #10 responsibilities include real authorized requests and source custody
with independent recovery, qualified discovery and actual #7/#13 adoption, dated
membership, issuer mapping/corporate-action events, and each global background
family. DC's dated members are a documented successor candidate; its required
`idx_type` for classification is not admitted by the current shared parameters.
That difference belongs with #12 and is not bypassed here by changing the shared
reader or pretending a THS snapshot is dated. Other classification systems remain
unconnected. No one-source partial engineering result closes all of #10.
