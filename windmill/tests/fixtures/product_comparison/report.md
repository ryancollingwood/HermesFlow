# Product comparison report

## Summary

- Source coverage: 2/3 (1 empty)
- Snapshots read: 4
- Unique products: 2
- Duplicate products ignored: 1
- Products with comparable price data: 2
- Same-currency price comparisons: 1
- Warnings: 3

## Source coverage

| Source | Execution trace | Source artifact | Products | Unique | Priced | Duplicates |
|---|---|---|---:|---:|---:|---:|
| Store A | `10000000-0000-0000-0000-000000000001` | `30000000-0000-0000-0000-000000000001` | 3 | 2 | 1 | 1 |
| Store B | `10000000-0000-0000-0000-000000000002` | `30000000-0000-0000-0000-000000000002` | 1 | 1 | 1 | 0 |
| Empty store | `10000000-0000-0000-0000-000000000003` | — | 0 | 0 | 0 | 0 |

## Product comparisons

### Desk Lamp

- Match key: `brand_sku:example\|sku 3`
- Source observations: 1

| Source artifact | Product ID | Prices |
|---|---|---|
| `30000000-0000-0000-0000-000000000001` | `0000000000000000000000000000000000000000000000000000000000000067` | — |

Price differences:

- No same-currency price pair was available.

### Canvas Tote

- Match key: `gtin:123456789012`
- Source observations: 2

| Source artifact | Product ID | Prices |
|---|---|---|
| `30000000-0000-0000-0000-000000000001` | `0000000000000000000000000000000000000000000000000000000000000065` | AUD 10 |
| `30000000-0000-0000-0000-000000000002` | `0000000000000000000000000000000000000000000000000000000000000068` | AUD 12.5 |

Price differences:

- AUD: 10 → 12.5 (Δ 2.5, 25%)

## Warnings

- `[duplicate_product]` duplicate match key gtin:123456789012 ignored within source (source artifact `30000000-0000-0000-0000-000000000001`)
- `[empty_source]` requested execution has no persisted product snapshots
- `[missing_comparable_price]` product has no offer with both a valid amount and currency (source artifact `30000000-0000-0000-0000-000000000001`)
