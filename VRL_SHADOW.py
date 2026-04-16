#!/home/user/kite_env/bin/python3
# ═══════════════════════════════════════════════════════════════
#  VRL_SHADOW.py — VISHAL RAJPUT TRADE v15.2 (Part 4)
#  Silent 1-min shadow strategy. Mirrors v15.2 entry + exit logic
#  on 1-min option candles. Never places orders, never alerts
#  during the day, never touches live state.
#
#  Purpose: 1-min vs 3-min head-to-head A/B comparison. Data only.
#
#  Outputs:
#    ~/lab_data/shadow_1min/shadow_trades_YYYYMMDD.csv
#    ~/lab_data/shadow_1min/shadow_scans_YYYYMMDD.csv
#
#  Single EOD Telegram at 15:35 via emit_eod_summary().
# ═══════════════════════════════════════════════════════════════

import csv
import os
import threading
import logging
from datetime import date, datetime, timedelta

import pandas as pd

import VRL_DATA as D
import VRL_CONFIG as CFG

logger = logging.getLogger("vrl_live")

# ── Paths ─────────────────────────────────────────────────────

SHADOW_DIR = os.path.join(D.LAB_DIR, "shadow_1min")
os.makedirs(SHADOW_DIR, exist_ok=True)


def _trades_csv(d: date = None) -> str:
    d = d or date.today()
    return os.path.join(SHADOW_DIR, "shadow_trades_" + d.strftime("%Y%m%d") + ".csv")


def _scans_csv(d: date = None) -> str:
    d = d or date.today()
    return os.path.join(SHADOW_DIR, "shadow_scans_" + d.strftime("%Y%m%d") + ".csv")


FIELDS_TRADES = [
    "date", "entry_time", "exit_time", "direction",
    "entry_price", "exit_price", "pnl_pts", "peak_pnl",
    "candles_held", "exit_reason", "straddle_delta",
]

FIELDS_SCANS = [
    "timestamp", "direction", "close", "ema9_high", "ema9_low",
    "band_width", "body_pct", "green", "straddle_delta",
    "fired", "reject_reason",
]


# ── Independent shadow state (NEVER mutates live state) ──────
# Single shadow position at a time (mirrors live's one-trade rule).
# Module-private — callers use the provided functions, not this dict.

_lock = threading.Lock()

shadow_state = {
    "in_trade":            False,
    "direction":           "",
    "entry_price":         0.0,
    "entry_time":          "",
    "peak_pnl":            0.0,
    "candles_held":        0,
    "entry_ema9_low":      0.0,
    "entry_ema9_high":     0.0,
    "entry_straddle_delta": 0.0,
    "last_band_ts":        "",
    "token":               0,
    "last_exit_time":      None,
    "last_exit_direction": "",
    # Running day stats (accumulated from exits)
    "trades_today":        0,
    "wins_today":          0,
    "losses_today":        0,
    "total_pnl":           0.0,
    "peak_sum":            0.0,
    "peaks_over_10":       0,
}


def reset_day():
    """Call on new trading day. Resets counters + closes any leftover trade."""
    with _lock:
        shadow_state.update({
            "in_trade": False, "direction": "", "entry_price": 0.0,
            "entry_time": "", "peak_pnl": 0.0, "candles_held": 0,
            "entry_ema9_low": 0.0, "entry_ema9_high": 0.0,
            "entry_straddle_delta": 0.0, "last_band_ts": "",
            "token": 0,
            "last_exit_time": None, "last_exit_direction": "",
            "trades_today": 0, "wins_today": 0, "losses_today": 0,
            "total_pnl": 0.0, "peak_sum": 0.0, "peaks_over_10": 0,
        })


