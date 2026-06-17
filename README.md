# Options Whale Scanner

A private, read-only options-flow scanner and dashboard for spotting unusual options activity. The scanner discovers optionable contracts, reads options market data, scores possible whale flow, explains why a flow may matter, and tracks outcomes over time.

> Watch only. Not a trade signal. This project does not place trades.

## Current status

This repo is now focused on the Options Whale Scanner workflow.

| Area | Status |
|---|---|
| Historical unusualness baseline | Wired into scan scoring/output |
| Bid/ask aggression classification | Implemented with quote-staleness safeguards |
| Multi-leg/spread/roll detection | Implemented as conservative “possible” labels |
| SPY/QQQ/IWM/DIA 0DTE noise handling | Implemented with stricter 0DTE index thresholds |
| Opening vs closing estimate | Implemented as current-day estimate only |
| Price-action confirmation | Implemented when underlying bars/price context are available |
| Alert outcome tracking | Implemented for 5m/15m/30m/60m windows |
| Next-day OI confirmation | Scaffolded/pending; requires next trading-day OI |
| Learning/ranking from what mattered | Scaffolded/pending; requires accumulated outcome history |
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
cd "/Users/DayTrade/Documents/trade scanner"
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
- real whale-flow alerts first
- debug candidates hidden by default
- row-level explanations
- score breakdowns
- aggression, multi-leg, opening/closing, price-context, and warning fields

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

All signals are probabilistic. Large options prints can be opening flow, closing flow, hedges, spreads, rolls, dealer positioning, or noisy index/ETF activity.

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

### Awaiting next-day OI confirmation

The scanner can estimate whether flow looks opening or closing using same-day data, but true confirmation requires next trading-day open interest. The dashboard should not mark next-day OI as confirmed until that data exists.

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

### Run outcome dashboard

```bash
python3 -m tools.options_outcome_dashboard --open
```

### Review outcomes once

```bash
python3 tools/review_options_alert_outcomes.py --limit 25
```

### Review outcomes in a loop

```bash
python3 tools/review_options_alert_outcomes.py --loop --interval-seconds 300 --limit 25
```

### Summarize outcomes

```bash
python3 tools/summarize_options_outcomes.py
```

### Export whale-flow history

```bash
python3 tools/export_options_whale_history.py --format json
python3 tools/export_options_whale_history.py --format csv
```

## Recommended daily workflow

1. Start the main dashboard.
2. Check that Alpaca/options data is connected.
3. Confirm filters are set.
4. Run or wait for a whale scan.
5. Focus on real whale alerts only.
6. Ignore debug candidates unless tuning filters.
7. Use your chart for confirmation.
8. Let outcome tracking review alert performance after enough bars exist.
9. Review outcome dashboard after the market session or later in the day.

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
GET  /api/options-whales/export.json
GET  /api/options-whales/export.csv
```

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

Main options scanner. Builds candidates, scores flow, applies unusualness, aggression, multi-leg, 0DTE noise, opening/closing, price context, alert tiers, storage, and latest-scan payloads.

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

Next-day OI review support/scaffolding.

### Outcome tracking

```text
scanner/options_alert_outcomes.py
```

Evaluates 5m/15m/30m/60m follow-through using underlying stock bars. Handles pending, completed, insufficient future session, and dirty/partial cases.

```text
tools/review_options_alert_outcomes.py
```

Reads latest alerts and appends outcome-review rows.

```text
tools/summarize_options_outcomes.py
```

Summarizes outcome rows and excludes dirty completed rows from clean stats.

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
data/options_universe.json
data/options_whale_latest.json
data/options_whale_outcomes.jsonl
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
data/options_whale_outcomes.jsonl
data/options_unusualness_baseline.jsonl
```

## Testing

Run the full validation suite:

```bash
python3 -m py_compile scanner_dashboard.py scanner/options_whale_scanner.py scanner/options_alert_outcomes.py tools/review_options_alert_outcomes.py tools/summarize_options_outcomes.py
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

### Outcome stats look incomplete

Outcome tracking needs future bars after an alert. Alerts late in the session may become `insufficient_future_session` because there is not enough regular-session time left for 5m/15m/30m/60m windows.

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

- true next-day OI confirmation after future OI data is available
- automatic review tool for next-day OI changes
- learned quality scoring after enough outcome history is collected
- cleaner dashboard group stats for which flow types worked best

Longer-term:

- stronger multi-leg/roll grouping across timestamps and sizes
- richer price-action context from VWAP/EMA/support/resistance
- better sector/ETF context
- historical backtesting of alert categories

## Disclaimer

This project surfaces possible unusual options flow and explains why it may matter. It is not financial advice, not a trade recommendation, and not a predictive guarantee. Always confirm on the chart, manage risk independently, and assume options flow can be hedging, closing, rolling, or noise.
