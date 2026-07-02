# Options Whale Scanner

A private, read-only options-flow scanner and dashboard for spotting unusual options activity. The scanner discovers optionable contracts, reads options market data, scores possible whale flow, explains why a flow may matter, and tracks outcomes over time.

> Watch only. Not a trade signal. This project does not place trades.

## Current status

This repo is focused on the Options Whale Scanner workflow.

| Area | Status |
|---|---|
| Historical unusualness baseline | Wired into scan scoring/output |
| Bid/ask aggression classification | Implemented with quote-staleness safeguards |
| Premium timing and freshness | Implemented; stale/old prints are clearly labeled |
| Symbol search | Implemented on the main dashboard; accepts any ticker present in scanner data |
| Multi-leg/spread/roll detection | Implemented as conservative “possible” labels |
| SPY/QQQ/IWM/DIA 0DTE noise handling | Implemented with stricter 0DTE index thresholds |
| Opening vs closing estimate | Implemented as current-day estimate only |
| Price-action confirmation | Implemented when underlying bars/price context are available |
| Alert outcome tracking | Implemented for 5m/15m/30m/60m windows |
| Outcome diagnostics | Implemented; records returned bars, requested windows, and pending reasons |
| Outcome compaction | Implemented as a safe dry-run-first cleanup tool |
| Next-day OI confirmation | Automated for the prior trading session from 9:45 AM–12:00 PM ET, with retry and unresolved-contract handling |
| Last good regular-session scan | Implemented; stale after-close scans do not wipe useful in-session dashboard data |
| Reliability calibration | Implemented by score × DTE × confidence × regime × direction using +0.10% moves and executable option returns |
| Tier-1 quality gate | Requires aligned trend regime, 20 sessions, 30 paired effective samples, ≥30% meaningful moves, and ≥50% positive executable returns |
| Stateful notification updates | Implemented per trading-session contract and direction; repeated alerts require material premium and quality improvement |
| Adaptive symbol rotation | Implemented with persisted coverage, overnight reset, duration clamps, and per-symbol coverage age |
| Option outcome health | Exposed on the dashboard and data-health API, including unavailable option bars and executable-return coverage |
| Dashboard explanation clarity | Implemented on the main dashboard |

## Safety rules

This repository is intentionally read-only.

It does **not** include:

- broker order placement
- auto-trading
- buy/sell instructions
- account-management actions
- guaranteed prediction language

Market data access is used for scanning, dashboard display, exports, and outcome review only.

## Main workflow

Use the main dashboard:

```bash
cd /path/to/mr.scanner
python3 scanner_dashboard.py --open
```

Dashboard URL:

```text
http://127.0.0.1:8765
```

The dashboard provides:

- Alpaca/options data status
- option universe status
- scan controls
- filter controls
- a free-form ticker search box
- real whale-flow alerts first
- stale/old premium prints separated and labeled
- debug candidates hidden by default
- row-level explanations
- score breakdowns
- premium timing, pressure, follow-through, next-day OI, price context, and warning fields
- clearer scan metrics such as contracts evaluated, passed filters, near misses, and stale quote rejects
- option-bar availability, executable-return coverage, and endpoint-error health
- reliability tables with meaningful-move and executable-return proof
- per-symbol coverage age and adaptive-rotation diagnostics
- last-good regular-session scan display after market close when fresh quotes are stale

Use the outcome dashboard separately:

```bash
python3 -m tools.options_outcome_dashboard --open
```

Outcome dashboard URL:

```text
http://127.0.0.1:8775
```

## What the scanner looks for

The scanner analyzes options contracts for:

- unusual volume
- high premium
- volume/open-interest imbalance
- bid/ask aggression
- possible sweep-like activity
- possible block prints
- possible multi-leg, spread, hedge, or roll structures
- 0DTE index/ETF noise
- opening vs closing clues
- underlying price-action confirmation
- historical unusualness versus local baseline data
- premium timing and quote freshness
- same-contract follow-through

All signals are probabilistic. Large options prints can be opening flow, closing flow, hedges, spreads, rolls, dealer positioning, or noisy index/ETF activity.

## Tier-1 alert proof

Tier-1 is intentionally conservative. A candidate must pass the normal freshness, score, aggression, spread, premium, confidence, and price-context checks, plus all of these learned-quality gates:

