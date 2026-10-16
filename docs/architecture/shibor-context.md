# Shibor context: rate points, explicit dates and exact source recovery

Owner: #10 market context; the narrowly scoped field-name compatibility fix belongs
to the existing #12 source reader. This is one rates-family slice, not all global
rates, a discount curve, a bond-return model or a company funding-cost estimate.

## Source and reuse basis

Checked 2026-10-16: [Tushare Shibor fields](https://tushare.pro/document/2?doc_id=149)
define `date,on,1w,2w,1m,3m,6m,9m,1y` and `start_date/end_date` queries.
[CFETS Shibor](https://www.chinamoney.com.cn/english/bmkshb/) describes the RMB
simple unsecured interbank benchmark and its eight maturities; its display uses
rate percent and change in basis points. These references explain the adapter;
they are not evidence that this repository has obtained current quotes or rights
to redistribute a supplier's data.

Use the current Relay Registry, HTTP/retry/capture/inspector and approved page
exporter, not a second SDK or direct CFETS scraping client. The existing bounded
`research_numbers.number` and `arithmetic_context` supply Decimal handling.
The stock positive-price parser and price-ratio returns are inappropriate here.
No statistical, calendar, curve-fitting, storage or scheduling dependency is needed.

The source registry's enabled declaration did not prove field compatibility:
`research_sources` originally required every field to start with a letter.
Its shared request/response name predicate now additionally admits **only**
`1w,2w,1m,3m,6m,9m,1y` for **only** `shibor`. It does not loosen parameter names,
sensitive-column denial, projection, unrelated APIs or arbitrary digit-leading
names. Original metadata, source rows, retry policy and authorization remain intact.

## Reading contract

`read_shibor_context` is routed through the existing `read_market_payload` and
`market_relay_cli`. Supply the existing capture-result object, not a freehand list
of quotations, in this envelope (placeholder below is deliberately not runnable):

```json
{
  "schema": "relay-shibor-reading-v1",
  "rate_result": "REPLACE WITH THE EXISTING CAPTURE-RESULT OBJECT",
  "start": "20261001",
  "end": "20261015",
  "baseline_dates": ["20261008", "20261012"]
}
```

The requested columns must contain all nine documented fields. The capture must
start at offset zero, terminate completely, retain the exact registry snapshot
and bind exactly the stated start/end scope. Unknown filters, partial capture,
malformed/out-of-range dates and duplicate dates anywhere in received pages refuse
the shared input. The interval is bounded to 3,660 calendar days; one to twelve
unique baseline dates must lie within it and strictly precede end.

These are **explicit endpoint comparisons**, not rolling 5/20/60-session windows.
A baseline missing from returned dates is `date-not-returned`; there is no nearest
quote, last observation, weekday, exchange-calendar or forward-fill substitution.
Missing intermediate dates do not invalidate a pair whose two endpoint quotes
exist. `elapsed_calendar_days` describes elapsed dates, never trading sessions.
Absent dates are not presumed holidays or source failures. Source dates remain
Asia/Shanghai calendar dates, not invented publication timestamps or historical PIT.

For each of the eight tenors, expose the end quote and each baseline quote.
Numeric zero and negative values are valid numerical inputs, not missingness or
an assertion that negative Shibor was actually published. Missing null/empty cells
and invalid numeric cells are distinct; invalid source text is not echoed as a rate.
All received dates are scanned, including non-baseline rows, with whole-scope
per-tenor valid/missing/invalid cell counts. Invalid cells limit only comparisons
using those cells; structural capture/date failures are shared failures.

`change_pp = end_rate_percent - baseline_rate_percent` and
`change_bp = change_pp * 100`. For example, synthetic `1.00` to `1.25` is
`+0.25` percentage points / `+25` basis points, **not** an investment return.
An independent 80-digit Decimal context is used; inexact extreme arithmetic is
`arithmetic-unavailable`, not silently rounded into an observation. There is no
compounding, FX conversion, interpolation, bootstrapping or economic approval.

At each requested date, two source-quote spreads (`3m - on`, `1y - on`) use the
same point subtraction, with both operands and unavailable statuses retained.
A spread is not a causal explanation or a risk-free discount curve.

Every baseline's four comparison-status buckets sum to eight, and the end-quote
buckets also sum to eight. `capture-checked` certifies capture/date structure,
not all numeric cells or economic validity. A complete empty capture keeps all
eight tenors unavailable instead of dropping the scope. Pagination or a tenor
filter does not change full counts, spreads or the content version.

## Existing consumers and custody

```sh
python -m decision_kernel.interfaces.market_relay_cli \
  --input /approved/shibor-reading.json \
  --registry /checkout/docs/sources/relay-registry.json --entity-id 3m
```

This is an offline read of approved input. Exit zero means the view was read;
callers still inspect quote/comparison statuses. No workflow or source operation
is dispatched by it. Output contains source quotations and lineage and is not
cleared for public upload merely because the command succeeded.

The existing [market custody](market-custody.md) `save/read` route now visits the
single `rate_result` slot and uses the original query/root manifest versions,
SourceInput/SourceRef, journal/CAS, byte bounds and fresh readers. No new store or
manifest format is introduced. Preserve the original explicit public-rights gate:
a boolean expresses an already-established permission, not a supplier licence.
`--path` refers only to an initialized synthetic local GitStore; real data targets
continue to use their existing approved bindings.

An independently started read needs the exact root SourceRef, not the old capture
file or a Relay key. Missing rate-source pages produce `partial-restoration`,
`view=null` and a read error, with zero new source requests; they do not produce
an all-zero curve. Snapshot/page/identity checks and unknown-write reconciliation
are inherited unchanged. A retained requested-column excerpt is not original HTTP
bytes, a second live capture or proof of indefinite vendor availability.

## Verification, remaining work and retirement

`tests/test_shibor_context.py` exercises real synthetic Relay capture, signed
arithmetic, field compatibility and denials, explicit dates, partial cells,
empty/partial capture, full-scope counts/paging, both ordinary CLI operations,
source loss, rights refusal, immutable prior refs and lost-ack reconciliation.
A temporary real GitStore test removes the input and starts another process to
read only the root reference; no production records or supplier data are used.

Engineering tests do not certify live Shibor acquisition, original-source units
at a particular retrieval, lawful long-term public custody, independent later
source recovery, Brief adoption or economic use. Prior stock-master/THS gaps and
US count-only receipts are unaffected. Real operations require their own bounded
purpose, source access, applicable rights and receipt; no new operation is implied.

Retirement removes this reader, route, single-slot custody adapter, the Shibor-only
field exception, this document and its dedicated tests together, after accounting
for actual users of the exception. Keep general source/Decimal/custody protections,
domestic and US readers, existing records and historical failures.
