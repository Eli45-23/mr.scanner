# Options Whale Scanner

For installation on another Mac, see [docs/RUN_ON_IMAC.md](docs/RUN_ON_IMAC.md).

A read-only full-market options-flow scanner. It discovers optionable contracts from Alpaca, scans contracts across the market, scores unusual options activity, and ranks possible whale flow.

Every result includes:

> Possible whale flow — not a trade signal.

## What it detects
- Full-market unusual call/put activity
- Large premium contracts
- High volume relative to open interest
- Possible sweep-like activity
- Possible block prints
- Possible multi-leg/spread/roll/hedge structures
- Opening-vs-closing estimates that require next-day OI review
- Underlying price-action context when available

## What tiers mean
- `Tier 1`: highest-quality possible whale-flow read with aggressive activity, large premium, acceptable spread, and price context.
- `Tier 2`: high-scoring flow with some confirmation or minor warnings.
- `Tier 3`: unusual but unclear; dashboard/history only by default.
- `Ignore`: below threshold or too low quality.

Unusual options flow is not predictive by itself. Large prints may be spreads, hedges, rolls, closing transactions, or noisy data. The scanner uses probabilistic language only.

## Data requirements and limitations
The scanner uses Alpaca market-data endpoints for option contracts, snapshots, quotes, trades, and stock bars when available. OPRA is the official options feed. Indicative data is fallback/limited data and is displayed with warnings. If an endpoint or entitlement is unavailable, the dashboard shows a clear warning and keeps running.

## What it does not do
- No trade execution
- No broker order endpoints
- No auto-trading automation
- No account access

## Whale-only dashboard

The clean dashboard is now `options_whale_dashboard.py`. It only serves the Options Whale Scanner UI and only polls `/api/options-whales/*` routes. It does not load the old Market View, Watchlist, Alert Brain, Control Center, legacy alerts table, or old setup panels.

Run it with:

```bash
python options_whale_dashboard.py --open
```

The old `scanner_dashboard.py` remains in the repo for compatibility while the project is being refactored, but the whale-only dashboard is the preferred dashboard for the Options Whale Scanner workflow.

## Legacy momentum scanner
The old stock momentum scanner is disabled by default with:

```bash
ENABLE_OPTIONS_WHALE_SCANNER=true
ENABLE_LEGACY_MOMENTUM_SCANNER=false
```

Legacy paper-trading helper files remain in the repo only as deprecated historical tooling. They are not part of the Options Whale Scanner workflow.

## Setup

### 1. Python
Use Python 3.11+ if possible.

### 2. Install dependencies
```bash
pip install requests
```

### 3. Environment variables
Required for live mode:
```bash
export ALPACA_API_KEY="your_key"
export ALPACA_SECRET_KEY="your_secret"
```

For read-only options contract discovery, your `.env` may need:

```bash
ALPACA_OPTIONS_CONTRACTS_BASE_URL=https://api.alpaca.markets
ALPACA_OPTIONS_DATA_BASE_URL=https://data.alpaca.markets
```

Optional:
```bash
export DISCORD_WEBHOOK_URL="your_discord_webhook"
```

### 4. Dry-run test
```bash
python tools/test_options_access.py
python tools/rebuild_options_universe.py
python tools/run_options_whale_scan.py
```

### 5. Dashboard
Preferred whale-only dashboard:

```bash
python options_whale_dashboard.py --open
```

Compatibility dashboard:

```bash
python scanner_dashboard.py --open
```

### 6. Export whale-flow history
```bash
python tools/export_options_whale_history.py --format json
python tools/export_options_whale_history.py --format csv
```

The scanner defaults to full-market options whale flow. It does not require a watchlist or symbol parameter.

## Your workflow
This tool is intended to do this:
1. surface unusual options flow
2. explain why the flow may matter
3. show risk and uncertainty
4. require your own chart confirmation

## Runtime files generated
- `logs/options_whale_alerts.jsonl`
- `logs/options_whale_scans.jsonl`
- `logs/options_oi_reviews.jsonl`
- `data/options_universe.json`
- `data/options_whale_latest.json`

These runtime files should stay out of Git.

## Good next upgrades
- Stronger underlying price-action confirmation
- Better ask-side/bid-side aggression scoring
- Next-day OI review automation
- Historical validation of which flow types mattered
- Cleaner sector/group filters for full-market context
