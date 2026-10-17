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

## Same-input factor reconciliation and normal consumption

`stock-history-reconcile` is an explicit choice in the **same** manual host.
It uses the history inputs and confirmations above, with a smaller cohort of
1–4 SH/SZ securities. Ordinary `stock-history` remains unchanged and performs
no point queries. This mode consumes the current operation's original price,
factor and suspension captures; it does not revalidate an old failed run.

The original normal reader first reads the baseline history. For each qualified,
complete factor interval, `reconcile_adj_factors` finds gaps against the actual
calendar. The host registers all admissible code/date point requests before the
first point HTTP attempt, then uses its **existing** capture and retry session.
It never calls the separate factor-audit operation, repeats a baseline capture,
resets the 600-second deadline or extends a Retry-After budget. Invalid or absent
intervals have unknown gap counts and cannot be replaced with point queries.

The whole operation plans at most 32 logical queries. With `N` codes and `V`
represented venues, the baseline has `3*N + 2*V + V*(end != calendar_end)`
queries. The point budget is `min(16, 32 - baseline_queries)`: four codes across
two venues permit 16 points for yesterday's end, or 14 when the separate
freshness calendars are needed. When known gaps exceed the budget, no points
are selected or fetched; all known gaps remain and the receipt records
`point_budget_exceeded=true`. The existing per-input 2 MiB custody limit is
separate; fitting this query count does not certify that a payload fits custody.

Actual point captures enter the existing per-venue `factor_supplements` map.
The same `read_market_payload` then reads the combined history using the original
raw/qfq formula, listing rules, scope and windows. Invalid, empty or incomplete
points preserve their local gaps; they do not discard an independent valid point.
Original captures remain unchanged. Complete baseline factors require no point
HTTP; an empty supplements map can still use the existing explicit combined
lineage representation without changing numerical observations.

Each public stock slot retains the original counter fields and adds:

- `baseline`: the normal reader's counter-only result before supplements;
- `factor_reconciliation`: known original, supplemented and remaining gap counts,
  or null when the original factor input is unavailable, plus
  `original_capture_rewritten=false`;
- `raw_horizons_unchanged`: comparison of complete raw horizon observations
  inside the process, with no prices or return values in the receipt.

`checked-stock-history-reconcile` requires the combined reader's original full
history qualification, all planned queries checked or validly empty, an intact
point budget and unchanged raw observations. Shared unselected master quarantine
still yields `available-with-quarantine`. Complete factors cannot excuse missing
prices, suspension conflicts or unknown/stale freshness. Baseline and final
states describe the **same input operation**, with `original_batch_revalidated=false`.
They are not two independent supplier observations or two independent sessions.

The private collector separates these existing reading payloads from the
counter-only public wrapper. It performs no file, SourceInput or store write.
The manual host receives only the report and uploads only `result.json`.
Generated arguments and successful counts are not replayable captures or saved
SourceRefs. True independent source recovery and #7/#13 adoption still need
source-specific retention rights and actual original custody records.

`test_market_history_reconciliation.py` passes the collector's actual synthetic
HTTP outputs directly to the ordinary CLI and existing custody. It removes the
input files and reads each venue's root in a fresh process without credentials,
checking the same view and zero source requests. Budget boundaries, baseline
reuse, failed points, original history dependencies, shared deadline, refusal,
long Retry-After, receipt redaction and lost-point degradation are exercised.
These extend the existing factor semantics tests rather than replacing their
proof or claiming live supplier custody.

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

Retirement removes `stock-history` and `stock-history-reconcile`, their exclusive input/host/period module,
optional stock suspension map, tests and this documentation together. Retain
shared single-session suspension helpers until their existing consumer also
retires. Do not delete historical source operations or their failures; reverting
code cannot undo those effects. Global expansion remains lower priority than
unresolved A-share coverage, semantics, custody and real consumer acceptance.

## 精确因子补查与来源组合

已有完整区间捕获仍可能缺少交易日。只对原缺失日使用同源点查询，保留原缺口、不填值；
见[因子日期核验与补充读取](ashare-factor-reconciliation.md)。普通reader的可选
`factor_supplements`与有限`factor-audit`宿主共用同一检查，不把诊断成功代签原批次重放。