```text
regime: TRENDING_UP for bullish flow or TRENDING_DOWN for bearish flow
distinct sessions: at least 20
paired effective samples: at least 30
15-minute +0.10% meaningful-move posterior rate: at least 30%
15-minute positive executable option-return posterior rate: at least 50%
```

CHOPPY, RANGE_BOUND, LOW_VOLUME_FAKE_MOVE, REVERSAL_ATTEMPT, UNKNOWN, stale, misaligned, or unqualified flow stays dashboard-only. Negative reliability adjustments may be applied before a cohort qualifies; positive bonuses require both outcome metrics to pass.

Repeated flow is tracked by trading-session contract and direction. A second notification requires at least 15 minutes, at least 25% additional premium, and one material improvement: score +5, a newly aligned regime, stronger aggression, or price-confirmation score +2. Otherwise the dashboard records a state update without another notification.

## Dashboard terms

### Real whale alert

A row that passed the scanner filters and quality checks. These rows are shown first and include full contract fields such as type, strike, expiration, DTE, volume, OI, Vol/OI, premium, score, classification, direction, price context, and reason.

### Debug Candidate — Not an Alert

A near miss. It looked interesting but failed one or more quality checks. Debug candidates are hidden by default because they are for tuning the scanner, not for watchlist decisions.

Examples of why a debug candidate may fail:

- stale quote
- wide spread
- weak price confirmation
- low score
- weak premium
- noisy SPY/QQQ/IWM/DIA 0DTE flow
- missing bid/ask context

### Debug loose mode

Debug loose mode is for scanner tuning and discovery only. When enabled, the dashboard should make it obvious with:

```text
DEBUG LOOSE MODE — discovery only, not alert quality.
```

Normal use should keep debug loose mode off unless explicitly enabled in config or environment variables.

### Stale or old premium print

A stale row means the option trade/quote data is old relative to the scan time. These rows can still be useful for historical context, but they should not be treated as fresh flow.

### Last good regular-session scan

After regular options-market hours, quotes can go stale and scans may return no fresh results. The scanner preserves the last useful regular-session payload in:

```text
data/options_whale_last_good_regular_session.json
```

The dashboard can keep showing that preserved scan with a market-closed notice instead of being wiped by an empty stale after-close scan.

### Awaiting next-day OI confirmation

The scanner can estimate whether flow looks opening or closing using same-day data, but true confirmation requires next trading-day open interest. Same-day flow remains `suspected_opening`, `suspected_closing`, or unresolved.

When the dashboard is running, the review job starts at 9:45 AM ET, selects every unique contract from the most recent prior trading session, and retries every 15 minutes until noon if no OI data is found. Expired or unavailable contracts remain explicitly unresolved; missing OI is never interpreted as closing flow.

## Symbol search

The main dashboard search box accepts any ticker text. It is not limited to a preset list.

Examples:

```text
AAPL
MSFT
META
GOOGL
AMZN
IWM
DIA
GLD
SMH
AMD
NFLX
```

Search behavior:

- trims spaces
- converts the input to uppercase internally
- matches `underlying_symbol` exactly first
- falls back to `option_symbol` contains search text
- does not reject ETFs or stocks
- shows `No fresh [TICKER] whale alerts right now.` if no current rows match
- preserves normal dashboard behavior when search is cleared

## Main commands

### Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

The runtime dependency list is intentionally small right now. `requests` is required for Alpaca REST calls.

### Configure local credentials

Create a local `.env` file. Do not commit it.

```bash
cp .env.example .env
chmod 600 .env
```

Required for live market-data access:

```dotenv
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_OPTIONS_CONTRACTS_BASE_URL=https://api.alpaca.markets
ALPACA_OPTIONS_DATA_BASE_URL=https://data.alpaca.markets
ALPACA_STOCK_FEED=sip
ALPACA_OPTIONS_FEED=opra
ALPACA_ALLOW_INDICATIVE_OPTIONS_FALLBACK=true
```

Optional:

```dotenv
DISCORD_WEBHOOK_URL=
OPENAI_API_KEY=
ENABLE_AI_REVIEW=false
```

### Test Alpaca/options access

```bash
python3 tools/test_options_access.py
```

### Rebuild option universe

```bash
python3 tools/rebuild_options_universe.py
```

