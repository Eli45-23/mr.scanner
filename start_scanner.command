#!/bin/zsh
set -u

PROJECT="/Users/DayTrade/Documents/Codex/2026-05-23/files-mentioned-by-the-user-elite/elite_scanner"
DASHBOARD_URL="http://127.0.0.1:8765"
PYTHON="$PROJECT/.venv/bin/python"

cd "$PROJECT" || exit 1
mkdir -p logs

screen_running() {
  screen -ls 2>/dev/null | grep -q "[.]$1[[:space:]]"
}

if ! screen_running elite_scanner; then
  pkill -f "elite_momentum_scanner.py --mode live" >/dev/null 2>&1 || true
  sleep 1
  screen -dmS elite_scanner /bin/zsh -lc "cd '$PROJECT' && exec '$PYTHON' elite_momentum_scanner.py --mode live >> logs/scanner_runtime.log 2>&1"
fi

if ! screen_running elite_dashboard; then
  pkill -f "scanner_dashboard.py --host 127.0.0.1 --port 8765" >/dev/null 2>&1 || true
  sleep 1
  screen -dmS elite_dashboard /bin/zsh -lc "cd '$PROJECT' && exec '$PYTHON' scanner_dashboard.py --host 127.0.0.1 --port 8765 >> logs/dashboard_server.log 2>&1"
fi

for _ in {1..12}; do
  curl -fsS "$DASHBOARD_URL/api/status" >/dev/null 2>&1 && break
  sleep 1
done

curl -fsS -X POST "$DASHBOARD_URL/api/start" \
  -H "Content-Type: application/json" \
  -d '{"mode":"live","scope":"watchlist"}' >/dev/null 2>&1 || true

sleep 2
open "$DASHBOARD_URL"

echo
echo "AAPL Scanner Startup Status"
echo "---------------------------"
screen_running elite_scanner && echo "Scanner running: yes" || echo "Scanner running: no"
screen_running elite_dashboard && echo "Dashboard running: yes" || echo "Dashboard running: no"
echo "Dashboard URL: $DASHBOARD_URL"

curl -fsS "$DASHBOARD_URL/api/status" 2>/dev/null | "$PYTHON" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit
notifications = data.get("notification_status") or {}
market = data.get("market_data_status") or {}
print("Telegram enabled/configured: {}/{}".format(
    "yes" if notifications.get("telegram_enabled") else "no",
    "yes" if notifications.get("telegram_configured") else "no",
))
print("Stock feed: {}".format(market.get("stock_feed_status") or market.get("stock_feed_requested") or "unknown"))
print("Options feed: {}".format(market.get("options_feed_status") or market.get("options_feed_requested") or "unknown"))
' || true

echo
echo "You can close this Terminal window."
