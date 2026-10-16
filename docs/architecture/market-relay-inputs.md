# Relay market inputs and bounded stock reading

## One existing capture contract, bounded market consumers

`application/market_relay_inputs.py` consumes the full result emitted by
`research_sources.capture_plan`: `case_id`, validated `use`, `status`, and
`capture`. It also requires the existing `RelayRegistry`. It does not fabricate
research IDs to wrap a bare table. The unpublished Draft's bare-capture signature
is replaced; no compatibility layer or second registry is retained.

The module delegates generic request identity, requested-column projection,
page sequence/termination, reported counts, clocks and transport-envelope checks
to `research_sources.inspect_result`. The market layer adds only its narrower
query and business-field contract. The two public qualifiers return normalized
views, not source originals, custody receipts, listings or investment signals.

`qualified_trade_calendar(result, registry=..., exchange=..., start=..., end=...)`
requires SSE or SZSE, compact `YYYYMMDD` dates and the exact requested interval.
The request must include `exchange,cal_date,is_open` fields, start at offset zero,
and contain no extra business filters such as `is_open=1`. All calendar dates,
including closed days, must occur exactly once with the requested exchange and
an explicit 0/1 flag. An empty response is missing calendar data, not a holiday.
A complete all-closed interval legitimately returns no open sessions.

`qualified_daily(result, registry=..., ts_code=..., start=..., end=..., calendar=...)`
requires one six-digit SH/SZ code and the exact same requested date interval.
It accepts only raw `daily` closes with explicit `ts_code,trade_date,close`
fields; `adj`, a second date filter, another API or nonzero initial offset is
refused. The supplied calendar must contain canonical, ordered, unique dates
inside the interval, even when prices are empty. Price rows must match the code
and an open date, with no duplicate date. Existing `market_daily` positive finite
Decimal validation is reused rather than implementing another numeric policy.
Missing prices are not filled. A legitimate empty terminal page may have no
returned fields; nonempty rows must have the requested fields. Short/terminal
page semantics remain owned by the existing generic inspector.

## From captures to a real reading entrypoint

`read_relay_stock_surface` takes `calendar_result`, `stock_results` keyed by code,
`registry`, `exchange`, `start`, `end`, `declared_entities`, and `coverage`.
Optional `horizons` defaults to 5/20/60; `offset`, `limit` and `entity_id` use the
existing `read_market_surface` pagination. The explicit end must be an open
calendar day. Each declared code must belong to the declared SH/SZ venue.

A malformed shared calendar rejects the surface. Missing or rejected individual
stock results remain in the full declared denominator and carry distinct source
qualification states. Valid empty stock captures are different from a rejected
capture. Page counts retain supplied/accepted/rejected/not-supplied totals.
Only `partial-universe` and `watchlist` are admitted: this reader cannot certify
`full-declared-universe`, all A shares, or a candidate-selection policy.
The independent stock reader has no sector/concept admission gate; it does not
provide new industry/concept classification or membership data.

Reading is raw CNY price-ratio context. Code syntax and a matching response do
not prove a security master, listing class, issuer identity, historical membership,
corporate-action adjustment, total return or point-in-time availability. The end
date is explicit, not certified as the latest completed session. A caller must
establish these other qualifications before any corresponding stronger use.

Each accepted code and the calendar retain query identity, actual capture clocks,
transport metadata and a deterministic inspected-view hash. A changed view gets
a new source version. These hashes detect byte/view differences; they do not
authenticate a publisher or constitute SourceRefs. Rejected input has no accepted
lineage. Input mappings are not modified.

The standalone module reads local JSON, without credentials, network or writes:

```sh
python -m decision_kernel.interfaces.market_relay_cli \
  --registry docs/sources/relay-registry.json --input retained-market.json --limit 50
```

Input has exactly `schema="relay-stock-reading-v1"`, `calendar_result`,
`stock_results`, `exchange`, `start`, `end`, `declared_entities`, `coverage`, and
optional `horizons` array. The result objects are the existing full capture results,
not externally supplied URLs or a new source schema. The CLI reuses the original
market CLI's duplicate-key/nonfinite guards and 2 MiB read bound. Exit 2 returns
stable errors on stderr. Paging or filtering never changes the unpaged scope.
The size limit bounds this local reading entrypoint, not future market coverage.