### Run one scan from CLI

```bash
python3 tools/run_options_whale_scan.py
```

### Run main dashboard

```bash
python3 scanner_dashboard.py --open
```

### Restart dashboard cleanly

```bash
lsof -ti :8765 | xargs kill -9
python3 scanner_dashboard.py --open
```

### Run outcome dashboard

```bash
python3 -m tools.options_outcome_dashboard --open
```

### Review outcomes once

```bash
python3 tools/review_options_alert_outcomes.py --limit 100
```

### Review outcomes in a loop

```bash
python3 tools/review_options_alert_outcomes.py --loop --interval-seconds 300 --limit 100
```

### Summarize outcomes

```bash
python3 tools/summarize_options_outcomes.py
```

### Compact outcome history safely

Dry-run first:

```bash
python3 tools/compact_options_outcomes.py --dry-run
```

Apply only after reviewing the dry-run output:

```bash
python3 tools/compact_options_outcomes.py --apply
```

The apply mode creates a backup before replacing the runtime outcome file.

### Review next-day OI

The dashboard runs this automatically. To review manually after next-trading-day OI should be available:

```bash
python3 tools/review_next_day_oi.py --limit 100 --dry-run
python3 tools/review_next_day_oi.py --limit 100
python3 tools/review_next_day_oi.py --source-date 2026-07-01
```

### Export a daily review package

```bash
python3 tools/export_review_package.py --date 2026-07-01 --start 09:00 --end 16:00
```

The exporter selects outcome rows by `detected_at`, OI reviews by `original_time`, and operational logs by their event timestamp. This prevents next-day reviews from being assigned to the wrong trading session. The ZIP includes options-flow logs, price observations, outcome data, coverage statistics, regime heartbeats, OI reviews, and dashboard snapshots.

### Export whale-flow history

```bash
python3 tools/export_options_whale_history.py --format json
python3 tools/export_options_whale_history.py --format csv
```

## Recommended daily workflow

1. Start the main dashboard during regular market hours.
2. Check that Alpaca/options data is connected.
3. Confirm filters are set.
4. Run or wait for a whale scan.
5. Use symbol search if focusing on one ticker such as AAPL, QQQ, SPY, or any other symbol.
6. Focus on real whale alerts first.
7. Treat debug candidates as tuning/discovery only.
8. Treat stale/old premium prints as context only, not fresh flow.
9. Use your chart for confirmation.
10. Let outcome tracking review alert performance after enough bars exist.
11. Review the outcome dashboard after the market session or later in the day.
12. Check the next-day OI addendum after the automated 9:45 AM–12:00 PM retry window.

After market close, the dashboard may show the last good regular-session scan instead of an empty stale after-close scan.

## Default scanner filters

The default options whale config includes:

```text
max_dte: 7
include_0dte: true
include_weeklies: true
min_score: 75
min_premium: 100000
min_volume: 500
min_volume_oi_ratio: 2.0
max_spread_percent: 15
max_contracts_per_scan: 10000
max_results: 100
reliability_min_sessions: 20
reliability_min_effective_samples: 30
strict_cohort_min_meaningful_rate: 0.30
strict_cohort_min_executable_positive_rate: 0.50
notification_dedupe_minutes: 15
notification_update_min_premium_growth: 0.25
rotation_duration_min_seconds: 5
rotation_duration_max_seconds: 300
option_bar_unavailable_warning_rate: 0.02
```

Index/ETF 0DTE flow has stricter thresholds:

```text
symbols: SPY, QQQ, IWM, DIA
min_score: 85
min_premium: 250000
max_spread_percent: 8
min_price_confirmation_score: 6
```

## Main API routes

The dashboard uses local API routes under `/api/options-whales/*`:

```text
GET  /api/options-whales/status
GET  /api/options-whales/latest
GET  /api/options-whales/history
GET  /api/options-whales/filters
POST /api/options-whales/filters
GET  /api/options-whales/scan
POST /api/options-whales/auto-scan/pause
POST /api/options-whales/auto-scan/resume
GET  /api/options-whales/universe/status
POST /api/options-whales/universe/rebuild
GET  /api/options-whales/coverage
GET  /api/options-whales/reliability
GET  /api/options-whales/data-health
GET  /api/options-whales/export.json
GET  /api/options-whales/export.csv
```

