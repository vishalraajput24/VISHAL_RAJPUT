# ═══════════════════════════════════════════════════════════════
#  VRL_AUTH.py — VISHAL RAJPUT TRADE v12.16
#  Zerodha Kite authentication. Auto-login via TOTP.
#  v12.15: Standalone cron execution, stale token date check.
# ═══════════════════════════════════════════════════════════════

import json
import os
import re
import time
import logging
from datetime import date

import pyotp
import requests
from kiteconnect import KiteConnect

import VRL_DATA as D

logger = logging.getLogger("vrl_live")

def _read_token() -> dict:
    try:
        if os.path.isfile(D.TOKEN_FILE_PATH):
            with open(D.TOKEN_FILE_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _write_token(data: dict):
    os.makedirs(D.STATE_DIR, exist_ok=True)
    tmp = D.TOKEN_FILE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, D.TOKEN_FILE_PATH)

def _auto_login(kite) -> str:
    user_id     = os.getenv("ZERODHA_USER_ID", "")
    password    = os.getenv("ZERODHA_PASSWORD", "")
    totp_secret = os.getenv("TOTP_SECRET", "")
    api_secret  = D.KITE_API_SECRET
    session     = requests.Session()

    logger.info("[AUTH] Step 1: Password login")
    r          = session.post("https://kite.zerodha.com/api/login",
                              data={"user_id": user_id, "password": password}, timeout=15)
    request_id = r.json()["data"]["request_id"]
    logger.info("[AUTH] Step 1 OK")

    logger.info("[AUTH] Step 2: TOTP")
    totp = pyotp.TOTP(totp_secret).now()
    session.post("https://kite.zerodha.com/api/twofa",
                 data={"user_id": user_id, "request_id": request_id,
                       "twofa_value": totp, "twofa_type": "totp"}, timeout=15)
    logger.info("[AUTH] Step 2 OK")
    time.sleep(2)

    logger.info("[AUTH] Step 3: Fetching request_token")
    login_url     = kite.login_url()
    request_token = ""

    r = session.get(login_url, timeout=10, allow_redirects=False)
    finish_url = r.headers.get("Location", "")
    logger.info("[AUTH] Step 3a: finish_url=" + finish_url[:60])

    try:
        r2  = session.get(finish_url, timeout=10, allow_redirects=False)
        loc = r2.headers.get("Location", "")
        m   = re.search(r"request_token=([A-Za-z0-9]+)", loc)
        if m:
            request_token = m.group(1)
    except Exception as e:
        m = re.search(r"request_token=([A-Za-z0-9]+)", str(e))
        if m:
            request_token = m.group(1)

    if not request_token:
        raise RuntimeError("[AUTH] request_token not found after finish step")

    logger.info("[AUTH] Step 3 OK — " + request_token[:8] + "...")

    logger.info("[AUTH] Step 4: Generating session")
    sess         = kite.generate_session(request_token, api_secret=api_secret)
    access_token = sess["access_token"]
    logger.info("[AUTH] Done ✓")
    return access_token

def get_kite():
    kite      = KiteConnect(api_key=D.KITE_API_KEY)
    saved     = _read_token()
    today_str = date.today().isoformat()

    # Delete tokens older than 1 day (never serve yesterday's token)
    if saved.get("date") and saved.get("date") < today_str:
        logger.warning("[AUTH] Stale token from " + saved.get("date") + " — ignoring")
        saved = {}

    if saved.get("date") == today_str and saved.get("access_token"):
        logger.info("[AUTH] Trying saved token")
        kite.set_access_token(saved["access_token"])
        try:
            kite.profile()
            logger.info("[AUTH] Token valid ✓")
            return kite
        except Exception:
            logger.warning("[AUTH] Saved token expired")

    for attempt in range(3):
        try:
            token = _auto_login(kite)
            kite.set_access_token(token)
            _write_token({"date": today_str, "access_token": token})
            logger.info("[AUTH] Auto-login successful ✓")
            return kite
        except Exception as e:
            logger.error("[AUTH] Attempt " + str(attempt + 1) + " failed: " + str(e))
            if attempt < 2:
                time.sleep(3)

    raise RuntimeError("[AUTH] All login attempts failed")


def force_fresh_login():
    """v12.15: Force fresh login, ignoring cached token. For cron use."""
    kite      = KiteConnect(api_key=D.KITE_API_KEY)
    today_str = date.today().isoformat()
    for attempt in range(3):
        try:
            token = _auto_login(kite)
            kite.set_access_token(token)
            _write_token({"date": today_str, "access_token": token})
            print("[AUTH] Fresh login OK ✓ token cached for " + today_str)
            return kite
        except Exception as e:
            print("[AUTH] Attempt " + str(attempt + 1) + " failed: " + str(e))
            if attempt < 2:
                time.sleep(5)
    print("[AUTH] All fresh login attempts failed")
    return None


# v12.15: Standalone execution for cron job at 8 AM
# Previously cron ran `python3 VRL_AUTH.py` which did nothing (no __main__)
# Now it forces fresh login and caches today's token
def _tg_alert(msg):
    """Send auth alert to Telegram."""
    try:
        url = "https://api.telegram.org/bot" + D.TELEGRAM_TOKEN + "/sendMessage"
        requests.post(url, json={
            "chat_id": D.TELEGRAM_CHAT_ID,
            "text": msg, "parse_mode": "HTML"
        }, timeout=10)
    except Exception:
        pass


if __name__ == "__main__":
    print("[AUTH] Cron login starting — " + date.today().isoformat())
    # Delete stale token if > 1 day old
    saved = _read_token()
    if saved.get("date") and saved.get("date") != date.today().isoformat():
        print("[AUTH] Stale token from " + saved.get("date") + " — deleting")
        try:
            os.remove(D.TOKEN_FILE_PATH)
        except Exception:
            pass
    result = force_fresh_login()
    if result:
        # Verify token actually works
        try:
            result.profile()
            _tg_alert("🔑 <b>AUTH OK</b> " + date.today().isoformat()
                      + "\nToken fresh + verified ✓")
            print("[AUTH] Cron login complete + verified ✓")
        except Exception as e:
            _tg_alert("⚠️ <b>AUTH WARNING</b>\nToken saved but profile check failed: "
                      + str(e)[:100])
            print("[AUTH] Token saved but verification failed: " + str(e))
    else:
        _tg_alert("🚨 <b>AUTH FAILED</b>\nCron login failed at 8:00 AM\n"
                  "Bot will retry at 9:10 startup\nCheck manually if repeated")
        print("[AUTH] ⚠️ Cron login FAILED")