def day_summary() -> dict:
    """Return current day's shadow stats (used by dashboard + EOD)."""
    with _lock:
        n = shadow_state.get("trades_today", 0)
        wins = shadow_state.get("wins_today", 0)
        losses = shadow_state.get("losses_today", 0)
        pnl = round(shadow_state.get("total_pnl", 0), 1)
        avg_peak = round(shadow_state.get("peak_sum", 0) / n, 1) if n > 0 else 0.0
        wr = round((wins / n) * 100) if n > 0 else 0
        return {
            "trades": n, "wins": wins, "losses": losses,
            "pnl": pnl, "wr": wr, "avg_peak": avg_peak,
            "peaks_over_10": shadow_state.get("peaks_over_10", 0),
        }


# ── CSV helpers ───────────────────────────────────────────────

def _append_csv(path: str, fields: list, row: dict):
    is_new = not os.path.isfile(path)
    try:
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if is_new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        logger.debug("[SHADOW_1MIN] CSV write error " + path + ": " + str(e))


# ── Indicator layer — 1-min EMA9 band ────────────────────────

def _ema9_bands_1min(df: pd.DataFrame) -> pd.DataFrame:
    """Compute EMA9 of high and low columns. Span=9 matches live band spec."""
    out = df.copy()
    out["ema9_high"] = df["high"].ewm(span=9, adjust=False).mean().round(2)
    out["ema9_low"]  = df["low"].ewm(span=9,  adjust=False).mean().round(2)
    return out


# ── Gate 7 period + threshold (shared logic with VRL_ENGINE) ─

def _period_and_threshold(now: datetime):
    tiers = CFG.straddle_thresholds() or {}

    def _m(tier, pos):
        s = (tiers.get(tier) or {}).get(pos)
        if not s:
            return None
        hh, mm = str(s).split(":")
        return int(hh) * 60 + int(mm)

    mod = now.hour * 60 + now.minute
    open_s = _m("opening", "start") or 585
    open_e = _m("opening", "end")   or 630
    mid_e  = _m("midday",  "end")   or 840

    if open_s <= mod < open_e:
        return "OPENING", (tiers.get("opening") or {}).get("min_delta", 1)
    if open_e <= mod < mid_e:
        return "MIDDAY",  (tiers.get("midday")  or {}).get("min_delta", 5)
    return "CLOSING", (tiers.get("closing") or {}).get("min_delta", 3)


# ── Scan one option side (CE or PE) on 1-min candles ─────────

