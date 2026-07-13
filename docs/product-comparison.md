# Product comparison and reporting rules

HF-026 reads HF-025 snapshot rows for explicitly requested execution trace IDs.
The database query is bounded by those IDs and deterministically ordered before
the pure comparison function runs. Rows for an execution that was not requested
are rejected rather than silently entering the report.

## Product identity

Products are matched using the first available key in this fixed order:

1. GTIN;
2. normalized brand plus MPN;
3. normalized brand plus SKU;
4. canonical URL;
5. normalized brand plus product name;
6. normalized product ID as the final fallback.

Text keys use Unicode-aware case folding and collapse punctuation/whitespace.
Within one execution/source-artifact pair, the first row in stable normalized-ID
order wins for a repeated match key. Later rows are counted and reported as
duplicates. Across sources, observations with the same key form one comparison
group.

## Prices

Only offers whose amount and currency statuses are both `valid` participate.
For multiple offers in one currency, the lowest amount represents that product
observation. Prices are never converted: comparisons are made independently per
currency and require at least two source observations in that currency.

The absolute difference is `maximum - minimum`. Percentage difference is
`(maximum - minimum) / minimum × 100`, rounded to two decimal places with
trailing zeroes removed. A zero minimum produces `null`/`n/a`, avoiding an
undefined percentage. Missing usable prices and multi-currency groups remain
visible as structured warnings.

## Report artifacts and result envelope

`render_product_report` first stores the exact comparison dataset as an
intermediate JSON artifact derived from every covered raw source artifact. It
then stores the Markdown report as a final artifact derived from that dataset.
The standard `ExecutionResult` contains both artifact summaries and the
Windmill job reference. A warning-bearing or empty comparison is presented as a
partial result; a clean non-empty comparison is a success.
