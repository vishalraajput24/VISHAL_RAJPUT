#!/usr/bin/env python3
"""
VRL_LEVELS.py — Institutional level computation (shadow data collection only)

PURE OBSERVATION — does NOT block any live trades.
Logs filter pass/fail per V9 entry into ~/lab_data/shadow_levels_data.csv.

After 2 weeks of data, analyze and decide which filters to promote to live.

Levels computed:
  Yesterday spot: PDH, PDL, PDC, Pivot, TC, BC, CPR_W
  Today opening : ORH, ORL (9:15-9:30)
  Yesterday opt : opt_PDH, opt_PDL, opt_PDC (per strike + opt_type)

Shadow filters (proposed):
  G7  : DTE != 0                    (avoid expiry day theta trap)
  G8  : Pivot alignment             (CE > pivot, PE < pivot)
  G9  : CPR width <= 70             (skip chop days)
  G10 : NOT Mon 9:45-10:30          (worst hour-of-week)
  G11 : VWAP alignment on 15-min    (CE: fut > VWAP+25, PE: fut < VWAP-25)
"""

import os, csv, threading, requests
from datetime import datetime, date, time as _dtime, timedelta
import logging

logger = logging.getLogger("vrl_live")

# ── Telegram (imported lazily to avoid circular import) ──
_TG_BASE = "https://api.telegram.org/bot"


