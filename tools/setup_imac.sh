#!/bin/zsh
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT"

if [[ ! -f "elite_momentum_scanner.py" || ! -f "requirements.txt" ]]; then
  echo "Error: run this script from inside the cloned elite_scanner repository."
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is not installed."
  exit 1
fi

mkdir -p logs exports state

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Creating Python virtual environment..."
  python3 -m venv .venv
fi

echo "Installing Python dependencies..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

chmod +x ./*.command
find tools -maxdepth 1 -type f -name '*.sh' -exec chmod +x {} \;

echo
echo "iMac setup completed."
echo "Next steps:"
echo "1. Create .env from .env.example and add private credentials locally."
echo "2. Run: .venv/bin/python tools/check_runtime_ready.py"
echo "3. Run: .venv/bin/python tools/send_telegram_test.py"
echo "4. Start: ./start_scanner.command"
