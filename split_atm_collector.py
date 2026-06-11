"""
Split-ATM 1-Min Data Collector
Collects every minute during market hours:
  CE = floor(spot / 50) * 50   (ITM call)
  PE = ceil(spot  / 50) * 50   (ITM put)

Writes to: ~/lab_data/split_atm_1min/split_atm_YYYYMMDD.csv
Columns include ema9_high and ema9_low for V10 gate backtesting.

Run: nohup python3 split_atm_collector.py >> ~/logs/split_atm_collector.log 2>&1 &
"""

import os, sys, json, csv, time, logging
from datetime import datetime, date, timedelta

import pandas as pd
from kiteconnect import KiteConnect

# ─── CONFIG ───────────────────────────────────────────────────
BASE_DIR        = os.path.expanduser("~")
REPO_DIR        = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE      = os.path.join(REPO_DIR, "state", "access_token.json")
OUT_DIR         = os.path.join(BASE_DIR, "lab_data", "split_atm_1min")
KITE_API_KEY    = os.getenv("KITE_API_KEY", "")
NIFTY_SPOT_TOKEN = 256265
STRIKE_STEP     = 50
EMA_SPAN        = 9
WARMUP_CANDLES  = 60   # candles prepended for EMA warmup

MARKET_OPEN  = (9, 15)
MARKET_CLOSE = (15, 30)
BLACKOUT_END = (9, 45)

FIELDNAMES = [
    "timestamp", "ce_strike", "pe_strike",
    "ce_open", "ce_high", "ce_low", "ce_close", "ce_volume",
    "pe_open", "pe_high", "pe_low", "pe_close", "pe_volume",
    "spot_ref",
    "ce_ema9c", "ce_ema9h", "ce_ema9l",
    "pe_ema9c", "pe_ema9h", "pe_ema9l",
    "ce_mom_gap",   # ce_close - ce_ema9h
    "pe_decay_cls", # pe_close - pe_ema9c  (new OPP DECAY metric)
    "pe_decay_low", # pe_close - pe_ema9l  (original OPP DECAY metric)
    "pe_mom_gap",   # pe_close - pe_ema9h
    "ce_decay_cls", # ce_close - ce_ema9c
    "ce_decay_low", # ce_close - ce_ema9l
]

# ─── LOGGING ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("split_atm_collector")

# ─── HELPERS ──────────────────────────────────────────────────

def load_kite() -> KiteConnect:
    if not KITE_API_KEY:
        log.error("KITE_API_KEY env not set")
        sys.exit(1)
    with open(TOKEN_FILE) as f:
        saved = json.load(f)
    today = date.today().isoformat()
    if saved.get("date") != today or not saved.get("access_token"):
        log.error(f"Token stale or missing. Date in file: {saved.get('date')}, today: {today}")
        sys.exit(1)
    kite = KiteConnect(api_key=KITE_API_KEY)
    kite.set_access_token(saved["access_token"])
    log.info("Kite connected. Token date: " + saved["date"])
    return kite


