# A-share history continuity and date-local quote explanations

Owner: market reading / #10. Reuse the existing `relay-security-stock-reading-v1`
reader and its raw/qfq 5/20/60 market-session observations. This capability does
not introduce another adjustment formula, capture format, storage backend or
scheduler. Single-session coverage and its suspension explanations remain in
[the existing session contract](ashare-session-coverage.md).

## Ordinary reading of authorized inputs

The original security-stock payload accepts optional `suspension_results`, a map
from a declared SH/SZ security code to its original complete `suspend_d` result.
The exact generated request contains `ts_code`, `start_date`, `end_date` and the
four fields `ts_code,trade_date,suspend_type,suspend_timing`. A date-only result,
S-only filter, foreign security, wrong calendar day, incomplete capture or changed
request is not equivalent. Source admission and `_view` still check the actual
capture. No result is reconstructed from counters.

Each supplied stock/factor/suspension identity receives a date-local explanation
on ordinary raw and qfq reading entries. The original declared master denominator
remains intact; `inspected_count` and `not_inspected_count` distinguish the supplied
histories from unrequested master identities. Only inspected histories get a day
matrix, avoiding invented daily unknown records for every unrequested security.
Paging or selecting one code does not shrink that denominator or the summary.

The same original suspension-group and explanation functions serve single-day
and period inputs. Source positions are one-based page/row positions in the
original captured tables, not positions in a sorted or reconstructed table.
Multiple or invalid records isolate that security/day's explanation. An S with
no reported time window supports investigation of that day's missing quote, but
`all_day_confirmed=false` remains. It is never propagated to the next date.
Intraday S, R, missing evidence and complete evidence with no record are distinct.
A quote and a no-window S on the same day remain an explicit conflict.

`missing_quote_count=null` means the original price input was unavailable, not
zero missing sessions. Listing eligibility, original price/factor qualification,
anchor factor, intervening factor gaps, factor changes and raw/qfq window rules
are unchanged. A supported suspension neither fills a price nor changes a window
from missing to observed. These are current-reference histories, not a historical
constituent census, independently verified corporate actions or total returns.

Official source semantics: [daily](https://tushare.pro/document/2?doc_id=27),
[adj_factor](https://tushare.pro/document/2?doc_id=28), and
[suspend_d](https://tushare.pro/document/2?doc_id=214). The suspension interface
supports code and date-range queries but does not establish the blanket rule
that an empty timing field proves a full-day halt.

## Existing manual host: a finite explicit cohort

Use the original `market-source-check.yml` on current main with:

- `selection=stock-history`, `exchange=CN`, explicit past `start` and `end`;
- `stock_codes` as JSON containing 1–8 unique six-digit `.SH` / `.SZ` codes;
- `access_confirmed=true`, `request_context_confirmed=true`, `explain_quotes=true`;
- the exact main `code_sha`, with `member_code` and `resume` empty.

Other selections reject unused stock codes. BSE and arbitrary identifiers are not
admitted to this cohort. These are explicit operational inputs, not stock picks.
The existing workflow has eleven inputs; GitHub's workflow-dispatch limit was
[increased to 25](https://github.blog/changelog/2025-12-04-actions-workflow-dispatch-workflows-now-support-25-inputs/).
No permissions, concurrency, credentials or artifact destinations are added.

The full cohort and all generated query arguments are recorded before the first
HTTP attempt. Each represented exchange uses the existing complete calendar and
current L master. Each eligible selected security uses one range query each for
`daily`, `adj_factor` and `suspend_d`, with their original pagination. If the end
is before Singapore yesterday, a separate complete calendar segment measures
actual session lag; missing freshness evidence remains unknown, never lag zero.
A quarantined, unreferenced or outside-listing selected identity is not replaced
by the first successful stock and does not disappear from the cohort.

Original limits remain: one shared 600-second source deadline, capture bounds of
10,000 rows / 8 MiB, master pages 500 x 20, other pages 5,000 x 2, initial attempt
plus at most three retries, minimum 30 seconds and longer Retry-After waits.
Authorization/rate-limit refusal and other original global stops are preserved.
At most 30 logical queries are planned for eight securities and two exchanges.
A local failure does not by itself discard an unrelated peer's result. Unknown
or rejected shared input cannot certify coverage. No automatic second run,
source fallback or larger retry budget is introduced.

The public receipt contains per-slot window states, eligible-day explanation
counts, factor gap/change counts, suspension quarantine-day counts, calendar lag,
reference/cohort denominators and generated request contexts. It contains no
returned prices, factors, names, source rows, page/row witnesses or source hashes.
The explicit request itself necessarily identifies the requested security; this
is not permission to disclose the returned supplier data or diagnostic identities.

`cohort_history_qualified` requires complete admissible inputs for that selected
security, all six original windows observed, no factor gaps, no unknown/conflicting
quote days or quarantined suspension days, and measured lag zero. Final
`checked-stock-history` additionally requires all planned queries checked or
validly checked-empty. Unselected master quarantine is separately reported as
`available-with-quarantine`; gaps remain `completed-with-gaps`. None of these
statuses certifies whole-market history, service uptime or economic adoption.
The original members resumer rejects history slots. A context is a future
investigation aid, not a replayable source or an implemented history resumer.

## Custody, validation and retirement

The original market CLI, three-surface stock input, SourceInput/query-root
manifests, CAS and fresh reader visit this optional map. No second schema or
store is created. The original 32-query, page and byte limits are not increased.
Only inputs the caller has rights to retain may be saved. A missing saved
suspension source degrades its explanation to unknown while valid original
raw/qfq history remains readable, without network replacement.

`test_market_history_continuity.py` executes real synthetic HTTP/capture,
original raw/qfq calculations, multi-page evidence, original CLI and temporary
Git custody with deletion of the input and a fresh subprocess root read. It
also checks factor/anchor gaps, date-local S/R semantics, unchanged denominators,
unknown freshness, global refusal, long waits, local peer isolation, empty
captures, eight-code bounds and default host routing. Synthetic weekdays are
not presented as real exchange holiday calendars. Record actual source results
separately; no fixture certifies supplier health or real retained source recovery.

Retirement removes `stock-history`, its exclusive input/host/period module,
optional stock suspension map, tests and this documentation together. Retain
shared single-session suspension helpers until their existing consumer also
retires. Do not delete historical source operations or their failures; reverting
code cannot undo those effects. Global expansion remains lower priority than
unresolved A-share coverage, semantics, custody and real consumer acceptance.
