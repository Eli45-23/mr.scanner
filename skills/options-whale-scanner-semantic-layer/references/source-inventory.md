# Source Inventory

## Coverage

- Coverage level: Limited.
- Sources checked: June 22 scanner-review ZIP, matching local scanner logs, repository README, scanner outcome-review tool, and Alpaca SIP one-minute bars used for the June 22 review.
- Missing high-value lanes: A multi-session outcome dataset, next-day OI confirmations, and an approved benchmark for alert usefulness.
- Update boundary: Manual review of future archives; do not overwrite definitions automatically.

## Sources

| Source | Type | Locator | Supports | Gaps or caveats |
| --- | --- | --- | --- | --- |
| Scanner review archive | Local export | `exports/2026-06-22-scanner-review.zip` | June 22 scans and alerts | Single-day coverage. |
| Scanner logs | Local JSONL | `logs/options_whale_scans.jsonl`, `logs/options_whale_alerts.jsonl` | Export verification | Live logs can change after an export. |
| Scanner documentation | Local Markdown | `README.md` | Product behavior and safety boundaries | Not performance evidence. |
| Outcome tool | Local Python | `tools/review_options_alert_outcomes.py` | Outcome definitions and gaps | Requires full market-session windows. |