## Scan metrics

Latest scan payloads include clearer metric names:

```text
underlyings_considered
underlyings_scanned
contracts_evaluated
passed_filter_count
near_miss_count
final_result_count
stale_quote_rejection_count
max_contracts_per_scan
contract_cap_reached
scan_session_state
coverage_rotation_symbols_dynamic
coverage_cycle_duration_ewma_seconds
coverage_rotation_timing_reset
coverage_rotation_duration_clamped
coverage_symbol_ages_seconds
```

`/api/options-whales/data-health` reports the latest outcome detection date, total episodes, unavailable option-bar count/rate, executable-window coverage, endpoint errors, and the configured warning threshold. The dashboard warns when unavailable option bars exceed 2% or an endpoint error exists.

`candidates_found` is preserved for backward compatibility, but dashboard copy should prefer `Passed filters` because that is what the number represents.

If `contract_cap_reached` is true, later symbols may not have been scanned in that run.

## Repo map

### Dashboards

```text
scanner_dashboard.py
```

Main dashboard and local API server for the Options Whale Scanner.

```text
options_whale_dashboard.py
```

Lightweight alternate whale-only dashboard that delegates to `scanner_dashboard.py` backend functions.

```text
tools/options_outcome_dashboard.py
```

Outcome proof dashboard.

### Scanner core

```text
scanner/options_whale_scanner.py
```

Main options scanner. Builds candidates, scores flow, applies unusualness, aggression, multi-leg, 0DTE noise, opening/closing, price context, alert tiers, storage, latest-scan payloads, stale after-close preservation, and clearer scan metrics.

```text
scanner/options_data_client.py
```

Read-only Alpaca data client. It restricts requests to market-data/account/options/stocks paths and refuses non-market-data paths.

```text
scanner/options_whale_scoring.py
```

Core scoring utilities: midpoint, spread, volume/OI, premium estimate, score classification.

```text
scanner/options_whale_models.py
```

Shared data models and disclaimer text.

```text
scanner/options_whale_storage.py
```

JSONL storage/export helpers for scan and alert logs.

### Signal engines

```text
scanner/options_unusualness_baseline.py
```

Local historical unusualness baseline by symbol, DTE bucket, moneyness, option type, volume, and premium.

```text
scanner/options_flow_classifier.py
```

Bid/ask aggression, direction labels, opening/closing estimate, and multi-leg direction adjustment.

```text
scanner/options_multileg_detector.py
```

Conservative possible multi-leg/spread/straddle/strangle labels.

```text
scanner/options_price_context.py
```

Underlying price-action context using stock price, VWAP-like context, trend, high/low, and chop adjustments when available.

```text
scanner/options_sweep_detector.py
scanner/options_block_detector.py
```

Possible sweep/block heuristics.

```text
scanner/options_oi_review.py
```

Next-day OI review support.

### Outcome tracking

```text
scanner/options_alert_outcomes.py
```

Evaluates 5m/15m/30m/60m follow-through using underlying stock bars. Handles pending, completed, insufficient future session, and dirty/partial cases.

```text
tools/review_options_alert_outcomes.py
```

Reads latest alerts and appends outcome-review rows with diagnostics.

```text
tools/summarize_options_outcomes.py
```

Summarizes outcome rows and excludes dirty completed rows from clean stats.

```text
tools/compact_options_outcomes.py
```

Safe dry-run-first compaction utility for duplicate/dirty outcome history. It writes a compacted latest-by-alert-key file and can back up and replace the runtime outcome file when `--apply` is used.

### Setup and operations

```text
tools/setup_imac.sh
```

Creates venv, installs dependencies, creates runtime folders, and makes launchers executable.

```text
start_scanner.command
stop_scanner.command
open_dashboard.command
check_scanner_status.command
```

macOS launch/check helpers.

```text
docs/RUN_ON_IMAC.md
```

Mac installation and private credential setup guide.

## Runtime files

Runtime files are intentionally ignored by Git.

Important generated paths:

```text
logs/options_whale_alerts.jsonl
logs/options_whale_scans.jsonl
logs/options_oi_reviews.jsonl
logs/options_price_observations.jsonl
data/options_universe.json
data/options_whale_latest.json
data/options_whale_last_good_regular_session.json
data/options_whale_episode_outcomes.jsonl
data/options_whale_outcomes.jsonl
data/options_whale_outcomes_compacted.jsonl
data/options_unusualness_baseline.jsonl
exports/
state/
```