## Source-declared security references and end-anchored adjustment

`application/market_security_inputs.py` adds `qualified_security_master`,
`qualified_adj_factors`, and `read_security_stock_surface`. They reuse the same
market `_view`/row/date/lineage helpers and therefore the same #12 inspector;
there is no second HTTP transport, registry, paginator or custody writer.

The master takes `results` keyed by explicitly supplied `list_status`: `L`
(listed), `D` (delisted), `P` (suspended listing), or `G` (not yet trading).
Each result must be a complete zero-offset `stock_basic` exchange/status slice,
with no extra code, name, market or historical-date filter. Required fields are
`ts_code,symbol,name,exchange,curr_type,list_status,list_date,delist_date`.
Returned symbol, suffix, exchange and status must agree; only SSE/SH and SZSE/SZ,
CNY, bounded printable names and canonical dates are accepted. L/D/P need a
listing date; D also needs a non-earlier delisting date. G can lack a listing
date. Non-D rows cannot carry a delisting date. Duplicate securities, including
across status slices, refuse the master rather than silently choosing one row.
A malformed supplied slice refuses this shared scope; omitted status slices are
not-supplied, not inferred empty. A complete empty slice is separately counted.

Every returned master security becomes the reading denominator, including G,
new listings, delisted securities and securities with no usable prices. There is
no caller override to replace it with a winner list. Nevertheless, even four
completed status slices do **not** certify all-market discovery, historical
membership, survival-bias removal or an atomic snapshot: stock_basic is observed
at its actual retrieval times, not at the reading cutoff. Each slice retains its
own query, receipt, timestamps and version. Names and codes do not establish
permanent security IDs or company/issuer equivalence. BSE and other markets remain
unsupported, not folded into a Shanghai or Shenzhen scope.

Daily price eligibility conservatively requires a reported listing date <= the
price date and, when present, price date < delisting date; the delisting day is
excluded because the reference is not proof of trading that day. G cannot supply
usable historical prices. A violating series is rejected for that security;
missing prelisting, delisted or halted-day prices are not manufactured. P is a
source listing-status label, not a daily trading halt calendar. Historical use of
retrieved dates remains reconstruction, not point-in-time knowledge certification.

Factors require the exact single code, start/end and zero offset, with requested
`ts_code,trade_date,adj_factor`. Dates must be unique in the qualified calendar;
values reuse the bounded positive-finite Decimal policy. Equal or decreasing
factors are allowed; no monotonicity, factor=1 default, or action-type inference
is imposed. Factors can exist where prices do not. Such rows never fill a price.

The reader emits companion `raw` and `qfq` surfaces using the existing observation
and pagination core. Qfq uses `close[d] * factor[d] / factor[end]`, a private
34-digit Decimal context, and the **explicit end date** only. It never chooses the
latest received factor, a future factor, or a previous factor as the anchor. The
source conventions may differ from market software; this is not independently
certified investor total return. Missing the anchor disables only this security's
adjusted output; the original prices stay readable. Other missing factors omit
only the affected derived dates so only crossing windows fail. Derived numbers
outside the existing numeric range refuse the adjusted series, not its raw peer.

Both surfaces keep identical scope and page selection with separate observed and
missing counts. Per-security reference, price/factor lineage and qualification
states distinguish absent, rejected, empty, missing-anchor and factor-gap inputs.
Global qualification counts survive pagination. Details list calendar sessions
without factors (which can include prelisting dates), price dates lacking factors,
factor dates lacking prices, and changes between *adjacent supplied calendar
sessions*. Changes are not labelled dividends, splits or rights issues. Raw source,
factor-adjusted source and reference versions remain separately traceable.

