# Market radar: calendar-bound daily observation and explicit reading

This responsibility group transforms **already qualified** local daily closes into
three independent, browseable `sector`, `concept`, and `stock` surfaces. It is not
a live Relay adapter, a security/classification qualifier, a candidate-selection
policy, a source store, or a research/notification scheduler. No new dependency,
shared CLI change, CI policy change or second provider is introduced.

## Inputs and honest coverage

`observe_market_surface` in `decision_kernel.application.market_radar` requires:

- `kind`, `as_of`, `market`, `currency`, `adjustment`, `source_version`;
- an explicitly ordered, unique `market_sessions` list and `calendar_version`;
- `declared_entities`, `universe_version`, `coverage`, and `entities` price rows.

Each price row has exactly `trade_date` (canonical ISO date) and `close`. Closes are
positive, finite decimal-compatible values, at most 128 characters and a base-10
adjusted exponent from -99 to 99; arithmetic uses a private 34-digit Decimal
context. Currency is an uppercase three-letter declaration, not an FX conversion.
Versions are required **caller labels**, not authenticated SourceRefs or proof of
access, redistribution rights, source quality or completed market sessions.

The producer must qualify security versus issuer identities, venue/calendar,
classification namespace, listing/delisting/survivorship scope, source fields,
price convention and the actual completed-session cutoff. No weekday/holiday
heuristic or source/provider inference is performed here. The test calendar is
explicitly synthetic and is not suitable for market acquisition.

Every declared entity appears in the unpaged result, even when no price rows were
received or its input is malformed. Rows outside the declared universe are refused.
`scope_count` is the full declared denominator; `received_count` counts supplied
entities. `observed_count` means at least one supported absolute window,
`missing_count` means no supported absolute window, and `incomplete_count` counts
entities missing any requested absolute window. `horizon_counts` retains absolute
and relative coverage separately for each window. Entity-data failures are isolated;
invalid shared context fails the call, including when the universe is empty.

`full-declared-universe` describes the caller's scope, **not all A shares** or a
successful complete collection. `partial-universe` and stock-only `watchlist` are
separate declarations. Empty input, missing data and an empty filtered page never
mean zero market opportunities. `discovery_claim` remains
`not-established-by-supplied-universe`. Sector/concept outcomes do not gate stocks.

## Market windows, benchmark and membership

`observe_daily` uses 5/20/60 **declared market sessions** by default; callers can
request other distinct positive integer windows. Exactly N+1 calendar sessions,
including the cutoff, are needed for an N-session ratio. Missing calendar history
and missing prices are different results. Missing any session in a window blocks
that window, not a shorter complete window. A missing cutoff is not filled from
an earlier close. Valid future rows are ignored for calculations; duplicate rows,
including future duplicates, are rejected. In-range prices on undeclared market
days are rejected; data outside the calendar slice is not used.

Returns are `(end / baseline - 1) * 100`, with exact baseline/end dates. They are
**price ratios, not total returns, profit forecasts or trade signals**. `raw`,
`qfq`, `hfq` describe supplied data; this code does not adjust prices or prove
corporate-action qualification. Future-factor/look-ahead checks belong to the
upstream source qualifier, not a date filter over supplied closes.

An optional benchmark is a mapping with exactly `entity_id`, `series`,
`source_version`, `adjustment`, `market`, `currency`. Missing metadata is invalid.
Different currency, market or adjustment labels make relative performance
unavailable without destroying absolute windows. This conservative first slice
does not infer that raw index and adjusted-stock conventions are comparable.
Malformed benchmark rows or incomplete benchmark sessions also restrict only
relative output. Qualified matching windows report benchmark change in percent
and **percentage-point difference**, not a compounded relative wealth return.

Sector/concept membership, when supplied, requires `membership_version` and
`membership_as_of <= as_of`. Its keys must name declared groups; securities may
belong to multiple groups. No mapping means unknown; a present group with `[]`
is explicitly empty, while an absent group key remains unspecified. Stock inputs
are securities, not group-member lists. `membership_overlap` validates both maps
(including against an empty counterpart), rejects duplicate/bad members, and
reports exact security-ID intersections. Callers retain both snapshots/dates;
it does not infer issuer equivalence, historical membership or economic exposure.

## Direct read-only consumer

`read_market_surface(payload, offset=0, limit=50, entity_id=None)` accepts a mapping
of the surface arguments above. JSON arrays for horizons are accepted; scalar
coercion, unknown fields and invalid pages are rejected. Stable ID-sorted paging
preserves full-scope counts and supplies `selected_count` and `next_offset`.
It computes one explicit surface; it never requires another surface's success.

```sh
python -m decision_kernel.interfaces.market_cli --input qualified-surface.json --limit 50
python -m decision_kernel.interfaces.market_cli --input qualified-surface.json --offset 50 --limit 50
python -m decision_kernel.interfaces.market_cli --input qualified-surface.json --entity-id 000001.SZ
```

The separate module entrypoint avoids changing the shared CLI. It reads at most
2 MiB from the explicitly named local UTF-8 JSON file and prints JSON to stdout.
No input file or records are modified. Duplicate JSON keys (also nested), nonfinite
JSON constants, invalid UTF-8/JSON and oversized input fail explicitly with exit
2 and a stable error on stderr. The size limit bounds this local reading slice,
not the future full-market acquisition scope. Subprocess tests read all three
surfaces outside the working directory and verify input bytes are unchanged;
this is an engineering consumer, not evidence of actual #7/#13 research adoption.

## Reuse, validation ownership and next responsibilities

The existing Kernel reference is
`auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8`,
`src/decision_kernel/runtime/stock_radar_reading.py` (market-session windows,
window-local history gaps, full-plan denominators) and the independent-stock
consumer contract. This slice retains those semantics, not its HiThink adapter,
sector admission gate, thresholds, old state, timers or business schema. The
existing OS2 pure observation/application/module-CLI layout and Python standard
library supply the narrow implementation; a dataframe/calendar/vendor platform
would not supply this application's source-qualification or missingness policy.

`tests/test_market_radar.py` is owned by the market observation/reading capability.
It preserves the original baseline/relative/future/duplicate/coverage cases and
adds calendar gaps, independent windows, benchmark scope, empty validation,
declared denominators, membership and subprocess/JSON counterexamples. Retire
these modules, this document and exclusive tests together when their capability
retires. Shared CI, source/custody protection and original evidence remain.

**Outstanding #10 responsibilities, not waived by merging this engineering group:**

1. Reuse #12's [Relay registry](../sources/relay-registry.md), not a copied registry.
   Confirm actual `stock_basic`, `trade_cal`, `daily`, `adj_factor`, industry and
   concept/index/member endpoints, fields, dates, pagination, limits and rights.
   Enabled catalogue metadata is not a successful business request. The current
   shared `RelayHTTP.request` financial/disclosure allowlist is not widened here.
2. A bounded, source-qualified Relay market request/normalization adapter and
   actual source retention through the existing #6 writer/SourceRef contract;
   no source-version label in this module substitutes for a real receipt.
3. Actual independent market discovery and #7 research or #13 reading adoption,
   with qualified denominators and positive as well as missing/negative cases.
4. Relevant global indices, rates, sovereign bonds, FX, commodities and crypto,
   each with its own qualified calendar, unit and consumer. Unconnected families
   remain explicit rather than being counted as completed by this local slice.

Real source requests, public records writes, schedules, notifications, new
research, deployment and old-system cutover require their applicable explicit
scope. This group performs none of those actions and does not close #10.
