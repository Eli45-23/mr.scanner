#!/bin/zsh
set -u

DASHBOARD_URL="http://127.0.0.1:8765"

if curl -fsS "$DASHBOARD_URL/api/status" >/dev/null 2>&1; then
  open "$DASHBOARD_URL"
  echo "Opened AAPL Scanner dashboard: $DASHBOARD_URL"
else
  echo "Dashboard not running. Double-click start_scanner.command first."
fi

echo
echo "You can close this Terminal window."