Do not commit:

```text
.env
.env.*
config.local.json
secrets.json
logs/
state/
exports/
data/options_universe.json
data/options_whale_latest.json
data/options_whale_last_good_regular_session.json
data/options_whale_outcomes.jsonl
data/options_whale_outcomes_compacted.jsonl
data/options_unusualness_baseline.jsonl
```

## Testing

Run the full validation suite:

```bash
python3 -m py_compile scanner_dashboard.py scanner/options_whale_scanner.py scanner/options_alert_outcomes.py tools/review_options_alert_outcomes.py tools/summarize_options_outcomes.py tools/compact_options_outcomes.py
python3 -m unittest discover -s tests -v
git diff --check
python3 tools/check_config_consistency.py
```

Optional safety checks:

```bash
grep -R "submit_order\|place_order\|create_order\|buy this\|sell this" -n . --exclude-dir=.git --exclude-dir=.venv
```

## Troubleshooting

### Dashboard will not open

Check whether port 8765 is already in use:

```bash
lsof -i :8765
```

Restart cleanly:

```bash
lsof -ti :8765 | xargs kill -9
python3 scanner_dashboard.py --open
```

### No real whale alerts

This is normal if no flow passes the filters. The dashboard should show:

```text
No real whale alerts passed the filters right now.
```

Debug candidates may exist, but they are hidden by default because they are not alert quality.

### Search shows no results

The symbol search only searches the current scanner payload. If you type `AAPL` and see no fresh AAPL whale alerts, that means the scanner has no current fresh AAPL rows in the latest payload. Clear the search to return to the full scanner view.

### Market is closed and dashboard still shows old rows

This may be intentional. After regular options hours, the scanner can preserve the last useful regular-session scan so a stale empty after-close scan does not wipe the dashboard. The dashboard should label this state clearly.

### Options data unavailable

Check:

- API keys in `.env`
- Alpaca options contract permissions
- OPRA/options feed entitlement
- correct base URLs
- internet connection

Run:

```bash
python3 tools/test_options_access.py
```

### Full-market scans feel slow

Reduce scan size or interval:

```bash
OPTIONS_WHALE_MAX_CONTRACTS_PER_SCAN=5000 python3 scanner_dashboard.py --open
```

Or adjust config values for:

```text
max_contracts_per_scan
scan_interval_seconds
priority_batch_size
```

If the contract cap is reached, the dashboard should warn that later symbols may not have been scanned in that run.

Adaptive rotation continuously sizes the rotating symbol batch from measured scan duration and the coverage-age target. Overnight/session gaps reset the timing estimate, measured duration is clamped to 5–300 seconds, and requested rotating symbols cannot exceed the available rotating universe.

### Outcome stats look incomplete

Outcome tracking needs future bars after an alert. Alerts late in the session may become `insufficient_future_session` because there is not enough regular-session time left for 5m/15m/30m/60m windows.

### Outcome history has duplicate or dirty rows

Run a dry-run compaction first:

```bash
python3 tools/compact_options_outcomes.py --dry-run
```

Apply only after checking the dry-run output:

```bash
python3 tools/compact_options_outcomes.py --apply
```

## Development rules

Before committing:

```bash
python3 -m unittest discover -s tests -v
git diff --check
git status
```

Never commit runtime data or credentials.

Do not add order execution. Any future code that interacts with trading endpoints must be rejected unless the project scope changes explicitly.

## Roadmap

Near-term:

- richer pending-outcome re-review workflow after bars become available
- cleaner dashboard group stats for which flow types worked best
- multi-session monitoring of Tier-1 meaningful-move and executable-return calibration
- next-day OI coverage monitoring for expired same-day contracts

Longer-term:

- stronger multi-leg/roll grouping across timestamps and sizes
- richer price-action context from VWAP/EMA/support/resistance
- better sector/ETF context
- historical backtesting of alert categories

## Disclaimer

This project surfaces possible unusual options flow and explains why it may matter. It is not financial advice, not a trade recommendation, and not a predictive guarantee. Always confirm on the chart, manage risk independently, and assume options flow can be hedging, closing, rolling, or noise.
