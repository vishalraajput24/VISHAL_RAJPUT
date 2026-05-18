#!/bin/bash
# Usage: bash ~/VISHAL_RAJPUT/run_backtest.sh <name>
# Example: bash ~/VISHAL_RAJPUT/run_backtest.sh predict_signal
#          bash ~/VISHAL_RAJPUT/run_backtest.sh early_entry
#
# Pulls the latest version of the named backtest from the branch
# and runs it. Output is shown live and also saved to /tmp/<name>_result.txt

set -e
NAME="${1:?Usage: $0 <backtest_name>}"
SCRIPT="backtest_${NAME}.py"
BRANCH="claude/consolidate-trading-data-Sa3lH"
REPO="$HOME/VISHAL_RAJPUT"
OUT="/tmp/${NAME}_result.txt"

cd "$REPO"

# Find the Python that has pandas (same env the bot uses)
PYTHON=""
for candidate in \
    "$HOME/kite_env/bin/python3" \
    "$HOME/venv/bin/python3" \
    "$HOME/env/bin/python3" \
    "$HOME/.venv/bin/python3" \
    "$(which python3 2>/dev/null)"; do
    if [ -x "$candidate" ] && "$candidate" -c "import pandas" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "ERROR: no Python with pandas found. Install it: pip3 install pandas numpy"
    exit 1
fi

echo "==> Fetching latest $SCRIPT from $BRANCH..."
git fetch origin "$BRANCH" -q
git checkout "origin/$BRANCH" -- "$SCRIPT"
echo "==> Running $SCRIPT with $PYTHON (output → $OUT)"
echo ""
"$PYTHON" "$REPO/$SCRIPT" 2>&1 | tee "$OUT"
echo ""
echo "==> Saved: $OUT"
