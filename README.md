# Elite Momentum Scanner

For installation on another Mac, see [docs/RUN_ON_IMAC.md](docs/RUN_ON_IMAC.md).

A real-time stock momentum alert system built for traders who want to catch **unusual movement early** without auto-trading.

## What it detects
- Premarket movers
- Fast price jumps
- Relative volume spikes
- Premarket high / low breaks
- Opening range breakouts and breakdowns
- Big day movers with news catalysts

## Strategy and confirmation logic
Phase 1 classifies alert setups so the scanner can describe what is forming:
- Clean breakout / breakdown
- Liquidity sweep reclaim / rejection
- VWAP reclaim / rejection / loss / hold
- 5-minute and 15-minute opening range breakouts / breakdowns
- Fakeout risk and do-not-chase conditions

Phase 2 adds confirmation and entry-quality context beside the existing legacy alert grade (`A+`, `A`, `B`, `C`, `Avoid`):
- Volume quality / RVOL: labels weak, normal, strong, or climax volume and warns on low-volume fakeout risk.
- Candle strength: detects buyer control, seller control, rejection wicks, and high-volume indecision/churn.
- Retest / hold logic: detects breakout retest holding, breakdown retest rejecting, VWAP retest behavior, better entry areas, and late-entry risk.
- Extension / exhaustion: checks distance from VWAP, EMA9, and key levels, plus consecutive large candles and climax volume.
- Relative strength / weakness: compares watched stocks against SPY and QQQ when those bars are available.
- Market regime: classifies SPY/QQQ context as bull trend, bear trend, choppy, mixed, or unknown.
- Optional pressure score: when trade/quote data is explicitly supplied and enabled, estimates top-of-book buyer/seller pressure from latest trades, bid/ask, bid size, ask size, spread, and large prints.

The final alert can show both the old grade and the newer strategy/confirmation fields:
- Primary setup and secondary setups
- Strategy confidence
- Confirmation score and label
- Entry quality
- Risk label
- Volume, candle, relative strength, market regime, and optional pressure labels

These layers are alert intelligence only. They do not submit orders, select contracts for execution, close positions, cancel orders, or auto buy/sell.

### Alpaca data limitations
The scanner can use bars, snapshots, latest trades, and top-of-book quotes where those are available from the configured Alpaca data path. The optional pressure score is not full Level 2 order book analysis. It does not detect hidden institutional orders, dark-pool intent, iceberg orders, or any unavailable market-depth signal. If trade/quote pressure data is missing, pressure remains `UNKNOWN` and should not be treated as confirmation.

## What it sends
- Discord alerts with:
  - ticker
  - price
  - fast move %
  - day move %
  - relative volume
  - premarket levels
  - opening range levels
  - headline + news link when available

## What it does not do
- No trade execution
- No buy/sell automation
- No account access

