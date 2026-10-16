# Relay market inputs and bounded stock reading

## One existing capture contract, one market consumer

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
[daily](https://tushare.pro/document/2?doc_id=27) definitions. Official daily is
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
Actual market field/response qualification, security master/adjustment factors,
industry/concept membership, SourceRef custody and research adoption still need
bounded successor groups under #10. Global indices, rates, sovereign bonds, FX,
commodities and crypto retain their separate unfulfilled obligations.