def _tg_send_levels(text: str):
    """Best-effort TG alert — never raises."""
    try:
        import VRL_DATA as _D
        token = _D.TELEGRAM_TOKEN
        chat  = _D.TELEGRAM_CHAT_ID
        if not token or not chat:
            return
        requests.post(
            _TG_BASE + token + "/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as _e:
        logger.debug(f"[LEVELS] TG send error: {_e}")

# ── Module-level cache (computed once per day at startup) ──
_levels_lock      = threading.Lock()
_daily_levels     = {}   # {'PDH', 'PDL', 'PDC', 'Pivot', 'TC', 'BC', 'CPR_W', 'ORH', 'ORL'}
_opt_levels       = {}   # {(strike, 'CE'/'PE'): {'opt_PDH','opt_PDL','opt_PDC'}}
_last_compute_day = None

# ── VWAP state (refreshed every 15-min candle) ──
_vwap_state = {
    "fut_close"   : 0.0,    # latest 15-min futures close
    "vwap"        : 0.0,    # latest 15-min VWAP value
    "gap"         : 0.0,    # fut_close - vwap
    "last_update" : None,   # datetime of last refresh
}
_VWAP_FUT_TOKEN = 16914178  # NIFTY near-month future — update when rolling
_VWAP_BUFFER    = 25        # pts — CE needs gap > +25, PE needs gap < -25

# ── Output CSV ──
LAB_DIR  = os.path.join(os.path.expanduser("~"), "lab_data")
CSV_PATH = os.path.join(LAB_DIR, "shadow_levels_data.csv")
CSV_HEADERS = [
    "date", "time", "direction", "strike", "entry_price", "spot",
    "PDH", "PDL", "PDC", "Pivot", "TC", "BC", "CPR_W",
    "ORH", "ORL", "opt_PDC",
    "above_pivot", "in_cpr",
    "g7_dte_ok", "g8_pivot_ok", "g9_cpr_ok", "g10_time_ok",
    "g11_vwap_ok", "vwap_fut_close", "vwap_value", "vwap_gap",
    "all_pass", "dte", "dow",
]


def _ensure_csv():
    os.makedirs(LAB_DIR, exist_ok=True)
    if not os.path.isfile(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow(CSV_HEADERS)


def compute_today(D, kite, expiry) -> dict:
    """
    Called once at startup (or first scan of the day).
    Computes yesterday-based levels (PDH/PDL/PDC/CPR/Pivot) from spot history.
    Stores in module-level _daily_levels.
    Returns the levels dict.
    """
    global _daily_levels, _last_compute_day, _opt_levels
    today = date.today()
    with _levels_lock:
        if _last_compute_day == today and _daily_levels:
            return _daily_levels

        try:
            # Yesterday spot 1-min — fetch generously to ensure we get prev session
            spot_df = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "minute", 800)
            if spot_df is None or spot_df.empty:
                logger.warning("[LEVELS] No spot history — levels unavailable")
                return {}
            if spot_df.index.tz is not None:
                spot_df.index = spot_df.index.tz_localize(None)

            # Today + yesterday dates
            unique_dates = sorted(set(spot_df.index.date))
            today_d = today
            if today_d not in unique_dates and len(unique_dates) > 0:
                today_d = unique_dates[-1]
            yest = None
            for d_ in unique_dates[::-1]:
                if d_ < today_d:
                    yest = d_
                    break
            if not yest:
                logger.warning("[LEVELS] No yesterday data in spot history")
                return {}

            ydf = spot_df[spot_df.index.date == yest]
            ydf = ydf.between_time("09:15", "15:30")
            if ydf.empty:
                logger.warning(f"[LEVELS] Yesterday {yest} session empty")
                return {}

            PDH = float(ydf["high"].max())
            PDL = float(ydf["low"].min())
            PDC = float(ydf["close"].iloc[-1])
            Pivot = (PDH + PDL + PDC) / 3.0
            BC    = (PDH + PDL) / 2.0
            TC    = 2 * Pivot - BC
            CPR_W = abs(TC - BC)

            # Opening range (today 9:15-9:30) — may be empty if called before 9:30
            tdf = spot_df[spot_df.index.date == today_d]
            or_df = tdf.between_time("09:15", "09:29") if not tdf.empty else None
            ORH = float(or_df["high"].max()) if (or_df is not None and not or_df.empty) else 0.0
            ORL = float(or_df["low"].min())  if (or_df is not None and not or_df.empty) else 0.0

            _daily_levels = {
                "PDH": round(PDH, 2), "PDL": round(PDL, 2), "PDC": round(PDC, 2),
                "Pivot": round(Pivot, 2),
                "TC":    round(max(TC, BC), 2),
                "BC":    round(min(TC, BC), 2),
                "CPR_W": round(CPR_W, 2),
                "ORH":   round(ORH, 2),
                "ORL":   round(ORL, 2),
                "yest_date": str(yest),
                "today_date": str(today_d),
            }
            _last_compute_day = today
            logger.info(
                f"[LEVELS] PDH={_daily_levels['PDH']} PDL={_daily_levels['PDL']} "
                f"PDC={_daily_levels['PDC']} Pivot={_daily_levels['Pivot']} "
                f"CPR={_daily_levels['BC']}-{_daily_levels['TC']} (w={_daily_levels['CPR_W']}) "
                f"ORH={_daily_levels['ORH']} ORL={_daily_levels['ORL']}"
            )

        except Exception as e:
            logger.warning(f"[LEVELS] compute_today error: {e}")
            return {}

    return _daily_levels


def refresh_opening_range(D) -> dict:
    """Call after 9:30 to update ORH/ORL once opening range completes."""
    global _daily_levels
    if not _daily_levels:
        return {}
    try:
        spot_df = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "minute", 60)
        if spot_df is None or spot_df.empty:
            return _daily_levels
        if spot_df.index.tz is not None:
            spot_df.index = spot_df.index.tz_localize(None)
        today_d = date.today()
        tdf = spot_df[spot_df.index.date == today_d].between_time("09:15", "09:29")
        if not tdf.empty:
            with _levels_lock:
                _daily_levels["ORH"] = round(float(tdf["high"].max()), 2)
                _daily_levels["ORL"] = round(float(tdf["low"].min()), 2)
            logger.info(f"[LEVELS] OR refreshed: ORH={_daily_levels['ORH']} ORL={_daily_levels['ORL']}")
    except Exception as e:
        logger.debug(f"[LEVELS] refresh_OR error: {e}")
    return _daily_levels


