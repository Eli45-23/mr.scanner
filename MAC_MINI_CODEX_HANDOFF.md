# Mac Mini Codex Handoff

This file is a no-secrets handoff note for continuing the Elite Momentum Scanner work in a new Codex chat on the Mac mini.

## Project Folder

Copy this whole folder to the Mac mini:

```text
elite_scanner
```

Current source folder on this Mac:

```text
/Users/eli/Documents/Codex/2026-05-23/files-mentioned-by-the-user-elite/elite_scanner
```

Important: copy hidden files too, especially `.env`. Do not paste API keys into chat. The new Codex chat can check whether `.env` exists and whether keys are present without printing them.

## Current Purpose

The scanner watches this focused options-trading watchlist:

```text
AAPL, QQQ, META, SPY, ASTS, NVDA
```

It runs a local dashboard at:

```text
http://127.0.0.1:8765
```

It uses Alpaca/Python market data as the scanner engine. OpenAI is diagnostic only through the dashboard AI Review button. OpenAI must not place trades, send texts, change scanner rules automatically, or override Alpaca data.

## Current Live Behavior

- Scans watchlist every 10 seconds.
- Sends SMS/Messages alerts from the Mac running the scanner.
- Uses `WATCH` for early setups and `ALERT` for stronger confirmed setups.
- Full alerts remain strict.
- Recent added protections:
  - Bearish stock-specific watch path when SPY/QQQ are opposed.
  - Blocked bearish alerts can downgrade to watch-only.
  - `WATCH FAST IMPULSE UP/DOWN` for dramatic regular-hours jumps/flushes.
  - Generic high-RVOL alerts are blocked from full SMS when the broader day trend strongly conflicts with only a small fast move.
  - Structured AI Review card with strict JSON validation.
  - Alpaca CLI Health check safety guards.

## Safety Rules

- No buy/sell advice.
- Always remind to confirm in Webull.
- Do not add order, cancel, close, or trading commands.
- Do not expose `.env` secrets, API keys, phone numbers, or tokens in logs/chat.
- Keep OpenAI diagnostic only.
- Keep scanner decisions on Alpaca/Python data.

## Start On Mac Mini

From the copied `elite_scanner` folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scanner_dashboard.py --host 127.0.0.1 --port 8765
```

Then open:

```text
http://127.0.0.1:8765
```

Start live watchlist mode from the dashboard, or ask Codex to start it.

Detached screen startup command:

```bash
screen -dmS elite_dashboard /bin/zsh -lc 'cd /path/to/elite_scanner && .venv/bin/python scanner_dashboard.py --host 127.0.0.1 --port 8765'
```

## Verification Commands

Run these from the project folder:

```bash
.venv/bin/python -m py_compile scanner_dashboard.py elite_momentum_scanner.py
.venv/bin/python elite_momentum_scanner.py --mode test
.venv/bin/python scanner_dashboard.py --test
curl -s http://127.0.0.1:8765/api/status
curl -s http://127.0.0.1:8765/api/symbols
curl -s 'http://127.0.0.1:8765/api/alerts?limit=20'
```

Expected recent test status before handoff:

```text
scanner tests: 42/42 passed
dashboard tests: 25/25 passed
compile: passed
```

## Mac Mini Setup Checklist

- Messages app signed in and able to send SMS/iMessage.
- Mac mini set to stay awake during market hours.
- `.env` copied and present.
- Alpaca keys present in `.env`.
- OpenAI key present in `.env` if AI Review is wanted.
- Dashboard opens locally.
- `/api/status` says:
  - `running: true` after live mode starts
  - `scope: watchlist`
  - `interval: 10`
  - `has_sms_alerts: true`
  - `has_openai_key: true` if AI Review is enabled

## Prompt To Paste Into New Codex Chat On Mac Mini

```text
We are continuing the Elite Momentum Scanner project. Please read MAC_MINI_CODEX_HANDOFF.md in this folder first. Do not print secrets. Verify the project works on this Mac mini, recreate .venv if needed, run compile/tests, start the dashboard at http://127.0.0.1:8765, start live watchlist mode for AAPL, QQQ, META, SPY, ASTS, NVDA, and confirm SMS/OpenAI/Alpaca status without exposing keys. Keep OpenAI diagnostic only and do not add trading/order commands.
```

## Notes From Today

Today’s audit showed the bot was healthy and not blind:

- 594 candidates generated.
- 22 SMS alerts.
- 55 watch alerts.
- Main blockers were RVOL, extended breaks, market opposition for bearish setups, fast move conflicts, and stale option quotes near/after close.
- The newest updates were applied to the live bot and tests passed afterward.

