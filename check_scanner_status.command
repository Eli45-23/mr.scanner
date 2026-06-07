#!/bin/zsh
set -u

PROJECT="/Users/DayTrade/Documents/Codex/2026-05-23/files-mentioned-by-the-user-elite/elite_scanner"
DASHBOARD_URL="http://127.0.0.1:8765"
PYTHON="$PROJECT/.venv/bin/python"

cd "$PROJECT" || exit 1

screen_running() {
  screen -ls 2>/dev/null | grep -q "[.]$1[[:space:]]"
}

print_json_status() {
  local file_path="$1"
  local kind="$2"
  [[ -s "$file_path" ]] || return
  tail -n 1 "$file_path" | "$PYTHON" -c '
import json, sys
kind = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit
if kind == "market":
    keys = ("timestamp", "symbol", "stock_feed_status", "options_feed_status", "opra_status", "feed_warning")
else:
    keys = ("timestamp", "channel", "alert_type", "symbol", "sent", "error", "token_redacted")
for key in keys:
    if key in data:
        print("{}: {}".format(key, data.get(key)))
' "$kind"
}

echo
echo "AAPL Scanner Status"
echo "-------------------"
screen_running elite_scanner && echo "Scanner screen: running" || echo "Scanner screen: stopped"
screen_running elite_dashboard && echo "Dashboard screen: running" || echo "Dashboard screen: stopped"

if curl -fsS "$DASHBOARD_URL/api/status" >/dev/null 2>&1; then
  echo "Dashboard API: available at $DASHBOARD_URL"
else
  echo "Dashboard API: unavailable"
fi

echo
echo "Latest market-data status"
print_json_status "logs/market_data_status.jsonl" market

echo
echo "Latest notification status"
print_json_status "logs/notification_status.jsonl" notification

latest_log="$(find logs -maxdepth 1 -type f -name '*scanner*.log' -print0 2>/dev/null | xargs -0 ls -t 2>/dev/null | head -n 1)"
if [[ -n "$latest_log" ]]; then
  echo
  echo "Latest scanner log lines ($latest_log)"
  tail -n 8 "$latest_log" |
    sed -E \
      -e 's#bot[0-9]+:[A-Za-z0-9_-]+#bot[REDACTED]#g' \
      -e 's#(TOKEN|SECRET|API_KEY|CHAT_ID)[=:][^[:space:]]+#\1=[REDACTED]#Ig'
fi

echo
echo "No API keys or Telegram tokens are displayed."
echo "You can close this Terminal window."