Use the existing CLI above with `schema="relay-security-stock-reading-v1"` and
`calendar_result`, `master_results`, `stock_results`, `factor_results`, `exchange`,
`start`, `end`, plus optional `horizons`. `master_results` is keyed by status and
the two series maps by security code. All values are retained full capture results.
Price/factor codes not in the master are refused. Do not add `coverage`,
`declared_entities`, or an adjustment label to this schema. Both conventions are
returned explicitly; `coverage` stays `partial-universe`. Empty scope does not
mean zero opportunities. The original `relay-stock-reading-v1` route remains
unchanged. No new CLI executable, file layout or source storage format is added.

## Source retention and permissions

The exact input results remain usable with existing
`research_sources.source_inputs(result, registry)` and the #6 writer. That path
exports explicitly labelled requested-column excerpts, not original HTTP bytes.
This group's test round-trips those exports through `SourceInput.from_dict`;
it does not claim a production SourceRef was saved or recovered. The existing
writer must separately check rights, perform actual storage, return a real
SourceRef and verify independent reading. Do not reconstruct a source original
from this normalized view or create a second source store.

No HTTP client, provider fallback, schedule, shared CLI route, CI policy,
dependency or private-data permission is added. Actual capture still belongs to
the existing Registry / RelayHTTP / Retry / capture_plan flow and its authorization
checks. These functions and this CLI perform no market collection, records write,
new research, notification, deployment or legacy-system switch.

## Evidence, reuse and maintenance

Source-field basis, checked 2026-10-16: the archived
[Relay manual](../sources/vendor/tushare-relay-v0.5.61.md), existing
[byte-bound service registry](../sources/relay-registry.md), official Tushare
[trade_cal](https://tushare.pro/document/2?doc_id=26) and
[daily](https://tushare.pro/document/2?doc_id=27),
[stock_basic](https://tushare.pro/document/2?doc_id=25),
[adj_factor](https://tushare.pro/document/2?doc_id=28), and
[end-anchored adjustment](https://tushare.pro/document/2?doc_id=146) definitions. Official daily is
unadjusted and does not supply suspended-day rows. Service enabled flags and
published definitions are not today's successful business request or permission
to redistribute. No actual market endpoint was called to certify this group.

Legacy Kernel's window/missingness semantics are already inherited by #45's
[market daily core](market-radar.md). This group reuses that core, the shared
capture inspector and existing SourceInput export rather than copying a vendor
SDK, generic paginator, dataframe platform, storage service or another numeric
validator. Standard library code supplies this narrow adapter and CLI.

`tests/test_market_relay_inputs.py` is owned by the market input/reading capability.
It preserves the Draft's seven semantic protections with full real-contract
fixtures and adds malformed/empty input, edited completeness and receipts,
zero-origin pagination, terminal empty fields, context/venue boundaries,
missing/rejected denominators and an external-directory subprocess consumer.
The actual HTTP/capture code uses an injected synthetic opener: no provider is
contacted. The synthetic calendar is intentionally not a real exchange calendar.

Retire the adapter, module CLI, this document and exclusive tests together when
this capability exits. Shared source, numeric, custody and CI protections remain.
`tests/test_market_security_inputs.py` owns reference/factor and paired reading
protections: explicit status coverage, malformed/duplicate references, listing
intervals, missing anchor/factors/prices, 5/20/60 session windows, separate versions,
range/context guards and both exchanges. It uses the original market test opener
with real Registry/RelayHTTP/Retry/capture_plan and a new-process existing CLI.
An artificial 100-to-50 close with a 1-to-2 factor tests normalization, not a real
corporate action. Both test modules share this synthetic capture fixture; preserve
or move that fixture if only the raw reader retires. The entire reference/factor
module and these exclusive protections leave together when that capability exits.
Official SDK/pandas transport or forward-filling would change the existing Relay
and gap policies; the narrow Decimal join is therefore local, not a new data platform.

Production market field/response qualification and retained source-reference
restoration, identified corporate-action events, issuer/permanent-ID mapping,
industry/concept membership, SourceRef custody and research adoption still need
bounded successor groups under #10. Global indices, rates, sovereign bonds, FX,
commodities and crypto retain their separate unfulfilled obligations.
