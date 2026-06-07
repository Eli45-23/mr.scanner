# Run the AAPL Scanner on an iMac

This setup keeps credentials local to the iMac. Never commit `.env`, local configuration, logs, state, or exports.

## 1. Clone the private repository

Authenticate GitHub CLI or Git on the iMac first, then run:

```bash
git clone https://github.com/Eli45-23/mr.scanner.git elite_scanner
cd elite_scanner
```

## 2. Automated setup

```bash
chmod +x tools/setup_imac.sh
./tools/setup_imac.sh
```

The setup script creates `.venv`, upgrades pip, installs dependencies, creates required runtime folders, and makes the launchers executable.

## 3. Manual virtual-environment setup

Use these commands instead of the automated setup when preferred:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Add private local configuration

Copy the existing `.env` directly from the current Mac to the iMac using a private method, or create it locally:

```bash
cp .env.example .env
chmod 600 .env
```

Configure these private/local values:

- Alpaca API key and secret
- `ALPACA_STOCK_FEED=sip`
- `ALPACA_OPTIONS_FEED=opra`
- Telegram bot token and chat ID
- `ENABLE_TELEGRAM_ALERTS=true`
- `PHASE3_HEADS_UP_SYMBOLS=AAPL`
- `MARKET_CONTEXT_SYMBOLS=SPY,QQQ`
- Any local SMS/desktop notification settings

Never commit `.env`, `config.local.json`, or `secrets.json`.

## 5. Verify the runtime

```bash
.venv/bin/python tools/check_runtime_ready.py
.venv/bin/python tools/send_telegram_test.py
```

There is no separate `news_watcher.py` in this repository, so `tools/check_alpaca_news_access.py` is not required.

## 6. Start the scanner and dashboard

```bash
./start_scanner.command
```

The launcher starts screen sessions named `elite_scanner` and `elite_dashboard`, then opens the dashboard.

## 7. Open the dashboard

```bash
./open_dashboard.command
```

Dashboard URL: [http://127.0.0.1:8765](http://127.0.0.1:8765)

## 8. Check status

```bash
./check_scanner_status.command
```

## 9. Stop the scanner and dashboard

```bash
./stop_scanner.command
```

## Optional Desktop shortcuts

Create symlinks so the launchers continue to resolve the cloned project directory:

```bash
ln -s "$PWD/start_scanner.command" "$HOME/Desktop/Start AAPL Scanner.command"
ln -s "$PWD/stop_scanner.command" "$HOME/Desktop/Stop AAPL Scanner.command"
ln -s "$PWD/open_dashboard.command" "$HOME/Desktop/Open AAPL Dashboard.command"
ln -s "$PWD/check_scanner_status.command" "$HOME/Desktop/Check AAPL Scanner Status.command"
```

## Updating from GitHub

Stop the scanner before updating:

```bash
./stop_scanner.command
git pull --ff-only origin main
./tools/setup_imac.sh
./start_scanner.command
```
