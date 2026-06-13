#!/usr/bin/env python3
"""
Weekly dependency-update notifier (check-only, never installs).

Runs `pip list --outdated` against the interpreter that launched this script
(point the cron at kite_env's python so it reports the live bot's venv) and
sends the result to Telegram. NOTHING is auto-upgraded — on a live trading
bot, a breaking major (e.g. pandas 3.0) must be reviewed and applied by hand
in a market-closed window. See CLAUDE.md.

Cron (Sunday 03:00, market closed):
  0 3 * * 0 env $(cat ~/.env | grep -v '^#' | xargs) \
    ~/kite_env/bin/python3 ~/VISHAL_RAJPUT/dep_update_check.py \
    >> ~/logs/dep_update_check.log 2>&1
"""
import json
import os
import subprocess
import sys
from datetime import datetime

import requests

# Broker/runtime libs worth calling out explicitly — an upgrade here may track
# a real broker API change, but still needs a manual import-test + restart.
KEY_PKGS = {"kiteconnect", "mstock-tradingapi-a", "autobahn", "twisted"}


def get_outdated():
    out = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--outdated", "--format=json"],
        capture_output=True, text=True, timeout=180,
    )
    try:
        return json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return []


def send_tg(text):
    token = os.getenv("TG_TOKEN", "")
    chat_id = os.getenv("TG_GROUP_ID", "")
    if not token or not chat_id:
        print("[dep_check] TG_TOKEN / TG_GROUP_ID not set — printing only:\n" + text)
        return
    r = requests.post(
        "https://api.telegram.org/bot" + token + "/sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    if not r.ok:
        print(f"[dep_check] TG send failed {r.status_code}: {r.text}")


def main():
    venv = os.path.dirname(os.path.dirname(sys.executable))
    pkgs = get_outdated()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not pkgs:
        send_tg(f"📦 <b>Dep check</b> ({stamp})\nvenv: <code>{venv}</code>\n"
                f"✅ All packages up to date — nothing to upgrade.")
        return

    key = [p for p in pkgs if p["name"].lower() in KEY_PKGS]
    other = [p for p in pkgs if p["name"].lower() not in KEY_PKGS]

    lines = [f"📦 <b>Dep check</b> ({stamp}) — {len(pkgs)} outdated",
             f"venv: <code>{venv}</code>"]
    if key:
        lines.append("\n<b>⭐ Broker/runtime libs:</b>")
        for p in key:
            lines.append(f"  • {p['name']}: {p['version']} → {p['latest_version']}")
    if other:
        lines.append("\n<b>Other:</b>")
        for p in other:
            lines.append(f"  • {p['name']}: {p['version']} → {p['latest_version']}")
    lines.append("\n⚠️ Review &amp; apply manually in a market-closed window "
                 "(no auto-install).")
    send_tg("\n".join(lines))


if __name__ == "__main__":
    main()
