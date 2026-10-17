# SH/SZ single-session coverage and exact reading

This is the next A-share reliability consumer of the existing market and source
contracts, not another data provider or a completion claim for the whole radar.
[Availability and member recovery](ashare-availability.md) remain unchanged.

## Why a session-wide input

[Tushare daily](https://tushare.pro/document/2?doc_id=27) accepts `trade_date` and
returns unadjusted daily prices. Suspended securities have no daily rows.
[Tushare adj_factor](https://tushare.pro/document/2?doc_id=28) also supports all
returned securities on one date; its factor is not an adjusted price or return.
[The trading calendar](https://tushare.pro/document/2?doc_id=26) supplies explicit
open/closed dates. These interface descriptions do not establish Relay health,
publication rights, or a historical/current exchange-certified universe.

The existing RelayRegistry, capture/inspect, RetryingRelay, strict/isolated
security masters, calendar reader, `_daily` and `_factors` are reused directly.
Pure grouped row tables go through the original numeric/date predicates; they
are never relabelled as separately captured queries or new SourceRefs. No new
SDK, retry engine, dependency, storage format, background worker or global family.

## Ordinary reading and custody

`market_relay_cli` accepts `schema=relay-ashare-session-reading-v1`, `start`, `end`,
`calendar_end`, and three maps of original registered results:

- `calendar_results`: SSE/SZSE complete `trade_cal` over start through calendar_end.
- `master_results`: SSE/SZSE complete `stock_basic` with exchange and list_status=L.
- `session_results`: daily/adj_factor complete queries using exactly trade_date=end.

Required fields are the original calendar/master fields, and ts_code/trade_date
plus close or adj_factor. Missing map slots remain unknown. Extra keys, caller
URLs, mixed query dates or registry snapshots are not admitted. Limits remain
8 MiB per reading input, at most 10,000 rows per source and 120 calendar dates.
`reference_policy` defaults to strict; explicit `isolate-identified` uses the
original whole-code quarantine, including duplicates and suspect master dates.

All declared master identities and all received SH/SZ identities are accounted
for. Unreferenced SH/SZ codes are visible but not qualified. BJ rows are counted
outside this supported scope, never folded into the SH/SZ denominator. Unknown
identity syntax rejects that source slice; a duplicate/invalid identified quote
quarantines the whole affected code rather than taking its first or last row.
A failed/incomplete shared daily capture is **input-unavailable**, not thousands
of confirmed missing quotes. A complete empty capture is instead observed absence.
An unavailable exchange master has an unknown denominator, not zero securities.
Calendar/master failures in one exchange do not erase a usable other exchange.

Each eligible security exposes independent raw and factor states. Their fixed
observed/missing/quarantined/input-unavailable counts sum to the eligible count.
Quarantined references and dates outside listing remain visible, but their quotes
cannot bypass admission. Matched counts require both valid values on the same
requested session; no adjusted-price arithmetic, filling or halt inference occurs.

Pagination (1-100 entries) and exact entity lookup preserve all unpaged counts and
the same content-bound `view_version`. Source lineage remains the original whole
capture. The existing market_custody save/read path visits these six slots using
SourceInput, unchanged query/root manifests, CAS/journal and fresh readers. It
requires actual public rights before any write. A missing stored source produces
partial restoration and unknown dependent counts, with no network fallback.

## Existing manual non-publishing host

Use `market-source-check.yml` with `selection=session-coverage`, `exchange=CN`,
explicit `start` and past `end`, reviewed `code_sha`, `access_confirmed=true`, and
`request_context_confirmed=true`. Leave member_code and resume empty. The exact
end is never substituted. Calendars extend from start to the Singapore calendar
day before the call, so `session_lag` counts explicit later open dates. A weekend
is not guessed as a missing trading session. This excludes today's incomplete
session; it does not guarantee provider finality or publication latency.

Six fixed query slots are declared before source I/O: two calendars, two current
L masters, one day-wide daily query, one day-wide factor query. Masters use the
already adopted 500x20 page policy; daily/factor/calendar use 5000x2. Both quote
queries are shared across exchanges, not repeated once per security. A failed
calendar limits its master; no usable reference prevents pointless price calls.
The same 600-second deadline, 10,000-row ceiling, initial plus three attempts,
minimum 30-second/longer Retry-After waits and global refusal stops remain.

Only the original single result.json is uploaded. It contains fixed query
counters, generated arguments/query keys/page policy/registry digest, terminal
retry timing from the original retry_context helper, and venue coverage counts.
It never includes returned stock IDs/names/prices/factors/source hashes or rows.
This opted-in operational metadata is not source custody. `resume-members` still
rejects these new slots; terminal eligibility/timing is evidence, not an automatic
retry authorization, saved source page, or newly implemented session resumer.

`checked-session-coverage` requires six complete nonempty qualified inputs, zero
calendar lag, no unreferenced SH/SZ identities, and both quote values for every
eligible reference in both exchanges. Master quarantine yields the separately
labelled `available-with-quarantine`. Missing prices (including possible halts),
factors, incomplete inputs or stale targets remain completed-with-gaps/exit 2.
Neither result certifies all historical statuses, BSE, 5/20/60-day windows,
corporate actions, discovery, research adoption or long-term availability.

## Verification and retirement

`test_market_session_coverage.py` uses the real registered synthetic HTTP capture,
more-than-5000-row pagination, ordinary reader/CLI, failure and Retry-After paths,
unknown denominators, per-code quarantine, original temporary Git custody and a
new process reading only a root reference after the input is deleted. Existing
market, source and custody tests remain; synthetic evidence is not live data.

Retire the session schema/reader, its finite checker and host choice, custody map
adaptation, tests and this document together. Original market/member/global and
shared source/store/CI contracts remain while their consumers remain. The original retry_context projector now serves both session and member consumers;
preserve it until neither consumer needs it. Code rollback
does not retract published data or erase historic failures. Live operation results
and unresolved product obligations stay in #10, not this implementation document.

## Same-input missing-quote explanations

The existing `relay-ashare-session-reading-v1` payload optionally accepts
`suspension_result`: one original registered `suspend_d` capture with only
`trade_date` as the business scope and all four requested fields
`ts_code,trade_date,suspend_type,suspend_timing`. This must be the same explicit
session as the daily/factor inputs. A per-code or S-only filtered result cannot
establish absence from the full S/R slice and is rejected. No new source adapter,
HTTP client, storage format or alternative market feed is introduced.

Omitting the field preserves the old six-input output. Explicit `null`, an
incomplete capture, missing requested fields, a wrong date or unidentifiable
code produces unavailable evidence, not an empty halt list. A complete empty
capture is different: no event was returned for this requested slice. Date and
identity checks cover every received page; invalid or multiple identifiable
records isolate that code's explanation without discarding its original price.
Multiple records are not first/last-selected or assumed to be in event order.

Semantics are bounded by the official [daily endpoint](https://tushare.pro/document/2?doc_id=27)
and [daily suspension endpoint](https://tushare.pro/document/2?doc_id=214), checked
2026-10-17. `daily` describes unadjusted prices and absence during suspension;
`suspend_d` describes S/R and a field for intraday time windows, with irregular
updates. The documentation does not establish a universal full-day guarantee
from a blank time field. Consequently:

| Original quote and same-day evidence | Explanation, not a replacement value |
|---|---|
| Missing quote; single S without a reported intraday window | `suspension-supported-missing`; **not** certified full-day suspension |
| Missing quote; S with a valid intraday window | `intraday-only-unexplained`; partial-day events do not excuse a missing daily bar |
| Missing quote; R | `resumption-unexplained`; no forward/backward state inference |
| Missing quote; no row in a complete S/R slice | `no-suspension-record`; not proof of supplier fault or normal trading |
| Missing quote; unavailable or quarantined evidence | Explicit evidence unknown/quarantine, never “no halts” |
| Observed quote; S with no reported window | `quote-suspension-conflict`; retain both observations for review |
| Unavailable/quarantined quote or ineligible reference | Keep original restriction; a halt cannot rehabilitate it |

An empty string and JSON null mean no window was reported, not that a full-day
halt has independently been proved. Malformed time strings are not coerced to
blank. Supported intraday syntax is bounded `HH:MM-HH:MM`, with valid increasing
clock times; no union of event intervals, exchange trading-session inference or
automatic provider alias is introduced. All explanations keep
`all_day_confirmed=false`. A later semantic extension requires its own evidence.

Each ordinary reader entry retains its security identity, original quote/factor
status and `quote_explanation`. Suspension witnesses are one-based original
`page`/`row` positions with query lineage in `source_lineage.suspend_d`. A missing
quote is an absence in the complete daily capture, not a manufactured row.
`quote_explanations.by_exchange` counts the **whole eligible scope**, before
pagination or an `entity_id` selection. Unknown denominators stay null, and a
missing daily input yields unknown missing-quote counts. Full content versions
bind these explanations and evidence; paging does not change their denominator.
BJ remains out of SH/SZ scope; unreferenced SH/SZ suspension identities are
counted separately rather than silently joined into the master.

The original `matched_count`, `raw_counts`, `factor_counts`, scope, quarantine and
`qualified_scope_covered` never change because an explanation was added.
`strict_coverage_reclassified=false` is explicit. Thus suspension-supported
missing quotes do **not** become observed quotes, zero returns, recovered source
requests or a looser success threshold. This is a diagnosis, not a tradeability
certificate, a historic halt calendar or a correction of an earlier batch.

### Same host, one additional scoped query

`market-source-check.yml` keeps `selection=session-coverage` and adds the optional
`explain_quotes=true` flag (default false), still `exchange=CN` and explicit `access_confirmed=true` / `request_context_confirmed=true` with
empty member/resume arguments. Internally this is identified as
`session-reconcile`; the raw selector list stays unchanged. The same `check_session_sources` performs the
original six queries plus **one** date-wide S/R query, not one HTTP call per
security. Its generated parameters are recorded before HTTP, using the existing
registry, capture, source stop and retry metadata. The original 600-second,
10,000-row, 8-MiB, initial-plus-three/minimum-wait policy and concurrency group
are unchanged; `suspend_d` uses the existing 5,000-by-two page envelope. A failure
of only the suspension source does not erase the price/factor result. Member
recovery still rejects these slots; this is not a session-resume implementation.

The native artifact contains counts and generated request arguments **only**.
It deliberately omits security identities, row witnesses, names, prices, factor
values and returned source bodies. No new flag asserts supplier publication
rights. The source-based by-code diagnostic is available through the ordinary
reader on authorized input, and through the original `save`/`read` custody path
only with established source rights. Same-call diagnosis is not persistent
retention: old count-only receipts cannot recover lost missing-price identities,
and the public diagnostic receipt must not be advertised as a replayable source.

Both original and reconciliation modes keep the strict status contract. Empty
suspension evidence is structurally valid; SH/SZ evidence quarantine, conflicting
quotes or unreferenced suspension identities prevent the new mode's success.
Even well-supported missing quotes still leave `completed-with-gaps`. Calendar
lag remains relative to the original declared calendar end, including on a
deliberately historical diagnostic day; no “latest” substitution is performed.

`test_market_quote_explanations.py` protects this consumer with real synthetic
capture, S/R/blank/intraday distinctions, duplicate/invalid/date/scope rejection,
source absence versus empty results, unknown denominators, complete page-row
witnesses, original counters, CLI routing, bounded source failure, publication
projection and original custody/fresh-process recovery. Existing session and
other market tests remain. Retirement removes this optional slot, explanation
module, host flag and exclusive tests/documentation together; retain shared
session/capture/retry/custody contracts and all original records/failed receipts.
