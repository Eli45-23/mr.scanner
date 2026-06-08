# One Official AAPL Scanner Setup

Only one scanner should run live at a time. The iMac is the preferred active bot station. The Mac Mini is a backup and must use the same GitHub commit and the same non-secret configuration values.

## Official Profile

- Alert symbol: `AAPL`
- Context-only symbols: `SPY,QQQ`
- Alert profile: `AAPL_TESTING`
- Telegram alert types: `PHASE3_HEADS_UP,STOCK_ONLY_WARNING,NORMAL_WATCH,NORMAL_SMS`
- Stock feed: `sip`
- Options feed: `opra`

Each machine needs a unique identity:

```dotenv
SCANNER_INSTANCE_NAME=iMac
SCANNER_MACHINE_ROLE=primary
SCANNER_ALERT_PROFILE=AAPL_TESTING
```

Use `MacMini` and `backup` on the backup machine. Keep the Telegram bot token and chat ID identical on both machines.

## Switch Active Machines

Stop the current active scanner:

```bash
./stop_scanner.command
screen -ls
ps aux | grep -i "elite_momentum_scanner|scanner_dashboard" | grep -v grep
```

Update and verify the next machine:

```bash
git pull --ff-only origin main
./tools/setup_imac.sh
.venv/bin/python tools/check_config_consistency.py
./start_scanner.command
```

## Confirm Configuration

```bash
.venv/bin/python tools/check_config_consistency.py
```

Compare the output from both machines. The commit, alert profile, alert symbols, context symbols, Telegram alert types, destination last four digits, and feeds must match.

## Export and Compare Review Packages

```bash
.venv/bin/python tools/export_review_package.py --date 2026-06-08 --start 09:00 --end now --output-dir exports

.venv/bin/python tools/compare_review_packages.py \
  --left exports/imac_review_package.zip \
  --right exports/mac_mini_review_package.zip \
  --left-name iMac \
  --right-name MacMini \
  --output exports/comparison_2026-06-08.md
```

The comparison produces Markdown, JSON, and CSV files. Review machine identity, commit, enabled alert types, alert counts, matched timelines, and Telegram destination differences before changing alert behavior.
