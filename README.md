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
Optional:
```bash
export DISCORD_WEBHOOK_URL="your_discord_webhook"
```

Optional manual AI review:
```bash
export OPENAI_API_KEY="your_openai_key"
export OPENAI_MODEL="gpt-4.1-mini"
export ENABLE_AI_REVIEW="true"
export AI_REVIEW_TIMEOUT_SECONDS="8"
export AI_REVIEW_MAX_ALERTS="10"
export AI_REVIEW_MAX_WATCHLIST_ROWS="20"
```

OpenAI is used only for manual dashboard diagnostics. It reviews current scanner rows and recent alert history to help spot timing, direction-label, missed-context, risk, or rule-tuning issues. It does not make live alert decisions, send texts, submit orders, or change the scanner loop.

For reliable phone alerts, use Pushover instead of texting yourself through Apple Messages:
```bash
export PUSHOVER_APP_TOKEN="your_pushover_app_token"
export PUSHOVER_USER_KEY="your_pushover_user_key"
```

You can test phone push after adding those values to `.env`:
```bash
python elite_momentum_scanner.py --test-phone-push
```

Telegram can be enabled as an additional notification channel. It only receives Phase 3 heads-ups or normal alerts that already passed their existing gates:
```bash
export ENABLE_TELEGRAM_ALERTS="false"
export TELEGRAM_BOT_TOKEN=""
export TELEGRAM_CHAT_ID=""
export TELEGRAM_ALERT_TYPES="PHASE3_HEADS_UP,NORMAL_SMS"
export TELEGRAM_AAPL_ONLY="true"
export TELEGRAM_SEND_TEST_ON_START="false"
export TELEGRAM_TIMEOUT_SECONDS="8"
```

The scanner can also send one protective `AAPL Morning Playbook` per weekday
near 09:25 ET. It reuses the existing Telegram destination and summarizes
PMH/PML/PDH/PDL/PDC, current market structure, liquidity-sweep context, and
discipline reminders. The playbook is context-only, cannot approve trades, and
does not change existing alert gates.

```bash
export ENABLE_MORNING_PLAYBOOK="true"
export MORNING_PLAYBOOK_SEND_TIME_ET="09:25"
export MORNING_PLAYBOOK_TELEGRAM_ENABLED="true"
export MORNING_PLAYBOOK_MAX_CHARS="1200"
```

Send-once state is stored in `state/morning_playbook_state.json`. Delivery and
failure records are written to `logs/morning_playbook.jsonl`. Both runtime
paths are ignored by Git. Telegram failures are logged safely and do not stop
the scanner.

After messaging the bot once, find the chat ID and send a test:
```bash
python tools/get_telegram_chat_id.py
python tools/send_telegram_test.py
```

The scanner sends Mac desktop notifications and Apple Messages texts for high-quality alerts. Keep the Messages thread open on your Macs if you want to see the text-alert history across computers using the same iCloud account.

You can test computer alerts on the Mac running the scanner:
```bash
python elite_momentum_scanner.py --test-desktop-notification
```

### 4. Dry-run test
```bash
python tools/test_options_access.py
python tools/rebuild_options_universe.py
python tools/run_options_whale_scan.py
```

### 5. Dashboard
```bash
python scanner_dashboard.py --open
```

### 6. Export whale-flow history
```bash
python tools/export_options_whale_history.py --format json
python tools/export_options_whale_history.py --format csv
```

The dashboard defaults to full-market options whale flow. It does not require a watchlist or symbol parameter.

The dashboard `Clear` button stops any running scan and resets visible Market View rows, alert history, counters, and alert cooldown state. It does not remove your API keys or scanner configuration.

The `Alpaca Health` button is diagnostic only. It uses the Alpaca CLI for manual pre-market troubleshooting checks such as account status, authentication, and the market clock; the scanner engine still uses the Python Alpaca API/WebSocket path for market data and alert decisions. The health check does not submit, cancel, close, or change orders.

Manual Alpaca CLI checks:
```bash
alpaca doctor
alpaca clock --quiet
alpaca account get --quiet
```

If `ALPACA_LIVE_TRADE=true` is set, the dashboard will warn that Alpaca CLI calls appear pointed at live trading. Treat that as a high-caution configuration signal even though the health check is read-only.

### Deprecated legacy paper tooling

Older paper-trading helper files remain in the repository for history only. They are quarantined from the Options Whale Scanner workflow and are not part of the default dashboard, scanner loop, alerts, or docs workflow.

The `AI Review` button is also diagnostic only. It sends a compact snapshot of current scanner rows and recent alerts to OpenAI for a structured timing/direction review. Results are cached briefly to avoid repeated API calls from button clicks. The live scanner still uses its Python Alpaca rule engine for all watch and alert decisions, and the AI review cannot trigger texts or trades.

The AI Review response is displayed as a structured card with timing, direction label quality, missed setup, rule strictness, risk level, confidence, suggested tuning, plain-English summary, next watch item, and do-not-chase warning. If `ENABLE_AI_REVIEW=false`, the dashboard stays fully usable and the scanner keeps running without AI review.

## Your workflow
This tool is intended to do this:
1. surface unusual options flow
2. explain why the flow may matter
3. show risk and uncertainty
4. require your own chart confirmation

## Files generated
- `logs/alerts.csv`
- `logs/alerts.jsonl`
- `state/scanner_state.json`

## Good next upgrades
- SMS / email alerts
- browser dashboard
- options-flow provider integration
- float / short interest filters
- earnings calendar filter
- sector/industry momentum grouping
- watchlist import from CSV