def update_vwap(kite) -> dict:
    """
    Fetch latest 15-min NIFTY futures bars, compute today's running VWAP.
    Call at startup and every 15-min candle boundary in main loop.
    Returns current _vwap_state dict.
    NEVER raises — silent on failure.
    """
    global _vwap_state
    try:
        import numpy as np
        from_dt = datetime.now() - timedelta(hours=7)
        data    = kite.historical_data(_VWAP_FUT_TOKEN, from_dt, datetime.now(), "15minute")
        if not data:
            return _vwap_state

        import pandas as pd
        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        today_d = date.today()
        df = df[df.index.date == today_d]
        df = df.between_time("09:15", "15:30")
        if df.empty:
            return _vwap_state

        # Running VWAP from 9:15
        df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0
        df["tv"]      = df["typical"] * df["volume"]
        cum_tv  = df["tv"].cumsum()
        cum_vol = df["volume"].cumsum()
        df["vwap"] = cum_tv / cum_vol.replace(0, np.nan)

        last = df.iloc[-1]
        fut_close = round(float(last["close"]), 2)
        vwap_val  = round(float(last["vwap"]), 2)
        gap       = round(fut_close - vwap_val, 2)

        with _levels_lock:
            _vwap_state["fut_close"]   = fut_close
            _vwap_state["vwap"]        = vwap_val
            _vwap_state["gap"]         = gap
            _vwap_state["last_update"] = datetime.now()

        logger.info(
            f"[VWAP] fut={fut_close}  vwap={vwap_val}  "
            f"gap={gap:+.1f}  "
            f"({'BULL' if gap > 0 else 'BEAR'} bias)"
        )
    except Exception as e:
        logger.debug(f"[VWAP] update error: {e}")
    return _vwap_state


def get_vwap_state() -> dict:
    """Return current VWAP state (for dashboard etc)."""
    return dict(_vwap_state)


def compute_opt_pdc(D, strike: int, opt_type: str, token: int) -> dict:
    """
    Yesterday's option PDC for a single strike+opt_type. Cached.
    Returns {'opt_PDH','opt_PDL','opt_PDC'} or {} on failure.
    """
    key = (int(strike), opt_type)
    with _levels_lock:
        if key in _opt_levels:
            return _opt_levels[key]
    try:
        df = D.get_historical_data(token, "minute", 800)
        if df is None or df.empty:
            return {}
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        dates = sorted(set(df.index.date))
        today_d = date.today()
        yest = None
        for d_ in dates[::-1]:
            if d_ < today_d:
                yest = d_; break
        if not yest: return {}
        ydf = df[df.index.date == yest].between_time("09:15", "15:30")
        if ydf.empty: return {}
        out = {
            "opt_PDH": round(float(ydf["high"].max()), 2),
            "opt_PDL": round(float(ydf["low"].min()), 2),
            "opt_PDC": round(float(ydf["close"].iloc[-1]), 2),
        }
        with _levels_lock:
            _opt_levels[key] = out
        return out
    except Exception as e:
        logger.debug(f"[LEVELS] opt_pdc {strike}{opt_type} error: {e}")
        return {}


def evaluate_filters(direction: str, spot_px: float, entry_time_dt: datetime,
                     dte: int, opt_pdc: float = 0.0) -> dict:
    """
    Returns dict of shadow filter pass/fail. PURE OBSERVATION — does not block.
    direction: 'CE' or 'PE'
    spot_px:   current NIFTY spot
    entry_time_dt: datetime of entry
    dte:       days to expiry (0 = today is expiry)
    opt_pdc:   yesterday close for this option (0 if unknown)
    """
    L = _daily_levels
    if not L:
        return {"g7_dte_ok": None, "g8_pivot_ok": None, "g9_cpr_ok": None,
                "g10_time_ok": None, "all_pass": None}

    # G7: DTE != 0 (skip expiry day)
    g7 = dte != 0

    # G8: pivot alignment
    pivot = L.get("Pivot", 0)
    if direction == "CE":
        g8 = spot_px > pivot
    elif direction == "PE":
        g8 = spot_px < pivot
    else:
        g8 = False

    # G9: CPR width <= 70
    g9 = L.get("CPR_W", 999) <= 70

    # G10: NOT (Monday AND 9:45 <= time <= 10:30)
    is_mon  = entry_time_dt.weekday() == 0
    in_morn = _dtime(9, 45) <= entry_time_dt.time() <= _dtime(10, 30)
    g10 = not (is_mon and in_morn)

    # G11: VWAP alignment — CE: fut > VWAP+25, PE: fut < VWAP-25
    gap = _vwap_state.get("gap", 0.0)
    vwap_ready = _vwap_state.get("vwap", 0.0) > 0
    if not vwap_ready:
        g11 = None   # VWAP not yet computed — unknown
    elif direction == "CE":
        g11 = gap > _VWAP_BUFFER
    elif direction == "PE":
        g11 = gap < -_VWAP_BUFFER
    else:
        g11 = False

    return {
        "g7_dte_ok"  : g7,
        "g8_pivot_ok": g8,
        "g9_cpr_ok"  : g9,
        "g10_time_ok": g10,
        "g11_vwap_ok": g11,
        "all_pass"   : g7 and g8 and g9 and g10 and (g11 is not False),
    }