def _scan_side(option_type: str, token: int, atm_strike: int,
               spot_ltp: float, now: datetime) -> dict:
    """Run v15.2 entry gates on 1-min candles for one side.
    Returns a dict identical in spirit to E.check_entry but computed on 1-min.
    Writes one row to shadow_scans CSV. Never raises."""

    out = {
        "fired": False, "reject_reason": "", "direction": option_type,
        "close": 0.0, "ema9_high": 0.0, "ema9_low": 0.0,
        "band_width": 0.0, "body_pct": 0.0, "green": False,
        "straddle_delta": None, "period": "", "threshold": 0,
    }

    try:
        df = D.get_historical_data(token, "minute", 15)
        if df is None or df.empty or len(df) < 4:
            out["reject_reason"] = "insufficient_1m_data"
            return out

        df = _ema9_bands_1min(df)
        last = df.iloc[-2]    # last closed 1-min candle
        prev = df.iloc[-3]

        close = float(last["close"])
        open_ = float(last["open"])
        high  = float(last["high"])
        low   = float(last["low"])
        ema9h = float(last["ema9_high"])
        ema9l = float(last["ema9_low"])
        prev_close = float(prev["close"])
        prev_ema9h = float(prev["ema9_high"])
        band_width = round(ema9h - ema9l, 2)
        candle_range = high - low
        body = abs(close - open_)
        body_pct = round((body / candle_range * 100) if candle_range > 0 else 0, 1)
        green = close > open_

        out.update({
            "close": round(close, 2), "ema9_high": round(ema9h, 2),
            "ema9_low": round(ema9l, 2), "band_width": band_width,
            "body_pct": body_pct, "green": green,
        })

        # Mirror live reject-order so CSV reject_reasons stay comparable.

        # Gate 1: market-open time window (only enforced when market open)
        if D.is_market_open():
            warmup = CFG.entry_ema9_band("warmup_until", "09:45")
            cutoff = CFG.entry_ema9_band("cutoff_after", "15:10")
            mins = now.hour * 60 + now.minute
            wh, wm = warmup.split(":")
            ch, cm = cutoff.split(":")
            if mins < int(wh) * 60 + int(wm):
                out["reject_reason"] = "before_" + warmup + "_warmup"
                return out
            if mins >= int(ch) * 60 + int(cm):
                out["reject_reason"] = "after_" + cutoff + "_cutoff"
                return out

        # Gate 2: cooldown same direction
        cd_min = CFG.entry_ema9_band("cooldown_minutes", 5)
        le_ts = shadow_state.get("last_exit_time")
        le_dir = shadow_state.get("last_exit_direction", "")
        if le_ts and le_dir == option_type:
            try:
                elapsed = (now - datetime.fromisoformat(le_ts)).total_seconds() / 60
                if elapsed < cd_min:
                    out["reject_reason"] = "cooldown_" + str(round(cd_min - elapsed, 1)) + "min"
                    return out
            except Exception:
                pass

        # Gate 3: fresh breakout
        if not (close > ema9h and prev_close <= prev_ema9h):
            if close <= ema9h:
                out["reject_reason"] = "below_band_close=" + str(round(close, 1))
            else:
                out["reject_reason"] = "stale_breakout_prev_close=" + str(round(prev_close, 1))
            return out

        # Gate 4: green
        if not green:
            out["reject_reason"] = "red_candle"
            return out

        # Gate 5: body
        body_min = CFG.entry_ema9_band("body_pct_min", 30)
        if body_pct < body_min:
            out["reject_reason"] = "weak_body_" + str(int(body_pct)) + "pct"
            return out

        # Gate 6: band width (same chop filter as live)
        min_bw = CFG.entry_ema9_band("min_band_width_pts", 8)
        if band_width < min_bw:
            out["reject_reason"] = "narrow_band_" + str(round(band_width, 1)) + "pts"
            return out

        # Gate 7 (v15.2.5 Fix 5): straddle = DISPLAY ONLY, never blocks.
        # Mirrors the live engine policy change. Still logged to the scan CSV
        # for A/B analysis but no longer rejects a 1-min shadow entry.
        period, _thr = _period_and_threshold(now)
        out["period"] = period
        out["threshold"] = 0   # deprecated; kept for back-compat
        sd = None
        try:
            sd = D.get_straddle_delta(
                atm_strike, lookback_minutes=int(CFG.straddle_filter("lookback_minutes", 15)))
        except Exception:
            sd = None
        out["straddle_delta"] = sd
        # No reject path — shadow fires on the same gates as live.
        out["fired"] = True
    except Exception as e:
        logger.debug("[SHADOW_1MIN] scan error " + option_type + ": " + str(e))
        out["reject_reason"] = "err_" + str(e)[:40]
    return out


# ── Exit management on 1-min ──────────────────────────────────

