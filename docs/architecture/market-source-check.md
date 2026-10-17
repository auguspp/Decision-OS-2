# Non-publishing market source checks

This is an explicit operational check of the **existing** Relay and market
readers, not a new market feed, store, source exporter or discovery algorithm.
It separates permission to make a finite query from permission to redistribute
supplier data. It deliberately produces no replayable source representation.

## Why this narrower consumer exists

The [market custody reader](market-custody.md) already preserves and restores
permitted source projections. The existing `relay-source-runtime.yml` capture
operation stores returned rows in public-repository artifacts, and correctly
requires `public_rights_confirmed`. That flag must not be set merely to test
whether the service responds. Its existing gates are unchanged.

The official [Tushare service agreement](https://tushare.pro/document/1?doc_id=405),
section II(II)5, checked 2026-10-16, describes a personal, non-transferable,
non-commercial viewing licence. It is not evidence of a particular Relay
customer's redistribution licence. Access purchase, a service-enabled flag or an
engineering instruction does not establish that licence. The specific agreement
with a supplier may differ and remains a separate check; this document is not a
legal certification of any supplier's rights.

`market_source_check.check_sources` uses `validate_plan`,
`RelayHTTP.request_registered`, `RetryingRelay`, `capture_registered`,
`inspect_result`, and the already delivered security/group/calendar qualifiers.
The lower-level capture exists only as an in-memory argument to those readers;
no `capture_plan(public_rights_confirmed=True)` or `source_inputs` export is used.
The unchanged publication-gated capture/save operations remain the only routes
for their own purposes. A vendor SDK, second transport, credential, database or
scheduler would duplicate these existing responsibilities.

## Fixed finite scope and selection

The caller names SSE or SZSE and an explicit compact-date interval. All dates
must be earlier than the current Singapore calendar date, with at most 120
calendar dates. No current-day sampling, weekday approximation, cutoff shifting
or future-data use is allowed. An actual complete `trade_cal` supplies sessions;
an end date absent from it is not silently substituted.

The check reads the whole returned venue/L `stock_basic` slice and the THS A/I
and A/N directory slices. For each **qualified** directory it selects the first
sorted security/index code, not a price winner or a user watchlist. It checks
that stock's `daily` and `adj_factor`, and each group's `ths_daily` and
`ths_member`. Missing/invalid directories prevent only their dependent checks;
member checks do not depend on calendar success. The final in-memory consumer
is `read_three_market_surfaces`; its full declared denominators are preserved,
while only a single sample per channel has been requested.

The L slice is not all listing statuses, a historical/PIT universe or all of
China's markets. Source-code order is not a candidate-selection policy. A source
currency, date, field or classification mismatch is recorded as rejection, not
fixed by filtering the original capture or weakening an existing qualifier.

Each of ten named query slots is executed at most once, with the existing
same-request retry policy (initial plus three attempts; at least 30 seconds and
valid longer Retry-After). Two pages at 5,000 rows bound each query; this is an
operational check bound, not a purchased quota or a promise of exhaustive data.
One shared 600-second deadline covers the call. Authentication/entitlement,
rate-limit, secret reflection and run-deadline outcomes stop subsequent traffic;
there is no alternate key, host or retry loop at the channel layer.

## Receipt versus source data

Only code-owned status labels, allowlisted error codes, row/page/request counts,
query-slot/API names, declared/observed counts, fixed structural counters, and operator date/venue scope
leave the use case. Source values, names, selected security/index identifiers,
prices, member lists, response identifiers, body/view hashes, arbitrary exception
prose and original attempt diagnostics are not emitted. Error text outside the
explicit code allowlist becomes `CHECK_REJECTED`. Code-owned `GROUP_COUNT`,
`GROUP_NAME`, and master name/duplicate errors retain their exact stable codes;
unknown strings are still never released.

`checked-sample` requires nonempty, qualified responses in all ten slots and at
least one usable window in each composed channel (qfq for the stock). It is not
proof of all 5/20/60 windows, continuous health or full-market coverage.
`checked-empty`, rejected, capture-incomplete and not-attempted remain distinct;
any gap yields `completed-with-gaps` and exit 2. No result means zero market
opportunities. Interrupted work retains an in-progress/interrupted engineering
receipt rather than claiming completion.

The host writes only `result.json`, atomically replacing a temporary receipt,
and uploads that single file, never a capture directory. Data remain transient
within the process and normal transport; this does not certify secure memory
erasure or absence of supplier/GitHub infrastructure logs. The receipt cannot
reconstruct market rows, verify a publisher or be passed off as a SourceRef.
Actual source custody, second-run row restoration and user research/reading
adoption still require suitable data rights and the already delivered #55 path.

## Host and permissions

`market-source-check.yml` is manual, main-only, exact-code-SHA and first-attempt
only. It shares `decision-os-relay-source` serialization with the current source
host and never cancels an active source run. Inputs are exactly `code_sha`,
`exchange`, `start`, `end`, and `access_confirmed`, with optional `selection`
(default `sample` for prior callers); an unknown selector or selection is refused.
The latter authorizes this query-only use, not third-party publication. The job
has `contents: read`, does not expose a store token and passes the existing Relay
Secret only to the check step. There is no cron, notification, model execution,
record write, source export or deployment operation.

After a normal PR, Ready full, ordinary merge and applicable main verification,
an authorized owner may dispatch it on main and independently inspect the
returned diagnostic artifact. Merely merging the workflow proves no live source
call. Runtime failures remain operational evidence rather than CI failures to
be hidden with reruns. Explicit new checks do not reset or retrospectively repair
an earlier failed sample.


## Targeted diagnosis without changing qualification

`selection=catalogues` requests only the two `ths_index` A/I and A/N slices using
exactly the existing field set, zero-origin pagination, retry and deadline rules.
It never requests the calendar, master, prices, factors or members, never derives
a sample and never invokes three-surface reading. Dates are validated operational
context only; they do not date the current directories. Successful, nonempty
qualification of both yields `checked-catalogues`, not `checked-sample`. Empty,
rejected, incomplete and stopped results remain exit 2. Only selected slots occur
in `queries`; an omitted slot is not tested or passed. `surfaces` remains empty.

After the existing generic inspector, catalogue diagnostics expose only counts
for the six fixed expected field names and fixed type buckets (missing/null/
boolean/integer/number/string/other), plus padded/nonprintable-name counts and
integral/fractional floating-number counts. They expose neither source values nor
row positions or IDs. These structural counters do not coerce values, select
valid rows, change catalogue admission or establish usable classification counts.
No source-supplied field name becomes a diagnostic key. The catalogue qualifier
now accepts bounded exact-integer floating representations (see
[the count contract](market-group-inputs.md#count-representation-compatibility));
the diagnostics still report their original `number` type, not a rewritten
`integer` type. Fractional counts retain the specific rejection code.

`last_attempt_failure` uses the existing transport's recorded failure classification,
HTTP status and reviewed business-error enum, projected to explicit finite code
sets. It describes the latest failed attempt, including one recovered by a later
successful retry; it is not necessarily the final query outcome. No failure
metadata means null, not proof there was no unrecorded network issue. Unknown
classes become `unclassified`, invalid statuses and unknown business errors become
null. This three-field object emits no original exception prose, request IDs,
headers, timestamps, source digests or retry details. The separate execution
diagnostic below does not change its contract. No new transport classifier or
retry policy is introduced. Earlier receipts lacking this information cannot be
reconstructed.

The change responds to the first live check's `CHECK_REJECTED` diagnosis loss.
Existing failed evidence stays intact. A new targeted diagnosis is a new explicit
operation, never a rerun that repairs the old sample. It proves only the actual
new result at its exact code/operation identity. Source/publication permissions and
later compatible normalization must be established separately from counters.

## Retry execution diagnosis

Every attempted market query can now carry `retry_diagnostics` with schema
`market-retry-diagnostics-v1`. The original `RelayHTTP` parses the two documented
remaining headers into a separate transient `last_header_diagnostics` field;
the original `RetryingRelay` records its decision and completed wait calls in
each attempt. `capture_registered` also binds the last page request's decision
to `request_execution.page_number`, including a deadline refusal before HTTP.
The market projection uses those records, never the client's possibly stale
last error or a reconstruction from today's policy.

| Receipt field | Exact meaning |
| --- | --- |
| `recorded_attempts` | Recorded wrapper attempts across this query's pages; distinct from the original `http_attempts` counter. A post-page count rejection does not count the same attempt twice. |
| `completed_wait_seconds` | Sum of requested durations whose original `sleep` calls returned normally. Interrupted/planned waits are not added; this is not measured wall time. Missing old completion records make the total null. |
| `last_response` | Last recorded response-header snapshot, bound to its page number and attempt within that page. It may precede a later transport failure or a deadline before the next page. |
| `last_failure` | Last recorded failed wrapper attempt, with its own headers, decision, completed wait and `request_recovered`. The boolean only means a later attempt in that same page returned a response; it does not qualify the capture or market reading. |
| `last_request` | Page number and the original wrapper's final decision for the last page request. `response` can coexist with a later table/count/reader rejection. `run-deadline` can occur with zero HTTP. |

`retry_after` contains only `state` and parsed `seconds`; `key_remaining` and
`ip_remaining` contain only `state` and parsed `remaining`. States are `valid`,
`missing`, `invalid`, `withheld`, or `not-recorded`. Explicit missing headers are
different from unavailable old diagnostics or a transport failure without a
recorded response. Remaining values must be a single bounded nonnegative ASCII
decimal integer (at most 12 digits); duplicate/merged, signed, floating, boolean,
non-ASCII, overlong and credential-reflecting values cannot become counts. Valid
Retry-After seconds retain the original parser's value, including zero, HTTP-date
conversion and waits longer than the execution budget; they are never clamped.

The decision is made by the existing retry owner, in the original short-circuit
order: `not-retryable` / `retry-after-unusable`, then `attempt-limit`, `wait-budget`,
`run-deadline`, or `retry`. `required_wait_seconds` is the original policy delay,
so an observed zero-second Retry-After can require the existing minimum 30 seconds.
The first blocking condition does not claim all other budgets were available.
`response` denotes return from the request wrapper; `not-recorded` denotes missing
old/unrecognized diagnostics. Existing `pending_retry` and query status remain
unchanged. Not-attempted query diagnostics stay null.

This is a bounded snapshot, not the full attempt timeline, a cooling permit,
source authentication or source custody. The archived
[vendor manual](../sources/vendor/tushare-relay-v0.5.61.md) describes shared Key/IP
limits with actual server headers authoritative. A remaining count supplies no
reset time, exclusive allocation or promise that the next request will succeed.
No successful-request pacing, default cooldown, alternate Key/host or extra retry
is inferred. The existing 4 attempts, 180-second per-page wait budget, shared
600-second market deadline and terminal global stops remain in force.

The same projection serves sample/catalogue/member, session, history/reconcile
and factor checks through the original fixed host. `retry_context` still means
only the existing member recovery admission, which excludes rate limits; its
`eligible=false` says nothing about Retry-After validity. These new facts neither
authorize cross-run recovery nor reconstruct headers absent from older receipts.

Original headers, request IDs, credentials, source values, response bodies,
timestamps and arbitrary diagnostic prose never leave this projection. Successful
`page`/`transport` key sets, inspected views and `SourceInput` exports remain
unchanged: attempts and top-level execution diagnostics are not source excerpts.
`tests/test_relay_retry_diagnostics.py` and `tests/test_market_retry_diagnostics.py`
protect this source-to-receipt responsibility, including host routing, page
identity, recovered-but-rejected responses, interruption and legacy reading.
Retiring this diagnostic capability removes its side field, attempt/capture
additions, market projection/wiring and these tests together; the shared retry,
source export, custody and recovery protections continue under their owners.

## Classification diagnosis across the received directory

The former `MARKET_RELAY_GROUP_CLASSIFICATION` composite is split into
`MARKET_RELAY_GROUP_DUPLICATE_CODE`, `MARKET_RELAY_GROUP_EXCHANGE_MISMATCH` and
`MARKET_RELAY_GROUP_TYPE_MISMATCH`. Invalid identifiers keep `GROUP_CODE`.
The same predicates in `market_group_inputs._classification_checks` serve the
qualifier and diagnostic reader. Acceptance, whole-slice rejection, original
row order, count normalization and source bytes are unchanged. Qualifiers still
stop at the first failed predicate; historical receipts keep their original code.

`catalogue_shape.classification` counts every received row across all inspected
pages, even after a prior row would have failed qualification. Fixed
`reason_counts` can overlap; `rows_with_issues` counts each affected row only once.
Missing exchange/type counts distinguish null/empty from other mismatches without
returning the value. Duplicate counts refer to occurrences after the first valid
identifier, across page boundaries, including identifiers on otherwise invalid
rows. Identical/different duplicate counts compare the original JSON values of
the six fixed columns with the first occurrence (no trimming or numeric coercion).
Invalid identifiers are counted separately and never deduplicated. The per-query
seen map is discarded, so separate I/N slices never contaminate each other.

`coverage=received-pages-only` explicitly limits incomplete captures; otherwise
`complete-capture-not-qualified` still certifies neither source completeness nor
market acceptance. Counts do not repair, filter, publish or save any row, and do
not disclose identifiers, row positions, digests or unrecognized source values.
The existing `catalogues` selection and operational stop rules are unchanged.
A new targeted result must be inspected before proposing a compatibility change;
old diagnostics without these counters cannot be retroactively reconstructed.

`tests/test_market_classification.py` protects the shared predicates, independent
counts, cross-page duplicates, incomplete/empty input, unchanged bytes, channel
isolation and every incremental host receipt. Retire these diagnostic additions,
their predicates where unused, this section and exclusive tests together; retain
all current qualification, transport, custody and CI protections.

## Independent stock-only operational check

`selection=stock` is a bounded use of the same checker, not another feed or host.
It executes only `calendar`, `master`, `stock_daily` and `stock_factor`. The first
source-code-sorted security from the qualified venue/L master supplies the two
price/factor queries; the complete returned master still supplies the denominator.
There are no THS catalogue, member or index requests, even when those sources
are unavailable or known to contain conflicting records. The existing composed
reader receives only the stock input; only its stock result is counted or emitted.
Unrequested channels are neither tested nor passed, and no group inference runs.

Four nonempty qualified queries plus at least one usable qfq stock window produce
`checked-stock-sample`. Missing/invalid calendars, empty or rejected masters,
incomplete transport, missing factors and unusable stock windows keep the gap
status and exit 2. Original raw/qfq, listing, calendar, decimal and factor-endpoint
checks are reused unchanged. Access, exact main SHA, first-attempt-only execution,
retry/deadline, stop rules and non-publication protections remain unchanged.
The default ten-slot `sample` and two-directory `catalogues` modes are unchanged.

This check cannot certify full-market discovery, a ranking policy, all horizons,
healthy group directories, source custody, issuer identity or research adoption.
It does not resolve previous THS duplicate-code failures. Those retain a separate
source-contract responsibility; choosing the first or last conflicting record
without evidence is not an allowed repair. Only diagnostic counts/statuses leave
the process. No production SourceRef, source archive or private rights are added.

`tests/test_market_stock_check.py` uses the original synthetic opener and real
Relay/capture/reader/host chain. It checks exact four-slot traffic, no THS calls,
full master denominators, negative and empty inputs, retry and terminal refusal,
permission-before-traffic and redacted host output. Retire this selection, host
success marker, workflow choice, section and tests together when unused; retain
the existing modes and shared qualification/custody/CI protections.

## Explicit reconciled history consumer

The original host also admits `stock-history-reconcile` for 1–4 explicitly
declared SH/SZ securities, `exchange=CN`, with access, request-context and
suspension-explanation confirmations. It uses the same history acquisition and
original normal reader, adding only exact missing-day factor captures within
the shared operation budget. The public host still receives only counters.
See [the history contract](ashare-history-continuity.md#same-input-factor-reconciliation-and-normal-consumption)
for baseline/combined qualification, the freshness-aware point limit, consumer
validation and retirement. It is distinct from the factor-only diagnostic and
does not recover earlier unretained histories or publish source data.

## Maintenance and evidence ownership

`tests/test_market_source_check.py` uses the existing synthetic Response and
Registry fixtures, the actual Relay/capture/market code, sentinel supplier values,
and the real host file writer. It checks isolation, absence of source data in
every incremental receipt, authorization before traffic, existing 30-second
retry, terminal denial, no source export, and interruption without prose leakage.
Synthetic fixtures are not exchange calendars, market facts or production
SourceRefs. Repository CI, actual live execution and supplier permissions remain
separate evidence.

Retire this application use case, module host, dedicated workflow, tests and
this document together when no longer used. Keep shared Relay, market-reader,
publication/custody and CI protections. No other product line's files change.