The separate `paper_trade_cli.py` helper can be used for Alpaca paper-trading experiments, but it is not part of the scanner loop and does not change scanner alerts.

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
python elite_momentum_scanner.py --mode test
python elite_momentum_scanner.py --mode dry-run
```

### 5. Live mode
```bash
python elite_momentum_scanner.py --mode live
```

### 6. Browser dashboard
```bash
python scanner_dashboard.py --open
```

The dashboard can scan three scopes:
- `Watchlist`: only the configured symbols.
- `Discovery`: broad-market candidates selected from active Alpaca symbols.
- `Hybrid`: configured watchlist plus top discovery candidates.

The Market View labels each row with data quality:
- `Fresh`: current enough to score and alert.
- `Stale`: latest bar is older than the configured freshness window.
- `Incomplete`: not enough recent 1-minute bars for reliable calculations.
- `Low volume`: below the minimum volume filter.

`Recent RVOL` compares the latest 1-minute bar volume to recent 1-minute bars in the current scan window. It is not historical same-time-of-day RVOL.

The dashboard also includes an options view for day-trade calls and puts:
- `Best Call` and `Best Put` show the selected contract for each ticker.
- `Option Quality` is `Tradable`, `Wide spread`, `Low liquidity`, `Stale quote`, or `No clean contract`.
- `Opt Score` ranks contract quality using bid/ask spread, quote freshness, liquidity, delta fit, and 0DTE preference.
- The feed badge is `OPRA`, `Indicative`, or `Simulated`; treat `Indicative` as non-execution-grade because it is not the official OPRA quote feed.
- Dry-run option contracts are simulated and labeled as simulated data.

The dashboard `Clear` button stops any running scan and resets visible Market View rows, alert history, counters, and alert cooldown state. It does not remove your API keys or scanner configuration.

The `Alpaca Health` button is diagnostic only. It uses the Alpaca CLI for manual pre-market troubleshooting checks such as account status, authentication, and the market clock; the scanner engine still uses the Python Alpaca API/WebSocket path for market data and alert decisions. The health check does not submit, cancel, close, or change orders.

Manual Alpaca CLI checks:
```bash
alpaca doctor
alpaca clock --quiet
alpaca account get --quiet
```

If `ALPACA_LIVE_TRADE=true` is set, the dashboard will warn that Alpaca CLI calls appear pointed at live trading. Treat that as a high-caution configuration signal even though the health check is read-only.

### Alpaca CLI paper-trading lab

`paper_trade_cli.py` is a separate paper-only helper for learning and testing Alpaca CLI order flow. It defaults to dry-run order previews and refuses to run when `ALPACA_LIVE_TRADE=true`.

`ai_paper_trade_lab.py` is an AI-assisted paper-trading lab. It can prepare only three actions: `PAPER_ORDER`, `CLOSE_POSITION`, or `NO_TRADE`. It requires AI confidence to be exactly `100` before execution is allowed, and it still requires `--execute-paper --confirm PAPER`.

`spy_paper_autotrader.py` is the SPY-only paper autotrader. It reads scanner alerts and can open or close SPY paper positions only when hard 100-confidence rules pass. Its default instrument is SPY options: bullish signals buy the scanner-selected SPY call, bearish signals buy the scanner-selected SPY put, and opposite signals close the open SPY option paper position.

Health check:
```bash
python paper_trade_cli.py health
```

Preview a paper order request without submitting:
```bash
python paper_trade_cli.py preview-order --symbol AAPL --side buy --type market --qty 1
```

Submit a paper order only with an explicit confirmation:
```bash
python paper_trade_cli.py submit-paper-order --symbol AAPL --side buy --type market --qty 1 --execute-paper --confirm PAPER
```

List paper orders:
```bash
python paper_trade_cli.py list-orders
```

Ask AI for a paper-trading action:
```bash
python ai_paper_trade_lab.py plan --notional 100
```

Execute the stored AI paper plan only if it passed the 100-confidence gate:
```bash
python ai_paper_trade_lab.py execute --execute-paper --confirm PAPER
```

Check what the SPY-only paper autotrader would do:
```bash
python spy_paper_autotrader.py once
```

Let the SPY-only bot execute one SPY option paper action by itself:
```bash
python spy_paper_autotrader.py --execute-paper --confirm PAPER once
```

Run the SPY-only options paper bot continuously:
```bash
python spy_paper_autotrader.py --execute-paper --confirm PAPER loop --interval-seconds 10
```

Run the bot in SPY stock-paper mode instead of options:
```bash
python spy_paper_autotrader.py --instrument stock --execute-paper --confirm PAPER loop --interval-seconds 10
```

By default, SPY bearish signals do not open paper shorts. To allow paper shorts:
```bash
python spy_paper_autotrader.py --instrument stock --allow-paper-shorts --execute-paper --confirm PAPER loop --interval-seconds 10
```

Close a paper position manually:
```bash
python paper_trade_cli.py close-position --symbol AAPL --execute-paper --confirm PAPER
```

Safety notes:
- This helper uses the Alpaca CLI profile, not scanner API env vars.
- It strips scanner Alpaca API env vars before calling the CLI so the CLI profile stays in control.
- It blocks obvious live mode via `ALPACA_LIVE_TRADE=true`.
- It limits symbols to the scanner watchlist unless `--allow-non-watchlist` is supplied.
- It logs paper order attempts to `logs/paper_trade_cli.jsonl`.
- It is for paper trading practice only and is not financial advice.
- The AI lab refuses execution unless the AI plan confidence is exactly `100`; this is a safety gate, not a real guarantee that a trade will work.
- The SPY paper autotrader is SPY-only and uses hard scanner gates: fresh full alert, A+ grade, score >= 90, aligned market, scanner-selected SPY option contract present, tradable option context, spread <= 5%, RVOL >= 2.0x, no stale/extended/opposing/cooldown blocker, and fast/day direction agreement.
- In options mode it only buys options to open; it does not sell options to open.

The `AI Review` button is also diagnostic only. It sends a compact snapshot of current scanner rows and recent alerts to OpenAI for a structured timing/direction review. Results are cached briefly to avoid repeated API calls from button clicks. The live scanner still uses its Python Alpaca rule engine for all watch and alert decisions, and the AI review cannot trigger texts or trades.

The AI Review response is displayed as a structured card with timing, direction label quality, missed setup, rule strictness, risk level, confidence, suggested tuning, plain-English summary, next watch item, and do-not-chase warning. If `ENABLE_AI_REVIEW=false`, the dashboard stays fully usable and the scanner keeps running without AI review.

## Your workflow
This tool is intended to do this:
1. alert you to unusual movement
2. send context quickly
3. let **you** check the chart and decide whether to trade

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