def _check_exit(now: datetime, token: int) -> tuple:
    """Evaluate exit rules on the current shadow position.
    Returns (reason or None, exit_price, peak, candles_held)."""
    with _lock:
        if not shadow_state["in_trade"]:
            return None, 0.0, 0.0, 0
        entry = float(shadow_state["entry_price"])
        direction = shadow_state["direction"]

    ltp = D.get_ltp(token) if token else 0
    if ltp <= 0:
        return None, 0.0, shadow_state["peak_pnl"], shadow_state["candles_held"]

    pnl = round(ltp - entry, 2)
    with _lock:
        if pnl > shadow_state["peak_pnl"]:
            shadow_state["peak_pnl"] = pnl
        shadow_state["candles_held"] += 1
        peak = shadow_state["peak_pnl"]
        candles = shadow_state["candles_held"]

    # Rule 1: emergency SL
    if pnl <= CFG.exit_ema9_band("emergency_sl_pts", -20):
        return "EMERGENCY_SL", ltp, peak, candles

    # Rule 2: EOD
    if D.is_market_open():
        eod = CFG.exit_ema9_band("eod_exit_time", "15:30")
        eh, em = eod.split(":")
        if now.hour * 60 + now.minute >= int(eh) * 60 + int(em):
            return "EOD_EXIT", ltp, peak, candles

    # Rule 3: stale
    stale_c = CFG.exit_ema9_band("stale_candles", 5)
    stale_p = CFG.exit_ema9_band("stale_peak_max", 3)
    if candles >= stale_c and peak < stale_p:
        return "STALE_ENTRY", ltp, peak, candles

    # Rule 4: BE+2 lock (same threshold as live — peak >= 10)
    be2_thr = CFG.exit_ema9_band("breakeven_lock_peak_threshold", 10)
    be2_off = CFG.exit_ema9_band("breakeven_lock_offset", 2)
    if peak >= be2_thr:
        lock = round(entry + be2_off, 2)
        if ltp <= lock:
            return "BREAKEVEN_LOCK", lock, peak, candles

    # Rule 5: 1-min EMA9_LOW_BREAK (primary trail)
    try:
        df = D.get_historical_data(token, "minute", 10)
        if df is not None and not df.empty and len(df) >= 2:
            df = _ema9_bands_1min(df)
            last = df.iloc[-2]
            last_close = float(last["close"])
            last_el    = float(last["ema9_low"])
            ts = str(last.name)
            if shadow_state.get("last_band_ts") != ts:
                with _lock:
                    shadow_state["last_band_ts"] = ts
                if last_el > 0 and last_close < last_el:
                    return "EMA9_LOW_BREAK", ltp, peak, candles
    except Exception:
        pass

    return None, ltp, peak, candles


# ── Public tick — called every 1-min from VRL_MAIN ────────────

def tick(kite, spot_ltp: float, atm_strike: int, expiry,
         now: datetime = None):
    """One 1-min cycle: scan + possibly enter, or manage current shadow exit.
    Called from VRL_MAIN on every closed 1-min candle. Completely silent.
    Writes shadow_scans on every call; writes shadow_trades on exit."""
    if now is None:
        now = datetime.now()
    if not atm_strike or expiry is None:
        return

    # Resolve ATM tokens (CE + PE) — same resolver live uses.
    try:
        tokens = D.get_option_tokens(kite, atm_strike, expiry) or {}
    except Exception:
        tokens = {}

    # Path 1: already in a shadow trade → manage exit only.
    if shadow_state.get("in_trade"):
        _manage(tokens, now)
        return

    # Path 2: scan both sides, log both, enter first firer (CE preferred).
    fired_side = None
    for side in ("CE", "PE"):
        info = tokens.get(side)
        if not info:
            continue
        scan = _scan_side(side, int(info["token"]), atm_strike, spot_ltp, now)
        _append_csv(_scans_csv(), FIELDS_SCANS, {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "direction": side,
            "close": scan["close"],
            "ema9_high": scan["ema9_high"],
            "ema9_low": scan["ema9_low"],
            "band_width": scan["band_width"],
            "body_pct": scan["body_pct"],
            "green": int(bool(scan["green"])),
            "straddle_delta": scan["straddle_delta"] if scan["straddle_delta"] is not None else "",
            "fired": int(bool(scan["fired"])),
            "reject_reason": scan["reject_reason"],
        })
        if scan["fired"] and fired_side is None:
            fired_side = (side, scan, int(info["token"]))

    if fired_side:
        side, scan, tok = fired_side
        with _lock:
            shadow_state.update({
                "in_trade": True,
                "direction": side,
                "entry_price": scan["close"],
                "entry_time": now.strftime("%H:%M:%S"),
                "peak_pnl": 0.0,
                "candles_held": 0,
                "entry_ema9_low": scan["ema9_low"],
                "entry_ema9_high": scan["ema9_high"],
                "entry_straddle_delta": scan["straddle_delta"] or 0.0,
                "last_band_ts": "",
                "token": tok,
            })
        logger.info("[SHADOW_1MIN] ENTRY " + side + " @ " + str(scan["close"])
                    + " ema9h=" + str(scan["ema9_high"])
                    + " ema9l=" + str(scan["ema9_low"])
                    + " straddleΔ=" + str(scan["straddle_delta"]))


