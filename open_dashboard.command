#!/bin/zsh
set -u

SCRIPT_PATH="${0:A}"
while [[ -L "$SCRIPT_PATH" ]]; do
  TARGET="$(readlink "$SCRIPT_PATH")"
  [[ "$TARGET" = /* ]] && SCRIPT_PATH="$TARGET" || SCRIPT_PATH="${SCRIPT_PATH:h}/$TARGET"
  SCRIPT_PATH="${SCRIPT_PATH:A}"
done
PROJECT="${SCRIPT_PATH:h}"
DASHBOARD_URL="http://127.0.0.1:8765"

cd "$PROJECT" || exit 1

if curl -fsS "$DASHBOARD_URL/api/status" >/dev/null 2>&1; then
  open "$DASHBOARD_URL"
  echo "Opened AAPL Scanner dashboard: $DASHBOARD_URL"
else
  echo "Dashboard not running. Double-click start_scanner.command first."
fi

echo
echo "You can close this Terminal window."
