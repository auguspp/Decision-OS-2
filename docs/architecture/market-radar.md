# #10 Market Radar — first deterministic daily observation slice

Scope: **engineering transformation only**, from already qualified daily closes. This does
not itself access Relay, capture source bytes, persist records, claim complete universe
coverage, or run a scheduled discovery process.

## Consumers and three independent entrypoints

`observe_market_surface(kind="sector" | "concept" | "stock", ...)` returns a
scoped and browseable surface. Stock is **not** gated on sector or concept hits.
Every input identifies `source_version`, `universe_version`, `coverage`,
`adjustment` and `as_of`. Membership is optional exact-code linkage;
`membership_overlap` reports literal intersection only, not common economic exposure.
Empty or watchlist input never implies no market opportunity.

`observe_daily` computes returns from exactly N **observed sessions**, records
both dates, and only computes benchmark-relative percentage-point excess when
benchmark dates match. No future data, stale end date or fabricated baseline is
allowed. `raw`, `qfq`, `hfq` are explicit caller declarations, **not**
adjustments performed by this module. Callers must prove normalized identities,
survivorship, membership effective dates, comparable adjustment and dates.

## Data integration / outstanding acceptance

- #12 has archived Tushare Relay manual and service capabilities; availability in
  the catalogue is **not** business validation or permission to execute.
- Existing `RelayHTTP.request` only allows scoped financial/participant and
  disclosure APIs. Do not silently widen its allowlist. A separately reviewed #10
  market request adapter is required after confirming `stock_basic`,
  `trade_cal`, `daily`, `adj_factor`, sector/concept membership and index
  fields, pagination, limits, data rights and actual usable credentials.
- No production data was fetched or written in this responsibility group.
  Consumer integration with #7 research and #13 reading remains unverified.
- Genuine classification coverage, independent full-market stock discovery,
  global six-category background, storage SourceRefs and a real market-day
  end-to-end run are **still open #10 obligations**. Finishing this slice
  must not close #10 or be reported as a live radar.