def _manage(tokens: dict, now: datetime):
    """Exit-side bookkeeping for an open shadow position."""
    direction = shadow_state.get("direction", "")
    tok = shadow_state.get("token") or (tokens.get(direction) or {}).get("token")
    if not tok:
        return
    reason, price, peak, candles = _check_exit(now, int(tok))
    if not reason:
        return

    with _lock:
        entry     = float(shadow_state["entry_price"])
        entry_tm  = shadow_state["entry_time"]
        sd_at_entry = shadow_state.get("entry_straddle_delta", 0.0) or 0.0
    pnl = round(float(price) - entry, 2)

    _append_csv(_trades_csv(), FIELDS_TRADES, {
        "date": date.today().isoformat(),
        "entry_time": entry_tm,
        "exit_time": now.strftime("%H:%M:%S"),
        "direction": direction,
        "entry_price": round(entry, 2),
        "exit_price": round(float(price), 2),
        "pnl_pts": pnl,
        "peak_pnl": round(peak, 2),
        "candles_held": candles,
        "exit_reason": reason,
        "straddle_delta": round(sd_at_entry, 2) if sd_at_entry else "",
    })
    logger.info("[SHADOW_1MIN] EXIT " + direction + " " + reason
                + " pnl=" + str(pnl) + " peak=" + str(round(peak, 1))
                + " candles=" + str(candles))

    # Close + accumulate daily stats + set cooldown
    with _lock:
        shadow_state["trades_today"] += 1
        shadow_state["total_pnl"]    += pnl
        shadow_state["peak_sum"]     += peak
        if pnl > 0:
            shadow_state["wins_today"] += 1
        else:
            shadow_state["losses_today"] += 1
        if peak >= 10:
            shadow_state["peaks_over_10"] += 1
        shadow_state.update({
            "in_trade": False, "direction": "",
            "entry_price": 0.0, "entry_time": "",
            "peak_pnl": 0.0, "candles_held": 0,
            "entry_ema9_low": 0.0, "entry_ema9_high": 0.0,
            "entry_straddle_delta": 0.0, "last_band_ts": "",
            "token": 0,
            "last_exit_time": now.isoformat(),
            "last_exit_direction": direction,
        })


# ── EOD summary (fires ONCE at 15:35 from VRL_MAIN) ──────────

def emit_eod_summary(tg_send_fn, live_stats: dict = None):
    """Render the single EOD Telegram for shadow vs live comparison.
    `tg_send_fn(text)` delivers the Telegram message.
    `live_stats` should contain {trades, wins, pnl, wr} from live state."""
    s = day_summary()
    live = live_stats or {}
    msg = (
        "[SHADOW 1-MIN] Day Summary\n"
        "Trades: " + str(s["trades"]) + " | Wins: " + str(s["wins"])
        + " | Losses: " + str(s["losses"])
        + " | WR: " + str(s["wr"]) + "%\n"
        "Total PNL: " + ("+" if s["pnl"] >= 0 else "") + str(s["pnl"]) + " pts\n"
        "Avg peak: " + str(s["avg_peak"]) + " | Peaks >=10: " + str(s["peaks_over_10"]) + "\n"
        "\n"
        "vs LIVE 3-MIN: " + str(live.get("trades", 0)) + " trades | "
        + str(live.get("wr", 0)) + "% WR | "
        + ("+" if float(live.get("pnl", 0)) >= 0 else "")
        + str(live.get("pnl", 0)) + " pts"
    )
    try:
        tg_send_fn(msg)
    except Exception as e:
        logger.warning("[SHADOW_1MIN] EOD telegram send: " + str(e))