def get_split_atm(spot: float):
    ce = int((spot // STRIKE_STEP) * STRIKE_STEP)
    pe = int(((spot + STRIKE_STEP - 1) // STRIKE_STEP) * STRIKE_STEP)
    return ce, pe


def resolve_token(kite: KiteConnect, strike: int, opt_type: str, expiry: date) -> int:
    instruments = kite.instruments("NFO")
    expiry_str = expiry.isoformat()
    for inst in instruments:
        if (inst.get("name") == "NIFTY"
                and int(inst.get("strike", 0)) == strike
                and str(inst.get("expiry", "")) == expiry_str
                and inst.get("instrument_type") == opt_type):
            return inst["instrument_token"]
    return None


def get_nearest_expiry(kite: KiteConnect) -> date:
    instruments = kite.instruments("NFO")
    today = date.today()
    expiries = sorted(set(
        inst["expiry"] for inst in instruments
        if inst.get("name") == "NIFTY"
        and inst.get("instrument_type") in ("CE", "PE")
        and isinstance(inst.get("expiry"), date)
        and inst["expiry"] >= today
    ))
    return expiries[0] if expiries else None


def ema(series, span=EMA_SPAN):
    return series.ewm(span=span, adjust=False).mean()


def fetch_1min_with_warmup(kite: KiteConnect, token: int, now: datetime) -> pd.DataFrame:
    extra = timedelta(minutes=WARMUP_CANDLES * 2 + 120)
    from_dt = now - extra
    candles = kite.historical_data(
        instrument_token=token,
        from_date=from_dt,
        to_date=now,
        interval="minute",
        continuous=False,
        oi=False,
    )
    if not candles or len(candles) < 3:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    df.rename(columns={"date": "timestamp"}, inplace=True)
    df["ema9c"] = ema(df["close"])
    df["ema9h"] = ema(df["high"])
    df["ema9l"] = ema(df["low"])
    return df


def out_path(d: date) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    return os.path.join(OUT_DIR, f"split_atm_{d.strftime('%Y%m%d')}.csv")


def already_written(path: str, ts_str: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        with open(path) as f:
            for row in csv.DictReader(f):
                if row.get("timestamp") == ts_str:
                    return True
    except Exception:
        pass
    return False


def append_row(path: str, row: dict):
    is_new = not os.path.isfile(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerow(row)
        f.flush()


def is_market_open(now: datetime) -> bool:
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t <= MARKET_CLOSE and now.weekday() < 5


# ─── TOKEN CACHE (per session) ────────────────────────────────
_token_cache: dict = {}   # (strike, opt_type) → token int
_locked_ce:   int  = 0    # locked CE strike for the day
_locked_pe:   int  = 0    # locked PE strike for the day
RELOC_BUFFER  = 75        # pts beyond current midpoint to trigger relocation


def get_token(kite, strike, opt_type, expiry):
    key = (strike, opt_type)
    if key not in _token_cache:
        tok = resolve_token(kite, strike, opt_type, expiry)
        if tok:
            _token_cache[key] = tok
            log.info(f"Token resolved: {opt_type} {strike} → {tok}")
        else:
            log.warning(f"Token not found: {opt_type} {strike}")
            return None
    return _token_cache.get(key)


def resolve_strikes(spot: float) -> tuple:
    """Return locked strikes, relocating only if spot moves >= RELOC_BUFFER
    beyond current midpoint. Locks at open on first call each day."""
    global _locked_ce, _locked_pe
    if _locked_ce == 0:
        # First call of the day — lock at open
        _locked_ce, _locked_pe = get_split_atm(spot)
        log.info(f"Strikes LOCKED at open: CE={_locked_ce} PE={_locked_pe} spot={spot:.0f}")
        return _locked_ce, _locked_pe

    midpoint = (_locked_ce + _locked_pe) / 2
    if abs(spot - midpoint) >= RELOC_BUFFER:
        new_ce, new_pe = get_split_atm(spot)
        log.info(f"Strike RELOC: {_locked_ce}/{_locked_pe} → {new_ce}/{new_pe} "
                 f"(spot={spot:.0f} drift={spot-midpoint:+.0f})")
        _locked_ce, _locked_pe = new_ce, new_pe

    return _locked_ce, _locked_pe


# ─── MAIN LOOP ────────────────────────────────────────────────

def collect_once(kite, expiry, now):
    # Get spot LTP
    try:
        ltp_data = kite.ltp(["NSE:NIFTY 50"])
        spot = ltp_data["NSE:NIFTY 50"]["last_price"]
    except Exception as e:
        log.warning(f"Spot LTP failed: {e}")
        return

    ce_strike, pe_strike = resolve_strikes(spot)
    log.info(f"Spot={spot:.0f}  CE={ce_strike}  PE={pe_strike}")

    ce_tok = get_token(kite, ce_strike, "CE", expiry)
    pe_tok = get_token(kite, pe_strike, "PE", expiry)
    if not ce_tok or not pe_tok:
        log.warning("Token missing, skipping this candle")
        return

    try:
        ce_df = fetch_1min_with_warmup(kite, ce_tok, now)
        time.sleep(0.4)
        pe_df = fetch_1min_with_warmup(kite, pe_tok, now)
    except Exception as e:
        log.error(f"Fetch error: {e}")
        return

    if ce_df.empty or pe_df.empty or len(ce_df) < 2 or len(pe_df) < 2:
        log.warning("Insufficient candles")
        return

    # Last CLOSED candle = iloc[-2]
    ce = ce_df.iloc[-2]
    pe = pe_df.iloc[-2]
    ts_str = ce["timestamp"].strftime("%Y-%m-%d %H:%M:%S")

    path = out_path(date.today())
    if already_written(path, ts_str):
        log.debug(f"Already written {ts_str}")
        return

    ce_mom_gap   = round(float(ce["close"] - ce["ema9h"]), 2)
    pe_decay_cls = round(float(pe["close"] - pe["ema9c"]), 2)
    pe_decay_low = round(float(pe["close"] - pe["ema9l"]), 2)
    pe_mom_gap   = round(float(pe["close"] - pe["ema9h"]), 2)
    ce_decay_cls = round(float(ce["close"] - ce["ema9c"]), 2)
    ce_decay_low = round(float(ce["close"] - ce["ema9l"]), 2)

    row = {
        "timestamp":   ts_str,
        "ce_strike":   ce_strike,
        "pe_strike":   pe_strike,
        "ce_open":     round(float(ce["open"]),  2),
        "ce_high":     round(float(ce["high"]),  2),
        "ce_low":      round(float(ce["low"]),   2),
        "ce_close":    round(float(ce["close"]), 2),
        "ce_volume":   int(ce["volume"]),
        "pe_open":     round(float(pe["open"]),  2),
        "pe_high":     round(float(pe["high"]),  2),
        "pe_low":      round(float(pe["low"]),   2),
        "pe_close":    round(float(pe["close"]), 2),
        "pe_volume":   int(pe["volume"]),
        "spot_ref":    round(spot, 2),
        "ce_ema9c":    round(float(ce["ema9c"]), 2),
        "ce_ema9h":    round(float(ce["ema9h"]), 2),
        "ce_ema9l":    round(float(ce["ema9l"]), 2),
        "pe_ema9c":    round(float(pe["ema9c"]), 2),
        "pe_ema9h":    round(float(pe["ema9h"]), 2),
        "pe_ema9l":    round(float(pe["ema9l"]), 2),
        "ce_mom_gap":   ce_mom_gap,
        "pe_decay_cls": pe_decay_cls,
        "pe_decay_low": pe_decay_low,
        "pe_mom_gap":   pe_mom_gap,
        "ce_decay_cls": ce_decay_cls,
        "ce_decay_low": ce_decay_low,
    }

    append_row(path, row)
    log.info(f"Wrote {ts_str} | CE={ce['close']} mom={ce_mom_gap:+.1f} | "
             f"PE={pe['close']} decay_cls={pe_decay_cls:+.1f} decay_low={pe_decay_low:+.1f}")


def main():
    log.info("=== Split-ATM Collector starting ===")
    kite   = load_kite()
    expiry = get_nearest_expiry(kite)
    log.info(f"Expiry: {expiry}")

    while True:
        now = datetime.now()

        if not is_market_open(now):
            if now.weekday() >= 5:
                log.info("Weekend — sleeping 1h")
                time.sleep(3600)
            elif (now.hour, now.minute) < MARKET_OPEN:
                secs = ((MARKET_OPEN[0] - now.hour) * 60 + (MARKET_OPEN[1] - now.minute)) * 60
                log.info(f"Pre-market — sleeping {secs//60}m")
                time.sleep(max(secs - 30, 30))
            else:
                log.info("Market closed — exiting")
                break
            continue

        # Refresh expiry daily (handles weekly expiry rollover)
        if now.hour == 9 and now.minute == 15:
            expiry = get_nearest_expiry(kite)
            _token_cache.clear()
            global _locked_ce, _locked_pe
            _locked_ce = 0
            _locked_pe = 0
            log.info(f"Daily refresh — expiry: {expiry}")

        # Wait until HH:MM:32 (32s after candle close) to ensure candle is settled
        target_sec = 32
        cur_sec = now.second
        if cur_sec < target_sec:
            time.sleep(target_sec - cur_sec)
        elif cur_sec > target_sec + 25:
            # too late in this minute, wait for next
            sleep_secs = 60 - cur_sec + target_sec
            time.sleep(sleep_secs)
            continue

        collect_once(kite, expiry, datetime.now())

        # Sleep to next minute boundary
        now2 = datetime.now()
        sleep_secs = 60 - now2.second + target_sec
        if sleep_secs > 60:
            sleep_secs -= 60
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