def log_entry(direction: str, strike: int, entry_price: float, spot_px: float,
              entry_time_dt: datetime, dte: int, opt_pdc: float = 0.0):
    """
    Called every V9 entry. Computes filters + writes one CSV row.
    NEVER blocks the trade — pure data collection.
    """
    try:
        _ensure_csv()
        f = evaluate_filters(direction, spot_px, entry_time_dt, dte, opt_pdc)
        L = _daily_levels

        pivot = L.get("Pivot", 0)
        bc, tc = L.get("BC", 0), L.get("TC", 0)
        above_pivot = spot_px > pivot if pivot else None
        in_cpr      = bc <= spot_px <= tc if (bc and tc) else None

        row = [
            entry_time_dt.strftime("%Y-%m-%d"),
            entry_time_dt.strftime("%H:%M:%S"),
            direction, strike, round(entry_price, 2), round(spot_px, 2),
            L.get("PDH", 0), L.get("PDL", 0), L.get("PDC", 0),
            L.get("Pivot", 0), L.get("TC", 0), L.get("BC", 0), L.get("CPR_W", 0),
            L.get("ORH", 0), L.get("ORL", 0), round(opt_pdc, 2),
            above_pivot, in_cpr,
            f.get("g7_dte_ok"), f.get("g8_pivot_ok"),
            f.get("g9_cpr_ok"), f.get("g10_time_ok"),
            f.get("g11_vwap_ok"),
            round(_vwap_state.get("fut_close", 0), 2),
            round(_vwap_state.get("vwap", 0), 2),
            round(_vwap_state.get("gap", 0), 2),
            f.get("all_pass"), dte, entry_time_dt.strftime("%A"),
        ]
        with open(CSV_PATH, "a", newline="") as fh:
            csv.writer(fh).writerow(row)

        # Human-readable log line
        marks = lambda b: "✓" if b is True else "✗" if b is False else "?"
        logger.info(
            f"[SHADOW-LVL] {direction} {strike} entry={entry_price} spot={spot_px} | "
            f"G7={marks(f.get('g7_dte_ok'))}(dte={dte}) "
            f"G8={marks(f.get('g8_pivot_ok'))}(pivot={pivot}) "
            f"G9={marks(f.get('g9_cpr_ok'))}(cpr_w={L.get('CPR_W',0)}) "
            f"G10={marks(f.get('g10_time_ok'))} "
            f"G11={marks(f.get('g11_vwap_ok'))}(gap={_vwap_state.get('gap',0):+.1f}) → "
            f"all={marks(f.get('all_pass'))}"
        )

        # ── Telegram alert — clean and focused ───────────────────
        g11v      = f.get('g11_vwap_ok')
        vwap_gap  = _vwap_state.get('gap', 0)
        sl_price  = round(entry_price - 12, 1)
        vwap_icon = "✅" if g11v is True else "❌" if g11v is False else "❓"

        tg_text = (
            f"<b>{direction} {strike}</b>\n"
            f"Entry: <b>{round(entry_price, 1)}</b>  |  SL: {sl_price}\n"
            f"{vwap_icon} VWAP gap: {vwap_gap:+.1f} pts"
        )
        _tg_send_levels(tg_text)
    except Exception as e:
        logger.warning(f"[SHADOW-LVL] log_entry error: {e}")


def get_levels() -> dict:
    """Return current daily levels (for dashboard etc)."""
    return dict(_daily_levels)
