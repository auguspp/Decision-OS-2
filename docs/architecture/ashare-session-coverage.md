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
