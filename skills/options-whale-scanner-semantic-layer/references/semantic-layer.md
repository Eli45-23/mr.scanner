# Options Whale Scanner Semantic Layer

## Quick Reference

- Area: Options Whale Scanner performance and alert quality.
- Intended users: Scanner operator and analyst.
- Coverage level: Limited — one reviewed session archive, matching local logs, and scanner implementation/docs.
- Default time zone: Treat stored timestamps as UTC; present session analysis in America/New_York.
- Freshness expectation: A regular-session outcome window is usable only when its full horizon ends before 4:00 PM ET.

## Entity Clarification

| Entity | Means | Does not mean | Grain |
| --- | --- | --- | --- |
| Scan | One scanner run and its filter diagnostics | One independent market opportunity | Scan timestamp |
| Alert record | One logged scanner observation | Necessarily a new trade event | Logged row |
| Distinct flow event | First appearance of `option_symbol + trade_time` | A guaranteed opening trade | Option trade event |
| Fresh flow | `fresh premium print` at detection | Confirmed directional trade | Option trade event |
| Delayed/stale flow | `old trade print` or `stale / old premium print` | Useless research evidence | Option trade event |

## Key Metrics

| Metric | Definition | Grain | Canonical source | Caveats |
| --- | --- | --- | --- | --- |
| Contracts evaluated | Sum of `contracts_evaluated` across scans | Scan | `options_whale_scans.jsonl` | Repeated scans evaluate many of the same contracts. |
| Filter pass rate | Sum `passed_filter_count` / sum `contracts_evaluated` | Session | Scan export | Not a precision or profitability metric. |
| Distinct events | Unique `candidate.option_symbol + candidate.trade_time`, using first detection | Session | Alert export | Related multi-leg flow can still create multiple events. |
| Repeat-observation rate | `(alert records - distinct events) / alert records` | Session | Alert export | Measures delivery noise, not bad data. |
| Freshness mix | Distinct events grouped by `candidate.fresh_flow_label` | Session | Alert export | Small stale samples must not drive tuning. |
| Directional follow-through | Signed underlying return after detection; bullish uses raw return, bearish uses inverse return | Event/window | Alert export + SIP one-minute bars | Underlying move is not option P&L or a trade backtest. |

## Standard Filters And Dimensions

| Dimension | Default logic | Override when |
| --- | --- | --- |
| Analysis date | Filter detection timestamp to the requested market date | Reviewing trade-time latency or overnight outcomes |
| Event identity | Deduplicate on option symbol and trade time, retaining first detection | A question explicitly concerns rescans or follow-through updates |
| Outcome eligibility | Require a complete 5/15/30/60 minute regular-session window | A separate after-hours methodology is approved |
| Freshness | Compare Fresh, Delayed, and Stale separately | Sample sizes are too small; report as insufficient |

## Data Sources

| Source | Use it for | Caveats |
| --- | --- | --- |
| `exports/2026-06-22-scanner-review.zip` | Frozen session source of truth | Covers one session only. |
| `exports/2026-06-22-scanner-review/options_whale_scans.jsonl` | Scan operations and filters | Export matched local log on June 22 review. |
| `exports/2026-06-22-scanner-review/options_whale_alerts.jsonl` | Event quality, freshness, scoring, direction | Alert rows require event-level deduplication. |
| `data/options_whale_outcomes.jsonl` | Native outcome-review status | Late-session windows may be insufficient. |
| `tools/review_options_alert_outcomes.py` | Outcome-window methodology | Does not establish profitability. |
| `README.md` | Scanner purpose and operational constraints | Product documentation, not performance evidence. |

## Gotchas

- Do not report raw alert rows as independent signals.
- Treat direction labels as probabilistic until next-day open-interest confirmation.
- Separate stale quote rejections from stale trade prints; they describe different quality issues.
- Do not score post-close or truncated outcome windows as failures.
- Do not use one day to change thresholds without multi-session validation.

## Open Questions

- Does fresh flow outperform delayed flow over at least 20 regular sessions after symbol-level correlation is controlled?
- Which score components predict favorable movement versus repeated but low-information alerts?
- Which related strikes should be clustered into one notification?
