#!/usr/bin/env bash
# Daily analysis report — run at 4 PM IST via cron
# Generates markdown report and commits it to GitHub on main directly.
set -e

export PATH="$HOME/bin:$PATH"
REPO="$HOME/VISHAL_RAJPUT"
DATE=$(date +%Y-%m-%d)
REPORT="$REPO/analysis/daily/$DATE.md"

cd "$REPO"

# Ensure we are on main and up to date
git checkout main
git pull --rebase --quiet

# Generate the report
/home/vishalraajput24/kite_env/bin/python3 "$REPO/analysis/daily_report.py" "$DATE"

# Nothing to commit if report unchanged (idempotent re-runs)
if git diff --quiet HEAD -- "$REPORT" && git ls-files --error-unmatch "$REPORT" &>/dev/null 2>&1; then
    echo "[$DATE] Report unchanged — nothing to commit."
    exit 0
fi

git add "$REPORT"
git commit -m "analysis: daily report $DATE [auto]"
git push origin main

echo "[$DATE] Report committed and pushed."
