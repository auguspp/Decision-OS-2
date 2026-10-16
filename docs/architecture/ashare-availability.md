# A-share availability without hiding source defects

Owner: #10; priority is domestic availability/reliability before more global
families. This extends the existing security/THS readers and manual source check,
not a second source, storage system, candidate engine, scheduler or default policy.

## Strict acceptance and explicit isolation are different purposes

The existing `qualified_security_master` and `qualified_ths_catalog`, and all
payloads without `reference_policy`, remain strict. The stock-security and THS
payloads may explicitly set `"reference_policy": "isolate-identified"`.
`relay-three-market-reading-v1` accepts that setting separately on each supplied
surface. The ordinary `read_market_payload` / `market_relay_cli` is the consumer;
there is no parallel observation or adjustment algorithm.

A complete, scope-consistent capture is inspected with the original value
predicates. An identifiable security/group with invalid metadata is quarantined.
Every occurrence of a duplicate code is quarantined, including the first and
byte-identical duplicates: no first/last choice, status priority, silent dedup,
cleared dates, replacement currency, or synthesized identity. The supplied rows
are not changed. Quarantined codes cannot use otherwise-valid price/factor/index
or member inputs to bypass their reference failure.

Unparseable identity, a security-code venue outside the requested exchange,
incomplete/truncated/stalled capture, mutated query/snapshot, or missing shared
calendar still refuses the affected shared scope. SH/SZ identifiers inherit the
original supported contract; this does not certify Beijing coverage. A source
status row is not a permanent issuer ID or historical membership certificate.

All identifiable codes remain in the declared denominator, including quarantined
ones. `security_reference` / `classification` is null for those entries, with
explicit `qualification.master` / `qualification.catalogue` and fixed reason
codes. Their horizons have no computed change; the original observer's missing
required inputs are not evidence that the exchange itself omitted observations.
Other qualified entries continue to use the original raw/qfq/index calculations.

`master.quality` / `catalogue.quality` distinguish:

- source rows and unique code scope;
- eligible and quarantined code counts;
- total rows belonging to quarantined codes and duplicate-code groups.

The two reconciliation identities are `eligible_count + quarantined_count =
scope_count` and `eligible_count + quarantined_row_count = source_row_count`.
Repeated rows are not counted as distinct securities. An empty complete slice,
a missing slice, and an incomplete received prefix are not interchangeable.
The view remains `partial-universe`: eligibility is not full-market price coverage,
and a missing status/exchange or unrequested security is not zero opportunities.

## Ordinary reading and original custody

Add the policy to an otherwise valid `relay-security-stock-reading-v1` or
`relay-ths-group-reading-v1` input. Then use the existing host, for example:

```sh
python -m decision_kernel.interfaces.market_relay_cli \
  --input /approved/local/input.json --registry docs/sources/relay-registry.json
```

Pagination or an explicit entity filter retains the original scope, reference
qualification, source/universe versions and unrequested-input gaps. No implicit
latest date or replacement observation is introduced.

The original `market_custody_cli save/read` and query/root SourceRef manifests
retain the explicit policy along with every original capture page. A fresh
reader can reconstruct the same view, including quarantine, without the input
file or Relay credentials. Missing required reference/calendar pages remain a
partial restoration with an unavailable view, never an invented empty market.
Existing public-rights preflight, immutability, journal/CAS reconciliation,
input/request budgets and independent readback are unchanged. A synthetic local
Git test is not real supplier custody or proof of redistribution permission.

## Bounded operational checks, not full-market certification

The existing main-only manual `market-source-check.yml` has two additional choices:
`availability` uses its ten domestic slots (calendar, master, two catalogues,
stock price/factor and one index/member sample per group kind).
`stock-availability` uses only four stock slots, so a second venue check need not
repeat undated THS directories. Original sample/catalogues/stock/US modes retain
their prior behavior and defaults.

New choices explicitly consume the ordinary isolated reader. Samples are the
first sorted eligible code with a reported listing date at/before the requested
start. They are not a watchlist or discovery output. A code failing reference
qualification cannot be selected. No eligible sample is a recorded gap, not a
fallback to user-supplied names or a later listing date.

Only these new modes request stock_basic/ths_index in 500-row pages (up to 20),
rather than 5,000-row pages (up to two). The 10,000-row ceiling, generic 8 MiB
capture bound, shared 600-second deadline, per-page initial plus three attempts,
minimum 30-second/longer valid Retry-After waits and global auth/rate-limit stops
remain. A final full page at the cap is incomplete, not presumed terminated.
This reduces individual response size; it does not establish the root cause of
prior server timeouts or guarantee that pagination is supported correctly. No
second API/key/host or automatic retry of the old full-size query is used.

The single allowlisted `result.json` exposes quality counters and each selected
sample's 5/20/60 session state, not its identity, source dates, prices, return,
lineage or capture. Raw and qfq must both have all three horizons observed for
the stock sample; each group also requires its original member query. Shared
input failures retain null scope/window counters. Short usable windows survive
a long-window gap, but that sample does not pass complete-horizon acceptance.

`sample_readable=true` / exit 0 means only that these bounded samples were read.
`strict_reference_passed=false` and `available-with-quarantine` explicitly retain
reference defects; `checked-availability-sample` additionally has no quarantined
references in the supplied scope. A clean empty source is not sample-readable.
Neither status certifies all securities' prices, issuer economics, source
corrections, long-term availability, retained data, or Brief/research adoption.

Actual source runs and their immutable artifact identities belong in #10. No
supplier rows or SourceRefs are exported by this checker. Access permission does
not establish public custody rights; do not replay successful windows merely to
manufacture an archive, or rerun failures solely to change a green indicator.

## Verification and retirement

`test_ashare_availability.py` exercises original capture/value predicates,
whole-code conflicts, complete scope/pagination, original custody, and a real
fresh CLI process after deletion of the input. `test_ashare_availability_check.py`
exercises synthetic HTTP, multi-page capture, ordinary readers, all three
surfaces, SH/SZ scope, horizon gaps, stalls, terminal failures and redacted host
receipts. Existing strict readers and source checks remain regression consumers.

Retirement removes the opt-in routes/slot policy, inspectors, exclusive tests,
manual choices and acceptance labels together, preserving strict consumers and
historical source data. Do not reinterpret an already retained isolated recipe
as strict acceptance. Reverting source code does not undo previous source calls
or stored artifacts. Global expansion remains lower priority than the unresolved
A-share source, coverage and operational acceptance work.
