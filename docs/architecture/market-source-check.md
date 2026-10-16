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
null. Original exception prose, request IDs, headers, timestamps, source digests
and retry details are not emitted. No new transport classifier or retry policy is
introduced. Earlier receipts lacking this information cannot be reconstructed.

The change responds to the first live check's `CHECK_REJECTED` diagnosis loss.
Existing failed evidence stays intact. A new targeted diagnosis is a new explicit
operation, never a rerun that repairs the old sample. It proves only the actual
new result at its exact code/operation identity. Source/publication permissions and
later compatible normalization must be established separately from counters.

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
