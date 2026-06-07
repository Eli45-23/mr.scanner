#!/bin/zsh
set -u

SCRIPT_PATH="${0:A}"
while [[ -L "$SCRIPT_PATH" ]]; do
  TARGET="$(readlink "$SCRIPT_PATH")"
  [[ "$TARGET" = /* ]] && SCRIPT_PATH="$TARGET" || SCRIPT_PATH="${SCRIPT_PATH:h}/$TARGET"
  SCRIPT_PATH="${SCRIPT_PATH:A}"
done
PROJECT="${SCRIPT_PATH:h}"

cd "$PROJECT" || exit 1

screen_running() {
  screen -ls 2>/dev/null | grep -q "[.]$1[[:space:]]"
}

screen -S elite_scanner -X quit >/dev/null 2>&1 || true
screen -S elite_dashboard -X quit >/dev/null 2>&1 || true
sleep 2
pkill -f "elite_momentum_scanner.py --mode live" >/dev/null 2>&1 || true
pkill -f "scanner_dashboard.py --host 127.0.0.1 --port 8765" >/dev/null 2>&1 || true
sleep 2

echo
echo "AAPL Scanner Stop Status"
echo "------------------------"
screen_running elite_scanner && echo "Scanner running: yes" || echo "Scanner running: no"
screen_running elite_dashboard && echo "Dashboard running: yes" || echo "Dashboard running: no"
echo
echo "You can close this Terminal window."
