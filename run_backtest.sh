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
echo "==> Fetching latest $SCRIPT from $BRANCH..."
git fetch origin "$BRANCH" -q
git checkout "origin/$BRANCH" -- "$SCRIPT"
echo "==> Running $SCRIPT (output → $OUT)"
echo ""
python3 "$REPO/$SCRIPT" 2>&1 | tee "$OUT"
echo ""
echo "==> Saved: $OUT"
