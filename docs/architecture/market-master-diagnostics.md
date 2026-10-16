# Market master diagnostics

The existing non-publishing `market_source_check` now reports `master_shape` for
an inspected `stock_basic` capture in the existing `stock` and `sample` modes.
No new workflow, selector, transport, retry policy, data store or execution
permission is introduced. See [the operational check](market-source-check.md).

## One set of strict row checks

`market_security_inputs._master_row_checks` is shared by the original
`qualified_security_master` and the diagnostic consumer. It preserves the original
ordered checks: security syntax, venue/symbol/status identity, CNY, bounded name,
date parsing, required listing date, delisting consistency, and duplicate security.
The strict reader still rejects the entire supplied slice at its first failed
check and preserves its output shape, source lineage and version for valid input.
Its callers, including retained-market Odds, do not gain a partial-master bypass.

The source-declared L/P/G rows still require an absent delisting date; D requires
both dates in order. In particular, a parseable future delisting date in an L row
is not silently erased or approved as a planned event. Such an interpretation
needs separate evidence; this diagnostic neither supplies it nor loosens the rule.
Missing dates are not zero dates. Invalid dates do not become absent dates.

A predicate with an invalid prerequisite is `None` internally. Diagnostics count
it as not assessable, not passed. Strict reading necessarily sees that
prerequisite's failed check first. For example, invalid security syntax makes
venue matching and duplicate checking unavailable; an invalid delisting date
makes consistency unavailable while date parsing fails. Valid missing listing
dates still trigger the existing required-date rule where applicable.

## Complete received-row accounting, not qualified coverage

`master_shape` traverses all rows in all inspected pages, including rows after a
qualification failure. `rows_checked` is the received-row denominator;
`rows_with_issues` counts each row with at least one failed predicate once.
`reason_counts` and `not_assessable_counts` use only the fixed code-owned check
names. Failures can overlap and their sum is not a count of securities.

The eight fixed master fields have missing/null/boolean/integer/number/string/
other type buckets. Each field's buckets sum to the received-row denominator.
`date_counts` for list and delist dates separately distinguish missing, invalid,
on-or-before the explicit reading end, and after that end. These are counts only;
no supplier date, identifier, name, currency value or per-row position is returned.
A date after the reading end does not prove a future corporate-action schedule
relative to acquisition, or that a stock is tradable. Date counts are independent
of other row failures and do not certify the listed status.

Duplicate counts refer to later occurrences of each syntactically valid code,
across page boundaries and including otherwise invalid rows. The original JSON
representation of the eight requested columns is compared to the first occurrence
only in memory. Identical/different counts are not counts of distinct conflicting
issuers, nor evidence of which record is correct or latest. Invalid codes do not
participate in matching. Source rows are never sorted, deduplicated, repaired,
exported or saved by this diagnostic.

`received-pages-only` marks incomplete captures; `complete-capture-not-qualified`
means the generic capture terminated, not that the master passed market checks.
An empty capture has zero inspected rows, not a proved absence of securities.
Inspection rejects malformed capture envelopes before diagnostics; the counters
cannot override source admission, completeness, authorization or publication gates.

## Evidence, maintenance and exit

The first stock-only operation under #73 obtained a nonempty master but stopped
at `MASTER_DELIST_DATE`. Its original receipt contains no rows or offending dates;
these new counters cannot reconstruct its missing information. New source checks
require an explicit finite diagnostic purpose, not reruns to obtain a green result.
No field semantics or supplier root cause are inferred from that first failure.

`tests/test_market_master_diagnostics.py` compares the original ordered row
predicates across statuses and date/duplicate combinations, and checks simultaneous
failures, missing/invalid data, cross-page repeats, incomplete/empty captures,
unchanged source bytes, reference shape and every incremental real-host receipt.
All inputs are synthetic. CI, actual source invocation, source rights, long-term
custody and economic use remain separate evidence.

Retire `master_shape`, its exclusive tests and this document together if no longer
used. Retain the strict security-master reader and its checks while consumed by
market reading or Odds, as well as existing source/custody/CI protections. No new
source-independent master, archive format or compatibility claim is created.
