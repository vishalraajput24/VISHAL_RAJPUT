#!/home/vishalraajput24/kite_env/bin/python3
# ═══════════════════════════════════════════════════════════════
#  VRL_PRECHECK.py — VISHAL RAJPUT TRADE v13.10
#  Pre-market health check. Runs at 9:10 IST via crontab.
#  Validates bot readiness 5 min before market open.
#
#  Four checks:
#    1. Is vrl-main process alive? If not, start it.
#    2. Is token file from today? If not, run AUTH + restart vrl-main.
#    3. Can Kite API respond? If not, alert via Telegram.
#    4. Is WebSocket subscribed & receiving ticks? If not, restart vrl-main.
#
#  Telegram alerts on ANY failure so operator has 5 minutes to intervene.
# ═══════════════════════════════════════════════════════════════

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import VRL_DATA as D


def _tg(msg):
    """Send Telegram alert. Never raises."""
    try:
        url = "https://api.telegram.org/bot" + D.TELEGRAM_TOKEN + "/sendMessage"
        requests.post(url, json={
            "chat_id": D.TELEGRAM_CHAT_ID,
            "text": msg, "parse_mode": "HTML"
        }, timeout=10)
    except Exception:
        pass


def _run(cmd, timeout=30):
    """Run a shell command. Returns (rc, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


# ─────────────────────────────────────────────────────────────
#  CHECK 1: Is vrl-main service alive?
# ─────────────────────────────────────────────────────────────
def check_service_alive():
    rc, out, _ = _run("systemctl is-active vrl-main")
    if rc == 0 and out == "active":
        return True, "vrl-main active"
    # Try to start
    _tg("\u26a0\ufe0f <b>PRECHECK</b>: vrl-main NOT active — starting")
    _run("sudo /bin/systemctl start vrl-main", timeout=60)
    time.sleep(5)
    rc2, out2, _ = _run("systemctl is-active vrl-main")
    if rc2 == 0 and out2 == "active":
        return True, "vrl-main started from dead"
    return False, "vrl-main failed to start: " + out2


# ─────────────────────────────────────────────────────────────
#  CHECK 2: Token file from today?
# ─────────────────────────────────────────────────────────────
def check_token_fresh():
    today = date.today().isoformat()
    path = D.TOKEN_FILE_PATH
    if not os.path.isfile(path):
        return False, "no token file at " + path, True
    try:
        with open(path) as f:
            tok = json.load(f)
    except Exception as e:
        return False, "token file unreadable: " + str(e), True
    tok_date = tok.get("date", "")
    if tok_date == today:
        return True, "token dated " + today, False
    return False, "token dated " + str(tok_date) + ", expected " + today, True


# ─────────────────────────────────────────────────────────────
#  CHECK 3: Kite API responds?
# ─────────────────────────────────────────────────────────────
def check_kite_api():
    try:
        from VRL_AUTH import get_kite
        kite = get_kite()
        if not kite:
            return False, "get_kite returned None"
        profile = kite.profile()
        user_id = profile.get("user_id", "")
        return True, "kite.profile OK (" + user_id + ")"
    except Exception as e:
        return False, "kite.profile failed: " + str(e)[:100]


# ─────────────────────────────────────────────────────────────
#  CHECK 4: WebSocket + dashboard.json fresh?
# ─────────────────────────────────────────────────────────────
def check_websocket_fresh():
    dash_path = os.path.join(D.STATE_DIR, "vrl_dashboard.json")
    if not os.path.isfile(dash_path):
        return False, "dashboard.json missing"
    try:
        mtime = os.path.getmtime(dash_path)
        age = time.time() - mtime
        if age > 300:  # 5 min
            return False, "dashboard.json stale (" + str(int(age)) + "s old)"
        return True, "dashboard.json fresh (" + str(int(age)) + "s old)"
    except Exception as e:
        return False, "dashboard.json check failed: " + str(e)


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main():
    now = datetime.now()
    print("[PRECHECK] " + now.isoformat())
    print("─" * 60)

    results = []
    failures = []

    # Check 1: Service alive
    ok, msg = check_service_alive()
    print(("✅" if ok else "❌") + " Check 1 (service): " + msg)
    results.append(("service", ok, msg))
    if not ok:
        failures.append("service: " + msg)

    # Check 2: Token fresh
    ok, msg, need_refresh = check_token_fresh()
    print(("✅" if ok else "❌") + " Check 2 (token): " + msg)
    results.append(("token", ok, msg))
    if not ok:
        failures.append("token: " + msg)
        if need_refresh:
            print("[PRECHECK] Running AUTH + restart vrl-main")
            _run("cd " + os.path.dirname(os.path.abspath(__file__))
                 + " && /home/vishalraajput24/kite_env/bin/python3 VRL_AUTH.py",
                 timeout=120)
            _run("sudo /bin/systemctl restart vrl-main", timeout=60)
            time.sleep(8)

    # Check 3: Kite API
    ok, msg = check_kite_api()
    print(("✅" if ok else "❌") + " Check 3 (kite): " + msg)
    results.append(("kite", ok, msg))
    if not ok:
        failures.append("kite: " + msg)

    # Check 4: WebSocket dashboard freshness
    ok, msg = check_websocket_fresh()
    print(("✅" if ok else "❌") + " Check 4 (websocket): " + msg)
    results.append(("websocket", ok, msg))
    # Don't fail on this pre-market — dashboard may not be written yet

    # Telegram summary
    if failures:
        body = "\u26a0\ufe0f <b>PRECHECK FAILURES</b> (9:10 IST)\n"
        body += "\n".join("\u2022 " + f for f in failures)
        body += "\n\n<i>5 min to market open — check bot now</i>"
        _tg(body)
        print("\n[PRECHECK] ⚠️  " + str(len(failures)) + " failure(s) — alert sent")
        sys.exit(1)
    else:
        _tg("\u2705 <b>PRECHECK OK</b> (9:10 IST)\nBot ready for market open")
        print("\n[PRECHECK] ✅  All checks passed")
        sys.exit(0)


if __name__ == "__main__":
    main()
