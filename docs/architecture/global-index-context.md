# Global context: the bounded US index slice

`read_us_index_context` consumes existing registered Relay captures for
`us_tradecal` and `index_global`. Its actual local-file consumer is
`python -m decision_kernel.interfaces.global_index_cli`. It does not fetch data,
write custody, schedule updates, select stock candidates or invoke research.
The first supported slice is explicitly declared DJI, IXIC, RUT and SPX, not
complete US or global-market coverage. Other indices and the rates, treasury,
FX, commodity and crypto families remain separate implementation responsibilities.

## Source and reuse basis

The source fields were checked against the official
[index_global definition](https://tushare.pro/document/2?doc_id=211) and
[us_tradecal definition](https://tushare.pro/document/2?doc_id=253), read on
2026-10-16. The former supplies index codes, trading dates and closing **points**;
the latter supplies calendar dates and open/closed flags. This does not establish
Relay live health, upstream entitlement, redistribution rights or actual adoption.
The existing registry's captured service declaration identifies both as enabled
public GET candidates; it is not a new source request or a live acceptance test.

Reuse decision: use the current `_view`, `_rows`, `_daily` and `_lineage` market
adapters and the shared `market_daily` date, positive-level and fresh-context
Decimal primitives. Generic capture integrity, pagination, request admission,
transport, retries and SourceInput/SourceRef custody remain with their existing
owners. The small US calendar qualifier and index-point observation deliberately
do not pass an invented USD currency through a stock-price context. No second
SDK, calendar provider, storage platform or CI policy is introduced.

The old [six-family daily-update contract](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/docs/global-market-daily-updates.md)
is scope/reference evidence only. Its schedules, publisher, fallback providers
and old records are not ported or restarted by this reader. Existing domestic
stock-master and THS failures neither enter this reader nor disappear because it
has been implemented.

## Exact calendar and index qualification

Each calendar request must be `us_tradecal`, start at offset zero, request
`cal_date,is_open`, and bind the exact `start_date` and `end_date`. No `is_open`
filter, domestic `exchange`, single-date filter or changed interval is accepted.
The generic capture must be complete and bound to the current supplied registry
snapshot. Unique, parseable dates must cover every calendar date in the interval,
including closed days. Flags are exactly integer/string 0 or 1, not booleans or
floating-point approximations. Missing or contradictory calendar data refuses
the shared context. The explicit end must be an open date; it is never replaced
with a guessed nearest or latest trading day.

Each supplied index request must be `index_global` for that exact supported
`ts_code` and interval, offset zero and requested `ts_code,trade_date,close`.
Rows are checked through the existing `_daily` qualifier: exact code, unique
US-calendar open dates within scope, and finite positive bounded levels. A wrong
code, duplicate date, closed-day quote, invalid level, changed query or incomplete
capture rejects that index, without selecting first/last or silently deleting rows.
A bad shared calendar is not isolated away because every window depends on it.

A complete empty index capture is `checked-empty`, not `not-supplied`. An absent
capture and a rejected capture remain separate entries in the declared denominator.
No empty capture or missing declaration is evidence that a market has no activity.
A syntactically complete capture is still only a source declaration; this reader
does not independently authenticate an index publisher or composition.

## Observation and time semantics

The source US market date is retained as a date. It is not a UTC/Singapore date or
a proved globally simultaneous closing instant. No UTC-midnight cutoff, daylight
saving conversion, forward fill or intraday claim is made. Caller-supplied `end`
is explicit, not a certification of the latest completed session.

The default 5/20/60 horizons mean calendar-bound trading-session intervals, each
requiring N+1 levels including its baseline. A missing intermediate session
invalidates that window; a shorter complete window can still be observed. Returned
row counts never substitute for the calendar. Each observed window exposes its
baseline date/level, end date/level and `(end / baseline - 1) * 100`. Insufficient
calendar history and missing index levels are distinct. All supplied rows are
validated; out-of-calendar rows cannot be silently trimmed by the pure observer.

Units are `index-points`, with `currency: null`. No monetary USD price, FX-adjusted
performance, stock qfq/hfq convention, dividend reinvestment or certified total
return is inferred. The native source-index-level ratio is not a buy signal,
economic exposure relation or proof that an A-share issuer benefits.

The read interval is bounded to a 3,660-day difference. There may be 1–12 unique
positive integer horizons of at most 2,520 sessions; defaults remain 5/20/60.
These are local input/processing bounds, not supplier purchase or retry budgets.

## Complete scope, pagination and source references

The caller explicitly supplies `declared_indices` as a list of supported distinct
codes; an empty declaration is allowed but does not prove empty world coverage.
Undeclared supplied results and unsupported codes are refused. All declared entries
are qualified before pagination. Missing/rejected indices are not dropped, and
sorting by code is only navigation, never a performance/candidate rank.

`scope_count`, `observed_count`, qualification counts and `view_version` refer to
the complete declared context, including failures. `matched_count` and the page
refer to the optional exact entity selection. `offset` is a nonnegative integer;
`limit` is 1–100. An offset beyond the end returns an empty page with no next page.
The complete content version does not change merely because a page/filter changes.

The calendar and each checked index expose their original query/capture lineage.
These hashes bind a read representation, not a SourceRef, publication license or
permanent archive. Source records already held under authorization can be opened
through the existing custody contracts; this reader introduces no new archive.
It never publishes local inputs or sends them to another service.

## Use the existing source path and then read locally

Where access and use are authorized, prepare the two kinds of requests through
`relay_research_cli` / the existing registered source runtime, not this CLI:

```json
{"api":"us_tradecal","stage":"baseline","purpose":"US index context calendar","parameters":{"start_date":"20260701","end_date":"20261015","fields":"cal_date,is_open","limit":"1000"}}
{"api":"index_global","stage":"baseline","purpose":"Declared US index context","parameters":{"ts_code":"SPX","start_date":"20260701","end_date":"20261015","fields":"ts_code,trade_date,close","limit":"1000"}}
```

These are request examples, not executed evidence or permission to repeat failed
requests. Reuse an existing suitable capture; actual source access, retry limits,
public rights and long-term custody keep their current contracts. No new source
run is required merely to exercise this reader's synthetic engineering tests.

A local JSON input contains `calendar_result`, `index_results` keyed by code,
`declared_indices`, `start`, `end`, and optional `horizons` as a JSON integer array.
The results are **original existing capture-result objects**, not invented tables
or a replacement persistence schema. No extra top-level keys are accepted.

```console
python -m decision_kernel.interfaces.global_index_cli \
  --input /approved/local/us-index-input.json \
  --registry /checkout/docs/sources/relay-registry.json \
  --limit 20
```

The input is limited to 8 MiB, read with duplicate-key/non-finite JSON rejection.
The CLI writes only the resulting view to stdout and does not modify the input.
On invalid host input/shared context it emits a generic error and exit 2 without
raw values or tracebacks. Exit 0 means the view was read: it does **not** mean every
index or horizon qualified; consumers must inspect the per-entry outcomes/counts.
Output may contain the caller's approved source levels/lineage and must not be
uploaded publicly without the required rights. No Actions source workflow,
credential access, auto-custody, notification or deployment is added.

## Ordinary market reading and exact retained recovery

The US reader also uses the existing `read_market_payload` route. Its input
schema is `relay-us-index-reading-v1`, with the same `calendar_result`,
`index_results`, `declared_indices`, `start`, `end` and optional `horizons` fields.
The required-field contract lives with `global_index_inputs`; the dispatcher and
custody adapter share it. `global_index_cli` still accepts its original **bare**
input without a schema and delegates to this route. Neither entry changes the
original US calendar, supported codes, observations, units or source qualification.
The ordinary input adds only the schema to that same approved local input:

```console
python -m decision_kernel.interfaces.market_relay_cli \
  --input /approved/local/us-market-reading.json \
  --registry /checkout/docs/sources/relay-registry.json \
  --entity-id SPX --limit 1
```

For inputs with established public redistribution rights, use the existing
[market custody](market-custody.md) save/read operations. There is no US-specific
store, manifest format, journal or writer. The same `market-query-custody-v1`
manifests reference the existing requested-column SourceInputs; the same
`market-reading-custody-v1` root records the input recipe and exact query refs.
The adapter admits the existing `us_tradecal` and `index_global` field contracts.
Source pages stay in their source records, not duplicated in a new view archive.

`retain_market_reading` keeps the original public-rights gate and complete-capture
preflight before the first write. The boolean is a caller assertion, **not** a
supplier licence. Original permission/binding checks, CAS, identical-intent
reconciliation, page/query/reading bounds and fresh-reader verification apply.
Unknown writes stop; reconcile the same input through the existing journal, not
a new submission key. This change does not permit restricted inputs to enter
public Actions artifacts or records. Read access is not redistribution permission.

Once an authorized save returns its root reference, the ordinary reader can open
it without the original capture files or a Relay key:

```console
python -m decision_kernel.interfaces.market_custody_cli read \
  --binding /approved/local/readonly-binding.json \
  --registry /checkout/docs/sources/relay-registry.json \
  --input /approved/local/root-sourceref.json --entity-id SPX --limit 1
```

This uses the existing authorized store binding, not a new target. A restored
view preserves the exact source versions, requested date window, unit, pagination
and full declared-index denominator. Reading one index does not turn the scope
into one. A lost index page produces `partial-restoration`, a named source error
and that index's rejected input; available peers remain readable. A lost shared
calendar leaves `view=null` with a reading error, never zero market change or
zero missing observations. Registry drift is refused, not refreshed silently.
No failed source is replaced by a network request or a newer version.

The retained representation is a requested-column reading envelope, not original
HTTP bytes or execution-attempt diagnostics. The existing host strips those
unretained diagnostics before preparing public output. It continues to enforce
its 60,000-byte runtime payload bound; custody keeps its existing 2 MiB input
bound and query/page limits. Neither is raised to the standalone CLI's 8 MiB.
A diagnostic `market-source-check-v1` receipt is not accepted as a reading input
and cannot be converted into the missing source rows. No repeat of a successful
source window, public write, scheduled task or Brief adoption is authorized here.

`tests/test_us_index_custody.py` exercises the original source exporter/store,
existing root/query schemas, multi-page exact restoration, missing/empty sources,
local-Git and separate-process readers with capture files removed, ordinary and
legacy CLI equivalence, runtime rights/redaction, same-intent/unknown-write
reconciliation and scope/snapshot/page tampering. All fixtures and local stores
are synthetic. Retiring this integration also removes its route, US slot/field
adapters and dedicated tests; shared custody and domestic reading remain. Retiring
the underlying US reader must additionally retire its consumers of these constants.

## Finite non-publishing source check

When no suitable authorized capture can be reused, the existing
[non-publishing market check](market-source-check.md) can exercise this reader
in memory with `selection=us-indices` and `exchange=US`. It uses the same
`market-source-check.yml` manual main-only host, explicit reviewed `code_sha`,
first attempt and `access_confirmed=true`. Domestic `sample`, `catalogues` and
`stock` modes keep their defaults and traffic; `US` is rejected in those modes,
and SSE/SZSE are rejected for the US selection.

There are exactly five code-owned query slots: `us_calendar` and
`us_dji/us_ixic/us_rut/us_spx`. The entire requested `us_tradecal` interval is
qualified before index requests. A failed calendar or closed end causes no
index traffic; no domestic master, THS directory or A-share calendar is used.
Each index is qualified independently and the final `read_us_index_context`
consumes all four declared indices before a count-only projection. No index
level, change rate, source date, source identifier, lineage or hash is exported.
Fixed slot names identify the planned checks, not values copied from responses.

The check accepts at most 120 calendar dates and requires end strictly before
the call's **America/New_York** date, using the standard library's ZoneInfo.
A Singapore date rollover does not imply a completed US market day. Missing
zone data fails closed; UTC, weekdays and guessed offsets are not fallbacks.
This guard does not independently certify a latest session or publisher timing.
Domestic checks retain their existing Singapore-date restriction.

The unchanged capture/inspect/RetryingRelay path retains two pages at 5,000 rows,
initial plus three attempts, at least 30-second waits, longer valid Retry-After,
and one shared 600-second deadline. Authentication, rate limit, secret reflection
and deadline stop remaining traffic. No endpoint, key, retry budget or sibling
source is substituted. An index transport failure still allows unrelated indices
unless the existing global stop applies; every declared index stays in scope.

Only the existing single `result.json` artifact is uploaded. `scope_count=4`
means the declared four-index scope, not four qualified inputs. With a rejected
shared calendar, `observed_count`, qualification counts and window counts are
null, not false zeroes. For a readable context each 5/20/60 window has four
fixed buckets summing to four: observed, missing-market-sessions,
insufficient-calendar-history and inputs-unavailable. Complete empty captures
remain distinct from absent or rejected captures. Short usable windows survive
long-window gaps, but `checked-us-index-context` / exit 0 requires all five
nonempty qualified queries and all three windows observed for all four indices.
Any missing requirement is `completed-with-gaps` / exit 2, not a blank success.

No `public_rights_confirmed` assertion, source export, SourceRef or records write
is introduced. The diagnostic cannot restore source rows and does not fulfill
long-term custody, independent source recovery, ordinary reading/Brief adoption
or economic acceptance. Prior master/THS failures remain unchanged. A new finite
source operation requires a specific purpose and reconciliation of existing runs;
it is not a rerun to overwrite a failure. Retire this selector, US host option,
success marker and exclusive tests together if this check is no longer used;
keep the existing standalone reader and other check modes while they have users.

## Evidence and exit

`tests/test_us_index_context.py` uses synthetic calendars and index levels through
the real registry, Relay HTTP adapter, generic capture, qualified readers and
out-of-checkout CLI. It covers exact windows/units, missing end/intermediate dates,
empty versus absent data, cross-index/closed-day/duplicate/invalid rows, incomplete
captures, query/snapshot tampering, full-scope paging, caller Decimal traps,
unchanged inputs and safe CLI errors. Synthetic public-rights flags authorize
only those synthetic fixtures; they do not assert vendor redistribution rights.

Engineering CI, actual successful source acquisition, long-term authorized custody,
independent recovery, research/Brief adoption and complete global-family coverage
remain separate evidence. Retiring this slice removes its two dedicated modules,
CLI, tests and this document together. Shared market/source/custody/CI components
remain while their original consumers are active.

`tests/test_us_index_source_check.py` protects the real synthetic capture/reader/host
composition, full window accounting, US-date boundaries, pagination, retries,
isolation and every incremental redacted receipt. Live source results are
recorded in #10, not inferred from these synthetic tests.
