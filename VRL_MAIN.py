# ═══════════════════════════════════════════════════════════════
#  VRL_MAIN.py — VISHAL RAJPUT TRADE v19 (Vishal Clean V7+V9)
#  V7 (SHADOW): 15-min | 2-gate (close>ema9l, RSI>=40 rising) | signals only
#  V9 (LIVE):   3-min  | 3-gate (close>ema9l, BW 13-16, RSI 48-70)
#  V9 Exit: Emergency -12 | INITIAL(-12) → LOCK_4(@12) → LOCK_12(@24) →
#           LOCK_20(@30) → LOCK_30(@36) → LOCK_36(@40) → LOCK_50(@50+)
# ═══════════════════════════════════════════════════════════════

import csv
import json
import logging
import os
import requests
import signal
import sys
import threading
import time
import numpy as np
from copy import deepcopy
from datetime import date, datetime, timedelta, time as dtime

# ── Bootstrap dirs first ────────────────────────────────────────
import VRL_DATA as D
D.ensure_dirs()

from VRL_DATA   import setup_logger
from VRL_CONFIG import get_kite
from VRL_ENGINE import (
    check_entry, manage_exit, pre_entry_checks,
    compute_entry_sl,
    check_entry_v8,
)
import VRL_CONFIG as CFG

from VRL_LAB    import start_lab
import VRL_LEVELS as LEVELS   # shadow data collection — no live impact
import VRL_ENGINE as CHARGES
import VRL_MSTOCK as MSTOCK   # order execution via MStock (data stays on Kite)

# ── Loggers ─────────────────────────────────────────────────────
logger     = setup_logger("vrl_live", D.LIVE_LOG_FILE)
lab_logger = setup_logger("vrl_lab",  D.LAB_LOG_FILE)
# Wall-clock at module import — used by /pulse to report bot uptime.
_BOT_START_TS = time.time()

# ── Telegram base ───────────────────────────────────────────────
_TG_BASE = "https://api.telegram.org/bot"

# Global kite instance for REST fallback
_kite = None

# ═══════════════════════════════════════════════════════════════
#  STATE (re‑entry fields removed)
# ═══════════════════════════════════════════════════════════════

_state_lock = threading.Lock()

# Post-exit observation queue: tokens kept subscribed for 10 min after
# exit so VRL_LAB can record post-exit price action for analysis.
# Format: [(token, unsubscribe_at_timestamp_epoch), ...]
_post_exit_observation = []
_post_exit_lock = threading.Lock()
POST_EXIT_OBSERVATION_MINUTES = 10

DEFAULT_STATE = {
    # ── Position ───────────────────────────────────────────
    "in_trade"           : False,
    "symbol"             : "",
    "token"              : None,
    "direction"          : "",
    "strike"             : 0,
    "expiry"             : "",
    "entry_price"        : 0.0,
    "entry_time"         : "",
    "qty"                : D.get_lot_size(),
    "lot_count"          : 2,
    # ── Exit state ────────────────────────────────────────
    "peak_pnl"           : 0.0,
    "candles_held"       : 0,
    "force_exit"         : False,
    "_exit_failed"       : False,
    # ── v15.0 entry context (captured at entry, displayed at exit) ──
    "entry_mode"         : "",
    "entry_ema9_high"    : 0.0,
    "entry_ema9_low"     : 0.0,
    "entry_band_position": "",
    "entry_body_pct"     : 0.0,
    "current_ema9_high"  : 0.0,
    "current_ema9_low"   : 0.0,
    "last_band_check_ts" : "",
    "_last_cleanup_date" : "",
    # v16.0 ratchet state
    "active_ratchet_tier": "",
    "active_ratchet_sl"  : 0.0,
    "other_token"        : 0,
    # V7 re-entry watcher — 2-candle window after exit
    "_reentry_armed"     : False,
    "_reentry_exit_ts"   : 0.0,
    "_reentry_attempts"  : 0,         # count of candles checked
    "_reentry_last_checked_epoch": 0.0,
    "_next_candle2_after": 0.0,
    "_reentry_direction" : "",
    "_reentry_token"     : 0,
    "_reentry_strike"    : 0,
    # Same-candle guard: timestamp of last fired candle (str). Engine
    # rejects re-entry when current candle == this, stops the
    # 2026-05-07 same-candle re-fire bug.
    "_last_fired_candle_ts": "",
    # V8 EMERGENCY_SL 1-candle cooldown: set True when SL fires,
    # check_entry_v8 skips the very next candle then clears the flag.
    "_sl_cooldown_skip_next": False,
    "_force_exit_ts"        : 0.0,
    # ── Last exit memory (cooldown) ────────────────────────
    "last_exit_time"     : "",
    "last_exit_direction": "",
    "last_exit_peak"     : 0.0,
    "last_exit_reason"   : "",
    # ── Daily counters ─────────────────────────────────────
    "daily_pnl"          : 0.0,
    # ── Bot control ────────────────────────────────────────
    "paused"             : False,
    # ── Daily reset flags ──────────────────────────────────
    "_eod_reported"      : False,
    "_eod_exited"        : False,
    "_bias_done"         : False,
    "_straddle_done"     : False,
    "_hourly_rsi_ts"     : 0,
    "_straddle_alerted"  : False,
    # ── Loop bookkeeping ───────────────────────────────────
    "_last_1min_candle"  : "",
    "_last_dash_scan_min": "",
    "_last_warmup_log"   : "",
    "_last_scan"         : {},
    "prev_close"         : 0.0,
    # ── Exchange order tracking (live mode — legacy compat) ──
    "_sl_order_id"       : "",
    "_sl_trigger_at_exchange": 0,
    "lot1_active"        : True,  # legacy (always True in v15.0)
    "lot2_active"        : True,  # legacy (always True in v15.0)
    "lots_split"         : False, # legacy (always False in v15.0)
    "current_floor"      : 0.0,   # legacy (used for dashboard trail display)
    "_candle_low"        : 0.0,   # legacy
    "_last_milestone"    : 0,     # legacy
    "_static_floor_sl"   : 0.0,   # legacy
}

state   = deepcopy(DEFAULT_STATE)
_running = True

_v8_state = {
    "_last_fired_candle_ts": "",     # same-candle guard
    "_signals_today": 0,             # count for /pulse
    "_last_signal_time": "",
    # Paper position state (parallel to V7, independent).
    "in_trade": False,
    "symbol": "",
    "token": 0,
    "direction": "",
    "strike": 0,
    "entry_price": 0.0,
    "entry_time": "",
    "qty": 0,
    "peak_pnl": 0.0,
    "active_ratchet_tier": "",
    "active_ratchet_sl": 0.0,
    "candles_held": 0,
    "_last_minute": "",
    "_other_token": 0,          # other leg's token — needed for re-entry after restart
    "_reentry_exit_price": 0.0, # exit price of last trade — re-entry anti-chase gate
    # Re-entry watcher (cross-leg continuation, 2-candle window)
    "_reentry_armed": False,
    "_reentry_attempts": 0,
    "_reentry_last_checked_epoch": 0.0,
    "_reentry_direction": "",
    "_reentry_token": 0,
    "_reentry_strike": 0,
    "_reentry_other_token": 0,
    # Daily cumulative
    "_pnl_today_pts": 0.0,
    "_trades_today": 0,
    "_wins_today": 0,
    "_losses_today": 0,
    # 1-candle cooldown after EMERGENCY_SL (owned here, not in V7 state)
    "_sl_cooldown_skip_next": False,
    "_force_exit_ts"        : 0.0,
    # Exit candle guard: block re-entry on same 3-min candle we just exited from
    "_last_exit_candle_ts"  : "",
    # Both-sides rejection cooldown: unix timestamp of last scan where both CE+PE failed
    "_v8_both_rejected_ts": 0.0,
    # Date of last trade — used to detect new day and reset daily counters on restart
    "_last_trade_date": "",
    # Current expiry / DTE — synced from main loop every iteration so entry/exit always sees correct value
    "expiry": "",
    "dte": 0,
    # EMERGENCY_SL direction cooldown — only blocks the side that triggered the SL
    "_sl_cooldown_direction": "",
}
_v8_lock = threading.Lock()


def _v8_compute_trail_sl(entry_price: float, peak_pnl: float) -> tuple:
    """V8 SL ladder (3-min): LOCK_4 at +12, LOCK_10 at +18, then custom tiers up to LOCK_50."""
    if peak_pnl < 12:
        return round(entry_price - 12, 2), "INITIAL"
    if peak_pnl >= 50:
        lock, tier = 50, "LOCK_50"
    elif peak_pnl >= 40:
        lock, tier = 36, "LOCK_36"
    elif peak_pnl >= 36:
        lock, tier = 30, "LOCK_30"
    elif peak_pnl >= 30:
        lock, tier = 20, "LOCK_20"
    elif peak_pnl >= 24:
        lock, tier = 12, "LOCK_12"
    elif peak_pnl >= 18:
        lock, tier = 10, "LOCK_10"
    else:
        lock, tier = 4, "LOCK_4"
    return round(entry_price + lock, 2), tier


def _v8_execute_paper_entry(direction: str, strike: int, symbol: str, token: int,
                             entry_price: float, entry_result: dict,
                             other_token: int = 0):
    """Open a V8 paper position. Records in _v8_state, sends Telegram alert."""
    import VRL_CONFIG as CFG
    lot_count = CFG.get().get("lots", {}).get("count", 2)
    qty = lot_count * D.get_lot_size()
    now_dt  = datetime.now()
    now_str = now_dt.strftime("%H:%M:%S")
    is_reentry = (entry_result.get("entry_mode") == "REENTRY_XLEG")

    # ── Shadow level data collection (observation only, no blocking) ──
    try:
        _spot_px = D.get_ltp(D.NIFTY_SPOT_TOKEN) or 0
        _dte = int(_v8_state.get("dte", 0) or 0)
        if _dte == 0:
            # Fallback: compute from expiry date if available
            try:
                _exp = _v8_state.get("expiry")
                if _exp:
                    _exp_dt = datetime.strptime(str(_exp), "%Y-%m-%d").date()
                    _dte = max(0, (_exp_dt - date.today()).days)
            except Exception:
                pass
        _opt_pdc = 0.0
        try:
            _opt_lvl = LEVELS.compute_opt_pdc(D, int(strike or 0), direction, int(token or 0))
            _opt_pdc = float(_opt_lvl.get("opt_PDC", 0))
        except Exception:
            pass
        LEVELS.log_entry(
            direction=direction, strike=int(strike or 0),
            entry_price=float(entry_price), spot_px=float(_spot_px),
            entry_time_dt=now_dt, dte=_dte, opt_pdc=_opt_pdc,
        )
    except Exception as _lvl_e:
        logger.debug(f"[SHADOW-LVL] hook error: {_lvl_e}")

    with _v8_lock:
        if _v8_state.get("in_trade"):
            logger.warning("[V8] Entry attempted while already in_trade — BLOCKED (duplicate guard)")
            return
        _v8_state["in_trade"]              = True
        _v8_state["symbol"]                = symbol
        _v8_state["token"]                 = token
        _v8_state["direction"]             = direction
        _v8_state["strike"]                = int(strike or 0)
        _v8_state["entry_price"]           = float(entry_price)
        _v8_state["entry_time"]            = now_str
        _v8_state["qty"]                   = qty
        _v8_state["peak_pnl"]              = 0.0
        _v8_state["active_ratchet_tier"]   = "INITIAL"
        _v8_state["active_ratchet_sl"]     = round(entry_price - 12, 2)
        _v8_state["candles_held"]          = 0
        _v8_state["_last_fired_candle_ts"] = entry_result.get("fired_candle_ts", "")
        _v8_state["_other_token"]          = int(other_token or 0)
        # Clear any pending re-entry state (fresh setup wins)
        _v8_state["_reentry_armed"]        = False
        _v8_state["_reentry_attempts"]     = 0

    logger.info("[V8] PAPER ENTRY: " + symbol + " qty=" + str(qty)
                + " entry=" + str(entry_price) + " mode="
                + str(entry_result.get("entry_mode", "")))

    _ce_pe = "🟢" if direction == "CE" else "🔴"
    _mode_tag = "REENTRY (X-LEG)" if is_reentry else "FRESH"
    _xleg_line = ""
    if entry_result.get("xleg_other_dying"):
        _xleg_line = (
            "X-Leg ✓ " + ("PE" if direction=='CE' else 'CE') + " dying ("
            + "{:.1f}".format(entry_result.get("xleg_other_close", 0))
            + " < ema9l "
            + "{:.1f}".format(entry_result.get("xleg_other_ema9l", 0)) + ")\n"
        )
    _g6 = entry_result.get("g6_stochrsi_os_cross")
    _g6_line = ("G6  StochRSI_OsCross(5) "
                + ("✅ PASS" if _g6 else ("❌ SKIP" if _g6 is False else "—"))
                + (" k=" + str(entry_result.get("g6_k_now", "")) if _g6 is not None else "")
                + " [shadow]\n")
    _tg_send(
        "⚡ <b>V8 ENTRY " + _mode_tag + "</b>\n"
        + _ce_pe + " " + direction + " " + str(strike) + " x " + str(lot_count) + " LOTS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Entry  Rs" + "{:.2f}".format(entry_price) + "  @ " + now_str + " (3-min)\n"
        "Close  " + "{:.1f}".format(entry_result.get("close", 0))
        + " > EMA9L " + "{:.1f}".format(entry_result.get("ema9_low", 0)) + "\n"
        "Body   " + str(int(entry_result.get("body_pct", 0))) + "% GREEN\n"
        + _xleg_line + _g6_line +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>STOP</b>\n"
        "Hard SL  -12 pts (Rs" + "{:.1f}".format(entry_price - 12) + ")\n"
        "Trail: peak ≥12→+4 | ≥18→+10 | ≥24→+12 | ≥30→+20 | ≥36→+30 | ≥40→+36 | ≥50→+50\n",
        priority="critical"
    )
    _save_v8_state()


def _v8_execute_paper_exit(reason: str, exit_price: float):
    """Close V8 paper position. Logs trade to CSV, arms re-entry watcher."""
    with _v8_lock:
        if not _v8_state.get("in_trade"):
            return
        # Read all values FIRST (before clearing), then mark closed immediately.
        # Any concurrent call (TG thread vs main loop) now sees in_trade=False and returns —
        # eliminating the duplicate-exit race condition.
        entry_price = float(_v8_state.get("entry_price", 0))
        symbol      = _v8_state.get("symbol", "")
        direction   = _v8_state.get("direction", "")
        strike      = int(_v8_state.get("strike", 0) or 0)
        qty         = int(_v8_state.get("qty", 0) or 0)
        peak        = float(_v8_state.get("peak_pnl", 0))
        entry_time  = _v8_state.get("entry_time", "")
        candles     = int(_v8_state.get("candles_held", 0) or 0)
        tier        = _v8_state.get("active_ratchet_tier", "")
        token       = int(_v8_state.get("token", 0) or 0)
        other_tok   = int(_v8_state.get("_other_token", 0) or 0)
        dte_val     = int(_v8_state.get("dte", 0) or 0)
        pnl_pts_now = round(exit_price - entry_price, 2)
        # Clear position state
        _v8_state["in_trade"]            = False
        _v8_state["symbol"]              = ""
        _v8_state["token"]               = 0
        _v8_state["direction"]           = ""
        _v8_state["strike"]              = 0
        _v8_state["entry_price"]         = 0.0
        _v8_state["peak_pnl"]            = 0.0
        _v8_state["active_ratchet_tier"] = ""
        _v8_state["active_ratchet_sl"]   = 0.0
        _v8_state["candles_held"]        = 0
        # Update daily counters and arm re-entry under the same lock
        _v8_state["_pnl_today_pts"] = round(_v8_state.get("_pnl_today_pts", 0) + pnl_pts_now, 2)
        _v8_state["_trades_today"]  = _v8_state.get("_trades_today", 0) + 1
        if pnl_pts_now > 0:
            _v8_state["_wins_today"]   = _v8_state.get("_wins_today", 0) + 1
        elif pnl_pts_now < 0:
            _v8_state["_losses_today"] = _v8_state.get("_losses_today", 0) + 1
        if reason == "EMERGENCY_SL":
            _v8_state["_sl_cooldown_skip_next"] = True
            _v8_state["_sl_cooldown_direction"] = direction   # block SAME side only
        _v8_state["_reentry_armed"]              = False  # disabled — fresh setup only
        _v8_state["_reentry_attempts"]           = 0
        _v8_state["_reentry_last_checked_epoch"] = 0.0
        _v8_state["_reentry_direction"]          = direction
        _v8_state["_reentry_token"]              = token
        _v8_state["_reentry_strike"]             = strike
        _v8_state["_reentry_other_token"]        = other_tok
        _v8_state["_reentry_exit_price"]         = round(exit_price, 2)
        _v8_state["_last_trade_date"]            = date.today().isoformat()
        # Exit candle guard: record the 3-min bucket we're exiting in
        _now_exit = datetime.now()
        _exit_bucket_min = (_now_exit.minute // 3) * 3
        _v8_state["_last_exit_candle_ts"] = str(
            _now_exit.replace(minute=_exit_bucket_min, second=0, microsecond=0)
        )

    # --- Lock released: safe to read captured locals for logging ---
    pnl_pts = round(exit_price - entry_price, 2)
    pnl_rs  = round(pnl_pts * qty, 2)
    exit_time = datetime.now().strftime("%H:%M:%S")

    # Charges (reuse engine's calc)
    charges = {}
    try:
        from VRL_ENGINE import calculate_charges
        charges = calculate_charges(entry_price, exit_price, qty, num_exit_orders=1)
        net_pnl = charges["net_pnl"]
        total_charges = charges["total_charges"]
    except Exception:
        net_pnl = pnl_rs
        total_charges = 0.0

    # Log to CSV + DB (entry_mode tagged V8 so we can split V7/V8 in analysis)
    try:
        _v8_row = {
            "date": date.today().isoformat(),
            "entry_time": entry_time, "exit_time": exit_time,
            "symbol": symbol, "direction": direction, "strike": strike,
            "entry_price": entry_price, "exit_price": exit_price,
            "pnl_pts": pnl_pts, "pnl_rs": pnl_rs,
            "gross_pnl_rs": pnl_rs, "net_pnl_rs": net_pnl,
            "peak_pnl": peak, "exit_reason": reason,
            "dte": dte_val, "candles_held": candles, "session": "",
            "sl_pts": -12, "vix_at_entry": 0,
            "entry_mode": "V8_" + tier,
            "bias": "", "hourly_rsi": 0,
            "brokerage": charges.get("brokerage", 0) if isinstance(charges, dict) else 0,
            "stt": charges.get("stt", 0) if isinstance(charges, dict) else 0,
            "exchange_charges": charges.get("exchange", 0) if isinstance(charges, dict) else 0,
            "gst": charges.get("gst", 0) if isinstance(charges, dict) else 0,
            "stamp_duty": charges.get("stamp", 0) if isinstance(charges, dict) else 0,
            "total_charges": total_charges, "num_exit_orders": 1,
            "qty_exited": qty, "entry_slippage": 0, "exit_slippage": 0,
            "lot_id": "ALL",
            "entry_ema9_high": "", "entry_ema9_low": "",
            "exit_ema9_high": "", "exit_ema9_low": "",
            "entry_band_position": "", "exit_band_position": "",
            "entry_body_pct": "",
            "xleg_signal": "", "xleg_other_close": "", "xleg_other_ema9l": "",
            "xleg_other_dying": "", "xleg_other_margin": "",
            "spike_close": "", "spike_target": "", "spike_fill": "", "spike_wait_used": "",
        }
        import csv as _csv
        log_path = D.TRADE_LOG_PATH
        with open(log_path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES, extrasaction="ignore")
            w.writerow(_v8_row)
        import VRL_DB as _VDB
        _VDB.insert_trade(_v8_row)
    except Exception as _le:
        logger.warning("[V8] Trade log write error: " + str(_le))

    logger.info("[V8] PAPER EXIT: " + symbol + " qty=" + str(qty)
                + " ref=" + str(exit_price) + " reason=" + reason
                + " pnl=" + str(pnl_pts) + "pts")

    _emoji = "🟢" if pnl_pts >= 0 else "🔴"
    _tg_send(
        "⚡ <b>V8 EXIT " + direction + " " + str(strike) + "</b>\n"
        + reason + "    " + ("+" if pnl_pts >= 0 else "") + "{:.1f}".format(pnl_pts) + " pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Entry  Rs" + "{:.1f}".format(entry_price) + "\n"
        "Exit   Rs" + "{:.1f}".format(exit_price) + "\n"
        "Peak   +" + "{:.1f}".format(peak) + " pts  Trail " + str(tier) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Gross  " + ("+" if pnl_rs >= 0 else "") + "Rs" + "{:.0f}".format(pnl_rs) + "\n"
        "Charges -Rs" + "{:.0f}".format(total_charges) + "\n"
        "Net    " + ("+" if net_pnl >= 0 else "") + "Rs" + "{:.0f}".format(net_pnl) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "V8 DAY " + ("+" if _v8_state.get("_pnl_today_pts", 0) >= 0 else "")
        + "{:.1f}".format(_v8_state.get("_pnl_today_pts", 0)) + " pts ("
        + str(_v8_state.get("_wins_today", 0)) + "W "
        + str(_v8_state.get("_losses_today", 0)) + "L)",
        priority="critical"
    )
    _save_v8_state()


def _v8_check_exit():
    """Tick-based exit check for V8 position. Called every scan cycle."""
    with _v8_lock:
        if not _v8_state.get("in_trade"):
            return
        token       = int(_v8_state.get("token", 0) or 0)
        entry_price = float(_v8_state.get("entry_price", 0))
        peak        = float(_v8_state.get("peak_pnl", 0))

    if not token: return
    ltp = D.get_ltp(token)
    if ltp <= 0: return
    pnl = round(ltp - entry_price, 2)

    # Update peak
    if pnl > peak:
        with _v8_lock:
            _v8_state["peak_pnl"] = pnl
        peak = pnl

    # Compute trail SL using V7 ladder (12-step + 30/40/50)
    trail_sl, trail_tier = _v8_compute_trail_sl(entry_price, peak)
    with _v8_lock:
        prev_tier = _v8_state.get("active_ratchet_tier", "")
        _v8_state["active_ratchet_tier"] = trail_tier
        _v8_state["active_ratchet_sl"]   = trail_sl

    # Tier upgrade alert (matches V7 style)
    if prev_tier and prev_tier != trail_tier and trail_tier != "INITIAL":
        _tg_send(
            "⚡ <b>V8 SL UPGRADED → " + trail_tier + "</b>\n"
            "Peak +{:.1f}".format(peak) + " pts\n"
            "Prev " + str(prev_tier) + "  →  New " + trail_tier
            + "  SL Rs" + "{:.1f}".format(trail_sl),
            priority="critical"
        )
        _save_v8_state()

    # Emergency SL (-12) — TICK based
    if pnl <= -12:
        _v8_execute_paper_exit("EMERGENCY_SL", round(entry_price - 12, 2))
        return

    # Trail SL — TICK based for locked tiers (peak ≥ 12)
    if trail_tier != "INITIAL" and ltp <= trail_sl:
        _v8_execute_paper_exit("VISHAL_TRAIL", float(trail_sl))
        return

    # EOD exit
    eod_str = CFG.exit_ema9_band("eod_exit_time", "15:20") if hasattr(CFG, "exit_ema9_band") else "15:20"
    try:
        _eh, _em = eod_str.split(":")
        eod_mins = int(_eh) * 60 + int(_em)
    except Exception:
        eod_mins = 15 * 60 + 20
    now_mins = datetime.now().hour * 60 + datetime.now().minute
    if now_mins >= eod_mins:
        _v8_execute_paper_exit("EOD_EXIT", float(ltp))

# ═══════════════════════════════════════════════════════════════
#  STRIKE LOCKING — stable scanning, no flickering
# ═══════════════════════════════════════════════════════════════

_locked_ce_strike = None
_locked_pe_strike = None
_locked_at_spot   = None
_locked_tokens    = {}
_LOCK_SHIFT_THRESHOLD = 150  # relock if spot moves 150+ pts
_last_dash_args = {}  # cached dashboard args for post-exit refresh
_v8_last_entry_scan_ts = 0.0  # throttle V8 entry scan to every 3s
_v9_last_results: dict = {"CE": None, "PE": None}  # last V9 gate results for dashboard
spot_3m: dict = {}  # BUG-B fix: module-level cache; updated by _write_dashboard() each call
# Shadow: dual-TF early entry tracking (1 week data collection before going live)
_v8_shadow_dt = {
    "active": False,       # CE shadow signal active
    "direction": "",       # CE or PE (kept for live_entry comparison)
    "bucket_ts": "",       # completed 3-min candle timestamp
    "entry_price": 0.0,    "entry_time": "",
    "peak_price": 0.0,     "peak_pts": 0.0,
    "live_entry": 0.0,
    "last_scan_ts": 0.0,
    # per-direction tracking
    "relock_ts": 0.0,   # unix ts of last ATM relock — blocks P1 signals 2 min
    "CE": {"active": False, "bucket_ts": "", "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "live_entry": 0.0,
           "shadow_sl": 0.0, "shadow_level": "INITIAL",
           "today_entry": 0.0, "today_date": "",
           "entry_tok": 0, "entry_strike": 0,
           "sl_ts": 0.0,  # unix ts of last SL-HIT — blocks re-entry 1 min
           "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0},
    "PE": {"active": False, "bucket_ts": "", "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "live_entry": 0.0,
           "shadow_sl": 0.0, "shadow_level": "INITIAL",
           "today_entry": 0.0, "today_date": "",
           "entry_tok": 0, "entry_strike": 0,
           "sl_ts": 0.0,  # unix ts of last SL-HIT — blocks re-entry 1 min
           "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0},
}

# Shadow Part 2 — buildup tracker (close > EMA9H, close < VWAP, RSI > 55 rising)
_v8_shadow_p2 = {
    "last_scan_ts": 0.0,
    "CE": {"active": False, "bucket_ts": "", "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0,
           "shadow_sl": 0.0, "shadow_level": "INITIAL",
           "today_entry": 0.0, "today_date": "",
           "p1_entry": 0.0, "entry_tok": 0, "entry_strike": 0,
           "exit_ts": 0.0,
           "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0},
    "PE": {"active": False, "bucket_ts": "", "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0,
           "shadow_sl": 0.0, "shadow_level": "INITIAL",
           "today_entry": 0.0, "today_date": "",
           "p1_entry": 0.0, "entry_tok": 0, "entry_strike": 0,
           "exit_ts": 0.0,
           "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0},
}

# ── V2 shadow trackers (A/B test: same entry, new exit logic) ──
# P1-V2: dynamic trail every 5s (LTP-8) after peak≥15, hard exit at +40
# P2-V2: same ratchet ladder as P1-V2 (peak<15 standard, peak≥15 entry+15 then +1/5s), hard exit +40
_v8_shadow_dt_v2 = {
    "CE": {"active": False, "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0, "entry_tok": 0,
           "dyn_trail_ts": 0.0},
    "PE": {"active": False, "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0, "entry_tok": 0,
           "dyn_trail_ts": 0.0},
}
_v8_shadow_p2_v2 = {
    "CE": {"active": False, "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0, "entry_tok": 0,
           "dyn_trail_ts": 0.0},
    "PE": {"active": False, "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0, "entry_tok": 0,
           "dyn_trail_ts": 0.0},
}

_bw_scan_last_bucket: str = ""  # BW-SCAN: tracks last logged 1-min bucket

# DELAY-ANALYSIS: snapshot LTP at +5s/+10s/+30s/+60s after each P1/P2 signal
# Pure research — shadow only, no TG, no trade impact
_delay_jobs: list = []   # list of pending snapshot dicts

def _shadow_trail_sl(entry: float, peak_pts: float):
    """Return (sl_price, level_name) for shadow signal trail ladder."""
    if   peak_pts >= 50: return round(entry + 50, 1), "LOCK+50"
    elif peak_pts >= 40: return round(entry + 36, 1), "LOCK+36"
    elif peak_pts >= 36: return round(entry + 30, 1), "LOCK+30"
    elif peak_pts >= 30: return round(entry + 20, 1), "LOCK+20"
    elif peak_pts >= 24: return round(entry + 12, 1), "LOCK+12"
    elif peak_pts >= 18: return round(entry + 10, 1), "LOCK+10"
    elif peak_pts >= 12: return round(entry +  4, 1), "LOCK+4"
    else:                return round(entry - 12, 1), "INITIAL"

# ── Shadow Analysis Tracker (pure logging, zero trade impact) ──
# Tracks last 2 peak_pts per direction for P1 and P2 to detect dead-market streaks
_shadow_analysis = {
    "CE": {"last_peaks": [], "last_peaks_p2": [], "cross_buf": []},
    "PE": {"last_peaks": [], "last_peaks_p2": [], "cross_buf": []},
}

def _log_shadow_analysis(signal_label, direction, fire_time, entry_price,
                         vwap_gap, other_vwap_gap, spot_adx, last_peaks,
                         ema9h_gap=0.0, xleg_buf=None, dte=0,
                         fut_vwap_gap=0.0, spot_ema9=0.0, spot_ema21=0.0, bw=0.0):
    """Log all analysis flags at signal fire — no trade impact."""
    flags = []

    # 1. Time blackout 13:00–14:15
    _h, _m = fire_time.hour, fire_time.minute
    if (_h == 13) or (_h == 14 and _m < 15):
        flags.append(f"DEAD_WINDOW({fire_time.strftime('%H:%M')})")

    # 2. ADX weak trend
    if 0 < spot_adx < 18:
        flags.append(f"WEAK_ADX({spot_adx})")

    # 3. Last 2 peaks both < 5 pts
    if len(last_peaks) >= 2 and all(p < 5 for p in last_peaks[-2:]):
        flags.append(f"LOW_PEAK_STREAK(last2={last_peaks[-2:]}")

    # 4. VWAP gap compression — both sides < 10 pts
    if vwap_gap is not None and other_vwap_gap is not None:
        if abs(vwap_gap) < 10 and abs(other_vwap_gap) < 10:
            flags.append(f"VWAP_COMPRESSED(self={vwap_gap:.1f} other={other_vwap_gap:.1f})")

    # 5. EMA9H gap bounds
    if ema9h_gap > 5:
        flags.append(f"EXTENDED_GAP({ema9h_gap:.2f})")
    elif 0 < ema9h_gap < 0.5:
        flags.append(f"TINY_GAP({ema9h_gap:.2f})")

    # 6. Cross-leg confirmation via rolling buffer (last 5 P2 scans of opposite side)
    _xleg_note = ""
    if xleg_buf is not None and len(xleg_buf) >= 3:
        _buf = xleg_buf[-5:]
        _rejected = sum(1 for v in _buf if not v)
        _total = len(_buf)
        _other = "PE" if direction == "CE" else "CE"
        if _rejected == _total:
            _xleg_note = f"XLEG_CONFIRMED({_other} all{_total} below_ema9h)"
        elif _rejected < _total // 2:
            flags.append(f"XLEG_AMBIGUOUS({_other} only {_rejected}/{_total} below_ema9h)")

    # 7. Futures VWAP bias vs signal direction (data collection — no trade impact)
    if fut_vwap_gap != 0.0:
        _fv_bias = "BULL" if fut_vwap_gap > 0 else "BEAR"
        if (direction == "CE" and fut_vwap_gap < -15) or (direction == "PE" and fut_vwap_gap > 15):
            flags.append(f"FUT_VWAP_MISMATCH({_fv_bias} gap={fut_vwap_gap:+.0f})")

    # 8. Spot EMA9 vs EMA21 alignment — always shown as context tag
    _ema_note = ""
    if spot_ema9 > 0 and spot_ema21 > 0:
        _ema_align = "BULL" if spot_ema9 > spot_ema21 else "BEAR"
        _ema_note = f"SPOT_EMA_{_ema_align}(ema9={spot_ema9:.0f} ema21={spot_ema21:.0f})"

    # ── EXCELLENT SCORE (0-100, LOG-ONLY — zero trade impact) ─────────────
    # Composite of the confirmed winning DNA. This is a HYPOTHESIS under test,
    # not a verdict: OI-wall proximity (~10 pts of edge) is NOT yet wired, so
    # false positives during the open chop are EXPECTED — measuring them is the
    # whole point of this shadow phase. Grades: A+>=80 A>=65 B>=50 C<50.
    _es = 0
    _es_parts = []
    _aligned = False
    if spot_ema9 > 0 and spot_ema21 > 0:
        _aligned = ((direction == "CE" and spot_ema9 > spot_ema21) or
                    (direction == "PE" and spot_ema9 < spot_ema21))
    if _aligned:
        _es += 28; _es_parts.append("trend")
    if 0 < bw <= 6:
        _es += 17; _es_parts.append("bw")
    if 0.8 <= ema9h_gap <= 2.5:
        _es += 17; _es_parts.append("gap+")
    elif 2.5 < ema9h_gap <= 5.0:
        _es += 10; _es_parts.append("gap")
    elif ema9h_gap > 5.0 and _aligned:
        _es += 5;  _es_parts.append("gapX")
    elif 0 < ema9h_gap < 0.8:
        _es += 8;  _es_parts.append("gapT")
    if "XLEG_CONFIRMED" in _xleg_note:
        _es += 18; _es_parts.append("xleg")
    _trend_est = (fire_time.hour * 60 + fire_time.minute) >= 630   # past 10:30 open chop
    _adx_ok = spot_adx >= 18
    if _trend_est and _adx_ok:
        _es += 10; _es_parts.append("trendOK")
    elif _trend_est or _adx_ok:
        _es += 5
    if (direction == "PE" and fut_vwap_gap < 0) or (direction == "CE" and fut_vwap_gap > 0):
        _es += 10; _es_parts.append("fut")
    _grade = "A+" if _es >= 80 else ("A" if _es >= 65 else ("B" if _es >= 50 else "C"))
    _es_tag = f"EXCELLENT={_es}({_grade})[{'+'.join(_es_parts)}]"

    _dte_tag = f"DTE={dte}"
    if flags:
        logger.info(f"[ANALYSIS] {signal_label} {direction} entry={entry_price:.1f} {_dte_tag} — "
                    f"FLAGS: {' | '.join(flags)}"
                    + (f" | {_xleg_note}" if _xleg_note else "")
                    + (f" | {_ema_note}" if _ema_note else "")
                    + f" | {_es_tag}")
    else:
        logger.info(f"[ANALYSIS] {signal_label} {direction} entry={entry_price:.1f} {_dte_tag} — "
                    f"clean (no flags)"
                    + (f" | {_xleg_note}" if _xleg_note else "")
                    + (f" | {_ema_note}" if _ema_note else "")
                    + f" | {_es_tag}")


def _lock_strikes(spot, dte, kite=None, expiry=None):
    """Lock ATM strikes and subscribe tokens.
    v16.7 final: ATM CE+PE for trading + ATM±50 CE+PE for pre-warm.
    Pre-warmed neighbors mean zero indicator-warmup gap when spot
    drifts past hysteresis buffer and relock fire.
    Multi-candidate scan (when enabled) uses the same neighbor tokens.
    """
    global _locked_ce_strike, _locked_pe_strike, _locked_at_spot, _locked_tokens
    _locked_ce_strike = D.resolve_strike_for_direction(spot, "CE", dte)
    _locked_pe_strike = D.resolve_strike_for_direction(spot, "PE", dte)
    _locked_at_spot = spot
    _locked_tokens = {}

    if kite and expiry:
        # Active legs (ATM)
        for _dt, _strike in [("CE", _locked_ce_strike), ("PE", _locked_pe_strike)]:
            _tk = D.get_option_tokens(kite, _strike, expiry)
            if _tk.get(_dt):
                _locked_tokens[_dt] = _tk[_dt]
                _locked_tokens[_dt]["strike"] = _strike  # ensure strike survives into V8 entry display

        # Pre-warm neighbors — ATM±50 CE+PE (always, regardless of multi flag)
        # Keys: CE_UP / CE_DN / PE_UP / PE_DN
        for _suffix, _delta in (("UP", +50), ("DN", -50)):
            _ce_n_strike = _locked_ce_strike + _delta
            _pe_n_strike = _locked_pe_strike + _delta
            _ce_n_tk = D.get_option_tokens(kite, _ce_n_strike, expiry)
            if _ce_n_tk.get("CE"):
                _locked_tokens["CE_" + _suffix] = _ce_n_tk["CE"]
            _pe_n_tk = D.get_option_tokens(kite, _pe_n_strike, expiry)
            if _pe_n_tk.get("PE"):
                _locked_tokens["PE_" + _suffix] = _pe_n_tk["PE"]

        _sub_tokens = [v["token"] for v in _locked_tokens.values() if v.get("token")]
        if _sub_tokens:
            D.subscribe_tokens(_sub_tokens)

    logger.info("[MAIN] Strikes LOCKED: ATM=" + str(_locked_ce_strike)
                + " (neighbors " + str(_locked_ce_strike - 50)
                + "/" + str(_locked_ce_strike + 50)
                + " pre-warmed) at spot=" + str(round(spot, 1)))
    if kite and expiry and _locked_ce_strike:
        try:
            _r11 = D.ensure_option_history(
                kite, _locked_ce_strike, expiry,
                min_candles=30, timeframes=("3minute",))
            if _r11.get("fetched"):
                logger.info("[PRELOAD] Strike lock " + str(_locked_ce_strike)
                            + " CE=" + str(_r11["ce_candles"])
                            + " PE=" + str(_r11["pe_candles"]))
        except Exception as _r11e:
            logger.debug("[PRELOAD] strike lock error: " + str(_r11e))

def _reset_strike_lock():
    """Reset lock after trade exit or session start.
    Unsubscribes every currently-locked token first so the WebSocket
    doesn't leak stale CE/PE/OTM subscriptions across relocks. Without
    this, a full trading day of ~20 relocks leaves 60+ dead tokens
    pinned against the Kite quota.

    EXCEPTION: tokens currently in the post-exit observation queue,
    the active trade's own token, and the active trade's other_token
    are skipped — those need to stay subscribed so exit monitoring
    and cross‑leg checks keep working."""
    global _locked_ce_strike, _locked_pe_strike, _locked_at_spot, _locked_tokens
    try:
        # Collect tokens currently being held for post-exit observation
        with _post_exit_lock:
            _post_exit_tokens = {tok for tok, _ in _post_exit_observation}
        _old = [v.get("token") for v in (_locked_tokens or {}).values()
                if isinstance(v, dict) and v.get("token")]
        # ── PATCH: also keep tokens of the currently open trade alive ──
        # Without this, a mid‑trade strike relock would unsubscribe the
        # opposite leg and break cross‑leg divergence monitoring.
        with _state_lock:
            _trade_tok = int(state.get("token", 0) or 0)
            _other_tok = int(state.get("other_token", 0) or 0)
        _keep = _post_exit_tokens | {_trade_tok, _other_tok} - {0}
        _to_drop = [t for t in _old if int(t) not in _keep]
        if _to_drop:
            D.unsubscribe_tokens(_to_drop)
    except Exception as _ue:
        logger.debug("[MAIN] reset_strike_lock unsubscribe: " + str(_ue))
    _locked_ce_strike = None
    _locked_pe_strike = None
    _locked_at_spot = None
    _locked_tokens = {}

# ═══════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ═══════════════════════════════════════════════════════════════

def _save_state():
    try:
        with _state_lock:
            subset = {k: state.get(k) for k in D.STATE_PERSIST_FIELDS}
        tmp    = D.STATE_FILE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(subset, f, indent=2, default=str)
        os.replace(tmp, D.STATE_FILE_PATH)
    except Exception as e:
        logger.error("[MAIN] State save error: " + str(e))

def _load_state():
    if not os.path.isfile(D.STATE_FILE_PATH):
        return
    try:
        with open(D.STATE_FILE_PATH) as f:
            saved = json.load(f)
        with _state_lock:
            for k, v in saved.items():
                if k in state:
                    state[k] = v
        logger.info("[MAIN] State loaded from disk")
        if state.get("in_trade"):
            logger.info("[MAIN] ⚠ Was in trade on last shutdown — "
                        + str(state.get("symbol")) + " monitoring resumed")
            _tg_send(
                "🔄 <b>Bot restarted mid-trade</b>\n"
                "Symbol : " + str(state.get("symbol")) + "\n"
                "Resuming exit monitoring."
            )
            # Refresh band context immediately so the dashboard doesn't
            # show zeroed current_ema9_high/low until the next manage_exit
            # tick. Best-effort — if the fetch fails we keep the persisted
            # values and the next 3-min candle will overwrite them.
            try:
                _rt_tok = state.get("token")
                if _rt_tok:
                    _rt_df = D.get_option_3min(_rt_tok, lookback=10)
                    if _rt_df is not None and len(_rt_df) >= 2:
                        _rt_last = _rt_df.iloc[-2]
                        with _state_lock:
                            state["current_ema9_high"] = round(
                                float(_rt_last.get("ema9_high", 0)), 2)
                            state["current_ema9_low"] = round(
                                float(_rt_last.get("ema9_low", 0)), 2)
                            state["last_band_check_ts"] = datetime.now().isoformat()
            except Exception as _rte:
                logger.debug("[MAIN] restart band refresh: " + str(_rte))
    except Exception as e:
        logger.error("[MAIN] State load error: " + str(e))


_V8_PERSIST_FIELDS = [
    "in_trade", "symbol", "token", "direction", "strike",
    "entry_price", "entry_time", "qty",
    "peak_pnl", "active_ratchet_tier", "active_ratchet_sl",
    "candles_held", "_other_token",
    "_sl_cooldown_skip_next", "_force_exit_ts",
    "_pnl_today_pts", "_trades_today", "_wins_today", "_losses_today",
    "_v8_both_rejected_ts", "_last_trade_date", "_last_exit_candle_ts",
]

def _save_v8_state():
    try:
        with _v8_lock:
            subset = {k: _v8_state.get(k) for k in _V8_PERSIST_FIELDS}
        tmp = D.V8_STATE_FILE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(subset, f, indent=2, default=str)
        os.replace(tmp, D.V8_STATE_FILE_PATH)
    except Exception as e:
        logger.error("[V8] State save error: " + str(e))

def _load_v8_state():
    if not os.path.isfile(D.V8_STATE_FILE_PATH):
        return
    try:
        with open(D.V8_STATE_FILE_PATH) as f:
            saved = json.load(f)
        with _v8_lock:
            for k, v in saved.items():
                if k in _v8_state:
                    _v8_state[k] = v
        logger.info("[V8] State loaded from disk")
        # Reset daily counters if state file is from a previous day
        _today = date.today().isoformat()
        _last_date = str(saved.get("_last_trade_date", ""))
        if _last_date != _today:
            with _v8_lock:
                _v8_state["_pnl_today_pts"] = 0.0
                _v8_state["_trades_today"]  = 0
                _v8_state["_wins_today"]    = 0
                _v8_state["_losses_today"]  = 0
                _v8_state["_v8_both_rejected_ts"] = 0.0
            logger.info("[V8] New trading day — daily counters reset (last_date=" + _last_date + ")")
        if _v8_state.get("in_trade"):
            _sym  = str(_v8_state.get("symbol", ""))
            _ep   = float(_v8_state.get("entry_price", 0))
            _peak = float(_v8_state.get("peak_pnl", 0))
            _tier  = str(_v8_state.get("active_ratchet_tier", "INITIAL"))
            _sl    = float(_v8_state.get("active_ratchet_sl", 0) or 0)
            if _sl <= 0: _sl = round(_ep - 12, 2)
            _tok   = int(_v8_state.get("token", 0) or 0)
            _ltp   = D.get_ltp(_tok) if _tok else 0
            _pnl   = round(_ltp - _ep, 1) if _ltp else 0
            _room  = round(_ltp - _sl, 1) if _ltp else 0
            _dir   = str(_v8_state.get("direction", ""))
            _strk  = str(_v8_state.get("strike", ""))
            _qty   = int(_v8_state.get("qty", 0) or 0)
            _etime = str(_v8_state.get("entry_time", ""))
            _emj   = "🟢" if _dir == "CE" else "🔴"
            logger.info("[V8] Was in trade on last shutdown — " + _sym + " monitoring resumed")
            _tg_send(
                "⚡ <b>V8 restarted mid-trade</b>\n"
                + _emj + " " + _dir + " " + _strk + " · qty " + str(_qty) + "\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Entry  Rs" + "{:.2f}".format(_ep) + "  @ " + _etime + "\n"
                + ("LTP    Rs" + "{:.2f}".format(_ltp)
                   + "  (" + ("+" if _pnl >= 0 else "") + str(_pnl) + " pts)\n" if _ltp else "LTP    — (no tick yet)\n")
                + "Peak   +" + "{:.1f}".format(_peak) + " pts\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Tier   " + _tier + " · SL Rs" + "{:.2f}".format(_sl)
                + ("  (Room " + ("+" if _room >= 0 else "") + str(_room) + ")" if _ltp else "") + "\n"
                "✅ Exit monitoring resumed."
            )
    except Exception as e:
        logger.error("[V8] State load error: " + str(e))


def _save_shadow_state():
    """Persist _v8_shadow_dt and _v8_shadow_p2 to disk so active signals survive restarts."""
    try:
        payload = {
            "p1": {
                "CE": dict(_v8_shadow_dt["CE"]),
                "PE": dict(_v8_shadow_dt["PE"]),
            },
            "p2": {
                "CE": dict(_v8_shadow_p2["CE"]),
                "PE": dict(_v8_shadow_p2["PE"]),
            },
            "p1_v2": {
                "CE": dict(_v8_shadow_dt_v2["CE"]),
                "PE": dict(_v8_shadow_dt_v2["PE"]),
            },
            "p2_v2": {
                "CE": dict(_v8_shadow_p2_v2["CE"]),
                "PE": dict(_v8_shadow_p2_v2["PE"]),
            },
            "saved_date": date.today().isoformat(),
        }
        tmp = D.SHADOW_STATE_FILE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, D.SHADOW_STATE_FILE_PATH)
    except Exception as e:
        logger.error("[SHADOW] State save error: " + str(e))


def _load_shadow_state():
    """Restore shadow signal state from disk on startup."""
    global _v8_shadow_dt, _v8_shadow_p2, _v8_shadow_dt_v2, _v8_shadow_p2_v2
    if not os.path.isfile(D.SHADOW_STATE_FILE_PATH):
        return
    try:
        with open(D.SHADOW_STATE_FILE_PATH) as f:
            saved = json.load(f)
        # Only restore if from today — stale state from yesterday is useless
        if saved.get("saved_date") != date.today().isoformat():
            logger.info("[SHADOW] State file is from previous day — skipping restore")
            return
        for _dir in ("CE", "PE"):
            if _dir in saved.get("p1", {}):
                _v8_shadow_dt[_dir].update(saved["p1"][_dir])
            if _dir in saved.get("p2", {}):
                _v8_shadow_p2[_dir].update(saved["p2"][_dir])
            if _dir in saved.get("p1_v2", {}):
                _v8_shadow_dt_v2[_dir].update(saved["p1_v2"][_dir])
            if _dir in saved.get("p2_v2", {}):
                _v8_shadow_p2_v2[_dir].update(saved["p2_v2"][_dir])
        _p1_ce = _v8_shadow_dt["CE"].get("active", False)
        _p1_pe = _v8_shadow_dt["PE"].get("active", False)
        _p2_ce = _v8_shadow_p2["CE"].get("active", False)
        _p2_pe = _v8_shadow_p2["PE"].get("active", False)
        active_list = []
        if _p1_ce: active_list.append(f"P1-CE@{_v8_shadow_dt['CE'].get('entry_price',0)}")
        if _p1_pe: active_list.append(f"P1-PE@{_v8_shadow_dt['PE'].get('entry_price',0)}")
        if _p2_ce: active_list.append(f"P2-CE@{_v8_shadow_p2['CE'].get('entry_price',0)}")
        if _p2_pe: active_list.append(f"P2-PE@{_v8_shadow_p2['PE'].get('entry_price',0)}")
        if active_list:
            logger.info("[SHADOW] Restored active signals: " + ", ".join(active_list))
            _tg_send(
                "🔄 <b>Shadow signals restored after restart</b>\n"
                + "\n".join(f"• {s}" for s in active_list)
                + "\n<i>Tracking resumed from original entry</i>"
            )
        else:
            logger.info("[SHADOW] State loaded — no active signals")
    except Exception as e:
        logger.error("[SHADOW] State load error: " + str(e))


def _reconcile_positions(kite):
    """
    Startup position reconciliation — compare saved state with MStock broker.
    If bot crashed mid-trade and position is gone at broker, reset state.
    If broker has position but state says no trade, alert for manual resolution.
    v13.2: Uses MStock get_net_position() — orders placed on MStock, not Kite.
    """
    if kite is None or D.PAPER_MODE:
        return
    try:
        mc        = MSTOCK.get_mstock()
        resp      = mc.get_net_position()
        data      = resp.json()
        positions = data.get("data", {}) if data.get("status") == "success" else {}
        net       = positions.get("net", []) if isinstance(positions, dict) else []
        # Find NFO positions with non-zero quantity
        nfo_positions = [p for p in net
                         if p.get("exchange") == "NFO"
                         and p.get("quantity", 0) != 0
                         and "NIFTY" in p.get("tradingsymbol", "")]

        saved_in_trade = state.get("in_trade", False)
        saved_symbol = state.get("symbol", "")

        if saved_in_trade and not nfo_positions:
            logger.warning("[RECONCILE] State says in_trade but NO broker position for "
                           + saved_symbol + " — resetting state")
            _tg_send(
                "⚠️ <b>POSITION MISMATCH</b>\n"
                "State : in_trade (" + saved_symbol + ")\n"
                "Broker: NO position found\n"
                "Action: State reset. Position was likely squared off manually."
            )
            with _state_lock:
                state["in_trade"] = False
                state["symbol"] = ""
                state["token"] = None

        elif not saved_in_trade and nfo_positions:
            symbols = [p["tradingsymbol"] for p in nfo_positions]
            logger.warning("[RECONCILE] State says NOT in_trade but broker has positions: "
                           + str(symbols))
            _tg_send(
                "⚠️ <b>POSITION MISMATCH</b>\n"
                "State : NOT in trade\n"
                "Broker: " + ", ".join(symbols) + "\n"
                "Action: Manual resolution needed. Bot will NOT auto-exit."
            )

        elif saved_in_trade and nfo_positions:
            broker_syms = [p["tradingsymbol"] for p in nfo_positions]
            if saved_symbol not in broker_syms:
                logger.warning("[RECONCILE] Symbol mismatch: state=" + saved_symbol
                               + " broker=" + str(broker_syms))
                _tg_send(
                    "⚠️ <b>SYMBOL MISMATCH</b>\n"
                    "State : " + saved_symbol + "\n"
                    "Broker: " + ", ".join(broker_syms) + "\n"
                    "Manual resolution needed."
                )
            else:
                logger.info("[RECONCILE] Position confirmed: " + saved_symbol)
        else:
            logger.info("[RECONCILE] Clean — no positions, no saved trade")

    except Exception as e:
        logger.error("[RECONCILE] Position check failed: " + str(e)
                     + " — continuing with saved state")


def _reset_daily(today_str: str):
    # V8 shadow daily counters
    _v8_state["_signals_today"]    = 0
    _v8_state["_last_signal_time"] = ""
    _v8_state["_last_fired_candle_ts"] = ""
    with _state_lock:
        state["daily_pnl"]             = 0.0
        state["_eod_reported"]         = False
        state["_eod_exited"]           = False
        state["aggressive_mode"]       = False
        state["paused"]                = False
        state["_bias_done"]            = False
        state["_straddle_done"]        = False
        state["_hourly_rsi_ts"]        = 0
        state["_straddle_alerted"]     = False
        state["_or_refreshed_today"]   = False  # reset OR refresh flag
        # Clear persisted scan dedup key so a crash-restart landing at
        # 09:30:45 (after the loop already scanned 09:30) doesn't treat
        # the current minute as already-scanned and silently skip the
        # first entry of the session.
        state["_last_scan_key"]        = ""
    D.clear_token_cache()
    D.reset_daily_warnings()
    _reset_strike_lock()
    logger.info("[MAIN] _eod_exited reset for new day")
    # DB maintenance
    try:
        import VRL_DB as _DB
        _DB.cleanup_old_db_data()
        from datetime import date as _d
        if _d.today().weekday() == 6:  # Sunday
            _DB.vacuum_db()
    except Exception:
        pass
    logger.info("[MAIN] Daily reset")
    # Force recompute of institutional levels for the new day
    try:
        LEVELS._last_compute_day = None
        LEVELS._daily_levels = {}
        LEVELS._opt_levels = {}
        LEVELS.compute_today(D, _kite, None)
    except Exception as _le:
        logger.debug(f"[LEVELS] daily recompute error: {_le}")

    # Reset VWAP for new day
    try:
        LEVELS._vwap_state = {"fut_close": 0.0, "vwap": 0.0,
                              "gap": 0.0, "last_update": None}
        state["_last_vwap_15m_slot"] = -1
        LEVELS.update_vwap(_kite)
    except Exception as _ve:
        logger.debug(f"[VWAP] daily reset error: {_ve}")
    _save_state()
    # Fetch 5 days of 3-min + 1-min candles for ATM±100 so GARCH is warm.
    try:
        from datetime import date as _dr10
        if state.get("_preload_done_today") != _dr10.today().isoformat():
            _spot_close = float(state.get("prev_close", 0) or 0)
            if _spot_close <= 0:
                _spot_close = D.get_ltp(D.NIFTY_SPOT_TOKEN)
            if _spot_close > 0:
                _atm_prov = D.resolve_atm_strike(_spot_close)
                _expiry_prov = D.get_nearest_expiry(_kite)
                if _atm_prov and _expiry_prov:
                    _r10_strikes = [_atm_prov + _off
                                    for _off in (-100, -50, 0, 50, 100)]
                    _r10_total = 0
                    for _r10_sk in _r10_strikes:
                        _r10_res = D.ensure_option_history(
                            _kite, _r10_sk, _expiry_prov,
                            min_candles=30, timeframes=("3minute", "minute"))
                        if _r10_res.get("fetched"):
                            _r10_total += (_r10_res.get("ce_candles", 0)
                                           + _r10_res.get("pe_candles", 0))
                    logger.info("[PRELOAD] Market-open 5-strike window ATM="
                                + str(_atm_prov) + " total_candles="
                                + str(_r10_total))
                    with _state_lock:
                        state["_preload_done_today"] = _dr10.today().isoformat()
                    _save_state()
    except Exception as _r10e:
        logger.warning("[PRELOAD] market-open error: " + str(_r10e))

# ═══════════════════════════════════════════════════════════════
#  PID FILE
# ═══════════════════════════════════════════════════════════════

def _write_pid():
    try:
        with open(D.PID_FILE_PATH, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

def _remove_pid():
    try:
        if os.path.isfile(D.PID_FILE_PATH):
            os.remove(D.PID_FILE_PATH)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
#  TRADE LOG
# ═══════════════════════════════════════════════════════════════

TRADE_FIELDNAMES = [
    "date", "entry_time", "exit_time", "symbol", "direction", "strike",
    "entry_price", "exit_price", "pnl_pts", "pnl_rs",
    "gross_pnl_rs", "net_pnl_rs",
    "peak_pnl", "exit_reason",
    "dte", "candles_held", "session", "sl_pts",
    "vix_at_entry", "entry_mode",
    "bias", "hourly_rsi",
    "brokerage", "stt", "exchange_charges", "gst", "stamp_duty",
    "total_charges", "num_exit_orders", "qty_exited",
    "entry_slippage", "exit_slippage", "lot_id",
    "entry_ema9_high", "entry_ema9_low",
    "exit_ema9_high", "exit_ema9_low",
    "entry_band_position", "exit_band_position",
    "entry_body_pct",
    # v16.7 Cross-leg divergence (LOG ONLY — 1-week eval)
    "xleg_signal", "xleg_other_close", "xleg_other_ema9l",
    "xleg_other_dying", "xleg_other_margin",
    # v16.7 Anti-spike pullback entry tracking
    "spike_close", "spike_target", "spike_fill", "spike_wait_used",
]

def _trade_csv_reader(f):
    """Return a DictReader that works whether or not the trade log has a header row.
    Peeks at the first 4 bytes: if it starts with 'date' the file has a header,
    otherwise inject TRADE_FIELDNAMES so no data row is silently consumed."""
    first = f.read(4)
    f.seek(0)
    if first.startswith("date"):
        return csv.DictReader(f)
    return csv.DictReader(f, fieldnames=TRADE_FIELDNAMES)


def _cleanup_trade_log():
    """One-time cleanup: remove corrupted rows where date doesn't match YYYY-MM-DD."""
    path = D.TRADE_LOG_PATH
    if not os.path.isfile(path):
        return
    try:
        import re
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        with open(path, "r") as f:
            reader = _trade_csv_reader(f)
            good_rows = [r for r in reader if date_re.match(r.get("date", ""))]
        # Rewrite with correct header + good rows only
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(good_rows)
        logger.info("[MAIN] Trade log cleaned: " + str(len(good_rows)) + " valid rows kept")
    except Exception as e:
        logger.warning("[MAIN] Trade log cleanup error: " + str(e))

def _compute_exit_band_position(exit_price: float,
                                current_ema9_high, current_ema9_low) -> str:
    """v15.2.5: where was price vs the band when we exited? ABOVE / IN / BELOW."""
    try:
        px = float(exit_price or 0)
        eh = float(current_ema9_high or 0)
        el = float(current_ema9_low  or 0)
        if eh <= 0 and el <= 0:
            return ""
        if px > eh:
            return "ABOVE"
        if px < el:
            return "BELOW"
        return "IN"
    except Exception:
        return ""

def _log_trade(st: dict, exit_price: float, exit_reason: str,
               candles_held: int = 0, saved_entry: float = None,
               lot_id: str = "ALL", qty: int = 0):
    os.makedirs(os.path.dirname(D.TRADE_LOG_PATH), exist_ok=True)
    is_new  = not os.path.isfile(D.TRADE_LOG_PATH)
    entry   = saved_entry if saved_entry is not None else st.get("entry_price", 0)
    pnl_pts = round(exit_price - entry, 2)
    _lot_qty = qty if qty > 0 else D.get_lot_size()
    pnl_rs  = round(pnl_pts * _lot_qty, 2)

    row = {
        "date"          : date.today().isoformat(),
        "entry_time"    : st.get("entry_time", ""),
        "exit_time"     : datetime.now().strftime("%H:%M:%S"),
        "symbol"        : st.get("symbol", ""),
        "direction"     : st.get("direction", ""),
        "strike"        : st.get("strike", 0),
        "entry_price"   : entry,
        "exit_price"    : round(exit_price, 2),
        "pnl_pts"       : pnl_pts,
        "pnl_rs"        : pnl_rs,
        "peak_pnl"      : round(st.get("peak_pnl", 0), 2),
        "exit_reason"   : exit_reason,
        "dte"           : st.get("dte_at_entry", 0),
        "candles_held"  : candles_held,
        "session"       : st.get("session_at_entry", ""),
        "sl_pts"        : st.get("sl_pts_at_entry", 0),
        "bias"          : D.get_daily_bias(),
        "vix_at_entry"  : round(D.get_vix(), 1),
        "hourly_rsi"    : D.get_hourly_rsi(),
        "entry_mode"    : st.get("entry_mode", "EMA9_BREAKOUT"),
        "entry_slippage": st.get("entry_slippage", 0),
        "exit_slippage" : 0,
        "lot_id"        : lot_id,
        "qty_exited"    : _lot_qty,
        "entry_ema9_high":     round(float(st.get("entry_ema9_high", 0) or 0), 2),
        "entry_ema9_low":      round(float(st.get("entry_ema9_low",  0) or 0), 2),
        "exit_ema9_high":      round(float(st.get("current_ema9_high", 0) or 0), 2),
        "exit_ema9_low":       round(float(st.get("current_ema9_low",  0) or 0), 2),
        "entry_band_position": st.get("entry_band_position", "") or "",
        "exit_band_position":  _compute_exit_band_position(
                                    exit_price,
                                    st.get("current_ema9_high", 0),
                                    st.get("current_ema9_low", 0)),
        "entry_body_pct":      round(float(st.get("entry_body_pct", 0) or 0), 1),
        # v16.7 Cross-leg divergence
        "xleg_signal":         st.get("_xleg_signal", "NA") or "NA",
        "xleg_other_close":    round(float(st.get("_xleg_other_close", 0) or 0), 2),
        "xleg_other_ema9l":    round(float(st.get("_xleg_other_ema9l", 0) or 0), 2),
        "xleg_other_dying":    bool(st.get("_xleg_other_dying", False)),
        "xleg_other_margin":   round(float(st.get("_xleg_other_margin", 0) or 0), 2),
        # v16.7 Anti-spike pullback
        "spike_close":         round(float(st.get("_spike_close", 0) or 0), 2),
        "spike_target":        round(float(st.get("_spike_target", 0) or 0), 2),
        "spike_fill":          round(float(st.get("_spike_fill", 0) or 0), 2),
        "spike_wait_used":     round(float(st.get("_spike_wait_used", 0) or 0), 1),
    }

    # Fix strike: use locked strike from state, fallback to ATM calculation
    if not row["strike"] or row["strike"] == 0:
        try:
            _spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
            if _spot > 0:
                _step = D.get_active_strike_step(st.get("dte_at_entry", 0))
                row["strike"] = D.resolve_atm_strike(_spot, _step)
        except Exception:
            pass

    # Calculate charges
    _num_exit_orders = 1
    _qty = _lot_qty
    try:
        _ch = CHARGES.calculate_charges(entry, exit_price, _qty, _num_exit_orders)
        row["brokerage"] = _ch["brokerage"]
        row["stt"] = _ch["stt"]
        row["exchange_charges"] = _ch["exchange"]
        row["gst"] = _ch["gst"]
        row["stamp_duty"] = _ch["stamp"]
        row["total_charges"] = _ch["total_charges"]
        row["gross_pnl_rs"] = _ch["gross_pnl"]
        row["net_pnl_rs"] = _ch["net_pnl"]
        row["pnl_rs"] = _ch["gross_pnl"]  # override to match charges calc qty
        row["num_exit_orders"] = _num_exit_orders
    except Exception:
        pass

    # One-shot header migration: add missing columns or prepend header when absent
    if not is_new:
        try:
            with open(D.TRADE_LOG_PATH, "r", newline="") as _f_chk:
                _r_chk = csv.reader(_f_chk)
                _hdr = next(_r_chk, None) or []
            _has_header = "date" in _hdr
            _missing = [c for c in TRADE_FIELDNAMES if c not in _hdr] if _has_header else list(TRADE_FIELDNAMES)
            if _missing or not _has_header:
                logger.info("[MAIN] Trade-log header upgrade: has_header="
                            + str(_has_header) + " missing=" + str(_missing))
                with open(D.TRADE_LOG_PATH, "r", newline="") as _f_rd:
                    if _has_header:
                        _old_rows = list(csv.DictReader(_f_rd))
                    else:
                        _old_rows = list(csv.DictReader(_f_rd, fieldnames=TRADE_FIELDNAMES))
                with open(D.TRADE_LOG_PATH, "w", newline="") as _f_wr:
                    _w = csv.DictWriter(_f_wr, fieldnames=TRADE_FIELDNAMES,
                                        extrasaction="ignore")
                    _w.writeheader()
                    for _orow in _old_rows:
                        for _c in _missing:
                            _orow.setdefault(_c, "")
                        _w.writerow(_orow)
                logger.info("[MAIN] Trade-log header upgrade: rewrote "
                            + str(len(_old_rows)) + " rows with new schema")
        except Exception as _me:
            logger.warning("[MAIN] Trade-log header migration error: " + str(_me))

    try:
        with open(D.TRADE_LOG_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow(row)
            f.flush()
    except Exception as e:
        logger.error("[MAIN] Trade log error: " + str(e))

    # Dual write: SQLite
    try:
        import VRL_DB as _DB
        _DB.insert_trade(row)
    except Exception as _dbe:
        logger.warning("[MAIN] DB insert_trade error: " + str(_dbe))

def _read_today_trades() -> list:
    today_str = date.today().isoformat()
    trades    = []
    if not os.path.isfile(D.TRADE_LOG_PATH):
        return trades
    try:
        with open(D.TRADE_LOG_PATH, "r") as f:
            for row in _trade_csv_reader(f):
                if row.get("date", "") == today_str:
                    trades.append(row)
    except Exception as e:
        logger.error("[MAIN] Read trades error: " + str(e))
    return trades

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM — SEND HELPERS
# ═══════════════════════════════════════════════════════════════

# Dynamic public IP — resolved once at module load
_WEB_IP = ""
try:
    import subprocess as _sp
    _WEB_IP = _sp.check_output(["curl", "-s", "ifconfig.me"], timeout=5).decode().strip()
except Exception:
    _WEB_IP = "unknown"

def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")

def _mode_tag() -> str:
    return "📄 PAPER" if D.PAPER_MODE else "💰 LIVE"

def _rs(pts: float) -> str:
    rupees = round(pts * D.get_lot_size(), 0)
    sign   = "+" if rupees >= 0 else ""
    return sign + "₹" + str(int(rupees))

def _short_sym(symbol: str, direction: str = "", strike: int = 0) -> str:
    """CE 22600 from direction+strike. Fallback to symbol suffix."""
    if direction and strike:
        return direction + " " + str(strike)
    if not symbol:
        return ""
    if symbol.endswith("CE"):
        return "CE"
    elif symbol.endswith("PE"):
        return "PE"
    return symbol

from collections import deque as _deque
_tg_timestamps = _deque(maxlen=20)
_TG_FLOOD_LIMIT = 15   # was 5 — Telegram allows ~30/sec; 15/10s is safe
_TG_FLOOD_WINDOW = 10  # seconds

def _tg_safe(s) -> str:
    """Escape <, >, & in dynamic content for Telegram HTML mode.
    Apply only to user/API-supplied strings, NOT to template literals."""
    if s is None:
        return ""
    try:
        return (str(s).replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))
    except Exception:
        return ""


def _tg_send(text: str, parse_mode: str = "HTML", chat_id: str = None,
             priority: str = "normal") -> bool:
    """Non-blocking Telegram send with flood control.

    Runs the POST in a daemon thread so the strategy loop never waits.
    `priority="critical"` bypasses flood control so exit-failure / DB-
    corruption / shutdown-with-open-trade alerts always deliver even
    during a 5-in-10s burst. Critical sends still append to the
    sliding window so bookkeeping stays accurate.
    """
    def _worker():
        if not D.TELEGRAM_TOKEN or not (chat_id or D.TELEGRAM_CHAT_ID):
            return
        is_critical = (str(priority).lower() == "critical")
        now_ts = time.time()
        while _tg_timestamps and now_ts - _tg_timestamps[0] > _TG_FLOOD_WINDOW:
            _tg_timestamps.popleft()
        if not is_critical and len(_tg_timestamps) >= _TG_FLOOD_LIMIT:
            wait = _TG_FLOOD_WINDOW - (now_ts - _tg_timestamps[0])
            if wait > 0:
                time.sleep(min(wait, _TG_FLOOD_WINDOW))
        _tg_timestamps.append(time.time())

        cid = chat_id or D.TELEGRAM_CHAT_ID
        url = _TG_BASE + D.TELEGRAM_TOKEN + "/sendMessage"
        # Sanitize unknown HTML tags in HTML mode; Telegram only allows
        # <b>, <i>, <u>, <s>, <code>, <pre>, <a href>.
        _safe_text = text
        if parse_mode == "HTML":
            try:
                import re as _re
                _safe_text = _re.sub(
                    r"<(?!/?(b|i|u|s|code|pre|a)(\s|>|/))",
                    "&lt;", text)
            except Exception:
                _safe_text = text
        try:
            resp = requests.post(url, json={
                "chat_id"              : cid,
                "text"                 : _safe_text,
                "parse_mode"           : parse_mode,
                "disable_notification" : False,
            }, timeout=10)
            if not resp.ok:
                logger.warning("[TG] Send failed: " + resp.text[:200])
            else:
                logger.debug("[TG] sent ok — " + text[:60].replace("\n", " "))
        except Exception as e:
            logger.error("[TG] send error: " + type(e).__name__)

    threading.Thread(target=_worker, daemon=True).start()
    return True

def _tg_send_file(file_path: str, caption: str = "", chat_id: str = None) -> bool:
    if not D.TELEGRAM_TOKEN:
        return False
    cid = chat_id or D.TELEGRAM_CHAT_ID
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/sendDocument"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(url, data={
                "chat_id": cid,
                "caption": caption[:1024],
            }, files={"document": f}, timeout=60)
        if not resp.ok:
            logger.warning("[TG] File send failed: " + resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.error("[TG] send_file error: " + type(e).__name__)
        return False

def _tg_inline_keyboard(text: str, keyboard: list, chat_id: str = None) -> dict:
    if not D.TELEGRAM_TOKEN:
        return {}
    cid = chat_id or D.TELEGRAM_CHAT_ID
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id"      : cid,
            "text"         : text,
            "parse_mode"   : "HTML",
            "reply_markup" : json.dumps({"inline_keyboard": keyboard}),
        }, timeout=10)
        if resp.ok:
            return resp.json().get("result", {})
    except Exception as e:
        logger.error("[TG] keyboard error: " + type(e).__name__)
    return {}

def _tg_answer_callback(callback_query_id: str, text: str = ""):
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/answerCallbackQuery"
    try:
        requests.post(url, json={
            "callback_query_id": callback_query_id,
            "text": text,
        }, timeout=5)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM — TRADE ALERTS
# ═══════════════════════════════════════════════════════════════

def _alert_bot_started():
    # ── Startup spam suppression — skip TG alert if restarted within 10 min ──
    _ts_file = os.path.join(os.path.expanduser("~"), "logs", "live", ".last_bot_start_ts")
    _now_ts = time.time()
    try:
        if os.path.exists(_ts_file):
            with open(_ts_file) as _tf:
                _last_ts = float(_tf.read().strip())
            if _now_ts - _last_ts < 600:  # 10 min cooldown
                logger.info(f"[MAIN] Startup TG alert suppressed — last restart {int(_now_ts - _last_ts)}s ago")
                with open(_ts_file, "w") as _tf:
                    _tf.write(str(_now_ts))
                return
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(_ts_file), exist_ok=True)
        with open(_ts_file, "w") as _tf:
            _tf.write(str(_now_ts))
    except Exception:
        pass

    _web_url = "http://" + _WEB_IP + ":8080" if _WEB_IP and _WEB_IP != "unknown" else "http://localhost:8080"
    _acct = D.get_account_info()
    _acct_line = ""
    if _acct.get("name"):
        _acct_line = ("Account : " + _acct["name"] + "\n"
                      "Balance : Rs" + "{:,}".format(int(_acct.get("total_balance", 0))) + "\n")
    try:
        _ms_line = "Orders  : " + MSTOCK.ms_get_banner_line() + "\n"
    except Exception:
        _ms_line = ""
    _tg_send(
        "<b>VISHAL RAJPUT TRADE " + D.VERSION + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time    : " + _now_str() + "\n"
        "Mode    : " + _mode_tag() + "\n"
        + _acct_line
        + _ms_line +
        "Web     : " + _web_url + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>STRATEGY</b>  Vishal Clean v19\n"
        ""
        "V9 LIVE   : 3-min  | 3-gate | PAPER trading\n"
        "Entry   : " + CFG.entry_ema9_band("warmup_until_v8", "09:35") + " - " + CFG.entry_ema9_band("cutoff_after", "15:00") + " IST\n"
        "Size    : 2 lots fixed\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>V9 GATES</b>\n"
        "G2) Close > EMA9_low\n"
        "G3) Band width 13-16 pts\n"
        "G5) 48 < RSI < 70\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>V9 SL LADDER</b>\n"
        "peak < 12  → INITIAL  entry - 12\n"
        "peak >= 12 → LOCK_4   entry + 4\n"
        "peak >= 24 → LOCK_12  entry + 12\n"
        "peak >= 30 → LOCK_20  entry + 20\n"
        "peak >= 36 → LOCK_30  entry + 30\n"
        "peak >= 40 → LOCK_36  entry + 36\n"
        "peak >= 50 → LOCK_50  entry + 50\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>EXITS</b>  Emergency -12 | EOD 15:20 | Trail\n"
        "/help for commands"
    )
    if not D.PAPER_MODE:
        _tg_send(
            "🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴\n"
            "⚡ <b>LIVE MODE — REAL MONEY</b> ⚡\n"
            "🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴\n"
            "Account: " + str(D.get_account_info().get("name", "")) + "\n"
            "Balance: ₹" + "{:,}".format(int(D.get_account_info().get("total_balance", 0))) + "\n"
            "Lots: 2 × " + str(D.get_lot_size()) + " = " + str(D.get_lot_size() * 2) + " qty\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Every order uses REAL money.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

def _alert_exit_critical(symbol: str, qty: int, reason: str = ""):
    """v15.2.5 richer CRITICAL alert — names the blocked trade,
    tells the operator exactly which Telegram command clears the lock
    once Kite shows the position is flat. All further exit attempts
    are suppressed until /reset_exit is received."""
    _reason_line = ("Reason : " + str(reason) + "\n") if reason else ""
    _tg_send(
        "🚨 <b>CRITICAL: EXIT FAILED</b>\n"
        "Symbol : " + symbol + "  Qty: " + str(qty) + "\n"
        + _reason_line +
        "Both LIMIT + MARKET exit attempts failed at the broker.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "1. Open Kite app and close this position manually NOW.\n"
        "2. Once flat on the broker side, send <b>/reset_exit</b> here\n"
        "   to re-enable automatic exits.\n"
        "Until then, all exit attempts are blocked to prevent duplicate\n"
        "orders or incorrect state.",
        priority="critical",   # bypass flood control
    )

def _alert_error(message: str):
    _tg_send("⚠️ <b>ERROR</b>  " + _now_str() + "\n" + message)

# ═══════════════════════════════════════════════════════════════
#  EOD REPORT
# ═══════════════════════════════════════════════════════════════

def _generate_eod_report():
    trades = _read_today_trades()
    today  = date.today().strftime("%d %b %Y")

    if not trades:
        _tg_send(
            "<b>EOD REPORT " + today + "</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "No trades today.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        return

    total_pts  = sum(float(t.get("pnl_pts", 0)) for t in trades)
    total_rs   = sum(float(t.get("gross_pnl_rs", t.get("pnl_rs", 0))) for t in trades)
    wins       = [t for t in trades if float(t.get("pnl_pts", 0)) > 0]
    losses     = [t for t in trades if float(t.get("pnl_pts", 0)) <= 0]
    n_trades   = len(trades)
    win_rate   = round(len(wins) / n_trades * 100, 0) if n_trades > 0 else 0
    best       = max((float(t.get("pnl_pts", 0)) for t in trades), default=0)
    worst      = min((float(t.get("pnl_pts", 0)) for t in trades), default=0)

    sign = "+" if total_pts >= 0 else ""

    trade_lines = ""
    for i, t in enumerate(trades[:5], 1):
        _pts    = float(t.get("pnl_pts", 0))
        _side   = t.get("direction", "")
        _strike = t.get("strike", 0)
        _reason = t.get("exit_reason", "")
        trade_lines += (
            str(i) + ". " + _side + " " + str(_strike) + "  "
            + "{:+.1f}".format(_pts) + "pts  " + _reason + "\n"
        )
    if len(trades) > 5:
        trade_lines += "+" + str(len(trades) - 5) + " more\n"

    _tg_send(
        "<b>EOD REPORT " + today + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + ("🟢" if total_pts >= 0 else "🔴")
        + " <b>" + sign + "{:.1f}".format(total_pts) + " pts   "
        + ("+" if total_rs >= 0 else "-") + "Rs" + "{:,}".format(abs(int(total_rs)))
        + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Trades   " + str(n_trades) + "   (" + str(len(wins)) + "W " + str(len(losses)) + "L)\n"
        "Win rate " + str(int(win_rate)) + "%\n"
        "Best     " + "{:+.1f}".format(best) + " pts\n"
        "Worst    " + "{:+.1f}".format(worst) + " pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + trade_lines +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

# ═══════════════════════════════════════════════════════════════
#  ENTRY + EXIT EXECUTION
# ═══════════════════════════════════════════════════════════════

def _wait_for_pullback(token: int, target_price: float, timeout_secs: int) -> tuple:
    """Anti-spike limit-pullback: poll LTP up to timeout_secs.
    Fill at current LTP the moment it touches target (close-buffer).
    Returns (fill_price, elapsed_secs) on fill, (None, elapsed_secs)
    on timeout. Aborts early if bot paused or market closes.

    Hard requirement: caller must not be in_trade (entry-path only).
    """
    if timeout_secs <= 0 or target_price <= 0 or token <= 0:
        return None, 0
    deadline = time.time() + timeout_secs
    start = time.time()
    while time.time() < deadline:
        if state.get("paused"):
            return None, round(time.time() - start, 1)
        if not D.is_market_open():
            return None, round(time.time() - start, 1)
        try:
            ltp = D.get_ltp(token)
        except Exception:
            ltp = 0
        if ltp and ltp > 0 and ltp <= target_price:
            return float(ltp), round(time.time() - start, 1)
        time.sleep(1)
    return None, float(timeout_secs)


def _execute_entry(kite, option_info: dict, option_type: str,
                   entry_result: dict, profile: dict,
                   expiry, dte: int, session: str = "MORNING"):
    token       = option_info["token"]
    symbol      = option_info["symbol"]
    entry_price = entry_result["entry_price"]

    import VRL_CONFIG as CFG
    lot_count = CFG.get().get("lots", {}).get("count", 2)
    total_qty = D.get_lot_size() * lot_count

    fill = place_entry(kite, symbol, token, option_type,
                       total_qty, entry_price)

    if not fill["ok"]:
        if fill.get("error") == "LIMIT_NOT_FILLED":
            _sym_skip = _short_sym(symbol, option_type, entry_result.get("_strike", 0))
            _tg_send(
                "⏭ <b>ENTRY SKIPPED</b>\n"
                + _sym_skip + " ₹" + str(round(entry_price, 1)) + "\n"
                "Price moved away — LIMIT not filled\n"
                "Protected from bad fill ✓"
            )
            logger.info("[MAIN] Entry skipped: LIMIT not filled for " + symbol)
        else:
            logger.error("[MAIN] Entry failed: " + fill["error"])
            _alert_error("Entry failed: " + fill["error"])
        return

    actual_price = fill["fill_price"]
    actual_qty   = fill["fill_qty"]
    _entry_slippage = fill.get("slippage", 0)
    hard_sl = abs(CFG.exit_ema9_band("emergency_sl_pts", -12))
    phase1_sl = compute_entry_sl(actual_price, hard_sl)

    # Extract the OTHER side token for manage_exit divergence check.
    _other_token_entry = 0
    try:
        _ce_locked = (_locked_tokens or {}).get("CE") or {}
        _pe_locked = (_locked_tokens or {}).get("PE") or {}
        if option_type == "CE" and _pe_locked:
            _other_token_entry = int(_pe_locked.get("token", 0) or 0)
        elif option_type == "PE" and _ce_locked:
            _other_token_entry = int(_ce_locked.get("token", 0) or 0)
    except Exception:
        pass

    with _state_lock:
        state["in_trade"]           = True
        # Same-candle guard: remember which closed candle this entry came
        # from so engine rejects re-entry on the same candle for any reason
        # (cooldown=0, fast scan loop, immediate emergency exit, etc.).
        _fts = entry_result.get("fired_candle_ts") if entry_result else None
        if _fts:
            state["_last_fired_candle_ts"] = str(_fts)
        state["symbol"]             = symbol
        state["token"]              = token
        state["direction"]          = option_type
        state["entry_price"]        = actual_price
        state["entry_time"]         = datetime.now().strftime("%H:%M:%S")
        state["strike"]             = entry_result.get("_strike", D.resolve_atm_strike(
            D.get_ltp(D.NIFTY_SPOT_TOKEN), D.get_active_strike_step(dte)))
        state["expiry"]             = expiry.isoformat() if expiry else ""
        state["qty"]                = actual_qty
        state["lot_count"]          = lot_count
        state["lot1_active"]        = True
        state["lot2_active"]        = True
        state["lots_split"]         = False
        try:
            _trade_strike = state["strike"]
            _trade_dir    = option_type
            _tce = int((_ce_locked or {}).get("token", 0) or 0)
            _tpe = int((_pe_locked or {}).get("token", 0) or 0)
            if not _tce or not _tpe:
                _both = D.get_option_tokens(kite, int(_trade_strike), expiry) or {}
                if not _tce:
                    _tce = int((_both.get("CE") or {}).get("token", 0))
                if not _tpe:
                    _tpe = int((_both.get("PE") or {}).get("token", 0))
            D.set_active_trade(_trade_strike, _trade_dir, _tce, _tpe)
        except Exception as _ate:
            logger.debug("[MAIN] set_active_trade: " + str(_ate))
        # Exit state
        state["_static_floor_sl"]   = 0
        state["current_floor"]      = phase1_sl
        state["peak_pnl"]           = 0.0
        state["candles_held"]       = 0
        state["_candle_low"]        = actual_price
        state["_last_milestone"]    = 0
        # v15.0 entry context — band values at entry
        state["entry_mode"]         = entry_result.get("entry_mode", "EMA9_BREAKOUT")
        state["entry_ema9_high"]    = round(float(entry_result.get("ema9_high", 0)), 2)
        state["entry_ema9_low"]     = round(float(entry_result.get("ema9_low", 0)), 2)
        state["entry_band_position"] = entry_result.get("band_position", "ABOVE")
        state["entry_body_pct"]     = round(float(entry_result.get("body_pct", 0)), 1)
        # Cross-leg divergence (LOG ONLY for 1-week eval; never blocks)
        state["_xleg_signal"]       = entry_result.get("xleg_signal", "NA")
        state["_xleg_other_close"]  = round(float(entry_result.get("xleg_other_close", 0) or 0), 2)
        state["_xleg_other_ema9l"]  = round(float(entry_result.get("xleg_other_ema9l", 0) or 0), 2)
        state["_xleg_other_dying"]  = bool(entry_result.get("xleg_other_dying", False))
        state["_xleg_other_margin"] = round(float(entry_result.get("xleg_other_margin", 0) or 0), 2)
        # Anti-spike pullback
        state["_spike_close"]       = round(float(entry_result.get("spike_close", 0) or 0), 2)
        state["_spike_target"]      = round(float(entry_result.get("spike_target", 0) or 0), 2)
        state["_spike_fill"]        = round(float(entry_result.get("spike_fill", 0) or 0), 2)
        state["_spike_wait_used"]   = round(float(entry_result.get("spike_wait_used", 0) or 0), 1)
        state["current_ema9_high"]  = round(float(entry_result.get("ema9_high", 0)), 2)
        state["current_ema9_low"]   = round(float(entry_result.get("ema9_low", 0)), 2)
        state["last_band_check_ts"] = ""
        state["other_token"]        = _other_token_entry

    _save_state()

    # ── v16.3.2 Entry alert ──
    _close = round(float(entry_result.get("close", actual_price)), 1)
    _ema9l = round(float(entry_result.get("ema9_low", 0)), 1)
    _body  = int(round(float(entry_result.get("body_pct", 0)), 0))
    _strike_label = entry_result.get("_strike_label", "ATM")
    _entry_score = entry_result.get("_entry_score", 0)

    _dir_emoji = "🟢" if option_type == "CE" else "🔴"
    _sym = _short_sym(symbol, option_type, entry_result.get("_strike", state.get("strike", 0)))
    _tm = datetime.now().strftime("%H:%M:%S")

    _slope = float(entry_result.get("ema9_low_slope", 0) or 0)
    _slope_tag = "+" if _slope >= 0 else ""
    _bw = float(entry_result.get("band_width", 0))
    _entry_mode_tag = entry_result.get("entry_mode", "EMA9_BREAKOUT")

    # Cross-leg divergence — display only, /xleg shows weekly accuracy
    _xls = entry_result.get("xleg_signal", "NA")
    _xl_other = "PE" if option_type == "CE" else "CE"
    _xl_margin = float(entry_result.get("xleg_other_margin", 0) or 0)
    if _xls == "PASS":
        _xl_line = ("X-Leg   ✓ " + _xl_other + " dying ("
                    + "{:+.1f}".format(_xl_margin) + " below own EMA9L)\n")
    elif _xls == "FAIL":
        _xl_line = ("X-Leg   ✗ " + _xl_other + " holding ("
                    + "{:+.1f}".format(_xl_margin) + " above own EMA9L)\n")
    else:
        _xl_line = "X-Leg   — no data\n"

    _rsi  = float(entry_result.get("rsi", 0) or 0)
    _rsi_prev = float(entry_result.get("rsi_prev", 0) or 0)
    _rsi_arrow = "↑" if entry_result.get("rsi_rising") else "↓"
    _core = (
        "Entry   Rs" + "{:.2f}".format(actual_price) + "   @ " + _tm + " (15-min)\n"
        "Mode    " + str(_entry_mode_tag) + "\n"
        "Close   " + "{:.1f}".format(_close) + "  &gt;  EMA9L " + "{:.1f}".format(_ema9l) + "\n"
        "RSI     " + "{:.1f}".format(_rsi) + " " + _rsi_arrow
        + " (prev " + "{:.1f}".format(_rsi_prev) + ")\n"
        + _xl_line +
        "Band    " + "{:.1f}".format(_bw) + " pts  (display)\n"
    )

    # V6 single emergency floor + simple trail ladder.
    _sl_pts = abs(CFG.exit_ema9_band("emergency_sl_pts", -12))
    _initial_sl = round(actual_price - _sl_pts, 1)
    _stop_block = (
        "<b>STOP</b>\n"
        "Hard SL   -" + str(_sl_pts) + " pts (Rs"
        + "{:.1f}".format(_initial_sl) + ")\n"
        "Trail (V8): ≥12→+4 | ≥24→+12 | ≥30→+20 | ≥36→+30 | ≥40→+36 | ≥50→+50\n"
    )

    _slip_block = ""
    if _entry_slippage and abs(float(_entry_slippage)) > 0.05:
        _slip_block = "Slippage: " + "{:+.2f}".format(float(_entry_slippage)) + " pts\n"

    _tg_send(
        "🕐 <b>V9 ENTRY " + ("FRESH" if _entry_mode_tag == "CLOSE_FILL" else str(_entry_mode_tag)) + "</b>\n"
        + _dir_emoji + " <b>" + _sym + " " + _strike_label + " x "
        + str(lot_count) + " LOTS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _core +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _stop_block
        + _slip_block +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    logger.info(
        "[MAIN] ENTRY " + option_type + " " + symbol
        + " price=" + str(actual_price)
        + " ema_gap=" + str(entry_result.get("ema_gap", 0))
        + " rsi=" + str(entry_result.get("rsi", 0))
        + " SL=" + str(phase1_sl)
    )

    if not D.PAPER_MODE:
        _first_flag = os.path.expanduser("~/state/.first_live_done")
        try:
            if os.path.isfile(_first_flag):
                with open(_first_flag) as _ff:
                    _first_ts = _ff.read().strip()
                if _first_ts and _first_ts.startswith(date.today().isoformat()):
                    _tg_send(
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "🚀 <b>FIRST LIVE TRADE EVER</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "Real money is moving now.\n"
                        "The journey begins.\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
        except Exception:
            pass
    # ── Live validation: 10 entry checks (silent on PASS, alerts on FAIL) ──
    try:
        from VRL_DB import validate_entry
        with _state_lock:
            _vstate = dict(state)
        _failures = validate_entry(_vstate, entry_result, kite)
        if _failures:
            _fail_msg = "⚠️ <b>ENTRY VALIDATION</b>\n"
            for _f in _failures:
                _fail_msg += "❌ " + _f + "\n"
                logger.warning("[VALIDATE] " + _f)
            _tg_send(_fail_msg)
        else:
            logger.info("[VALIDATE] Entry: 10/10 checks passed ✅")
    except Exception as _ve:
        logger.warning("[VALIDATE] Entry validation error: " + str(_ve))


def _execute_exit_v13(kite, exit_info: dict, saved_entry_price: float = None):
    """v13.0: Execute a single exit (partial or full).
    saved_entry_price: pre-captured entry price to avoid stale state after partial exit resets.
    """
    if state.get("_exit_failed"):
        logger.warning("[MAIN] Exit suppressed — previous CRITICAL failure unresolved")
        return

    lot_id = exit_info.get("lot_id", "ALL")
    reason = exit_info.get("reason", "UNKNOWN")
    exit_price = exit_info.get("price", 0)

    with _state_lock:
        symbol    = state["symbol"]
        token     = state["token"]
        direction = state["direction"]
        entry     = saved_entry_price if saved_entry_price is not None else state["entry_price"]
        peak      = state.get("peak_pnl", 0)
        candles   = state.get("candles_held", 0)
        _exit_strike = state.get("strike", 0)
        # Snapshot the active trail tier BEFORE state.update() wipes it below,
        # so the exit alert reports the real tier (LOCK_3/LOCK_5/LOCK_8/LOCK_15/LOCK_DYN)
        # instead of always falling back to INITIAL.
        _tier_snapshot = state.get("active_ratchet_tier", "") or "INITIAL"
        # v15.0: entry confirmation = band position at entry
        _entry_eh = round(float(state.get("entry_ema9_high", 0)), 1)
        _entry_el = round(float(state.get("entry_ema9_low", 0)), 1)
        _entry_body = int(round(float(state.get("entry_body_pct", 0)), 0))
        _entry_mode_e = state.get("entry_mode", "EMA9_BREAKOUT")
        _entry_conf = (_entry_mode_e + " | entry close &gt; EMA9h "
                       + str(_entry_eh) + " | body " + str(_entry_body) + "%")

    if lot_id == "ALL":
        exit_qty = state.get("qty", D.get_lot_size() * 2)
    else:
        exit_qty = D.get_lot_size()

    fill = place_exit(kite, symbol, token, direction,
                      exit_qty, exit_price, reason)

    if not fill["ok"] and fill.get("error") == "EXIT_FAILED_MANUAL_REQUIRED":
        with _state_lock:
            state["_exit_failed"] = True
        _save_state()   # v15.2.5 persist the block across crashes
        _alert_exit_critical(symbol, exit_qty, reason=reason)
        return

    actual_exit = fill["fill_price"] if fill["ok"] else exit_price
    pnl = round(actual_exit - entry, 2)

    # Update lot state + track per-lot exit data
    with _state_lock:
        if lot_id == "ALL":
            state["lot1_active"] = False
            state["lot2_active"] = False
            state["lot1_exit_price"] = round(actual_exit, 2)
            state["lot1_exit_pnl"] = round(pnl, 2)
            state["lot1_exit_reason"] = reason
            state["lot1_exit_time"] = datetime.now().strftime("%H:%M")
            state["lot2_exit_price"] = round(actual_exit, 2)
            state["lot2_exit_pnl"] = round(pnl, 2)
            state["lot2_exit_reason"] = reason
        elif lot_id == "LOT1":
            state["lot1_active"] = False
            state["lot1_exit_price"] = round(actual_exit, 2)
            state["lot1_exit_pnl"] = round(pnl, 2)
            state["lot1_exit_reason"] = reason
            state["lot1_exit_time"] = datetime.now().strftime("%H:%M")
        elif lot_id == "LOT2":
            state["lot2_active"] = False
            state["lot2_exit_price"] = round(actual_exit, 2)
            state["lot2_exit_pnl"] = round(pnl, 2)
            state["lot2_exit_reason"] = reason

    trade_done = not state.get("lot1_active") and not state.get("lot2_active")
    pnl_lots = pnl

    _log_trade(state, actual_exit, reason, candles, saved_entry=entry,
               lot_id=lot_id, qty=exit_qty)

    if trade_done:
        with _state_lock:
            state["daily_pnl"] = round(state.get("daily_pnl", 0) + pnl_lots, 2)
            state["last_exit_time"] = datetime.now().isoformat()
            state["last_exit_direction"] = direction
            state["last_exit_peak"] = peak
            state["last_exit_reason"] = reason
            if reason == "EMERGENCY_SL":
                state["_sl_cooldown_skip_next"] = True
            state["last_exit_price"] = round(actual_exit, 2)
            old_token = state["token"]
            # Capture strike + direction BEFORE state.update() wipes them
            # so we can register the exited strike with VRL_DATA for
            # post-exit lab data capture.
            old_strike = state.get("strike", 0)
            old_dir    = state.get("direction", "")
            old_entry_close = float(state.get("entry_price", 0) or 0)
            try:
                D.clear_active_trade()
            except Exception:
                pass
            # ── PATCH: store exit timestamp as epoch seconds ──
            _exit_epoch = time.time()
            state.update({
                "in_trade": False, "symbol": "", "token": None,
                "direction": "", "strike": 0,
                "entry_price": 0.0, "entry_time": "",
                "_static_floor_sl": 0.0, "current_floor": 0.0,
                "peak_pnl": 0.0,
                "candles_held": 0, "force_exit": False, "_exit_failed": False,
                "active_ratchet_tier": "", "active_ratchet_sl": 0.0,
                "_last_milestone": 0,
                # Re-entry watcher (V7): 2-candle window after exit.
                # Each new 15-min candle close is a re-entry attempt.
                # If 2 consecutive attempts fail, window expires and we
                # rely on fresh-entry path only.
                "_reentry_armed":      (reason != "FORCE_EXIT"),
                "_reentry_exit_ts":    _exit_epoch,
                "_reentry_attempts":   0,
                "_reentry_last_checked_epoch": 0.0,
                "_reentry_direction":  str(old_dir or ""),
                "_reentry_token":      int(old_token or 0),
                "_reentry_strike":     int(old_strike or 0),
                # Entry context (v15.0 band + body)
                "entry_mode": "",
                "entry_ema9_high": 0.0, "entry_ema9_low": 0.0,
                "entry_band_position": "", "entry_body_pct": 0.0,
                "current_ema9_high": 0.0, "current_ema9_low": 0.0,
                "last_band_check_ts": "",
                "other_token": 0,
                # Exchange SL-M tracking (live mode)
                "_sl_order_id": "", "_sl_trigger_at_exchange": 0,
                "lot1_active": True, "lot2_active": True, "lots_split": False,
                "lot1_exit_price": 0.0, "lot1_exit_pnl": 0.0,
                "lot1_exit_reason": "", "lot1_exit_time": "",
                "lot2_exit_price": 0.0, "lot2_exit_pnl": 0.0,
                "lot2_exit_reason": "",
            })
        if old_token:
            import time as _time_post
            _expire_at = _time_post.time() + (POST_EXIT_OBSERVATION_MINUTES * 60)
            with _post_exit_lock:
                _post_exit_observation.append((int(old_token), _expire_at))
            try:
                if old_strike:
                    D.register_post_exit_observation(
                        token=int(old_token),
                        strike=int(old_strike),
                        side=str(old_dir or ""),
                        expire_at=_expire_at,
                    )
            except Exception as _re:
                logger.debug("[POST_EXIT] register err: " + str(_re))
            logger.info(
                "[POST_EXIT] Token " + str(old_token)
                + " (" + str(old_dir) + " " + str(old_strike) + ")"
                + " held " + str(POST_EXIT_OBSERVATION_MINUTES)
                + " min for post-exit observation"
            )
        _reset_strike_lock()
        _day_pnl    = state.get("daily_pnl", 0)
        _sym_short  = _short_sym(symbol, direction, _exit_strike)
        _pnl_sign   = "+" if pnl >= 0 else ""
        _day_rs     = int(_day_pnl * D.get_lot_size())
        import VRL_CONFIG as _CFG_exit
        _cd_cfg     = _CFG_exit.get().get("cooldown", {})
        _num_eo = 2 if state.get("lots_split") else 1
        try:
            _ch = CHARGES.calculate_charges(entry, actual_exit,
                      exit_qty, _num_eo)
        except Exception:
            _ch = {"gross_pnl": pnl * (exit_qty / D.get_lot_size()) * D.get_lot_size(),
                   "total_charges": 0, "net_pnl": pnl * (exit_qty / D.get_lot_size()) * D.get_lot_size(),
                   "charges_pts": 0}
        _dir_emoji = "🟢" if direction == "CE" else "🔴"
        _sym_exit  = _short_sym(symbol, direction, _exit_strike)
        _sign_pnl  = "+" if pnl >= 0 else ""
        _net_sign  = "+" if _ch["net_pnl"] >= 0 else "-"

        _reason_line = ""
        _tier = _tier_snapshot
        if reason == "VISHAL_TRAIL":
            _reason_line = "Trail " + _tier + " triggered\n"
            _trig_close = exit_info.get("trigger_close")
            _trig_time = exit_info.get("trigger_time", "")
            _trig_sl = exit_info.get("trigger_sl")
            if _trig_close is not None and _trig_sl is not None:
                _reason_line += ("Trigger " + (str(_trig_time) + " " if _trig_time else "")
                                + "close Rs" + "{:.1f}".format(_trig_close)
                                + " (≤ SL Rs" + "{:.1f}".format(_trig_sl) + ")\n")
        _capture_line = ""
        try:
            _peak_f = float(peak) if peak else 0
            if _peak_f >= 1.0:
                _cap = int(round(pnl / _peak_f * 100))
                _capture_line = "Capture " + str(_cap) + "%\n"
            elif _peak_f > 0:
                _capture_line = "Capture —\n"
        except Exception:
            pass

        _tg_send(
            _dir_emoji + " <b>V9 EXIT " + _sym_exit + "</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>" + reason + "</b>    " + _sign_pnl + "{:.1f}".format(pnl) + " pts\n"
            + _reason_line +
            "Entry   Rs" + "{:.1f}".format(entry) + "\n"
            "Exit    Rs" + "{:.1f}".format(actual_exit) + "\n"
            "Peak    +" + "{:.1f}".format(peak) + " pts\n"
            + _capture_line +
            "Hold    " + str(candles) + " min\n"
            "Trail   " + _tier + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Gross   " + ("+" if _ch["gross_pnl"] >= 0 else "-")
            + "Rs" + "{:,}".format(abs(int(_ch["gross_pnl"]))) + "\n"
            "Charges -Rs" + "{:,}".format(int(_ch["total_charges"])) + "\n"
            "<b>Net     " + _net_sign + "Rs" + "{:,}".format(abs(int(_ch["net_pnl"]))) + "</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "DAY " + "{:+.1f}".format(_day_pnl) + " pts"
        )
    else:
        with _state_lock:
            state["daily_pnl"] = round(state.get("daily_pnl", 0) + pnl, 2)
        remaining = "LOT2" if state.get("lot2_active") else "LOT1"
        _sym_short_p = _short_sym(symbol, direction, _exit_strike)
        try:
            _ch_p = CHARGES.calculate_lot_charges(entry, actual_exit, D.get_lot_size())
        except Exception:
            _ch_p = {"net_pnl": pnl * D.get_lot_size(), "total_charges": 0}
        _tg_send(
            "💰 <b>" + lot_id + " " + _sym_short_p + "</b> "
            + ("+" if pnl >= 0 else "") + str(round(pnl, 1)) + "pts\n"
            "₹" + str(round(entry, 1)) + " → ₹" + str(round(actual_exit, 1)) + " | " + reason + "\n"
            "Net ₹" + "{:,}".format(abs(int(_ch_p["net_pnl"])))
            + " (charges ₹" + str(int(_ch_p["total_charges"])) + ")\n"
            + remaining + " riding..."
        )

    _save_state()
    if not D.PAPER_MODE:
        try:
            D.refresh_margin(kite)
        except Exception:
            pass
    if trade_done:
        try:
            _da = _last_dash_args
            if _da:
                _write_dashboard(
                    _da.get("spot_ltp", D.get_ltp(D.NIFTY_SPOT_TOKEN)),
                    _da.get("atm_strike", 0), _da.get("dte", 0),
                    _da.get("vix_ltp", D.get_vix()),
                    _da.get("session", ""), _da.get("profile", {}),
                    {}, _da.get("expiry"), datetime.now())
            else:
                _write_dashboard(D.get_ltp(D.NIFTY_SPOT_TOKEN), 0, 0,
                                 D.get_vix(), "", {}, {}, None, datetime.now())
        except Exception:
            pass
    logger.info("[MAIN] EXIT " + lot_id + " " + symbol
                + " price=" + str(actual_exit) + " pnl=" + str(pnl)
                + "pts reason=" + reason)

    if trade_done:
        try:
            from VRL_DB import validate_exit
            with _state_lock:
                _vstate = dict(state)
            _failures = validate_exit(
                _vstate, pnl, actual_exit, reason,
                entry, exit_qty, kite)
            if _failures:
                _fail_msg = "⚠️ <b>EXIT VALIDATION</b>\n"
                for _f in _failures:
                    _fail_msg += "❌ " + _f + "\n"
                    logger.warning("[VALIDATE] " + _f)
                _tg_send(_fail_msg)
            else:
                logger.info("[VALIDATE] Exit: 10/10 checks passed ✅")
        except Exception as _ve:
            logger.warning("[VALIDATE] Exit validation error: " + str(_ve))


# ═══════════════════════════════════════════════════════════════
#  CANDLE BOUNDARY
# ═══════════════════════════════════════════════════════════════

def _is_new_1min_candle(now: datetime) -> bool:
    key = now.strftime("%Y%m%d%H%M")
    with _state_lock:
        if state.get("_last_1min_candle") != key and now.second >= 35:
            state["_last_1min_candle"] = key
            return True
    return False


# ═══════════════════════════════════════════════════════════════
#  STRATEGY LOOP
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD SNAPSHOT — written every cycle for VRL_WEB.py
#  VRL_WEB.py reads this file. Zero calculation in web server.
# ═══════════════════════════════════════════════════════════════


def _update_dashboard_ltp():
    """Quick update — just LTP values in dashboard JSON. No API calls."""
    try:
        dash_path = os.path.join(D.STATE_DIR, 'vrl_dashboard.json')
        if not os.path.isfile(dash_path):
            return
        with open(dash_path) as f:
            dash = json.load(f)

        # Update spot + VIX
        spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        if spot > 0:
            dash.setdefault("market", {})["spot"] = round(spot, 2)
        vix = D.get_vix()
        if vix > 0:
            dash.setdefault("market", {})["vix"] = round(vix, 1)

        # Update option LTPs
        for side in ("CE", "PE"):
            sig = dash.get(side.lower(), {})
            oi = _locked_tokens.get(side) if _locked_tokens else None
            if oi:
                ltp = D.get_ltp(oi["token"])
                if ltp > 0:
                    sig["ltp"] = round(ltp, 2)

        # Update position if in trade
        with _state_lock:
            _tk = state.get("token")
            _ep = state.get("entry_price", 0)
            _it = state.get("in_trade", False)
        if _it and _tk:
            opt_ltp = D.get_ltp(_tk)
            if opt_ltp > 0:
                pos = dash.get("position", {})
                pos["ltp"] = round(opt_ltp, 2)
                pos["pnl"] = round(opt_ltp - _ep, 1)
                # Update lot PNLs
                running = round(opt_ltp - _ep, 1)
                l1 = pos.get("lot1", {})
                l2 = pos.get("lot2", {})
                if l1.get("status") == "active":
                    l1["pnl"] = running
                if l2.get("status") in ("active", "riding"):
                    l2["pnl"] = running

        dash["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dash["version"] = D.VERSION
        dash.setdefault("market", {})["market_open"] = D.is_market_open()

        tmp = dash_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dash, f, default=str)
        os.replace(tmp, dash_path)
    except Exception:
        pass


def _warmup_info(now, dte):
    """Returns (is_warm, candles_done, candles_needed, eta_hhmm)."""
    needed = 14
    done = 0
    try:
        df = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "3minute", 30)
        if df is not None and not df.empty:
            done = min(needed, len(df))
    except Exception:
        pass
    is_warm = done >= needed
    if is_warm:
        eta = "ready"
    else:
        remaining_candles = needed - done
        remaining_min = remaining_candles * 3
        target = now + timedelta(minutes=remaining_min)
        eta = target.strftime("%H:%M")
    return is_warm, int(done), needed, eta


def _write_dashboard(spot_ltp, atm_strike, dte, vix_ltp, session,
                     profile, all_results, expiry, now,
                     dir_strikes=None):
    """Write everything the dashboard needs to a single JSON file."""
    global spot_3m  # BUG-B fix: update module-level cache so strategy loop can read it
    if dir_strikes is None:
        dir_strikes = {}
    try:
        with _state_lock:
            st = dict(state)

        spot_3m = {}
        spot_3m = D.get_spot_indicators("3minute")

        hourly_rsi = 0
        try:
            hourly_rsi = D.get_hourly_rsi() if hasattr(D, "get_hourly_rsi") else 0
        except Exception:
            pass

        bias = ""
        try:
            bias = D.get_daily_bias() if hasattr(D, "get_daily_bias") else ""
            if not bias:
                bias = ""
        except Exception:
            bias = ""

        straddle_open = getattr(D, "_straddle_open", 0)
        straddle_captured = getattr(D, "_straddle_captured", False)

        def _build_signal(opt_type, result):
            _ltp_fallback = 0
            try:
                _tk = (dir_strikes or {}).get(opt_type, atm_strike)
                _ltp_fallback = D.get_ltp((_locked_tokens or {}).get(opt_type, {}).get("token", 0)) or 0
            except Exception:
                pass
            if not result:
                return {
                    "close": 0, "ema9_high": 0, "ema9_low": 0,
                    "band_width": 0, "body_pct": 0,
                    "fired": False,
                    "verdict": "MARKET CLOSED" if not D.is_market_open() else "WARMING UP",
                    "ltp": round(_ltp_fallback, 2),
                    "strike": dir_strikes.get(opt_type, atm_strike),
                    "g1_green": False, "g2_close_above_ema9l": False,
                    "g2b_slope_ok": False, "g3_bw_ok": False,
                    "g4_other_falling": False, "g5_rsi_ok": False,
                    "rsi": 0, "rsi_prev": 0,
                    "ema9_low_slope": 0,
                }
            _fired = result.get("fired", False)
            _mode = result.get("entry_mode", "")
            _close = float(result.get("close", result.get("entry_price", 0)))
            _eh = float(result.get("ema9_high", 0))
            _el = float(result.get("ema9_low", 0))
            _bw = round(_eh - _el, 1)
            _body = float(result.get("body_pct", 0))
            _green = bool(result.get("candle_green", False))
            _reject = result.get("reject_reason", "")
            _rsi = round(float(result.get("rsi", 0) or 0), 1)
            _rsi_prev = round(float(result.get("rsi_prev", 0) or 0), 1)
            _slope = round(float(result.get("ema9_low_slope", 0) or 0), 2)

            # V9 gate pass/fail flags
            _g1 = _green
            _g2 = (_close > _el) if (_el > 0 and _close > 0) else False
            _g2b = (_slope >= 0)
            _g3 = (13 <= _bw <= 16) if _bw > 0 else False
            _g4 = bool(result.get("g4_other_falling", result.get("xleg_other_dying", False)))
            _g5 = (48 < _rsi < 70 and _rsi > _rsi_prev) if _rsi > 0 else False

            if _fired:
                verdict = "✅ ALL GATES PASSED"
            elif _reject:
                verdict = _reject
            else:
                _fails = []
                if not _g1: _fails.append("G1:red_candle")
                if not _g2: _fails.append(f"G2:close({round(_close,1)})<ema9l({round(_el,1)})")
                if not _g2b: _fails.append(f"G2B:slope_falling({_slope:+.2f})")
                if not _g3: _fails.append(f"G3:BW={_bw}(need13-16)")
                if not _g4: _fails.append("G4:other_side_not_falling")
                if not _g5: _fails.append(f"G5:RSI={_rsi}(need48-70↑)")
                verdict = _fails[0] if _fails else "scanning"

            _ltp_out = round(result.get("entry_price", 0) or _ltp_fallback, 2)
            if _ltp_out == 0: _ltp_out = round(_ltp_fallback, 2)

            return {
                "close": round(_close, 2),
                "ema9_high": round(_eh, 2),
                "ema9_low": round(_el, 2),
                "band_width": _bw,
                "body_pct": round(_body, 1),
                "fired": _fired,
                "verdict": verdict,
                "ltp": _ltp_out,
                "strike": result.get("_strike", dir_strikes.get(opt_type, atm_strike)),
                "rsi": _rsi,
                "rsi_prev": _rsi_prev,
                "ema9_low_slope": _slope,
                "g1_green": _g1,
                "g2_close_above_ema9l": _g2,
                "g2b_slope_ok": _g2b,
                "g3_bw_ok": _g3,
                "g4_other_falling": _g4,
                "g5_rsi_ok": _g5,
                "g6_stochrsi": result.get("g6_stochrsi_os_cross"),
                "g6_k": result.get("g6_k_now", 0),
            }

        _is_warm, _w_done, _w_need, _w_eta = _warmup_info(now, dte)
        ce_signal = _build_signal("CE", all_results.get("CE"))
        pe_signal = _build_signal("PE", all_results.get("PE"))

        try:
            _tokens = D.get_option_tokens(None, atm_strike, expiry)
            for _sig, _side in [(ce_signal, "CE"), (pe_signal, "PE")]:
                if _sig.get("ltp", 0) == 0 and _side in _tokens:
                    _ltp = D.get_ltp(_tokens[_side]["token"])
                    if _ltp <= 0:
                        try:
                            _sym = _tokens[_side]["symbol"]
                            _q = D._kite.ltp("NFO:" + _sym)
                            _ltp = float(list(_q.values())[0]["last_price"])
                        except Exception:
                            pass
                    if _ltp > 0:
                        _sig["ltp"] = round(_ltp, 2)
        except Exception:
            pass

        position = {}
        if st.get("in_trade"):
            opt_ltp = D.get_ltp(st.get("token", 0))
            entry = st.get("entry_price", 0)
            running = round(opt_ltp - entry, 1) if opt_ltp > 0 else 0

            _ratchet_sl = float(st.get("active_ratchet_sl", 0) or 0)
            if _ratchet_sl > 0:
                _stop_price = round(_ratchet_sl, 2)
                _stop_type = "RATCHET_" + str(st.get("active_ratchet_tier", ""))
            else:
                _stop_price = round(st.get("current_ema9_low", 0), 2)
                if _stop_price <= 0:
                    _stop_price = round(entry - 12, 2)
                _stop_type = "EMA9_LOW"

            lot1 = {"status": "active", "pnl": running,
                    "sl": _stop_price, "sl_type": _stop_type}
            lot2 = {"status": "active", "pnl": running,
                    "sl": _stop_price, "sl_type": _stop_type}

            position = {
                "in_trade": True,
                "symbol": st.get("symbol", ""),
                "direction": st.get("direction", ""),
                "entry": entry,
                "entry_time": st.get("entry_time", ""),
                "ltp": round(opt_ltp, 2) if opt_ltp > 0 else 0,
                "pnl": running,
                "peak": round(st.get("peak_pnl", 0), 1),
                "candles": st.get("candles_held", 0),
                "strike": st.get("strike", 0),
                "sl": _stop_price,
                "active_ratchet_tier": st.get("active_ratchet_tier", ""),
                "current_floor": round(st.get("current_ema9_low", 0), 2),
                "current_rsi": round(float(st.get("current_rsi", 0) or 0), 1),
                "lot1": lot1,
                "lot2": lot2,
                "lot1_active": lot1["status"] == "active",
                "lot2_active": lot2["status"] == "active",
                "lots_split": False,
                "lot_size": CFG.lot_size(),
            }
        else:
            position = {"in_trade": False}

        _today_trades = _read_today_trades()
        _today_pnl_pts = 0.0
        _today_pnl_rs = 0.0
        _today_wins = 0
        _today_losses = 0
        for _tt in _today_trades:
            try:
                _p = float(_tt.get("pnl_pts", 0))
                _r = float(_tt.get("pnl_rs", 0))
                _today_pnl_pts += _p
                _today_pnl_rs += _r
                if _p > 0:
                    _today_wins += 1
                else:
                    _today_losses += 1
            except Exception:
                pass
        today_block = {
            "pnl": round(_today_pnl_pts, 1),
            "pnl_rs": round(_today_pnl_rs, 0),
            "trades": len(_today_trades),
            "wins": _today_wins,
            "losses": _today_losses,
            "paused": st.get("paused", False),
        }

        pass

        rolling_block = {"last10_wr": 0, "last20_wr": 0, "last10_pts": 0, "streak": 0}
        try:
            import VRL_DB as _DB_dash
            _l10 = _DB_dash.query("SELECT pnl_pts FROM trades ORDER BY date DESC, entry_time DESC LIMIT 10")
            _l20 = _DB_dash.query("SELECT pnl_pts FROM trades ORDER BY date DESC, entry_time DESC LIMIT 20")
            _w10 = len([t for t in _l10 if float(t.get("pnl_pts", 0)) > 0])
            _w20 = len([t for t in _l20 if float(t.get("pnl_pts", 0)) > 0])
            _pts10 = sum(float(t.get("pnl_pts", 0)) for t in _l10)
            rolling_block["last10_wr"] = round(_w10 / len(_l10) * 100) if _l10 else 0
            rolling_block["last20_wr"] = round(_w20 / len(_l20) * 100) if _l20 else 0
            rolling_block["last10_pts"] = round(_pts10, 1)
            _streak = 0
            for _t in _l10:
                if float(_t.get("pnl_pts", 0)) > 0:
                    _streak += 1
                else:
                    break
            if _streak == 0:
                for _t in _l10:
                    if float(_t.get("pnl_pts", 0)) <= 0:
                        _streak -= 1
                    else:
                        break
            rolling_block["streak"] = _streak
        except Exception:
            pass

        straddle_block = {
            "open": round(straddle_open, 1) if straddle_captured else 0,
            "captured": straddle_captured,
        }

        dashboard = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "version": D.VERSION,
            "mode": "PAPER" if D.PAPER_MODE else "LIVE",
            "market": {
                "spot": round(spot_ltp, 1),
                "atm": atm_strike,
                "locked_ce": _locked_ce_strike,
                "locked_pe": _locked_pe_strike,
                "dte": dte,
                "vix": round(vix_ltp, 1),
                "session": session,
                "regime": spot_3m.get("regime", ""),
                "bias": bias,
                "vwap": round(float(LEVELS._vwap_state.get("vwap", 0.0)), 2),
                "gap": round(float(LEVELS._vwap_state.get("gap", 0.0)), 1),
                "spot_ema9": spot_3m.get("ema9", 0),
                "spot_ema21": spot_3m.get("ema21", 0),
                "spot_spread": spot_3m.get("spread", 0),
                "spot_rsi": spot_3m.get("rsi", 0),
                "spot_adx_3m": round(float(spot_3m.get("adx", 0)), 1),
                "hourly_rsi": round(hourly_rsi, 1),
                "expiry": expiry.isoformat() if expiry else "",
                "market_open": D.is_market_open(),
                "indicators_warm": _is_warm,
            },
            "ce": ce_signal,
            "pe": pe_signal,
            "position": position,
            "today": today_block,
            "straddle": straddle_block,
            "account": {
                "name": D.get_account_info().get("name", ""),
                "balance": D.get_account_info().get("total_balance", 0),
                "used": D.get_account_info().get("used_margin", 0),
            },
            "rolling": rolling_block,
            "cooldown": {},
        }

        tmp = os.path.join(D.STATE_DIR, 'vrl_dashboard.json') + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dashboard, f, indent=2, default=str)
        os.replace(tmp, os.path.join(D.STATE_DIR, 'vrl_dashboard.json'))

    except Exception as e:
        logger.debug("[DASH] Snapshot write: " + str(e))


def _strategy_loop(kite):
    global _running
    today_str = date.today().isoformat()
    logger.info("[MAIN] Strategy loop started")
    os.makedirs(os.path.expanduser("~/state"), exist_ok=True)
    _cleanup_trade_log()
    try:
        D.compute_daily_bias(kite)
        logger.info("[MAIN] Daily bias: " + str(D.get_daily_bias()))
    except Exception as _be:
        logger.debug("[MAIN] Bias: " + str(_be))
    try:
        D.check_hourly_rsi(kite)
        logger.info("[MAIN] Hourly RSI: " + str(D.get_hourly_rsi()))
    except Exception as _he:
        logger.debug("[MAIN] H.RSI: " + str(_he))
    with _state_lock:
        state["_last_1min_candle"] = ""

    try:
        _now_startup = datetime.now()
        _startup_mins = _now_startup.hour * 60 + _now_startup.minute
        # Only check gap at true market open window (09:00–09:34).
        # Mid-day restarts must NOT compare prev_close to current intraday spot.
        if 540 <= _startup_mins < 574:
            _startup_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
            _prev_close = state.get("prev_close", 0)
            if _prev_close > 0 and _startup_spot > 0:
                _gap = abs(_startup_spot - _prev_close)
                _gap_threshold = CFG.get().get("strike", {}).get("gap_relock_threshold", 200)
                if _gap > _gap_threshold:
                    logger.info("[MAIN] GAP " + str(round(_gap)) + "pts — forcing strike relock at open")
                    _tg_send("🔔 <b>GAP OPEN</b> " + str(round(_gap)) + "pts — strikes will relock")
                    _reset_strike_lock()
        else:
            logger.info("[MAIN] Gap-open check skipped (mid-day restart at "
                        + _now_startup.strftime("%H:%M") + ")")
    except Exception:
        pass

    expiry = D.get_nearest_expiry(kite)

    try:
        _now = datetime.now()
        if expiry and _now.hour >= 9 and _now.minute >= 30:
            _ss = D.get_active_strike_step(D.calculate_dte(expiry))
            _sa = D.resolve_atm_strike(D.get_ltp(D.NIFTY_SPOT_TOKEN), _ss)
            if _sa > 0:
                D.capture_straddle(kite, _sa, expiry)
                logger.info("[MAIN] Straddle captured at startup")
    except Exception as _se:
        logger.debug("[MAIN] Straddle: " + str(_se))
    if expiry:
        logger.info("[MAIN] Expiry on startup: " + str(expiry))
    else:
        logger.warning("[MAIN] Expiry not resolved on startup — will retry in loop")

    while _running:
        try:
            now   = datetime.now()
            today = date.today()

            try:
                import time as _t_obs
                _now_epoch = _t_obs.time()
                _expired = []
                with _post_exit_lock:
                    _kept = []
                    for tok, expire_at in _post_exit_observation:
                        if _now_epoch >= expire_at:
                            _expired.append(tok)
                        else:
                            _kept.append((tok, expire_at))
                    _post_exit_observation[:] = _kept
                if _expired:
                    with _state_lock:
                        _active_token = state.get("token") or 0
                    _safe_to_drop = [t for t in _expired if t != _active_token]
                    try:
                        _lock_set = set()
                        if _locked_tokens:
                            for _v in _locked_tokens.values():
                                if isinstance(_v, dict):
                                    _tk = _v.get("token")
                                    if _tk:
                                        _lock_set.add(int(_tk))
                        with _state_lock:
                            for k in ("_locked_ce_token", "_locked_pe_token",
                                      "_locked_ce_token_2", "_locked_pe_token_2"):
                                _t = state.get(k)
                                if _t:
                                    _lock_set.add(int(_t))
                        _safe_to_drop = [t for t in _safe_to_drop if t not in _lock_set]
                    except Exception:
                        pass
                    if _safe_to_drop:
                        D.unsubscribe_tokens(_safe_to_drop)
                        logger.info(
                            "[POST_EXIT] Unsubscribed after observation: "
                            + str(_safe_to_drop)
                        )
            except Exception as _pe_err:
                logger.debug("[POST_EXIT] Cleanup error: " + str(_pe_err))

            if today.isoformat() != today_str:
                today_str = today.isoformat()
                _reset_daily(today_str)
                expiry = D.get_nearest_expiry(kite)
                if not expiry:
                    for _retry in range(5):
                        _wait = 2 ** (_retry + 1)
                        logger.warning('[MAIN] Expiry resolve failed, retry '
                                       + str(_retry + 1) + ' in ' + str(_wait) + 's')
                        time.sleep(_wait)
                        expiry = D.get_nearest_expiry(kite)
                        if expiry:
                            break
                if not expiry:
                    logger.critical('[MAIN] Cannot resolve expiry after 5 retries')
                    _tg_send('\U0001f6a8 <b>CRITICAL: Expiry resolution failed. Bot paused.</b>\nUse /resume after market opens.')
                    with _state_lock:
                        state['paused'] = True
                    time.sleep(60)
                    continue
            dte     = D.calculate_dte(expiry) if expiry else 0
            # Keep _v8_state expiry/dte in sync — entry/exit functions read from here
            try:
                with _v8_lock:
                    _v8_state["expiry"] = expiry.isoformat() if expiry else ""
                    _v8_state["dte"]    = dte
            except Exception:
                pass
            profile = {"conv_sl_pts": 12}
            session = D.get_session_block(now.hour, now.minute)
            spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)

            D.check_and_reconnect()

            # ── V8 tick-based exit: runs every 1-second scan cycle ──
            # Must be OUTSIDE the _is_new_1min_candle gate — exits need to
            # fire on every tick, not once per minute at candle close.
            _v8_check_exit()

            # ── V8 entry: scan every 10 seconds (outside 1-min gate) ──
            # BUG-16 fix: entry was gated to once-per-minute at :35s.
            # If candle turned green at :40s, bot missed it until next minute.
            # Now checks every 10s — same_candle_guard prevents double-entry.
            global _v8_last_entry_scan_ts
            _v8_force_exit_age = time.time() - float(_v8_state.get("_force_exit_ts", 0) or 0)
            _v8_in_force_cooldown = (_v8_force_exit_age < 180 and float(_v8_state.get("_force_exit_ts", 0) or 0) > 0)
            if (_v8_in_force_cooldown
                    and not _v8_state.get("in_trade")
                    and time.time() - _v8_last_entry_scan_ts >= 3):
                _v8_last_entry_scan_ts = time.time()
                logger.info(f"[REJECT-V8] force_exit_cooldown age={int(_v8_force_exit_age)}s — entries blocked 3 min after manual exit")
            if (not _v8_state.get("in_trade")
                    and not state.get("paused")
                    and D.is_trading_window(now)
                    and _locked_tokens
                    and not _v8_in_force_cooldown
                    and time.time() - _v8_last_entry_scan_ts >= 3):
                _v8_last_entry_scan_ts = time.time()
                try:
                    _v8_ce_info = (_locked_tokens or {}).get("CE", {})
                    _v8_pe_info = (_locked_tokens or {}).get("PE", {})
                    _v8_ce_tok  = int(_v8_ce_info.get("token", 0) or 0)
                    _v8_pe_tok  = int(_v8_pe_info.get("token", 0) or 0)
                    _v8_ce_gate_rejected = False
                    _v8_pe_gate_rejected = False
                    _v8_both_rej_ts = float(_v8_state.get("_v8_both_rejected_ts", 0) or 0)
                    _v8_in_both_cooldown = (_v8_both_rej_ts > 0 and time.time() - _v8_both_rej_ts < 60)
                    for _v8_dir, _v8_token, _v8_other in [
                        ("CE", _v8_ce_tok, _v8_pe_tok),
                        ("PE", _v8_pe_tok, _v8_ce_tok),
                    ]:
                        if not _v8_token:
                            continue
                        _v8_res = check_entry_v8(
                            token=_v8_token, option_type=_v8_dir,
                            spot_ltp=spot_ltp,
                            silent=False,
                            state=_v8_state, other_token=_v8_other)
                        # Store for dashboard display
                        _v8_res["_strike"] = (_v8_ce_info if _v8_dir == "CE" else _v8_pe_info).get(
                            "strike", _locked_ce_strike or _locked_pe_strike or 0)
                        _v9_last_results[_v8_dir] = _v8_res
                        if not _v8_res.get("fired"):
                            if _v8_dir == "CE": _v8_ce_gate_rejected = True
                            else: _v8_pe_gate_rejected = True
                            continue
                        if _v8_in_both_cooldown:
                            _age = round(time.time() - _v8_both_rej_ts)
                            logger.info(f"[REJECT-V8] {_v8_dir} both_sides_cooldown "
                                        f"age={_age}s — gates passed but blocked")
                            continue
                        _v8_state["_signals_today"] = int(_v8_state.get("_signals_today", 0)) + 1
                        _v8_state["_last_signal_time"] = now.strftime("%H:%M:%S")
                        _v8_strike = (_v8_ce_info if _v8_dir == "CE" else _v8_pe_info).get(
                            "strike", _locked_ce_strike or _locked_pe_strike or 0)
                        _v8_symbol = (_v8_ce_info if _v8_dir == "CE" else _v8_pe_info).get("symbol", "")
                        _v8_execute_paper_entry(
                            direction=_v8_dir, strike=_v8_strike,
                            symbol=_v8_symbol, token=_v8_token,
                            entry_price=_v8_res["entry_price"],
                            entry_result=_v8_res, other_token=_v8_other)
                        break
                    if _v8_ce_gate_rejected and _v8_pe_gate_rejected:
                        if not _v8_in_both_cooldown:
                            _v8_state["_v8_both_rejected_ts"] = time.time()
                            if _v8_both_rej_ts == 0:
                                logger.info("[V8] both_sides_cooldown ARMED — both CE+PE failed (1 min block)")
                            else:
                                logger.info("[V8] both_sides_cooldown RE-ARMED — both CE+PE failed again")
                except Exception as _v8e:
                    import traceback as _v8tb
                    logger.warning("[V8] entry scan error: " + str(_v8e) + "\n" + _v8tb.format_exc())


            # ── V2 EXIT + DYNAMIC TRAIL (A/B comparison — no real trades) ──
            # P1-V2: standard ladder below peak 15, then entry+15→+1/5s ratchet, hard exit +40
            # P2-V2: same ratchet as P1-V2 — standard ladder below peak 15, entry+15→+1/5s, hard exit +40
            # NOTE: uses is_market_open() so EOD exits at 15:15 fire correctly
            global _v8_shadow_dt_v2, _v8_shadow_p2_v2
            if D.is_market_open():
                # P1 V2
                for _v2_dir in ("CE", "PE"):
                    _v2d = _v8_shadow_dt_v2[_v2_dir]
                    if not _v2d.get("active"):
                        continue
                    _v2_tok   = int(_v2d.get("entry_tok", 0) or 0)
                    _v2_entry = float(_v2d.get("entry_price", 0))
                    _v2_sl    = float(_v2d.get("shadow_sl", round(_v2_entry - 12, 1)))
                    _v2_ltp   = D.get_ltp(_v2_tok) if _v2_tok else 0
                    if not _v2_ltp:
                        continue
                    # Update peak
                    _v2_pk_px  = max(float(_v2d.get("peak_price", _v2_entry)), _v2_ltp)
                    _v2_pk_pts = round(_v2_pk_px - _v2_entry, 1)
                    _v2d["peak_price"] = _v2_pk_px
                    _v2d["peak_pts"]   = _v2_pk_pts
                    # Check exits
                    _v2_reason = _v2_exit_px = None
                    if _v2_ltp >= _v2_entry + 40:
                        _v2_reason, _v2_exit_px = "TARGET+40", round(_v2_entry + 40, 1)
                    elif now.time() >= dtime(15, 15):
                        _v2_reason, _v2_exit_px = "EOD", (_v2_ltp if _v2_ltp > 0 else _v2_entry)
                    elif _v2_ltp <= _v2_sl:
                        _v2_reason, _v2_exit_px = "SL-HIT", _v2_sl
                    if _v2_reason:
                        _v2_pnl = round(_v2_exit_px - _v2_entry, 1)
                        logger.info(f"[SHADOW-P1-V2] {_v2_dir} {_v2_reason} "
                                    f"entry={_v2_entry} exit={_v2_exit_px:.1f} "
                                    f"pnl={_v2_pnl:+.1f} peak=+{_v2_pk_pts:.1f}")
                        _v2d.update({"active": False, "entry_price": 0.0, "entry_time": "",
                                     "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0})
                        _save_shadow_state()
                        continue
                    # Trail: ratchet after peak ≥ 15 — first tick locks entry+15, then +1 every 5s
                    # Tick 1 (SL < entry+15): jump to entry+15 (no +1, avoids immediate exit)
                    # Tick 2+: SL += 1 each 5s tick
                    if _v2_pk_pts >= 15 and time.time() - _v2d.get("dyn_trail_ts", 0) >= 5:
                        _v2d["dyn_trail_ts"] = time.time()
                        if _v2_sl < _v2_entry + 15:
                            _v2_new_sl = round(_v2_entry + 15, 1)
                        else:
                            _v2_new_sl = round(_v2_sl + 1, 1)
                        if _v2_new_sl > _v2_sl:
                            _v2d["shadow_sl"] = _v2_new_sl
                            logger.info(f"[SHADOW-P1-V2] {_v2_dir} dyn_trail "
                                        f"sl={_v2_new_sl:.1f} ltp={_v2_ltp:.1f} "
                                        f"peak=+{_v2_pk_pts:.1f}")
                    elif _v2_pk_pts < 15:
                        _v2_std_sl, _ = _shadow_trail_sl(_v2_entry, _v2_pk_pts)
                        if _v2_std_sl > _v2_sl:
                            _v2d["shadow_sl"] = _v2_std_sl
                # P2 V2 — same ratchet as P1-V2: standard ladder below peak 15,
                # then entry+15 on first tick, +1 every 5s after. Hard exit TARGET+40.
                for _v2p2_dir in ("CE", "PE"):
                    _v2p2d = _v8_shadow_p2_v2[_v2p2_dir]
                    if not _v2p2d.get("active"):
                        continue
                    _v2p2_tok   = int(_v2p2d.get("entry_tok", 0) or 0)
                    _v2p2_entry = float(_v2p2d.get("entry_price", 0))
                    _v2p2_sl    = float(_v2p2d.get("shadow_sl", round(_v2p2_entry - 12, 1)))
                    _v2p2_ltp   = D.get_ltp(_v2p2_tok) if _v2p2_tok else 0
                    if not _v2p2_ltp:
                        continue
                    # Update peak
                    _v2p2_pk_px  = max(float(_v2p2d.get("peak_price", _v2p2_entry)), _v2p2_ltp)
                    _v2p2_pk_pts = round(_v2p2_pk_px - _v2p2_entry, 1)
                    _v2p2d["peak_price"] = _v2p2_pk_px
                    _v2p2d["peak_pts"]   = _v2p2_pk_pts
                    # Check exits
                    _v2p2_reason = _v2p2_exit_px = None
                    if _v2p2_ltp >= _v2p2_entry + 40:
                        _v2p2_reason, _v2p2_exit_px = "TARGET+40", round(_v2p2_entry + 40, 1)
                    elif now.time() >= dtime(15, 15):
                        _v2p2_reason, _v2p2_exit_px = "EOD", (_v2p2_ltp if _v2p2_ltp > 0 else _v2p2_entry)
                    elif _v2p2_ltp <= _v2p2_sl:
                        _v2p2_reason, _v2p2_exit_px = "SL-HIT", _v2p2_sl
                    if _v2p2_reason:
                        _v2p2_pnl = round(_v2p2_exit_px - _v2p2_entry, 1)
                        logger.info(f"[SHADOW-P2-V2] {_v2p2_dir} {_v2p2_reason} "
                                    f"entry={_v2p2_entry} exit={_v2p2_exit_px:.1f} "
                                    f"pnl={_v2p2_pnl:+.1f} peak=+{_v2p2_pk_pts:.1f}")
                        _v2p2d.update({"active": False, "entry_price": 0.0, "entry_time": "",
                                       "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0,
                                       "dyn_trail_ts": 0.0})
                        _save_shadow_state()
                        continue
                    # Ratchet: peak ≥ 15 → first tick locks entry+15, then +1 every 5s
                    if _v2p2_pk_pts >= 15 and time.time() - _v2p2d.get("dyn_trail_ts", 0) >= 5:
                        _v2p2d["dyn_trail_ts"] = time.time()
                        if _v2p2_sl < _v2p2_entry + 15:
                            _v2p2_new_sl = round(_v2p2_entry + 15, 1)
                        else:
                            _v2p2_new_sl = round(_v2p2_sl + 1, 1)
                        if _v2p2_new_sl > _v2p2_sl:
                            _v2p2d["shadow_sl"] = _v2p2_new_sl
                            logger.info(f"[SHADOW-P2-V2] {_v2p2_dir} dyn_trail "
                                        f"sl={_v2p2_new_sl:.1f} ltp={_v2p2_ltp:.1f} "
                                        f"peak=+{_v2p2_pk_pts:.1f}")
                    elif _v2p2_pk_pts < 15:
                        _v2p2_std_sl, _ = _shadow_trail_sl(_v2p2_entry, _v2p2_pk_pts)
                        if _v2p2_std_sl > _v2p2_sl:
                            _v2p2d["shadow_sl"] = _v2p2_std_sl

            # ── BW-SCAN: log EMA9 band data every new 1-min candle ──
            # Fires once per minute (second >= 35). Shows 1m + 3m band for CE + PE.
            # Positions: ABOVE (close > ema9h) | INSIDE (ema9l <= close <= ema9h) | BELOW (close < ema9l)
            global _bw_scan_last_bucket
            _bw_now_key = now.strftime("%Y%m%d%H%M")
            if (D.is_trading_window(now) and _locked_tokens
                    and _bw_scan_last_bucket != _bw_now_key and now.second >= 35):
                _bw_scan_last_bucket = _bw_now_key
                try:
                    _bw_parts = []
                    for _bw_dir, _bw_info in [("CE", (_locked_tokens or {}).get("CE", {})),
                                               ("PE", (_locked_tokens or {}).get("PE", {}))]:
                        _bw_tok = int(_bw_info.get("token", 0) or 0)
                        if not _bw_tok:
                            continue
                        _bw_1m = D.get_option_1min(_bw_tok, 10)
                        _bw_3m = D.get_option_3min(_bw_tok, lookback=5)
                        _1m_str = _3m_str = "n/a"
                        if _bw_1m is not None and len(_bw_1m) >= 2:
                            _bwr = _bw_1m.iloc[-2]
                            _bwc  = float(_bwr["close"])
                            _bwel = float(_bwr.get("ema9_low", 0))
                            _bweh = float(_bwr.get("ema9_high", 0))
                            _bwbw = round(_bweh - _bwel, 1) if _bweh and _bwel else 0
                            _bwgp = round(_bwc - _bweh, 1) if _bweh else 0
                            _bwpos = "ABOVE" if _bwc > _bweh else ("INSIDE" if _bwc >= _bwel else "BELOW")
                            _1m_str = (f"c={_bwc:.1f} el={_bwel:.1f} eh={_bweh:.1f} "
                                       f"bw={_bwbw:.1f} gap={_bwgp:+.1f} [{_bwpos}]")
                        if _bw_3m is not None and len(_bw_3m) >= 2:
                            _bwr3 = _bw_3m.iloc[-2]
                            _bwc3  = float(_bwr3["close"])
                            _bwel3 = float(_bwr3.get("ema9_low", 0))
                            _bweh3 = float(_bwr3.get("ema9_high", 0))
                            _bwbw3 = round(_bweh3 - _bwel3, 1) if _bweh3 and _bwel3 else 0
                            _bwgp3 = round(_bwc3 - _bweh3, 1) if _bweh3 else 0
                            _bwpos3 = "ABOVE" if _bwc3 > _bweh3 else ("INSIDE" if _bwc3 >= _bwel3 else "BELOW")
                            _3m_str = (f"c={_bwc3:.1f} el={_bwel3:.1f} eh={_bweh3:.1f} "
                                       f"bw={_bwbw3:.1f} gap={_bwgp3:+.1f} [{_bwpos3}]")
                        _bw_parts.append(f"  {_bw_dir}  1m: {_1m_str}  ||  3m: {_3m_str}")
                    if _bw_parts:
                        logger.info(f"[BW-SCAN] {now.strftime('%H:%M')}\n" + "\n".join(_bw_parts))
                except Exception as _bwe:
                    logger.debug(f"[BW-SCAN] error: {_bwe}")

            # ── SHADOW: 1-min entry tracker (data collection, NO live trades) ──
            # Signal: 1-min close > EMA9_high + RSI 48-70 rising + close > 1-min VWAP
            # Both CE and PE tracked independently. Bucket = 1-min candle ts.
            global _v8_shadow_dt, _v8_shadow_p2

            # ── EOD/SL safety: close active signals even if _locked_tokens not yet set ──
            # Handles late restart case where strike lock hasn't happened yet
            # NOTE: uses is_market_open() (not is_trading_window) so EOD exit at 15:15 fires
            if D.is_market_open():
                for _sd_early, _sd_label_e in [(_v8_shadow_dt, "P1"), (_v8_shadow_p2, "P2")]:
                    for _sdir_e in ("CE", "PE"):
                        _sds_e = _sd_early[_sdir_e]
                        if not _sds_e.get("active"):
                            continue
                        _stok_e   = int(_sds_e.get("entry_tok", 0) or 0)
                        _sep_e    = float(_sds_e.get("entry_price", 0) or 0)
                        _ssl_e    = float(_sds_e.get("shadow_sl", round(_sep_e - 12, 1)) or round(_sep_e - 12, 1))
                        _sltp_e   = D.get_ltp(_stok_e) if _stok_e else 0
                        _speak_e  = float(_sds_e.get("peak_pts", 0) or 0)
                        _slvl_e   = _sds_e.get("shadow_level", "INITIAL")
                        _close_e  = None
                        if now.time() >= dtime(15, 15):
                            _close_e = ("EOD", _sltp_e if _sltp_e > 0 else _sep_e)
                        elif _sltp_e > 0 and _sltp_e <= _ssl_e:
                            _close_e = ("SL-HIT", _ssl_e)
                        if _close_e:
                            _reason_e, _exit_e = _close_e
                            _pnl_e = round(_exit_e - _sep_e, 1)
                            _icon_e = "✅" if _pnl_e >= 20 else ("🟡" if _pnl_e > 0 else "❌")
                            logger.info(f"[SHADOW-{_sd_label_e}] {_sdir_e} {_reason_e} "
                                        f"entry={_sep_e} exit={_exit_e:.1f} "
                                        f"pnl={_pnl_e:+.1f} peak=+{_speak_e:.1f} trail={_slvl_e}")
                            _tg_send(
                                f"{'🔵' if _sd_label_e == 'P1' else '🟡'} "
                                f"SHADOW {_sd_label_e} {_sdir_e} — {_reason_e}\n"
                                f"Entry: {_sep_e:.1f}  Exit: {_exit_e:.1f}\n"
                                f"PnL: {_icon_e} {_pnl_e:+.1f}  Peak: +{_speak_e:.1f}\n"
                                f"Trail reached: {_slvl_e}\n"
                                f"<i>⚠️ Shadow only</i>"
                            )
                            _upd_e = {
                                "active": False, "entry_price": 0.0, "entry_time": "",
                                "peak_price": 0.0, "peak_pts": 0.0,
                                "shadow_sl": 0.0, "shadow_level": "INITIAL",
                                "last_exit_pnl": _pnl_e, "last_exit_reason": _reason_e,
                                "last_exit_ts": time.time(),
                            }
                            # BUG-A fix: set sl_ts on P1 SL-HIT so cooldown blocks re-entry 60s
                            if _reason_e == "SL-HIT" and _sd_label_e == "P1":
                                _upd_e["sl_ts"] = time.time()
                            # P2 exit cooldown: set exit_ts on ANY P2 exit (SL-HIT, trail, EOD)
                            # Blocks P2 re-entry in same direction for 120s
                            if _sd_label_e == "P2":
                                _upd_e["exit_ts"] = time.time()
                            _sds_e.update(_upd_e)
                            _save_shadow_state()

            if (not _v8_state.get("in_trade")
                    and D.is_trading_window(now)
                    and _locked_tokens
                    and time.time() - _v8_shadow_dt["last_scan_ts"] >= 3):
                _v8_shadow_dt["last_scan_ts"] = time.time()
                try:
                    for _sh_dir, _sh_info in [("CE", (_locked_tokens or {}).get("CE", {})),
                                               ("PE", (_locked_tokens or {}).get("PE", {}))]:
                        _sh_tok = int(_sh_info.get("token", 0) or 0)
                        if not _sh_tok:
                            continue

                        # ── 1-min PRIMARY: last completed 1-min candle ──
                        _sh_1m = D.get_option_1min(_sh_tok, 100)   # full session for VWAP
                        if _sh_1m is None or len(_sh_1m) < 4:
                            continue
                        _sh_1m_comp   = _sh_1m.iloc[-2]   # last completed 1-min candle
                        _sh_1m_bk_ts  = str(_sh_1m_comp.name)
                        _sh_1m_close  = float(_sh_1m_comp["close"])
                        _sh_1m_open   = float(_sh_1m_comp["open"])
                        _sh_ema9h_1m  = float(_sh_1m_comp.get("ema9_high", 0))
                        _sh_ema9l_1m  = float(_sh_1m_comp.get("ema9_low", 0))
                        _sh_rsi_1m    = float(_sh_1m_comp.get("RSI", 0) or 0)
                        _sh_rsi_1m_p  = float(_sh_1m.iloc[-3].get("RSI", 0) or 0)
                        _sh_ema9l_1m_prev = float(_sh_1m.iloc[-3].get("ema9_low", 0))

                        # 1-min session VWAP (cumulative from 9:15, resets daily)
                        _sh_1m_day = _sh_1m[_sh_1m.index.date == now.date()].copy()
                        if len(_sh_1m_day) < 3:
                            continue
                        _sh_1m_day["_typ"] = (_sh_1m_day["high"] + _sh_1m_day["low"] + _sh_1m_day["close"]) / 3.0
                        _sh_1m_day["_tv"]  = _sh_1m_day["_typ"] * _sh_1m_day["volume"]
                        _sh_cum_vol = _sh_1m_day["volume"].cumsum().replace(0, np.nan)
                        _sh_1m_vwap = float((_sh_1m_day["_tv"].cumsum() / _sh_cum_vol).iloc[-2])

                        _sh_ds = _v8_shadow_dt[_sh_dir]  # per-direction state

                        # Bucket change: update bucket_ts only — DO NOT reset active signal
                        # Signal tracks until SL hit or EOD, independent of candle boundaries
                        if _sh_ds["bucket_ts"] != _sh_1m_bk_ts:
                            _sh_ds["bucket_ts"] = _sh_1m_bk_ts

                        # If signal active — track LTP using ORIGINAL token (not current ATM)
                        if _sh_ds["active"]:
                            _sh_track_tok = int(_sh_ds.get("entry_tok", 0) or _sh_tok)
                            _sh_ltp_pk  = D.get_ltp(_sh_track_tok)
                            if not _sh_ltp_pk:
                                continue   # LTP unavailable, skip this cycle
                            _sh_cur_sl  = _sh_ds.get("shadow_sl", round(_sh_ds["entry_price"] - 12, 1))
                            _sh_entry   = _sh_ds["entry_price"]

                            def _sh_close_signal(reason, exit_px):
                                _fin_peak = _sh_ds["peak_pts"]
                                _fin_lvl  = _sh_ds.get("shadow_level", "INITIAL")
                                _fin_pnl  = round(exit_px - _sh_entry, 1)
                                _fin_icon = "✅" if _fin_pnl >= 20 else ("🟡" if _fin_pnl > 0 else "❌")
                                _fin_msg  = (
                                    f"🔵 SHADOW P1 {_sh_dir} — {reason}\n"
                                    f"Entry: {_sh_entry:.1f}  Exit: {exit_px:.1f}\n"
                                    f"PnL: {_fin_icon} {_fin_pnl:+.1f}  Peak: +{_fin_peak:.1f}\n"
                                    f"Trail reached: {_fin_lvl}\n"
                                )
                                _p2_e2 = _v8_shadow_p2[_sh_dir].get("today_entry", 0.0)
                                _p2_d2 = _v8_shadow_p2[_sh_dir].get("today_date", "")
                                if _p2_e2 > 0 and _p2_d2 == str(now.date()):
                                    _sv2 = round(_sh_entry - _p2_e2, 1)
                                    _fin_msg += f"P2 at {_p2_e2:.1f} → P1 saved {_sv2:+.1f}pts\n"
                                if _sh_ds["live_entry"] > 0:
                                    _sv3 = round(_sh_ds["live_entry"] - _sh_entry, 1)
                                    _fin_msg += f"vs V9: {_sh_ds['live_entry']:.1f}  diff={_sv3:+.1f}pts\n"
                                _fin_msg += f"<i>⚠️ Shadow only</i>"
                                logger.info(
                                    f"[SHADOW-P1] {_sh_dir} {reason} "
                                    f"entry={_sh_entry} exit={exit_px:.1f} "
                                    f"pnl={_fin_pnl:+.1f} peak=+{_fin_peak:.1f} trail={_fin_lvl}"
                                )
                                _tg_send(_fin_msg)
                                # Track peak for analysis streak detection
                                _shadow_analysis[_sh_dir]["last_peaks"].append(_fin_peak)
                                _shadow_analysis[_sh_dir]["last_peaks"] = \
                                    _shadow_analysis[_sh_dir]["last_peaks"][-2:]
                                _sh_ds.update({
                                    "active": False, "entry_price": 0.0, "entry_time": "",
                                    "peak_price": 0.0, "peak_pts": 0.0, "live_entry": 0.0,
                                    "shadow_sl": 0.0, "shadow_level": "INITIAL",
                                    "bucket_ts": _sh_1m_bk_ts,  # block re-fire on same candle
                                    "sl_ts": time.time() if reason == "SL-HIT" else _sh_ds.get("sl_ts", 0.0),
                                    "last_exit_pnl": _fin_pnl, "last_exit_reason": reason,
                                    "last_exit_ts": time.time(),
                                })
                                _save_shadow_state()

                            # EOD check
                            if now.time() >= dtime(15, 15):
                                _sh_close_signal("EOD", _sh_ltp_pk)
                                continue

                            # SL hit check (LTP touches or goes below trail SL)
                            if _sh_ltp_pk <= _sh_cur_sl:
                                _sh_close_signal("SL-HIT", _sh_cur_sl)
                                continue

                            # Update peak + trail ladder
                            if _sh_ltp_pk > _sh_ds["peak_price"]:
                                _sh_ds["peak_price"] = _sh_ltp_pk
                                _sh_ds["peak_pts"]   = round(_sh_ltp_pk - _sh_entry, 1)
                                _new_sl, _new_lvl = _shadow_trail_sl(_sh_entry, _sh_ds["peak_pts"])
                                _old_lvl = _sh_ds.get("shadow_level", "INITIAL")
                                if _new_lvl != _old_lvl:
                                    _sh_ds["shadow_sl"]    = _new_sl
                                    _sh_ds["shadow_level"] = _new_lvl
                                    logger.info(
                                        f"[SHADOW-P1] {_sh_dir} trail ↑ {_new_lvl} "
                                        f"peak=+{_sh_ds['peak_pts']:.1f} sl_now={_new_sl:.1f}"
                                    )
                                    _tg_send(
                                        f"🔵 SHADOW P1 {_sh_dir} — trail ↑ {_new_lvl}\n"
                                        f"Peak: +{_sh_ds['peak_pts']:.1f} | SL now: {_new_sl:.1f}\n"
                                        f"<i>⚠️ Shadow only</i>"
                                    )
                                    _save_shadow_state()
                            continue

                        # ── 1-min: close > EMA9_high + RSI filter + above VWAP ──
                        _sh_1m_gap     = round(_sh_1m_close - _sh_ema9h_1m, 2)
                        _sh_vwap_gap   = round(_sh_1m_close - _sh_1m_vwap, 2)
                        _sh_1m_reject  = None
                        if not (_sh_ema9h_1m > 0 and _sh_1m_close > _sh_ema9h_1m):
                            _sh_1m_reject = f"1m_below_ema9h close={_sh_1m_close} ema9h={_sh_ema9h_1m} gap={_sh_1m_gap}"
                        elif _sh_1m_gap < 2.0:
                            _sh_1m_reject = f"1m_ema9h_gap_weak gap={_sh_1m_gap:.2f}(need>=2.0)"
                        elif not (_sh_rsi_1m > _sh_rsi_1m_p):
                            _sh_1m_reject = f"1m_rsi_falling rsi={_sh_rsi_1m:.1f} prev={_sh_rsi_1m_p:.1f}"
                        elif not (48 < _sh_rsi_1m < 70):
                            _sh_1m_reject = f"1m_rsi_outofrange rsi={_sh_rsi_1m:.1f}"
                        elif not (_sh_1m_close > _sh_1m_vwap):
                            _sh_1m_reject = f"1m_below_vwap close={_sh_1m_close:.1f} vwap={_sh_1m_vwap:.1f} gap={_sh_vwap_gap}"
                        if _sh_1m_reject:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-DTF] REJECT {_sh_dir} {_sh_1m_reject}")
                            # Detailed RSI-block log — once per candle bucket
                            if 'rsi' in _sh_1m_reject and _sh_ds.get("_rsi_block_ts") != _sh_1m_bk_ts:
                                _sh_ds["_rsi_block_ts"] = _sh_1m_bk_ts
                                _sh_bw_rb   = round(_sh_ema9h_1m - _sh_ema9l_1m, 1)
                                _sh_str_rb  = int(_sh_info.get("strike", 0) or 0)
                                logger.info(
                                    f"[RSI-SHADOW] {_sh_dir} {_sh_str_rb} BLOCKED "
                                    f"entry={_sh_1m_close:.1f} ema9h_gap={_sh_1m_gap:+.2f} bw={_sh_bw_rb} "
                                    f"vwap={_sh_1m_vwap:.1f} gap_vwap={_sh_vwap_gap:+.2f} "
                                    f"rsi={_sh_rsi_1m:.1f} reason={_sh_1m_reject.split()[0]}"
                                )
                            continue

                        # ── Cooldown gates (no trade impact — reject only) ──
                        # 1. ATM relock cooldown: EMA9H of new strike not settled yet
                        _sh_relock_age = time.time() - _v8_shadow_dt.get("relock_ts", 0)
                        if 0 < _sh_relock_age < 120:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-P1] REJECT {_sh_dir} relock_cooldown age={int(_sh_relock_age)}s")
                            continue
                        # 2. Post-SL cooldown: 1 min after SL-HIT, EMA9H still distorted
                        _sh_sl_age = time.time() - _sh_ds.get("sl_ts", 0)
                        if 0 < _sh_sl_age < 60:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-P1] REJECT {_sh_dir} sl_cooldown age={int(_sh_sl_age)}s")
                            continue

                        # ── FIRE ──
                        # Gate: XLEG_CONFIRMED — cross-leg must be below EMA9H all last 5 scans
                        _xleg_g_dir  = "PE" if _sh_dir == "CE" else "CE"
                        _xleg_g_buf  = _shadow_analysis[_xleg_g_dir].get("cross_buf", [])
                        _xleg_g_buf5 = _xleg_g_buf[-5:]
                        _xleg_g_ok   = len(_xleg_g_buf5) >= 3 and all(not v for v in _xleg_g_buf5)
                        if not _xleg_g_ok:
                            logger.info(
                                f"[SHADOW-P1] REJECT {_sh_dir} xleg_not_confirmed "
                                f"{_xleg_g_dir} buf={_xleg_g_buf5} n={len(_xleg_g_buf5)}"
                            )
                            continue
                        _sh_ltp    = D.get_ltp(_sh_tok)
                        # Gate: LTP must still be above VWAP at fire time
                        # Candle close may have been above VWAP but LTP can slip below by signal time
                        if _sh_ltp and _sh_ltp < _sh_1m_vwap:
                            logger.info(
                                f"[SHADOW-P1] REJECT {_sh_dir} ltp_slipped_below_vwap "
                                f"ltp={_sh_ltp:.1f} vwap={_sh_1m_vwap:.1f} "
                                f"slip={round(_sh_ltp - _sh_1m_vwap, 1)}"
                            )
                            continue
                        _sh_strike = int(_sh_info.get("strike", 0) or 0)
                        _sh_sl     = round(_sh_ltp - 12, 1)
                        _sh_ds.update({
                            "active": True, "bucket_ts": _sh_1m_bk_ts,
                            "entry_price": _sh_ltp, "entry_time": now.strftime("%H:%M:%S"),
                            "peak_price": _sh_ltp, "peak_pts": 0.0,
                            "shadow_sl": round(_sh_ltp - 12, 1), "shadow_level": "INITIAL",
                            "today_entry": _sh_ltp, "today_date": str(now.date()),
                            "entry_tok": _sh_tok, "entry_strike": _sh_strike,
                            "sl_ts": 0.0,  # clear stale cooldown/outcome from prior trade
                            "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0,
                        })
                        # V2 tracker: same entry, new exit (dynamic trail + hard exit +40)
                        _v8_shadow_dt_v2[_sh_dir].update({
                            "active": True, "entry_price": _sh_ltp,
                            "entry_time": now.strftime("%H:%M:%S"),
                            "peak_price": _sh_ltp, "peak_pts": 0.0,
                            "shadow_sl": round(_sh_ltp - 12, 1), "entry_tok": _sh_tok,
                        })
                        # Check if Part 2 fired earlier today → compute saved pts
                        _p2_ds      = _v8_shadow_p2[_sh_dir]
                        _p2_today   = _p2_ds.get("today_entry", 0.0)
                        _p2_date    = _p2_ds.get("today_date", "")
                        _p2_line    = ""
                        if _p2_today > 0 and _p2_date == str(now.date()):
                            _p2_saved = round(_sh_ltp - _p2_today, 1)
                            _p2_ds["p1_entry"] = _sh_ltp   # store P1 entry in P2 state
                            _p2_line = (f"P2 entered: {_p2_today:.1f} → "
                                        f"saved {_p2_saved:+.1f} pts\n")
                            logger.info(
                                f"[SHADOW-P1] {_sh_dir} P2 was at {_p2_today} "
                                f"P1 now {_sh_ltp} saved={_p2_saved:+.1f}pts"
                            )
                        _sh_bw = round(_sh_ema9h_1m - _sh_ema9l_1m, 1)
                        logger.info(
                            f"[SHADOW-P1] {_sh_dir} {_sh_strike} SIGNAL "
                            f"entry={_sh_ltp} sl={_sh_sl} "
                            f"ema9h_gap={_sh_1m_gap:+.2f} bw={_sh_bw} "
                            f"vwap={_sh_1m_vwap:.1f} gap_vwap={_sh_vwap_gap:+.2f} rsi={_sh_rsi_1m:.1f}↑"
                        )
                        # CROSS-TRADE: check if P2 is open in opposite direction
                        _cross_opp = "PE" if _sh_dir == "CE" else "CE"
                        _cross_p2  = _v8_shadow_p2[_cross_opp]
                        if _cross_p2.get("active") and _cross_p2.get("today_date") == str(now.date()):
                            logger.info(
                                f"[CROSS-TRADE] P1-{_sh_dir} just fired vs P2-{_cross_opp} already open "
                                f"p1_entry={_sh_ltp} p2_entry={_cross_p2['entry_price']:.1f} "
                                f"p2_peak={_cross_p2.get('peak_pts',0):.1f} strike={_sh_strike}"
                            )
                        # DELAY-ANALYSIS: track LTP + spot at +5s/+10s/+30s/+60s
                        _delay_jobs.append({
                            "label": f"P1-{_sh_dir}", "strike": _sh_strike,
                            "base": _sh_ltp, "tok": _sh_tok,
                            "spot_base": D.get_ltp(D.NIFTY_SPOT_TOKEN) or 0.0,
                            "fire_ts": time.time(),
                            "snaps":       {5: None, 10: None, 30: None, 60: None},
                            "spot_snaps":  {5: None, 10: None, 30: None, 60: None},
                        })
                        _tg_send(
                            f"🔵 <b>SHADOW P1 — {_sh_dir} {_sh_strike}</b>\n"
                            f"Entry: {_sh_ltp:.1f}  SL: {_sh_sl:.1f}\n"
                            f"EMA9H: {_sh_1m_gap:+.1f}  VWAP: {_sh_vwap_gap:+.1f}  RSI: {_sh_rsi_1m:.0f}↑\n"
                            + (_p2_line if _p2_line else "") +
                            f"─── Shadow Trail ───\n"
                            f"@+12→lock+4  @+18→lock+10  @+24→lock+12\n"
                            f"@+30→lock+20  @+36→lock+30\n"
                            f"@+40→lock+36  @+50→lock+50\n"
                            f"<i>⚠️ Shadow only — no real trade</i>"
                        )
                        _save_shadow_state()
                        # ── Analysis flags (no trade impact) ──
                        _other_sh_dir = "PE" if _sh_dir == "CE" else "CE"
                        _other_sh_vwap_gap = None
                        try:
                            _other_sh_info = (_locked_tokens or {}).get(_other_sh_dir, {})
                            _other_sh_tok2 = int(_other_sh_info.get("token", 0) or 0)
                            if _other_sh_tok2:
                                _other_sh_1m2 = D.get_option_1min(_other_sh_tok2, 5)
                                if _other_sh_1m2 is not None and len(_other_sh_1m2) >= 2:
                                    _osh_day2 = _other_sh_1m2[_other_sh_1m2.index.date == now.date()]
                                    if len(_osh_day2) >= 2:
                                        _osh_tv2 = ((_osh_day2["high"]+_osh_day2["low"]+_osh_day2["close"])/3)*_osh_day2["volume"]
                                        _osh_vwap2 = float((_osh_tv2.cumsum()/_osh_day2["volume"].cumsum().replace(0,float('nan'))).iloc[-2])
                                        _other_sh_vwap_gap = round(_osh_day2["close"].iloc[-2] - _osh_vwap2, 1)
                        except Exception:
                            pass
                        _xleg_sh_dir = "PE" if _sh_dir == "CE" else "CE"
                        _log_shadow_analysis(
                            "P1", _sh_dir, now, _sh_ltp,
                            _sh_vwap_gap, _other_sh_vwap_gap,
                            float(spot_3m.get("adx", 0)),
                            _shadow_analysis[_sh_dir]["last_peaks"],
                            ema9h_gap=_sh_1m_gap,
                            xleg_buf=_shadow_analysis[_xleg_sh_dir]["cross_buf"],
                            dte=dte,
                            fut_vwap_gap=float(LEVELS._vwap_state.get("gap", 0.0)),
                            spot_ema9=float(spot_3m.get("ema9", 0)),
                            spot_ema21=float(spot_3m.get("ema21", 0)),
                            bw=_sh_bw,
                        )
                        # PDH/PDL/Pivot/VWAP filter data for this P1 shadow signal
                        try:
                            LEVELS.log_entry(
                                direction=_sh_dir,
                                strike=int(_sh_strike or 0),
                                entry_price=float(_sh_ltp),
                                spot_px=float(D.get_ltp(D.NIFTY_SPOT_TOKEN) or 0),
                                entry_time_dt=now,
                                dte=dte,
                            )
                        except Exception as _lvl_sh_e:
                            logger.debug(f"[SHADOW-LVL] P1 hook error: {_lvl_sh_e}")
                        # ── signal_scans DB record ──
                        try:
                            import VRL_DB as _SC
                            _SC.insert_scan({
                                "timestamp": now.isoformat(),
                                "session": "P1",
                                "dte": dte,
                                "atm_strike": int(_sh_strike or 0),
                                "spot": float(D.get_ltp(D.NIFTY_SPOT_TOKEN) or 0),
                                "direction": _sh_dir,
                                "entry_price": float(_sh_ltp),
                                "ema9_high": float(_sh_ema9h_1m),
                                "ema9_low": float(_sh_ema9l_1m),
                                "band_position": "ABOVE",
                                "body_pct": float(_sh_1m_comp.get("body_pct", 0) or 0),
                                "spot_rsi_3m": float(spot_3m.get("rsi", 0)),
                                "spot_ema_spread_3m": float(spot_3m.get("spread", 0)),
                                "spot_regime": str(spot_3m.get("regime", "")),
                                "vix": float(D.get_vix()),
                                "fired": "1",
                                "trade_taken": 0,
                                "reject_reason": "",
                            })
                        except Exception as _sc_e:
                            logger.debug(f"[SHADOW-SCAN] P1 DB insert error: {_sc_e}")
                except Exception as _she:
                    logger.warning(f"[SHADOW-P1] error: {_she}")

            # ── SHADOW Part 2: buildup tracker (close > EMA9H, close < VWAP, RSI > 55 rising) ──
            if (not _v8_state.get("in_trade")
                    and D.is_trading_window(now)
                    and _locked_tokens
                    and time.time() - _v8_shadow_p2["last_scan_ts"] >= 3):
                _v8_shadow_p2["last_scan_ts"] = time.time()
                try:
                    for _s2_dir, _s2_info in [("CE", (_locked_tokens or {}).get("CE", {})),
                                               ("PE", (_locked_tokens or {}).get("PE", {}))]:
                        _s2_tok = int(_s2_info.get("token", 0) or 0)
                        if not _s2_tok:
                            continue

                        # ── 1-min last completed candle ──
                        _s2_1m = D.get_option_1min(_s2_tok, 100)
                        if _s2_1m is None or len(_s2_1m) < 4:
                            continue
                        _s2_comp     = _s2_1m.iloc[-2]
                        _s2_bk_ts    = str(_s2_comp.name)
                        _s2_close    = float(_s2_comp["close"])
                        _s2_open     = float(_s2_comp["open"])
                        _s2_ema9h    = float(_s2_comp.get("ema9_high", 0))
                        _s2_ema9l    = float(_s2_comp.get("ema9_low", 0))
                        _s2_bw       = round(_s2_ema9h - _s2_ema9l, 2) if _s2_ema9h > 0 and _s2_ema9l > 0 else 0.0
                        _s2_rsi      = float(_s2_comp.get("RSI", 0) or 0)
                        _s2_rsi_p    = float(_s2_1m.iloc[-3].get("RSI", 0) or 0)
                        _s2_ema9l_prev = float(_s2_1m.iloc[-3].get("ema9_low", 0))

                        # 1-min session VWAP
                        _s2_day = _s2_1m[_s2_1m.index.date == now.date()].copy()
                        if len(_s2_day) < 3:
                            continue
                        _s2_day["_typ"] = (_s2_day["high"] + _s2_day["low"] + _s2_day["close"]) / 3.0
                        _s2_day["_tv"]  = _s2_day["_typ"] * _s2_day["volume"]
                        _s2_cum_vol = _s2_day["volume"].cumsum().replace(0, np.nan)
                        _s2_vwap    = float((_s2_day["_tv"].cumsum() / _s2_cum_vol).iloc[-2])

                        _s2_ds = _v8_shadow_p2[_s2_dir]

                        # Bucket change: update bucket_ts only — DO NOT reset active signal
                        if _s2_ds["bucket_ts"] != _s2_bk_ts:
                            _s2_ds["bucket_ts"] = _s2_bk_ts

                        # If signal active — track LTP using ORIGINAL token (not current ATM)
                        if _s2_ds["active"]:
                            _s2_track_tok = int(_s2_ds.get("entry_tok", 0) or _s2_tok)
                            _s2_ltp_pk  = D.get_ltp(_s2_track_tok)
                            if not _s2_ltp_pk:
                                continue
                            _s2_cur_sl  = _s2_ds.get("shadow_sl", round(_s2_ds["entry_price"] - 12, 1))
                            _s2_entry   = _s2_ds["entry_price"]

                            def _s2_close_signal(reason, exit_px):
                                _s2_fin_peak = _s2_ds["peak_pts"]
                                _s2_fin_lvl  = _s2_ds.get("shadow_level", "INITIAL")
                                _s2_fin_pnl  = round(exit_px - _s2_entry, 1)
                                _s2_fin_icon = "✅" if _s2_fin_pnl >= 20 else ("🟡" if _s2_fin_pnl > 0 else "❌")
                                _s2_fin_msg  = (
                                    f"🟡 SHADOW P2 {_s2_dir} — {reason}\n"
                                    f"Entry: {_s2_entry:.1f}  Exit: {exit_px:.1f}\n"
                                    f"PnL: {_s2_fin_icon} {_s2_fin_pnl:+.1f}  Peak: +{_s2_fin_peak:.1f}\n"
                                    f"Trail reached: {_s2_fin_lvl}\n"
                                )
                                _s2_p1e2 = _s2_ds.get("p1_entry", 0.0)
                                if _s2_p1e2 > 0:
                                    _s2_diff2 = round(_s2_p1e2 - _s2_entry, 1)
                                    _s2_fin_msg += f"P1 at {_s2_p1e2:.1f} → P2 was {_s2_diff2:+.1f}pts earlier\n"
                                else:
                                    _s2_fin_msg += f"P1 not fired — VWAP never broke\n"
                                _s2_fin_msg += f"<i>⚠️ Shadow only</i>"
                                logger.info(
                                    f"[SHADOW-P2] {_s2_dir} {reason} "
                                    f"entry={_s2_entry} exit={exit_px:.1f} "
                                    f"pnl={_s2_fin_pnl:+.1f} peak=+{_s2_fin_peak:.1f} trail={_s2_fin_lvl}"
                                )
                                _tg_send(_s2_fin_msg)
                                # Track peak for analysis streak detection
                                _shadow_analysis[_s2_dir]["last_peaks_p2"].append(_s2_fin_peak)
                                _shadow_analysis[_s2_dir]["last_peaks_p2"] = \
                                    _shadow_analysis[_s2_dir]["last_peaks_p2"][-2:]
                                _s2_ds.update({
                                    "active": False, "entry_price": 0.0, "entry_time": "",
                                    "peak_price": 0.0, "peak_pts": 0.0,
                                    "shadow_sl": 0.0, "shadow_level": "INITIAL", "p1_entry": 0.0,
                                    "bucket_ts": _s2_bk_ts,  # block re-fire on same candle
                                    "last_exit_pnl": _s2_fin_pnl, "last_exit_reason": reason,
                                    "last_exit_ts": time.time(),
                                })
                                _save_shadow_state()

                            # EOD check
                            if now.time() >= dtime(15, 15):
                                _s2_close_signal("EOD", _s2_ltp_pk)
                                continue

                            # SL hit check
                            if _s2_ltp_pk <= _s2_cur_sl:
                                _s2_close_signal("SL-HIT", _s2_cur_sl)
                                continue

                            # Update peak + trail ladder
                            if _s2_ltp_pk > _s2_ds["peak_price"]:
                                _s2_ds["peak_price"] = _s2_ltp_pk
                                _s2_ds["peak_pts"]   = round(_s2_ltp_pk - _s2_entry, 1)
                                _s2_new_sl, _s2_new_lvl = _shadow_trail_sl(_s2_entry, _s2_ds["peak_pts"])
                                _s2_old_lvl = _s2_ds.get("shadow_level", "INITIAL")
                                if _s2_new_lvl != _s2_old_lvl:
                                    _s2_ds["shadow_sl"]    = _s2_new_sl
                                    _s2_ds["shadow_level"] = _s2_new_lvl
                                    logger.info(
                                        f"[SHADOW-P2] {_s2_dir} trail ↑ {_s2_new_lvl} "
                                        f"peak=+{_s2_ds['peak_pts']:.1f} sl_now={_s2_new_sl:.1f}"
                                    )
                                    _tg_send(
                                        f"🟡 SHADOW P2 {_s2_dir} — trail ↑ {_s2_new_lvl}\n"
                                        f"Peak: +{_s2_ds['peak_pts']:.1f} | SL now: {_s2_new_sl:.1f}\n"
                                        f"<i>⚠️ Shadow only</i>"
                                    )
                                    _save_shadow_state()
                            continue

                        # ── Part 2 gates ──
                        _s2_ema9h_gap  = round(_s2_close - _s2_ema9h, 2)
                        _s2_vwap_gap   = round(_s2_close - _s2_vwap, 2)
                        _s2_reject     = None
                        if not (_s2_ema9h > 0 and _s2_close > _s2_ema9h):
                            _s2_reject = f"below_ema9h gap={_s2_ema9h_gap}"
                        elif _s2_ema9h_gap < 2.0:
                            _s2_reject = f"ema9h_gap_weak gap={_s2_ema9h_gap:.2f}(need>=2.0)"
                        elif not (_s2_close <= _s2_vwap):
                            _s2_reject = f"already_above_vwap gap={_s2_vwap_gap:+.1f}"
                        elif not (_s2_rsi > _s2_rsi_p):
                            _s2_reject = f"rsi_falling rsi={_s2_rsi:.1f} prev={_s2_rsi_p:.1f}"
                        elif not (_s2_rsi > 55):
                            _s2_reject = f"rsi_weak rsi={_s2_rsi:.1f}"
                        # Update cross-leg buffer every 15 sec (reject AND fire both recorded)
                        if now.second % 15 == 0:
                            _s2_above_ema9h = (_s2_ema9h > 0 and _s2_close > _s2_ema9h)
                            _shadow_analysis[_s2_dir]["cross_buf"].append(_s2_above_ema9h)
                            _shadow_analysis[_s2_dir]["cross_buf"] = \
                                _shadow_analysis[_s2_dir]["cross_buf"][-5:]
                        if _s2_reject:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-P2] REJECT {_s2_dir} {_s2_reject}")
                            continue

                        # ── Relock cooldown: EMA9H of new strike not settled yet (same as P1) ──
                        _s2_relock_age = time.time() - _v8_shadow_dt.get("relock_ts", 0)
                        if 0 < _s2_relock_age < 120:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-P2] REJECT {_s2_dir} relock_cooldown age={int(_s2_relock_age)}s")
                            continue

                        # ── Exit cooldown: block P2 re-entry for 120s after any exit ──
                        _s2_exit_age = time.time() - _s2_ds.get("exit_ts", 0)
                        if 0 < _s2_exit_age < 120:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-P2] REJECT {_s2_dir} exit_cooldown age={int(_s2_exit_age)}s")
                            continue

                        # ── FIRE Part 2 ──
                        # Gate: XLEG_CONFIRMED — cross-leg must be below EMA9H all last 5 scans
                        _xleg_g2_dir  = "PE" if _s2_dir == "CE" else "CE"
                        _xleg_g2_buf  = _shadow_analysis[_xleg_g2_dir].get("cross_buf", [])
                        _xleg_g2_buf5 = _xleg_g2_buf[-5:]
                        _xleg_g2_ok   = len(_xleg_g2_buf5) >= 3 and all(not v for v in _xleg_g2_buf5)
                        if not _xleg_g2_ok:
                            logger.info(
                                f"[SHADOW-P2] REJECT {_s2_dir} xleg_not_confirmed "
                                f"{_xleg_g2_dir} buf={_xleg_g2_buf5} n={len(_xleg_g2_buf5)}"
                            )
                            continue
                        _s2_ltp    = D.get_ltp(_s2_tok)
                        # Gate: LTP must still be below VWAP at fire time
                        if _s2_ltp and _s2_ltp > _s2_vwap:
                            logger.info(
                                f"[SHADOW-P2] REJECT {_s2_dir} ltp_slipped_above_vwap "
                                f"ltp={_s2_ltp:.1f} vwap={_s2_vwap:.1f} "
                                f"gap={round(_s2_ltp - _s2_vwap, 1)}"
                            )
                            continue
                        _s2_strike = int(_s2_info.get("strike", 0) or 0)
                        _s2_sl_px  = round(_s2_ltp - 12, 1)
                        _s2_ds.update({
                            "active": True, "bucket_ts": _s2_bk_ts,
                            "entry_price": _s2_ltp, "entry_time": now.strftime("%H:%M:%S"),
                            "peak_price": _s2_ltp, "peak_pts": 0.0,
                            "shadow_sl": _s2_sl_px, "shadow_level": "INITIAL",
                            "today_entry": _s2_ltp, "today_date": str(now.date()),
                            "p1_entry": 0.0,
                            "entry_tok": _s2_tok, "entry_strike": _s2_strike,
                            "exit_ts": 0.0,  # clear stale cooldown/outcome from prior trade
                            "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0,
                        })
                        # V2 tracker: same entry, hard exit at +20
                        _v8_shadow_p2_v2[_s2_dir].update({
                            "active": True, "entry_price": _s2_ltp,
                            "entry_time": now.strftime("%H:%M:%S"),
                            "peak_price": _s2_ltp, "peak_pts": 0.0,
                            "shadow_sl": _s2_sl_px, "entry_tok": _s2_tok,
                        })
                        # Check if P1 already fired today (rare — price jumped above VWAP directly)
                        _s2_p1_today = _v8_shadow_dt[_s2_dir].get("today_entry", 0.0)
                        _s2_p1_date  = _v8_shadow_dt[_s2_dir].get("today_date", "")
                        _s2_p1_note  = ""
                        if _s2_p1_today > 0 and _s2_p1_date == str(now.date()):
                            _s2_diff = round(_s2_ltp - _s2_p1_today, 1)
                            _s2_p1_note = f"⚠️ P1 already fired: {_s2_p1_today:.1f} (P2 is {_s2_diff:+.1f})\n"
                        logger.info(
                            f"[SHADOW-P2] {_s2_dir} {_s2_strike} SIGNAL "
                            f"entry={_s2_ltp} sl={_s2_sl_px} "
                            f"ema9h_gap={_s2_ema9h_gap:+.2f} bw={_s2_bw:.1f} "
                            f"vwap={_s2_vwap:.1f} below_by={_s2_vwap_gap:.1f} rsi={_s2_rsi:.1f}↑"
                        )
                        # CROSS-TRADE: check if P1 is open in opposite direction
                        _cross_opp2 = "PE" if _s2_dir == "CE" else "CE"
                        _cross_p1   = _v8_shadow_dt[_cross_opp2]
                        if _cross_p1.get("active") and _cross_p1.get("today_date","") == str(now.date()):
                            logger.info(
                                f"[CROSS-TRADE] P2-{_s2_dir} just fired vs P1-{_cross_opp2} already open "
                                f"p2_entry={_s2_ltp} p1_entry={_cross_p1.get('entry_price',0):.1f} "
                                f"p1_peak={_cross_p1.get('peak_pts',0):.1f} strike={_s2_strike}"
                            )
                        # DELAY-ANALYSIS: track LTP + spot at +5s/+10s/+30s/+60s
                        _delay_jobs.append({
                            "label": f"P2-{_s2_dir}", "strike": _s2_strike,
                            "base": _s2_ltp, "tok": _s2_tok,
                            "spot_base": D.get_ltp(D.NIFTY_SPOT_TOKEN) or 0.0,
                            "fire_ts": time.time(),
                            "snaps":       {5: None, 10: None, 30: None, 60: None},
                            "spot_snaps":  {5: None, 10: None, 30: None, 60: None},
                        })
                        _tg_send(
                            f"🟡 <b>SHADOW P2 — {_s2_dir} {_s2_strike}</b> (buildup)\n"
                            f"Entry: {_s2_ltp:.1f}  SL: {_s2_sl_px:.1f}\n"
                            f"EMA9H: {_s2_ema9h_gap:+.1f}  below VWAP: {_s2_vwap_gap:.1f}  RSI: {_s2_rsi:.0f}↑\n"
                            + (_s2_p1_note if _s2_p1_note else "") +
                            f"─── Shadow Trail ───\n"
                            f"@+12→lock+4  @+18→lock+10  @+24→lock+12\n"
                            f"@+30→lock+20  @+36→lock+30\n"
                            f"@+40→lock+36  @+50→lock+50\n"
                            f"<i>⚠️ Shadow only — no real trade</i>"
                        )
                        _save_shadow_state()
                        # ── Analysis flags (no trade impact) ──
                        _other_s2_dir = "PE" if _s2_dir == "CE" else "CE"
                        _other_s2_vwap_gap = None
                        try:
                            _other_s2_info = (_locked_tokens or {}).get(_other_s2_dir, {})
                            _other_s2_tok2 = int(_other_s2_info.get("token", 0) or 0)
                            if _other_s2_tok2:
                                _other_s2_1m2 = D.get_option_1min(_other_s2_tok2, 5)
                                if _other_s2_1m2 is not None and len(_other_s2_1m2) >= 2:
                                    _os2_day = _other_s2_1m2[_other_s2_1m2.index.date == now.date()]
                                    if len(_os2_day) >= 2:
                                        _os2_tv = ((_os2_day["high"]+_os2_day["low"]+_os2_day["close"])/3)*_os2_day["volume"]
                                        _os2_vwap = float((_os2_tv.cumsum()/_os2_day["volume"].cumsum().replace(0,float('nan'))).iloc[-2])
                                        _other_s2_vwap_gap = round(_os2_day["close"].iloc[-2] - _os2_vwap, 1)
                        except Exception:
                            pass
                        _xleg_s2_dir = "PE" if _s2_dir == "CE" else "CE"
                        _log_shadow_analysis(
                            "P2", _s2_dir, now, _s2_ltp,
                            _s2_vwap_gap, _other_s2_vwap_gap,
                            float(spot_3m.get("adx", 0)),
                            _shadow_analysis[_s2_dir]["last_peaks_p2"],
                            ema9h_gap=_s2_ema9h_gap,
                            xleg_buf=_shadow_analysis[_xleg_s2_dir]["cross_buf"],
                            dte=dte,
                            fut_vwap_gap=float(LEVELS._vwap_state.get("gap", 0.0)),
                            spot_ema9=float(spot_3m.get("ema9", 0)),
                            spot_ema21=float(spot_3m.get("ema21", 0)),
                            bw=_s2_bw,
                        )
                        # PDH/PDL/Pivot/VWAP filter data for this P2 shadow signal
                        try:
                            LEVELS.log_entry(
                                direction=_s2_dir,
                                strike=int(_s2_strike or 0),
                                entry_price=float(_s2_ltp),
                                spot_px=float(D.get_ltp(D.NIFTY_SPOT_TOKEN) or 0),
                                entry_time_dt=now,
                                dte=dte,
                            )
                        except Exception as _lvl_s2_e:
                            logger.debug(f"[SHADOW-LVL] P2 hook error: {_lvl_s2_e}")
                        # ── signal_scans DB record ──
                        try:
                            import VRL_DB as _SC
                            _SC.insert_scan({
                                "timestamp": now.isoformat(),
                                "session": "P2",
                                "dte": dte,
                                "atm_strike": int(_s2_strike or 0),
                                "spot": float(D.get_ltp(D.NIFTY_SPOT_TOKEN) or 0),
                                "direction": _s2_dir,
                                "entry_price": float(_s2_ltp),
                                "ema9_high": float(_s2_ema9h),
                                "ema9_low": float(_s2_ema9l),
                                "band_position": "ABOVE",
                                "body_pct": float(_s2_comp.get("body_pct", 0) or 0),
                                "spot_rsi_3m": float(spot_3m.get("rsi", 0)),
                                "spot_ema_spread_3m": float(spot_3m.get("spread", 0)),
                                "spot_regime": str(spot_3m.get("regime", "")),
                                "vix": float(D.get_vix()),
                                "fired": "1",
                                "trade_taken": 0,
                                "reject_reason": "",
                            })
                        except Exception as _sc_e:
                            logger.debug(f"[SHADOW-SCAN] P2 DB insert error: {_sc_e}")
                except Exception as _s2e:
                    logger.warning(f"[SHADOW-P2] error: {_s2e}")

            # Capture live entry price into per-direction shadow state
            _live_dir = _v8_state.get("direction", "")
            if (_live_dir in ("CE", "PE")
                    and _v8_state.get("in_trade")
                    and _v8_shadow_dt[_live_dir]["active"]
                    and _v8_shadow_dt[_live_dir]["live_entry"] == 0):
                _live_px = float(_v8_state.get("entry_price", 0))
                _v8_shadow_dt[_live_dir]["live_entry"] = _live_px
                _saved = round(_live_px - _v8_shadow_dt[_live_dir]["entry_price"], 1)
                logger.info(
                    f"[SHADOW-DTF] LIVE ENTRY {_live_dir} fired "
                    f"shadow={_v8_shadow_dt[_live_dir]['entry_price']} live={_live_px} "
                    f"saved={_saved:+.1f}pts ({'EARLIER ✓' if _saved > 0 else 'LATER or same'})"
                )

            try:
                _wm_warm, _wm_done, _wm_need, _wm_eta = _warmup_info(now, dte)
                if D.is_market_open() and not _wm_warm:
                    _wm_key = now.strftime("%H:%M")
                    if state.get("_last_warmup_log") != _wm_key:
                        state["_last_warmup_log"] = _wm_key
                        logger.info("[MAIN] Warmup progress: " + str(_wm_done)
                                    + "/" + str(_wm_need)
                                    + " candles (ETA: " + _wm_eta + ")")
            except Exception:
                pass

            # ── Refresh opening range at 9:30 (once) ──
            try:
                if (now.hour == 9 and now.minute >= 30
                        and not state.get("_or_refreshed_today")):
                    LEVELS.refresh_opening_range(D)
                    state["_or_refreshed_today"] = True
            except Exception:
                pass

            # ── Refresh VWAP every 15-min candle boundary ──
            try:
                _cur_15m = now.hour * 4 + now.minute // 15
                if _cur_15m != state.get("_last_vwap_15m_slot", -1):
                    LEVELS.update_vwap(kite)
                    state["_last_vwap_15m_slot"] = _cur_15m
            except Exception:
                pass

            try:
                _wmsg, _wupd = D.run_warnings(
                    kite, state, expiry, dte, spot_ltp, now)
                if _wupd:
                    with _state_lock:
                        state.update(_wupd)
                for _wm in _wmsg:
                    _tg_send(_wm)
            except Exception as _we:
                logger.warning("[MAIN] Warnings: " + str(_we))

            _strad_open = getattr(D, "_straddle_open", 0)
            _strad_capt = getattr(D, "_straddle_captured", False)
            if (_strad_capt and _strad_open > 0
                    and not state.get("aggressive_mode")
                    and now.minute % 5 == 0 and now.second < 5):
                try:
                    _strad_curr = D.get_straddle_sum(kite, _locked_ce_strike, expiry) if hasattr(D, "get_straddle_sum") else 0
                    if _strad_curr > 0:
                        _decay_pct = (_strad_open - _strad_curr) / _strad_open
                        if _decay_pct >= 0.20:
                            with _state_lock:
                                state["aggressive_mode"] = True
                            _save_state()
                            logger.info("[MAIN] Aggressive mode ON — straddle decay "
                                        + str(round(_decay_pct * 100, 1)) + "%")
                            _tg_send("⚡ Aggressive mode activated\n"
                                     "Straddle decay " + str(round(_decay_pct * 100, 0))
                                     + "% — directional day confirmed")
                except Exception:
                    pass

            with _state_lock:
                _eod_done = state.get("_eod_reported")
            if now.hour == 15 and now.minute >= 25:
                try:
                    _saved_via = ""
                    _safe_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                    if _safe_spot > 0:
                        _saved_via = "WS"
                    elif kite is not None:
                        try:
                            q = kite.ltp(["NSE:NIFTY 50"])
                            _safe_spot = float(list(q.values())[0]["last_price"])
                            _saved_via = "REST"
                        except Exception as _re25:
                            logger.debug("[MAIN] 15:25+ REST fallback failed: "
                                         + str(_re25))
                    if _safe_spot > 0:
                        with _state_lock:
                            prev_src = state.get("_prev_close_src", "")
                            if prev_src != _saved_via:
                                logger.info("[MAIN] prev_close source: " + _saved_via
                                            + " @ " + now.strftime("%H:%M:%S")
                                            + " (spot=" + str(round(_safe_spot, 1)) + ")")
                                state["_prev_close_src"] = _saved_via
                            state["prev_close"] = round(_safe_spot, 1)
                except Exception:
                    pass

            if (now.hour == 15 and now.minute == 35
                    and not _eod_done
                    and now.second < 30):
                with _state_lock:
                    state["_eod_reported"] = True
                    _eod_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                    if _eod_spot <= 0 and kite is not None:
                        try:
                            q = kite.ltp(["NSE:NIFTY 50"])
                            _eod_spot = float(list(q.values())[0]["last_price"])
                            logger.info("[MAIN] EOD spot via REST: " + str(_eod_spot))
                        except Exception as _re:
                            logger.warning("[MAIN] EOD spot REST fallback failed: " + str(_re))
                    if _eod_spot > 0:
                        state["prev_close"] = round(_eod_spot, 1)
                        logger.info("[MAIN] prev_close saved: " + str(state["prev_close"]))
                    else:
                        logger.warning("[MAIN] prev_close NOT saved — both WS and REST returned 0")
                        try:
                            _tg_send(
                                "⚠️ <b>EOD prev_close SAVE FAILED</b>\n"
                                "Both WebSocket and REST ltp() returned 0 at 15:35.\n"
                                "Tomorrow's gap-relock guard will be disabled.\n"
                                "Manual fix option: set state.prev_close via restart"
                                + " + /status, or force relock after 9:15 open.",
                                priority="critical",
                            )
                        except Exception:
                            pass
                _save_state()
                try:
                    _generate_eod_report()
                except Exception as e:
                    logger.error("[MAIN] EOD report error: " + str(e))
            try:
                if now.hour == 15 and now.minute >= 45:
                    _today_iso = date.today().isoformat()
                    _need_cleanup = False
                    with _state_lock:
                        if state.get("_last_cleanup_date") != _today_iso:
                            state["_last_cleanup_date"] = _today_iso
                            _need_cleanup = True
                    if _need_cleanup:
                        logger.info("[MAIN] Running daily lab cleanup at "
                                    + now.strftime("%H:%M"))
                        try:
                            D.cleanup_old_lab_data()
                        except Exception as _ce:
                            logger.warning("[MAIN] Lab cleanup error: "
                                           + str(_ce))
                        try:
                            import VRL_DB as _DB_clean
                            _DB_clean.cleanup_old_db_data()
                        except Exception as _dce:
                            logger.warning("[MAIN] DB cleanup error: "
                                           + str(_dce))
                        _save_state()
            except Exception as _ce_outer:
                logger.debug("[MAIN] Daily cleanup dispatch: "
                             + str(_ce_outer))

            with _state_lock:
                _force = state.get("force_exit")
                _in_trade = state.get("in_trade")
                _token = state.get("token")
                _symbol = state.get("symbol", "")
                _entry_px = state.get("entry_price", 0)
            if _force and _in_trade:
                option_ltp = D.get_ltp(_token)
                _floor_sl = state.get("_static_floor_sl", 0)
                _exit_px = option_ltp if option_ltp > 0 else max(_entry_px, _floor_sl)
                _execute_exit_v13(kite,
                                  {"lots": "ALL", "lot_id": "ALL",
                                   "reason": "FORCE_EXIT", "price": _exit_px},
                                  saved_entry_price=_entry_px)
                time.sleep(1)
                continue
            if (now.hour > 15 or (now.hour == 15 and now.minute >= 30)):
                if not state.get("_eod_exited"):
                    with _state_lock:
                        state["_eod_exited"] = True
                    logger.info("[MAIN] _eod_exited=True at "
                                + now.strftime("%H:%M:%S")
                                + " (no trade open → flag-only)")

            if _in_trade:
                option_ltp = D.get_ltp(_token)
                if option_ltp <= 0 and kite is not None:
                    try:
                        q = kite.ltp(["NFO:" + _symbol])
                        option_ltp = float(q["NFO:" + _symbol]["last_price"])
                        logger.info("[MAIN] Option LTP via REST: " + str(option_ltp))
                    except Exception as e:
                        logger.warning("[MAIN] REST option LTP failed: " + str(e))
                if option_ltp > 0:
                    with _state_lock:
                        cur_1m = now.strftime("%H:%M")
                        if cur_1m != state.get("_last_candle_held_min", ""):
                            state["_last_candle_held_min"] = cur_1m
                            state["candles_held"] = state.get("candles_held", 0) + 1
                            state["_candle_low"] = option_ltp

                    _mex_other_tok = state.get("other_token", 0)
                    _prev_tier = state.get("active_ratchet_tier", "None") or "None"
                    _prev_sl   = float(state.get("active_ratchet_sl", 0) or 0)
                    exit_list = manage_exit(state, option_ltp, profile, other_token=_mex_other_tok)

                    try:
                        _new_tier  = state.get("active_ratchet_tier", "None")
                        _armed = _new_tier not in ("None", "", "INITIAL")
                        _exit_imminent = bool(exit_list)
                        if (state.get("in_trade") and _new_tier != _prev_tier
                                and _new_tier and _armed and not _exit_imminent):
                            _r_sl   = float(state.get("active_ratchet_sl", 0) or 0)
                            _r_ent  = float(state.get("entry_price", 0) or 0)
                            _r_lock = round(_r_sl - _r_ent, 1)
                            _r_peak = float(state.get("peak_pnl", 0) or 0)
                            _r_ltp  = float(option_ltp or 0)
                            _r_room = round(_r_ltp - _r_sl, 1) if _r_ltp > 0 else 0
                            _r_emoji = "🟢" if state.get("direction") == "CE" else "🔴"
                            _r_sym = _short_sym(state.get("symbol", ""),
                                                 state.get("direction", ""),
                                                 state.get("strike", 0))
                            _icon = "🔒"
                            if _new_tier == "LOCK_M5":
                                _icon = "⚠️"
                            elif _new_tier == "LOCK_3":
                                _icon = "🛡️"
                            elif _new_tier == "LOCK_5":
                                _icon = "🔒"
                            elif _new_tier == "LOCK_8":
                                _icon = "🔒🔒"
                            elif _new_tier == "LOCK_15":
                                _icon = "🔒🔒"
                            elif _new_tier == "LOCK_DYN":
                                _icon = "🔒🔒🔒"
                            _sl_old_str = ("Rs" + "{:.1f}".format(_prev_sl)
                                           if _prev_sl > 0 else "entry-10")
                            _tg_send(
                                _icon + " <b>V9 SL UPGRADED → " + _new_tier + "</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                + _r_emoji + " " + _r_sym + "   Peak +"
                                + "{:.1f}".format(_r_peak) + "\n"
                                "Prev  " + _prev_tier + "   " + _sl_old_str + "\n"
                                "New   " + _new_tier + "   Rs"
                                + "{:.1f}".format(_r_sl) + "   ⬆️\n"
                                "Lock  +" + "{:.1f}".format(_r_lock) + " pts\n"
                                "Room  " + "{:.1f}".format(_r_room) + " pts"
                            )
                    except Exception as _re:
                        logger.debug("[MAIN] trail tier alert error: " + str(_re))

                    if state.get("in_trade"):
                        _peak = state.get("peak_pnl", 0)
                        _last_ms = state.get("_last_milestone", 0)
                        _cur_el = round(float(state.get("current_ema9_low", 0)), 1)
                        _entry_px = state.get("entry_price", 0)
                        for _m in [5, 8, 10, 12, 15, 20, 25, 30, 40, 50]:
                            if _peak >= _m and _last_ms < _m:
                                with _state_lock:
                                    state["_last_milestone"] = _m
                                _r_tier = state.get("active_ratchet_tier", "") or "INITIAL"
                                _r_sl   = float(state.get("active_ratchet_sl", 0) or 0)
                                _cur_pnl = round(option_ltp - _entry_px, 1)
                                if _r_sl <= 0:
                                    _r_sl = round(_entry_px - 10, 1)
                                _lock = round(_r_sl - _entry_px, 1)
                                _room = round(option_ltp - _r_sl, 1)
                                _ms_icon = "📈"
                                if _r_tier == "LOCK_M5":
                                    _ms_icon = "⚠️"
                                elif _r_tier == "LOCK_3":
                                    _ms_icon = "🛡️"
                                elif _r_tier in ("LOCK_5", "LOCK_8"):
                                    _ms_icon = "🔒"
                                elif _r_tier in ("LOCK_15", "LOCK_DYN"):
                                    _ms_icon = "🔒🔒"
                                _lock_str = (("+" if _lock >= 0 else "")
                                             + "{:.1f}".format(_lock))
                                _tg_send(
                                    _ms_icon + " <b>V9 Peak +" + str(_m)
                                    + " pts</b>   " + _r_tier + "\n"
                                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                    "Peak  +" + "{:.1f}".format(_peak) + "\n"
                                    "Now   +" + "{:.1f}".format(_cur_pnl) + "\n"
                                    "SL    Rs" + "{:.1f}".format(_r_sl)
                                    + "   (" + _lock_str + " locked)\n"
                                    "Room  " + "{:.1f}".format(_room) + " pts"
                                )
                                break
                    _eod_cutoff = 25 if not D.PAPER_MODE else 28
                    if now.hour == 15 and now.minute >= _eod_cutoff:
                        if not D.PAPER_MODE and now.minute < 28:
                            logger.warning("[MAIN] 15:25 SAFETY — forcing exit before broker square-off")
                            _tg_send("⚠️ <b>15:25 SAFETY EXIT</b>\nClosing before broker auto square-off")
                        exit_list = [{"lots": "ALL", "lot_id": "ALL",
                                      "reason": "EOD_EXIT" if not D.PAPER_MODE else "MARKET_CLOSE",
                                      "price": option_ltp}]
                    if (now.hour > 15 or (now.hour == 15 and now.minute >= 30)):
                        if not state.get("_eod_exited"):
                            logger.warning("[MAIN] 15:30 catch-all — forcing exit on open trade")
                            _tg_send("⚠️ <b>15:30 MARKET CLOSE</b>\nForcing exit on open position")
                            exit_list = [{"lots": "ALL", "lot_id": "ALL",
                                          "reason": "MARKET_CLOSE", "price": option_ltp}]
                            state["_eod_exited"] = True

                    _scan_min = now.strftime("%H:%M")
                    if _scan_min != state.get("_last_dash_scan_min", "") and now.second >= 31:
                        state["_last_dash_scan_min"] = _scan_min
                        try:
                            _trade_scan = {}
                            _trade_dir = state.get("direction", "")
                            _trade_token = state.get("token")
                            _trade_strike = state.get("strike", 0)
                            for _dt in ("CE", "PE"):
                                if _dt == _trade_dir and _trade_token:
                                    _sr = check_entry(_trade_token, _dt, spot_ltp, dte, expiry, kite, silent=True)
                                    _sr["_strike"] = _trade_strike
                                else:
                                    _oi = _locked_tokens.get(_dt) if _locked_tokens else None
                                    if _oi:
                                        _sr = check_entry(_oi["token"], _dt, spot_ltp, dte, expiry, kite, silent=True)
                                        _sr["_strike"] = _locked_ce_strike if _dt == "CE" else _locked_pe_strike
                                    else:
                                        _sr = None
                                if _sr:
                                    _trade_scan[_dt] = _sr
                            _write_dashboard(spot_ltp, state.get("strike", 0),
                                             dte, D.get_vix(), session,
                                             profile, _trade_scan, expiry, now,
                                             dir_strikes={"CE": _locked_ce_strike, "PE": _locked_pe_strike})
                        except Exception:
                            pass

                    _saved_entry = state.get("entry_price", 0)
                    for _exit in exit_list:
                        _execute_exit_v13(kite, _exit, saved_entry_price=_saved_entry)

                    if not exit_list and state.get("in_trade"):
                        entry    = state.get("entry_price", 0)
                        pnl      = round(option_ltp - entry, 1)
                        last_ms  = state.get("_last_milestone", 0)
                        milestone= (int(pnl) // 10) * 10
                        if milestone > last_ms and milestone > 0 and state.get("lots_split"):
                            with _state_lock:
                                state["_last_milestone"] = milestone
                                _ms_trail_sl = state.get("lot2_trail_sl", 0)
                            _ms_sl_str = str(round(_ms_trail_sl, 1)) if _ms_trail_sl > 0 else "—"
                            _tg_send(
                                "📈 +" + str(milestone) + "pts | SL ₹" + _ms_sl_str + " (ATR)"
                            )
                        _save_state()

                if now.second % 5 < 2:
                    _update_dashboard_ltp()

                time.sleep(0.5)
                continue

            if (not state.get("paused")
                    and D.is_trading_window(now)
                    and _is_new_1min_candle(now)
                    and spot_ltp > 0
                    and expiry is not None):

                step       = D.get_active_strike_step(dte)
                atm_strike = D.resolve_atm_strike(spot_ltp, step)

                _relock = False
                _is_initial_lock = False
                _spot_move = 0.0
                _old_ce = None
                _old_pe = None
                if _locked_at_spot is None or _locked_ce_strike is None:
                    _relock = True
                    _is_initial_lock = True
                else:
                    _target_atm = int(round(spot_ltp / 50) * 50)
                    _lock_buffer = int(CFG.entry_ema9_band("lock_shift_pts", 10))
                    _dist_from_lock = abs(spot_ltp - _locked_ce_strike)
                    if (_target_atm != _locked_ce_strike
                            and _dist_from_lock >= 25 + _lock_buffer):
                        _relock = True
                        _spot_move = round(spot_ltp - _locked_at_spot, 1)
                        _old_ce = _locked_ce_strike
                        _old_pe = _locked_pe_strike
                        logger.info("[MAIN] ATM drift past hysteresis: locked="
                                    + str(_locked_ce_strike) + " target="
                                    + str(_target_atm) + " spot="
                                    + str(round(spot_ltp, 1))
                                    + " (dist=" + "{:.1f}".format(_dist_from_lock)
                                    + " > " + str(25 + _lock_buffer)
                                    + ") — RELOCKING (neighbor pre-warmed)")

                if _relock:
                    _lock_strikes(spot_ltp, dte, kite, expiry)
                    if not _is_initial_lock:
                        _v8_shadow_dt["relock_ts"] = time.time()
                        logger.info("[SHADOW-P1] Relock cooldown armed — P1 signals blocked 2 min")

                dir_strikes = {"CE": _locked_ce_strike, "PE": _locked_pe_strike}
                dir_tokens = dict(_locked_tokens)

                if not dir_tokens:
                    logger.warning("[MAIN] Locked tokens empty — forcing relock")
                    _lock_strikes(spot_ltp, dte, kite, expiry)
                    dir_tokens = dict(_locked_tokens)
                    dir_strikes = {"CE": _locked_ce_strike, "PE": _locked_pe_strike}
                    if not dir_tokens:
                        logger.warning("[MAIN] Relock failed — skipping cycle")
                        time.sleep(2)
                        continue

                # Seed dashboard with last V9 scan results (updated every 3s by V9 entry loop)
                all_results = {k: v for k, v in _v9_last_results.items() if v is not None}
                best_result = None
                best_type = None
                best_opt_info = None

                _now_scan = datetime.now()
                _scan_key = _now_scan.strftime("%Y%m%d%H%M")
                _should_scan = _scan_key != state.get("_last_scan_key", "")
                if not _should_scan:
                    time.sleep(1)
                    continue
                with _state_lock:
                    state["_last_scan_key"] = _scan_key
                    state["_last_scan_minute"] = _now_scan.strftime("%H:%M")

                if not D.is_tick_live(D.INDIA_VIX_TOKEN):
                    D.subscribe_tokens([D.INDIA_VIX_TOKEN])

                # ── RE-ENTRY WATCHER (V7: 2-candle window) ──
                _re_armed = bool(state.get("_reentry_armed", False))
                if _re_armed and not state.get("in_trade"):
                    _re_dir   = str(state.get("_reentry_direction", "") or "")
                    _re_token = int(state.get("_reentry_token", 0) or 0)
                    _re_strike = int(state.get("_reentry_strike", 0) or 0)
                    _re_exit_epoch = float(state.get("_reentry_exit_ts", 0) or 0)
                    _re_attempts = int(state.get("_reentry_attempts", 0) or 0)
                    _re_last_checked = float(state.get("_reentry_last_checked_epoch", 0) or 0)
                    if _re_dir and _re_token and _re_exit_epoch > 0:
                        try:
                            _re_15m = D.add_indicators(
                                D.get_historical_data(_re_token, "15minute", 30))
                            if _re_15m is not None and len(_re_15m) >= 16:
                                _re_last = _re_15m.iloc[-2]
                                _re_close_dt = _re_last.name + timedelta(minutes=15)
                                _re_close_epoch = _re_close_dt.timestamp()
                                # Only check this candle once: must be after exit AND newer than last check
                                if (_re_close_epoch > _re_exit_epoch
                                    and _re_close_epoch > _re_last_checked):
                                    with _state_lock:
                                        state["_reentry_attempts"] = _re_attempts + 1
                                        state["_reentry_last_checked_epoch"] = _re_close_epoch
                                    _re_attempts += 1
                                    _re_result = check_entry(
                                        token=_re_token, option_type=_re_dir,
                                        spot_ltp=spot_ltp, dte=dte,
                                        expiry_date=expiry, kite=kite,
                                        silent=False, state=state)
                                    _passed = _re_result.get("fired", False)
                                    # Body gate for V7 re-entry: require ≥ 20% body (no doji confirmation)
                                    if _passed:
                                        _re_body = float(_re_result.get("body_pct", 0) or 0)
                                        if _re_body < 20:
                                            _passed = False
                                            _re_result["fired"] = False
                                            _re_result["reject_reason"] = f"reentry_weak_body_{_re_body}pct"
                                            logger.info(f"[REENTRY-V9] {_re_dir} body={_re_body}% < 20% — rejected")
                                    if not _passed:
                                        _why = _re_result.get("reject_reason", "?")
                                        if _re_attempts >= 2:
                                            # 2-candle window exhausted → disarm
                                            _tg_send(
                                                "🚫 <b>RE-ENTRY DROPPED</b>\n"
                                                + _re_dir + " " + str(_re_strike)
                                                + " — 2/2 candles failed (V7)\n"
                                                "Last reason: " + str(_why) + "\n"
                                                "Waiting for fresh setup."
                                            )
                                            logger.info("[REENTRY] window exhausted (2/2): " + str(_why))
                                            with _state_lock:
                                                state["_reentry_armed"] = False
                                                state["_reentry_exit_ts"] = 0.0
                                                state["_reentry_direction"] = ""
                                                state["_reentry_token"] = 0
                                                state["_reentry_strike"] = 0
                                                state["_reentry_attempts"] = 0
                                                state["_reentry_last_checked_epoch"] = 0.0
                                        else:
                                            _tg_send(
                                                "⏳ <b>RE-ENTRY ATTEMPT 1/2 FAILED</b>\n"
                                                + _re_dir + " " + str(_re_strike)
                                                + " — Reason: " + str(_why) + "\n"
                                                "Waiting next 15-min candle (1 more attempt)."
                                            )
                                            logger.info(f"[REENTRY] attempt {_re_attempts}/2 failed: " + str(_why))
                                    else:
                                        _re_result["entry_mode"] = "REENTRY"
                                        _re_result["_strike"] = _re_strike
                                        _re_result["_strike_label"] = "REENTRY"
                                        _re_oi = {"token": _re_token, "symbol": ""}
                                        try:
                                            for _k, _v in (_locked_tokens or {}).items():
                                                if int(_v.get("token", 0) or 0) == _re_token:
                                                    _re_oi = {"token": _re_token,
                                                              "symbol": _v.get("symbol", "")}; break
                                            if not _re_oi.get("symbol") and kite and expiry and _re_strike:
                                                _re_tk = D.get_option_tokens(kite, _re_strike, expiry) or {}
                                                _re_si = _re_tk.get(_re_dir) or {}
                                                if _re_si.get("symbol"):
                                                    _re_oi["symbol"] = _re_si.get("symbol", "")
                                        except Exception:
                                            pass
                                        _re_result["_symbol"] = _re_oi.get("symbol", "")
                                        try:
                                            from VRL_ENGINE import evaluate_cross_leg as _xleg_re
                                            _other_dt_re = "PE" if _re_dir == "CE" else "CE"
                                            _other_oi_re = (_locked_tokens or {}).get(_other_dt_re) or {}
                                            _other_tok_re = int(_other_oi_re.get("token", 0) or 0)
                                            if _other_tok_re:
                                                _other_3m_re = D.get_option_3min(_other_tok_re, lookback=10)
                                                _xl_re_info = _xleg_re(_re_dir, _other_3m_re)
                                                _re_result.update(_xl_re_info)
                                        except Exception as _xre:
                                            logger.debug("[XLEG][REENTRY] " + str(_xre))
                                        _xl_gate_re = bool(CFG.entry_ema9_band("xleg_gate_enabled", True))
                                        _xl_sig_re = _re_result.get("xleg_signal", "NA")
                                        if _xl_gate_re and _xl_sig_re == "FAIL":
                                            _tg_send(
                                                "🚫 <b>RE-ENTRY BLOCKED — X-LEG FAIL</b>\n"
                                                + _re_dir + " " + str(_re_strike) + " confirmation candle was good but x-leg said no."
                                            )
                                            with _state_lock:
                                                state["_reentry_armed"] = False
                                                state["_reentry_exit_ts"] = 0.0
                                                state["_reentry_direction"] = ""
                                                state["_reentry_token"] = 0
                                                state["_reentry_strike"] = 0
                                            continue
                                        _saved_lex = state.get("last_exit_direction", "")
                                        with _state_lock:
                                            state["last_exit_direction"] = ""
                                        _re_ltp_now = D.get_ltp(_re_token)
                                        if _re_ltp_now <= 0:
                                            _re_ltp_now = float(_re_last["close"])
                                        ok, why = pre_entry_checks(
                                            kite, _re_token, state,
                                            _re_ltp_now, profile, session,
                                            direction=_re_dir)
                                        if not (ok and _re_oi.get("symbol")):
                                            with _state_lock:
                                                state["last_exit_direction"] = _saved_lex
                                                state["_reentry_armed"] = False
                                                state["_reentry_exit_ts"] = 0.0
                                            logger.info("[REENTRY] pre-entry blocked: " + str(why))
                                            continue
                                        _re_close = float(_re_result.get("close", 0) or 0)
                                        _tg_send(
                                            "🔄 <b>V9 RE-ENTRY CONFIRMED " + _re_dir + " "
                                            + str(_re_strike) + "</b>\n"
                                            "Confirmation candle " + _re_close_dt.strftime("%H:%M")
                                            + ": GREEN body "
                                            + str(int(_re_result.get("body_pct", 0))) + "%\n"
                                            "Filling at candle close Rs" + "{:.2f}".format(_re_close)
                                        )
                                        # V5 CLOSE FILL — re-entry at candle close, no wait
                                        _re_result["entry_price"] = _re_close
                                        _re_result["entry_mode"]  = "CLOSE_FILL"
                                        logger.info("[CLOSE_FILL] RE-ENTRY " + _re_dir
                                                    + " at candle close Rs" + str(_re_close))
                                        with _state_lock:
                                            state["_reentry_armed"] = False
                                            state["_reentry_exit_ts"] = 0.0
                                            state["_reentry_direction"] = ""
                                            state["_reentry_token"] = 0
                                            state["_reentry_strike"] = 0
                                        _execute_entry(kite, _re_oi, _re_dir,
                                                       _re_result, profile,
                                                       expiry, dte, session)
                                        if state.get("in_trade"):
                                            D.mark_trade_taken(_re_dir)
                                            time.sleep(0.5)
                                            continue
                        except Exception as _ree:
                            import traceback as _tb_re
                            logger.error("[REENTRY] check error: " + str(_ree)
                                         + "\n" + _tb_re.format_exc())

                # V7 15-min check_entry scan removed — V9 (check_entry_v8) handles all entries
                # V9 entry is handled above in the 10-second scan (outside 1-min gate)

                try:
                    vix_ltp = D.get_vix()
                except Exception:
                    vix_ltp = 0.0

                ce_res = all_results.get("CE", {})
                pe_res = all_results.get("PE", {})
                with _state_lock:
                    state["_last_scan"] = {
                        "time": now.strftime("%H:%M:%S"),
                        "session": session,
                        "vix": round(vix_ltp, 2),
                        "dte": dte,
                        "atm": atm_strike,
                        "fired": best_type or "No",
                        "fired_type": best_type or "—",
                        "ce": ce_res,
                        "pe": pe_res,
                    }

                global _last_dash_args
                _last_dash_args = {
                    "spot_ltp": spot_ltp, "atm_strike": atm_strike,
                    "dte": dte, "vix_ltp": vix_ltp,
                    "session": session, "profile": profile,
                    "expiry": expiry,
                }
                try:
                    _write_dashboard(spot_ltp, atm_strike, dte, vix_ltp, session,
                                     profile, all_results, expiry, now,
                                     dir_strikes=dir_strikes)
                except Exception as _de:
                    logger.debug("[DASH] " + str(_de))

                if best_result and best_opt_info:
                    # V5 CLOSE FILL — enter at candle close (body high of green candle).
                    # No pullback wait, no midpoint, no Option-B. Instant fill at close.
                    _entry_close_x = float(best_result.get("close", 0) or 0)
                    best_result["entry_price"] = _entry_close_x
                    best_result["entry_mode"]  = "CLOSE_FILL"
                    logger.info("[CLOSE_FILL] " + best_type + " entry at candle close Rs"
                                + str(_entry_close_x))

                if best_result and best_opt_info:
                    _xl_signal = "NA"
                    try:
                        from VRL_ENGINE import evaluate_cross_leg as _xleg
                        _other_dt = "PE" if best_type == "CE" else "CE"
                        _other_oi = (_locked_tokens or {}).get(_other_dt) or {}
                        _other_tok_xl = int(_other_oi.get("token", 0) or 0)
                        if _other_tok_xl:
                            _other_3m_xl = D.get_option_3min(_other_tok_xl, lookback=10)
                            _xl_info = _xleg(best_type, _other_3m_xl)
                            best_result.update(_xl_info)
                            _xl_signal = _xl_info.get("xleg_signal", "NA")
                            logger.info(
                                "[XLEG] " + best_type + " entry — other "
                                + _other_dt + " close=" + str(_xl_info.get("xleg_other_close"))
                                + " ema9l=" + str(_xl_info.get("xleg_other_ema9l"))
                                + " margin=" + "{:+.2f}".format(_xl_info.get("xleg_other_margin", 0))
                                + " → " + str(_xl_signal)
                            )
                    except Exception as _xe:
                        logger.debug("[XLEG] " + str(_xe))

                    _xl_gate = bool(CFG.entry_ema9_band("xleg_gate_enabled", True))
                    if _xl_gate and _xl_signal == "FAIL":
                        _xl_other_dt = "PE" if best_type == "CE" else "CE"
                        _xl_margin = float(best_result.get("xleg_other_margin", 0) or 0)
                        _tg_send(
                            "🚫 <b>X-LEG GATE — entry blocked</b>\n"
                            + best_type + " " + str(best_result.get("_strike", 0))
                            + "  | " + _xl_other_dt + " holding "
                            + ("+" if _xl_margin >= 0 else "")
                            + "{:.1f}".format(_xl_margin)
                            + " above own EMA9L\n"
                            "Backtest: blocking FAIL trades = +56 pts/5d"
                        )
                        logger.info("[XLEG-GATE] " + best_type
                            + " blocked (FAIL signal) — waiting fresh setup")
                        best_result = None
                        best_opt_info = None
                    _execute_entry(kite, best_opt_info, best_type,
                                   best_result, profile, expiry, dte, session)
                    if state.get("in_trade"):
                        D.mark_trade_taken(best_type)

            if now.second % 10 < 2:
                _update_dashboard_ltp()

            # ── Live status dump every 5s — readable by any external script ──
            if now.second % 5 == 0:
                try:
                    _ce_info = (_locked_tokens or {}).get("CE", {})
                    _pe_info = (_locked_tokens or {}).get("PE", {})
                    _ce_tok  = int(_ce_info.get("token", 0) or 0)
                    _pe_tok  = int(_pe_info.get("token", 0) or 0)
                    _status  = {
                        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "spot": round(D.get_spot_ltp(), 1),
                        "atm_strike": int(_ce_info.get("strike", 0) or 0),
                        "in_trade": bool(_v8_state.get("in_trade")),
                        "direction": _v8_state.get("direction", ""),
                        "entry_price": _v8_state.get("entry_price", 0),
                        "peak_pts": _v8_state.get("last_exit_peak", 0),
                        "daily_trades": _v8_state.get("daily_trades", 0),
                        "daily_pnl": _v8_state.get("daily_pnl", 0.0),
                        "daily_losses": _v8_state.get("daily_losses", 0),
                        "consecutive_losses": _v8_state.get("consecutive_losses", 0),
                        "CE": {
                            "strike": int(_ce_info.get("strike", 0) or 0),
                            "ltp": round(D.get_ltp(_ce_tok), 2) if _ce_tok else 0,
                        },
                        "PE": {
                            "strike": int(_pe_info.get("strike", 0) or 0),
                            "ltp": round(D.get_ltp(_pe_tok), 2) if _pe_tok else 0,
                        },
                    }
                    # add 3m indicators if available
                    for _sd, _stok in [("CE", _ce_tok), ("PE", _pe_tok)]:
                        if _stok:
                            try:
                                _df = D.add_indicators(D.get_historical_data(_stok, "3minute", 10))
                                if _df is not None and len(_df) >= 3:
                                    _r = _df.iloc[-2]
                                    _el = float(_r.get("ema9_low", 0))
                                    _eh = float(_r.get("ema9_high", 0))
                                    _status[_sd]["3m"] = {
                                        "close": round(float(_r["close"]), 2),
                                        "open":  round(float(_r["open"]), 2),
                                        "ema9l": round(_el, 2),
                                        "ema9h": round(_eh, 2),
                                        "bw":    round(_eh - _el, 2),
                                        "rsi":   round(float(_r.get("RSI", 0) or 0), 1),
                                    }
                            except Exception:
                                pass
                    with open("/home/vishalraajput24/state/vrl_status.json", "w") as _sf:
                        json.dump(_status, _sf, indent=2, default=str)
                except Exception:
                    pass

        except Exception as e:
            import traceback as _tb
            _tb_str = _tb.format_exc()
            logger.error("[MAIN] Loop error: " + str(e) + "\n" + _tb_str)
            time.sleep(2)

        # ── DELAY-ANALYSIS: snapshot LTP at +5s/+10s/+30s/+60s after P1/P2 signals ──
        try:
            _done_jobs = []
            for _dj in _delay_jobs:
                _elapsed = time.time() - _dj["fire_ts"]
                _tok     = _dj["tok"]
                for _delay in (5, 10, 30, 60):
                    if _dj["snaps"][_delay] is None and _elapsed >= _delay:
                        _snap_ltp  = D.get_ltp(_tok)
                        _snap_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                        _dj["snaps"][_delay]      = _snap_ltp  if _snap_ltp  else 0.0
                        _dj["spot_snaps"][_delay] = _snap_spot if _snap_spot else 0.0
                if all(v is not None for v in _dj["snaps"].values()):
                    _b   = _dj["base"]
                    _sb  = _dj.get("spot_base", 0.0)
                    _s5  = _dj["snaps"][5];  _sp5  = _dj["spot_snaps"][5]
                    _s10 = _dj["snaps"][10]; _sp10 = _dj["spot_snaps"][10]
                    _s30 = _dj["snaps"][30]; _sp30 = _dj["spot_snaps"][30]
                    _s60 = _dj["snaps"][60]; _sp60 = _dj["spot_snaps"][60]
                    logger.info(
                        f"[DELAY-ANALYSIS] {_dj['label']} {_dj['strike']} "
                        f"base={_b:.1f} spot_base={_sb:.0f} "
                        f"+5s=opt{_s5:.1f}({_s5-_b:+.1f})spot{_sp5:.0f}({_sp5-_sb:+.0f}) "
                        f"+10s=opt{_s10:.1f}({_s10-_b:+.1f})spot{_sp10:.0f}({_sp10-_sb:+.0f}) "
                        f"+30s=opt{_s30:.1f}({_s30-_b:+.1f})spot{_sp30:.0f}({_sp30-_sb:+.0f}) "
                        f"+60s=opt{_s60:.1f}({_s60-_b:+.1f})spot{_sp60:.0f}({_sp60-_sb:+.0f})"
                    )
                    _done_jobs.append(_dj)
            for _dj in _done_jobs:
                _delay_jobs.remove(_dj)
        except Exception as _dae:
            logger.debug("[DELAY-ANALYSIS] error: " + str(_dae))

        time.sleep(1)


# ═══════════════════════════════════════════════════════════════
# === TELEGRAM COMMANDS (merged from VRL_COMMANDS) ===
# ═══════════════════════════════════════════════════════════════

_WEB_IP = ""
try:
    import subprocess as _sp
    _WEB_IP = _sp.check_output(["curl", "-s", "ifconfig.me"], timeout=5).decode().strip()
except Exception:
    _WEB_IP = "unknown"


def _send_today_download(target_date: str = None):
    """Full day zip — all logs + data + state for a date.
    /download             → today
    /download YYYY-MM-DD  → specific day
    """
    if target_date is None:
        target_date = date.today().strftime("%Y-%m-%d")

    files = D.collect_logs_for_date(target_date)
    if not files:
        _tg_send("No files found for " + target_date)
        return

    zip_path = D.create_daily_zip(target_date)
    if not zip_path or not os.path.isfile(zip_path):
        _tg_send("Failed to create zip for " + target_date)
        return

    try:
        size_mb = round(os.path.getsize(zip_path) / (1024 * 1024), 2)
        file_count = len(files)
        categories = {}
        for _, arcname in files:
            cat = arcname.split("/")[0]
            categories[cat] = categories.get(cat, 0) + 1
        cat_summary = " | ".join(k + ":" + str(v) for k, v in sorted(categories.items()))
        _TG_SIZE_LIMIT_MB = 45
        caption = ("📦 VRL Logs — " + target_date
                   + "\n" + str(file_count) + " files | "
                   + str(size_mb) + " MB"
                   + "\n" + cat_summary)

        if size_mb > _TG_SIZE_LIMIT_MB:
            _link_hint = "http://" + str(_WEB_IP) + ":8080"
            logger.warning("[DOWNLOAD] zip " + os.path.basename(zip_path)
                           + " is " + str(size_mb) + "MB > "
                           + str(_TG_SIZE_LIMIT_MB) + "MB Telegram cap — "
                           "skipping send, file preserved at " + zip_path)
            _tg_send(
                "⚠️ <b>DOWNLOAD TOO LARGE FOR TELEGRAM</b>\n"
                "Date : " + target_date + "\n"
                "Size : " + str(size_mb) + " MB (cap " + str(_TG_SIZE_LIMIT_MB) + " MB)\n"
                "Files: " + str(file_count) + "\n"
                + cat_summary + "\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Local path: <code>" + zip_path + "</code>\n"
                "Fetch via SSH or browse " + _link_hint + "."
            )
            return

        _ok = False
        try:
            _ok = bool(_tg_send_file(zip_path, caption=caption))
        except Exception as _se:
            logger.error("[DOWNLOAD] Telegram file send raised: "
                         + type(_se).__name__ + " " + str(_se))
            _ok = False

        if _ok:
            logger.info("[DOWNLOAD] sent " + os.path.basename(zip_path)
                        + " (" + str(size_mb) + "MB, "
                        + str(file_count) + " entries)")
            try:
                os.remove(zip_path)
            except Exception:
                pass
        else:
            logger.warning("[DOWNLOAD] Telegram send failed — zip "
                           "preserved for SSH retrieval: " + zip_path)
            _tg_send(
                "⚠️ <b>DOWNLOAD DELIVERY FAILED</b>\n"
                "Date : " + target_date + "\n"
                "Size : " + str(size_mb) + " MB\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "File kept on disk for SSH pull:\n"
                "<code>" + zip_path + "</code>"
            )
    except Exception as e:
        _tg_send("Download error: " + str(e))


def _why_blocked(st: dict) -> str:
    if st.get("paused"):
        return "⏸ PAUSED"
    return "✅ Ready to enter"


def _cmd_pulse(args):
    """🩺 Doctor's pulse check — single-shot diagnostic dump."""
    try:
        import VRL_CONFIG as _CFG
        now = datetime.now()
        _up_secs = int(time.time() - _BOT_START_TS)
        _up_h = _up_secs // 3600
        _up_m = (_up_secs % 3600) // 60
        _up_str = (str(_up_h) + "h " if _up_h else "") + str(_up_m) + "m"

        _spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        _spot_live = D.is_tick_live(D.NIFTY_SPOT_TOKEN)
        with D._tick_lock:
            _se = D._ticks.get(int(D.NIFTY_SPOT_TOKEN))
        _tick_age = int(time.time() - _se["ts"]) if _se else -1
        _market = D.is_market_open()
        _acct = D.get_account_info() if hasattr(D, "get_account_info") else {}
        _user = _acct.get("name", "?")
        _lot = D.get_lot_size()

        try:
            _trades_today = _read_today_trades() if "_read_today_trades" in globals() else []
        except Exception:
            _trades_today = []
        _td_pnl = sum(float(t.get("pnl_pts", 0) or 0) for t in _trades_today)
        _td_wins = sum(1 for t in _trades_today if float(t.get("pnl_pts", 0) or 0) > 0)
        _td_loss = len(_trades_today) - _td_wins
        _last_t = _trades_today[-1] if _trades_today else None

        _in_trade = state.get("in_trade", False)
        _pos_str = "—"
        if _in_trade:
            _ep = float(state.get("entry_price", 0) or 0)
            _ltp = D.get_ltp(state.get("token", 0))
            _pn = round(_ltp - _ep, 1) if _ltp else 0
            _pk = float(state.get("peak_pnl", 0) or 0)
            _tier = state.get("active_ratchet_tier", "INITIAL") or "INITIAL"
            _sl = float(state.get("active_ratchet_sl", 0) or 0)
            if _sl <= 0: _sl = round(_ep - 10, 2)
            _lock = round(_sl - _ep, 1)
            _room = round(_ltp - _sl, 1) if _ltp else 0
            _dir_emj = "🟢" if state.get("direction") == "CE" else "🔴"
            _sym = state.get("direction", "") + " " + str(state.get("strike", ""))
            _pos_str = (
                _dir_emj + " " + _sym + "  " + ("+" if _pn >= 0 else "") + str(_pn) + "pts\n"
                + "Entry Rs" + str(_ep) + " → " + str(round(_ltp, 2)) + " · Peak +" + str(_pk) + "\n"
                + "Tier: " + _tier + " @ Rs" + str(round(_sl, 2))
                + " (Lock " + ("+" if _lock >= 0 else "") + str(_lock) + " · Room "
                + ("+" if _room >= 0 else "") + str(_room) + ")"
            )

        _v8_in_trade = _v8_state.get("in_trade", False)
        _v8_pos_str = ""
        if _v8_in_trade:
            _v8_ep  = float(_v8_state.get("entry_price", 0) or 0)
            _v8_tok = int(_v8_state.get("token", 0) or 0)
            _v8_ltp = D.get_ltp(_v8_tok) if _v8_tok else 0
            _v8_pn  = round(_v8_ltp - _v8_ep, 1) if _v8_ltp else 0
            _v8_pk  = float(_v8_state.get("peak_pnl", 0) or 0)
            _v8_tier = _v8_state.get("active_ratchet_tier", "INITIAL") or "INITIAL"
            _v8_sl  = float(_v8_state.get("active_ratchet_sl", 0) or 0)
            if _v8_sl <= 0: _v8_sl = round(_v8_ep - 12, 2)
            _v8_lock = round(_v8_sl - _v8_ep, 1)
            _v8_room = round(_v8_ltp - _v8_sl, 1) if _v8_ltp else 0
            _v8_dir_emj = "🟢" if _v8_state.get("direction") == "CE" else "🔴"
            _v8_sym = _v8_state.get("direction", "") + " " + str(_v8_state.get("strike", ""))
            _v8_pos_str = (
                "[V8] " + _v8_dir_emj + " " + _v8_sym + "  "
                + ("+" if _v8_pn >= 0 else "") + str(_v8_pn) + "pts\n"
                + "Entry Rs" + str(_v8_ep) + " → Rs" + str(round(_v8_ltp, 2))
                + " · Peak +" + str(_v8_pk) + "\n"
                + "Tier: " + _v8_tier + " @ Rs" + str(round(_v8_sl, 2))
                + " (Lock " + ("+" if _v8_lock >= 0 else "") + str(_v8_lock)
                + " · Room " + ("+" if _v8_room >= 0 else "") + str(_v8_room) + ")"
            )

        _ce_lck = _locked_ce_strike or "?"
        _pe_lck = _locked_pe_strike or "?"
        _last_scan = state.get("_last_scan_minute", "?")

        _eb = _CFG.get().get("entry", {}).get("ema9_band", {}) or {}
        _xb = _CFG.get().get("exit", {}).get("ema9_band", {}) or {}
        _cd = _CFG.entry_ema9_band("cooldown_minutes", 5) if hasattr(_CFG, "entry_ema9_band") else 5

        _err_lines = []
        try:
            _err_path = os.path.join(D.ERROR_LOG_DIR, date.today().strftime("%Y-%m-%d") + ".log")
            if os.path.isfile(_err_path):
                with open(_err_path) as _f:
                    _err_lines = [ln.strip() for ln in _f.readlines()[-5:]]
        except Exception:
            pass

        def _ok(b): return "✅" if b else "❌"
        if _market:
            _market_icon = "✅"; _market_str = "OPEN"
        else:
            _market_icon = "💤"; _market_str = "CLOSED (idle until 09:15 IST)"
        if _spot > 0 and _spot_live:
            _tick_icon = "✅"; _tick_str = str(round(_spot, 2)) + "  (" + str(_tick_age) + "s ago)"
        elif not _market:
            _tick_icon = "💤"
            _tick_str = ("idle (last " + str(round(_se["ltp"], 2))
                         + " · " + str(_tick_age // 60) + "m ago)") if _se else "idle (no history)"
        else:
            _tick_icon = "❌"
            _tick_str = "STALE — " + (str(_tick_age) + "s ago" if _tick_age >= 0 else "never")

        msg = (
            "🩺 <b>PULSE CHECK</b> · " + now.strftime("%H:%M:%S") + " IST\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>BOT</b>\n"
            + _ok(True) + " v" + D.VERSION.replace("v", "") + " · uptime " + _up_str + "\n"
            + _ok(True) + " " + ("PAPER" if D.PAPER_MODE else "LIVE")
            + " · " + str(_lot) + " × 2 lots\n"
            + _market_icon + " market " + _market_str + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>DATA</b>\n"
            + _ok(_user != "?") + " token: " + str(_user) + "\n"
            + _tick_icon + " spot tick: " + _tick_str + "\n"
            + _ok(_lot > 0) + " lot size: " + str(_lot) + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>TODAY</b>\n"
            + "🕐 V9: " + str(len(_trades_today)) + " trades · "
            + str(_td_wins) + "W " + str(_td_loss) + "L · "
            + ("+" if _td_pnl >= 0 else "") + "{:.1f}".format(_td_pnl) + " pts\n"
            + "⚡ V8 (live): "
            + str(_v8_state.get("_trades_today", 0)) + " trades · "
            + str(_v8_state.get("_wins_today", 0)) + "W "
            + str(_v8_state.get("_losses_today", 0)) + "L · "
            + ("+" if _v8_state.get("_pnl_today_pts", 0) >= 0 else "")
            + "{:.1f}".format(_v8_state.get("_pnl_today_pts", 0)) + " pts"
            + (" | V8 active: " + str(_v8_state.get("direction", "")) + " "
               + str(_v8_state.get("strike", ""))
               + " peak +" + "{:.1f}".format(_v8_state.get("peak_pnl", 0))
               if _v8_state.get("in_trade") else "")
            + "\n"
            + ("Last: " + str(_last_t.get("entry_time", "?")) + " "
               + str(_last_t.get("direction", "?")) + " "
               + str(_last_t.get("strike", "?")) + " "
               + ("+" if float(_last_t.get("pnl_pts", 0) or 0) >= 0 else "")
               + str(_last_t.get("pnl_pts", "?")) + " ("
               + str(_last_t.get("exit_reason", "?")) + ")\n" if _last_t else "")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>POSITION</b>\n"
            + (_pos_str + "\n" if _in_trade else "")
            + (_v8_pos_str + "\n" if _v8_in_trade else "")
            + ("—\n" if not _in_trade and not _v8_in_trade else "")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ENGINE</b>\n"
            + ("Locked: CE " + str(_ce_lck) + " · PE " + str(_pe_lck) + "\n"
               + "Last scan: " + str(_last_scan) + "\n"
               + "Bias: " + str(state.get("daily_bias", "?")) + "\n"
               if _market else "💤 awaiting market open\n")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>CONFIG</b>\n"
            "Body min: " + str(_eb.get("body_pct_min", "?")) + "%  "
            + "Band min: " + str(_eb.get("band_width_min", "?")) + "pts (display)\n"
            "Slope lookback: " + str(_eb.get("ema9_slope_lookback", "?")) + "c  "
            + "SL: -12 (TICK, single floor)\n"
            "Time: " + str(_eb.get("warmup_until", "?")) + " - "
            + str(_eb.get("cutoff_after", "?")) + "  "
            + "EOD: " + str(_xb.get("eod_exit_time", "?")) + "\n"
            "Cooldown: " + str(_cd) + "min BOTH sides\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ERRORS</b> (today, last 5)\n"
            + (_ok(False) + " " + str(len(_err_lines)) + " errors\n<pre>"
               + "\n".join(ln[:100] for ln in _err_lines) + "</pre>"
               if _err_lines else _ok(True) + " None\n")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>V9 SL LADDER (Em -12 TICK)</b>\n"
            "INITIAL  (peak <12)  entry-12\n"
            "LOCK_4   (peak >=12) entry+4\n"
            "LOCK_12  (peak >=24) entry+12\n"
            "LOCK_20  (peak >=30) entry+20\n"
            "LOCK_30  (peak >=36) entry+30\n"
            "LOCK_36  (peak >=40) entry+36\n"
            "LOCK_50  (peak >=50) entry+50\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>2-GATE ENTRY (V7 — 15-min RSI)</b>\n"
            "Time " + str(_eb.get("warmup_until", "09:35")) + " - "
            + str(_eb.get("cutoff_after", "15:00")) + "\n"
            "1. 15-min candle close > EMA9_low\n"
            "2. RSI >= 40 AND rising (RSI[fired] > RSI[prior])\n"
            "(spot bias tracked for display only — not a gate)\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>EXIT CHAIN</b>\n"
            "1. Emergency -12 pts (TICK) | 2. EOD 15:20 | 3. Vishal Trail (TICK)\n"
            "Cooldown: " + str(_cd) + "min BOTH sides. Scan stays live;\n"
            "first 2-gate fire (CE or PE) at 15-min close after cooldown enters.\n"
        )
        _tg_send(msg)
    except Exception as e:
        _tg_send("🩺 Pulse error: " + str(e))


def _cmd_help(args):
    _tg_send(
        "🤖 <b>VISHAL RAJPUT TRADE " + D.VERSION + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>DIAGNOSTIC</b>\n"
        "/pulse     — 🩺 full health check (one-shot)\n"
        "/xleg      — 📊 cross-leg divergence accuracy (7-day)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>TRADING</b>\n"
        "/status    — trade status + PNL\n"
        "/trades    — today's trade list\n"
        "/account   — balance + margin info\n"
        "/vishal_stock_fno — F&O positions live P&L\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>DATA</b>\n"
        "/download  — full day zip (or /download YYYY-MM-DD)\n"
        "/livecheck — last 50 log lines\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>CONTROL</b>\n"
        "/pause      — block new entries\n"
        "/resume     — re-enable entries\n"
        "/forceexit  — emergency exit all lots\n"
        "/deploy     — git pull main + restart\n"
        "/restart    — restart bot (no pull)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "VISHAL RAJPUT TRADE v19 — V9 live 3-min, "
        "3-rule exit chain (Emergency SL / EOD 15:20 / Vishal Trail), "
        + ("PAPER" if D.PAPER_MODE else "LIVE") + " 2 lots.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 Dashboard: http://" + _WEB_IP + ":8080"
    )


def _cmd_status(args):
    global _kite
    with _state_lock:
        st = dict(state)
    with _post_exit_lock:
        _post_n = len(_post_exit_observation)
    _post_exit_line = ""
    if _post_n:
        _post_exit_line = ("Post-exit watching: " + str(_post_n)
                           + " token(s)\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

    if not st.get("in_trade"):
        last_scan = st.get("_last_scan", {})
        _warmup_line = ""
        try:
            import json as _j
            import os as _os
            _dash_path = _os.path.join(D.STATE_DIR, "vrl_dashboard.json")
            if _os.path.isfile(_dash_path):
                with open(_dash_path) as _df:
                    _d = _j.load(_df)
                _mk = _d.get("market", {})
                if _mk.get("market_open") and not _mk.get("indicators_warm", True):
                    _wp = _mk.get("warmup_progress", 0)
                    _wn = _mk.get("warmup_needed", 14)
                    _we = _mk.get("warmup_eta", "—")
                    _warmup_line = ("🟡 WARMUP (" + str(_wp) + "/" + str(_wn) + " candles)\n"
                                    "ETA       : " + _we + "\n"
                                    "Trades blocked until indicators stable\n"
                                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
        except Exception:
            pass
        _tg_send(
            "📊 <b>STATUS — NO TRADE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + _warmup_line +
            _post_exit_line +
            "PNL    : " + str(round(st.get("daily_pnl", 0), 1)) + "pts\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Last scan : " + last_scan.get("time", "—") + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Bot       : " + _why_blocked(st)
        )
        return

    ltp = 0.0
    try:
        ltp = D.get_ltp(st.get("token"))
        if ltp <= 0 and _kite is not None:
            symbol = st.get("symbol")
            if symbol:
                q = _kite.ltp(["NFO:" + symbol])
                ltp = float(q["NFO:" + symbol]["last_price"])
                logger.info("[STATUS] LTP via REST: " + str(ltp))
    except Exception as e:
        logger.warning("[STATUS] LTP fetch error: " + str(e))
        ltp = 0.0

    entry   = st.get("entry_price", 0)
    pnl     = round(ltp - entry, 1) if ltp > 0 else 0
    peak    = st.get("peak_pnl", 0)

    _tier = st.get("active_ratchet_tier", "None")
    _rsl  = float(st.get("active_ratchet_sl", 0) or 0)
    if _tier and _tier not in ("", "None", "INITIAL") and _rsl > 0:
        _stop_line = "Trail  : " + _tier + " @ Rs" + str(round(_rsl, 1))
        _stop_dist = round(ltp - _rsl, 1) if ltp > 0 else "—"
    else:
        _init_sl   = round(entry - 10, 1)
        _stop_line = "Trail  : INITIAL @ Rs" + str(_init_sl)
        _stop_dist = round(ltp - _init_sl, 1) if ltp > 0 else "—"

    _tg_send(
        "📊 <b>STATUS — IN TRADE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time   : " + _now_str() + "\n"
        "Symbol : " + st.get("symbol", "") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Entry  : " + str(round(entry, 2)) + "\n"
        "LTP    : " + str(round(ltp, 2)) + "\n"
        "PNL    : " + ("+" if pnl >= 0 else "") + str(pnl) + "pts  " + _rs(pnl) + "\n"
        "Peak   : +" + str(round(peak, 1)) + "pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _stop_line + "  (" + str(_stop_dist) + "pts away)\n"
        "Ladder : @+12→LOCK_4  @+18→LOCK_10  @+24→LOCK_12\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _post_exit_line +
        "Day PNL: " + str(round(st.get("daily_pnl", 0), 1)) + "pts"
    )


def _cmd_account(args):
    try:
        _acct = D.get_account_info()
        if _kite:
            D.refresh_margin(_kite)
            _acct = D.get_account_info()
    except Exception:
        _acct = D.get_account_info()

    if not _acct.get("name"):
        _tg_send("Account info not available. Bot may not have fetched it yet.")
        return

    _tg_send(
        "👤 <b>ACCOUNT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Name     : " + _acct.get("name", "") + "\n"
        "User ID  : " + _acct.get("user_id", "") + "\n"
        "Broker   : " + _acct.get("broker", "Zerodha") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Balance  : ₹" + "{:,}".format(int(_acct.get("total_balance", 0))) + "\n"
        "Available: ₹" + "{:,}".format(int(_acct.get("available_margin", 0))) + "\n"
        "Used     : ₹" + "{:,}".format(int(_acct.get("used_margin", 0)))
    )


def _cmd_download(args):
    target = None
    if isinstance(args, list):
        args = " ".join(args)
    if args and args.strip():
        arg = args.strip()
        if len(arg) == 8 and arg.isdigit():
            target = arg[:4] + "-" + arg[4:6] + "-" + arg[6:8]
        elif len(arg) == 10 and arg[4] == "-" and arg[7] == "-":
            target = arg
        else:
            _tg_send("Usage: /download or /download 2026-04-16")
            return
    _send_today_download(target)


def _cmd_pause(args):
    with _state_lock:
        state["paused"] = True
    _tg_send("⏸ Paused. No new entries.")
    logger.info("[CTRL] Paused")


def _cmd_resume(args):
    with _state_lock:
        state["paused"] = False
    _tg_send("▶️ Resumed.")
    logger.info("[CTRL] Resumed")


def _cmd_forceexit(args):
    v7_open = False
    v8_open = False
    with _state_lock:
        if state.get("in_trade"):
            state["force_exit"] = True
            v7_open = True
    _v8_tok = 0
    _v8_entry_px = 0.0
    with _v8_lock:
        if _v8_state.get("in_trade"):
            v8_open = True
            _v8_tok = int(_v8_state.get("token", 0) or 0)
            _v8_entry_px = float(_v8_state.get("entry_price", 0) or 0)
            _v8_state["_force_exit_ts"] = time.time()  # BUG-C fix: arm 3-min re-entry cooldown

    # Close any active shadow P1/P2 signals
    _shadow_closed = []
    for _sd, _sd_label in [(_v8_shadow_dt, "P1"), (_v8_shadow_p2, "P2")]:
        for _sdir in ("CE", "PE"):
            _sds = _sd[_sdir]
            if _sds.get("active"):
                _stok = int(_sds.get("entry_tok", 0) or 0)
                _sep  = float(_sds.get("entry_price", 0) or 0)
                _sltp = D.get_ltp(_stok) if _stok else 0
                _exit_px = _sltp if _sltp > 0 else _sep
                _spnl = round(_exit_px - _sep, 1)
                _speak = round(_sds.get("peak_pts", 0), 1)
                _slvl = _sds.get("shadow_level", "INITIAL")
                _tg_send(
                    f"🚨 SHADOW {_sd_label} {_sdir} — FORCE EXIT\n"
                    f"Entry: {_sep:.1f}  Exit: {_exit_px:.1f}\n"
                    f"PnL: {_spnl:+.1f}  Peak: +{_speak:.1f}  Trail: {_slvl}\n"
                    f"<i>⚠️ Shadow only</i>"
                )
                _sds.update({
                    "active": False, "entry_price": 0.0, "entry_time": "",
                    "peak_price": 0.0, "peak_pts": 0.0,
                    "shadow_sl": 0.0, "shadow_level": "INITIAL",
                    "last_exit_pnl": _spnl, "last_exit_reason": "FORCE-EXIT",
                    "last_exit_ts": time.time(),
                })
                _shadow_closed.append(f"{_sd_label}-{_sdir}")
                logger.warning(f"[CTRL] Force exit shadow {_sd_label} {_sdir} pnl={_spnl:+.1f}")
    if _shadow_closed:
        _save_shadow_state()

    if not v7_open and not v8_open and not _shadow_closed:
        _tg_send("No open trade.")
        return
    if v7_open or v8_open:
        _tg_send("🚨 Force exit triggered.")
        logger.warning("[CTRL] Force exit")
    if v8_open:
        _ltp = D.get_ltp(_v8_tok) if _v8_tok else 0
        if _ltp <= 0:
            _ltp = _v8_entry_px
        _v8_execute_paper_exit("FORCE_EXIT", round(_ltp, 2))


def _cmd_deploy(args):
    import subprocess
    _cwd = os.path.dirname(os.path.abspath(__file__))

    def _run(cmd):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=_cwd)
        combined = (r.stdout + r.stderr).strip()
        return combined, r.returncode

    _tg_send("📦 Pulling latest from main...")

    # capture commit hash before pull
    before_sha, _ = _run(["git", "rev-parse", "--short", "HEAD"])

    fetch_out, rc = _run(["git", "fetch", "origin", "main"])
    if rc != 0:
        _tg_send("❌ Fetch failed:\n<pre>" + fetch_out[-600:] + "</pre>")
        return
    reset_out, rc = _run(["git", "reset", "--hard", "origin/main"])
    if rc != 0:
        _tg_send("❌ Reset failed:\n<pre>" + reset_out[-600:] + "</pre>")
        return

    after_sha, _ = _run(["git", "rev-parse", "--short", "HEAD"])

    if before_sha == after_sha:
        _tg_send("✅ Already up to date (no changes).\nSHA: " + after_sha + "\n🔄 Restarting...")
    else:
        commits, _ = _run(["git", "log", before_sha + ".." + after_sha,
                            "--oneline", "--no-decorate"])
        files, _   = _run(["git", "diff", "--name-only", before_sha, after_sha])
        _tg_send(
            "✅ <b>DEPLOY SUMMARY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>SHA</b>  " + before_sha + " → " + after_sha + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Changes</b>\n<pre>" + (commits[:600] if commits else "—") + "</pre>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Files</b>\n<pre>" + (files[:300] if files else "—") + "</pre>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔄 Restarting now..."
        )

    logger.info(f"[CTRL] Deploy: {before_sha} -> {after_sha}, restarting")
    _remove_pid()
    time.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _cmd_restart(args):
    _tg_send("🔄 Restarting...")
    logger.info("[CTRL] Restart requested")
    _remove_pid()
    time.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _cmd_livecheck(args):
    try:
        with open(D.LIVE_LOG_FILE, "r") as f:
            lines = f.readlines()
        last_50 = "".join(lines[-50:])
        if len(last_50) > 4000:
            last_50 = last_50[-4000:]
        import re as _re
        last_50 = _re.sub(r'(api_key|access_token|token|secret|password)\s*[=:]\s*\S+',
                          r'\1=***', last_50, flags=_re.IGNORECASE)
        _tg_send("<pre>" + last_50 + "</pre>")
    except Exception as e:
        _tg_send("Log error: " + str(e))


def _read_today_shadow_trades() -> list:
    """Parse shadow trade entries+exits from today's log file."""
    import re as _re
    today_str = date.today().strftime("%Y-%m-%d")
    log_path  = D.LIVE_LOG_FILE if hasattr(D, 'LIVE_LOG_FILE') else os.path.expanduser("~/logs/live/vrl_live.log")
    signals   = {}   # key=(strat,dir,entry) → dict
    results   = []
    try:
        with open(log_path) as fh:
            for raw in fh:
                if today_str not in raw:
                    continue
                t_match = _re.match(r'\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})', raw)
                t_str   = t_match.group(1) if t_match else ''
                # SIGNAL lines
                m = _re.search(
                    r'\[(SHADOW-P[12])\] (\w+) \d+ SIGNAL entry=([\d.]+)', raw)
                if m:
                    strat, dir_, entry = m.group(1), m.group(2), float(m.group(3))
                    key = (strat, dir_, entry)
                    signals[key] = {'strat': strat, 'dir': dir_, 'entry': entry,
                                    'entry_time': t_str, 'exit': None, 'exit_time': None,
                                    'pnl': None, 'peak': None, 'reason': None}
                    continue
                # EXIT lines (V1 only — no 'V2' in line)
                if 'V2' not in raw:
                    m = _re.search(
                        r'\[(SHADOW-P[12])\] (\w+) (SL-HIT|PROFIT|TARGET\+\d+|EOD) '
                        r'entry=([\d.]+) exit=([\d.]+) pnl=([+\-\d.]+) peak=\+?([\d.]+)', raw)
                    if m:
                        strat, dir_, reason = m.group(1), m.group(2), m.group(3)
                        entry, exit_, pnl, peak = (float(m.group(4)), float(m.group(5)),
                                                   float(m.group(6)), float(m.group(7)))
                        key = (strat, dir_, entry)
                        if key in signals:
                            signals[key].update(exit=exit_, exit_time=t_str,
                                                pnl=pnl, peak=peak, reason=reason)
                            results.append(signals.pop(key))
    except Exception as e:
        logger.error("[CTRL] Shadow trades parse error: " + str(e))
    # Append still-open signals
    for key, sig in signals.items():
        results.append(sig)
    results.sort(key=lambda x: x['entry_time'])
    return results


def _cmd_trades(args):
    live_trades   = _read_today_trades()
    shadow_trades = _read_today_shadow_trades()

    if not live_trades and not shadow_trades:
        _tg_send("📒 No trades today.")
        return

    lines = ""
    total = 0.0
    idx   = 1

    # Live/paper V9 trades from CSV
    for t in live_trades:
        pts  = float(t.get("pnl_pts", 0))
        total += pts
        sign = "+" if pts >= 0 else ""
        icon = "✅" if pts >= 0 else "❌"
        peak = float(t.get("peak_pnl", 0))
        captured = round(pts / peak * 100) if peak > 0 else 0
        lines += (
            icon + " <b>V9 Trade " + str(idx) + "</b>  " + t.get("direction", "") + "\n"
            "  " + t.get("entry_time", "") + " → " + t.get("exit_time", "") + "\n"
            "  Entry: ₹" + str(t.get("entry_price", "")) + " → Exit: ₹" + str(t.get("exit_price", "")) + "\n"
            "  PNL: " + sign + str(round(pts, 1)) + "pts\n"
            "  Peak: +" + str(round(peak, 1)) + "pts  Captured: " + str(captured) + "%\n"
            "  Reason: " + t.get("exit_reason", "") + "\n"
        )
        idx += 1

    # Shadow trades from log
    shadow_total = 0.0
    for t in shadow_trades:
        pnl  = t.get('pnl')
        peak = t.get('peak') or 0.0
        is_open = pnl is None
        if not is_open:
            shadow_total += pnl
        icon = "🟢" if is_open else ("✅" if pnl > 0 else "❌")
        strat_label = t['strat'].replace('SHADOW-', '')
        exit_t = t.get('exit_time') or '—'
        pnl_str = ("🟢 open" if is_open else
                   ("+" if pnl >= 0 else "") + str(round(pnl, 1)) + "pts")
        peak_str = ("+?" if is_open else "+" + str(round(peak, 1))) + "pts"
        lines += (
            icon + " <b>" + strat_label + " S" + str(idx) + "</b>  " + t['dir'] + "\n"
            "  " + t['entry_time'] + " → " + exit_t + "\n"
            "  Entry: " + str(t['entry']) + "  Exit: " + (str(t.get('exit') or '—') + "\n"
            "  PNL: " + pnl_str + "  Peak: " + peak_str + "\n"
            "  Reason: " + (t.get('reason') or 'open') + "\n")
        )
        idx += 1

    total += shadow_total
    sign = "+" if total >= 0 else ""
    _tg_send(
        "📒 <b>TODAY'S TRADES</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + lines
        + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Shadow Net: " + ("+" if shadow_total >= 0 else "") + str(round(shadow_total, 1)) + "pts  "
        "| Total: " + sign + str(round(total, 1)) + "pts"
    )


def _cmd_xleg(args):
    try:
        import csv as _csv
        from datetime import date as _date, timedelta as _td
        path = D.TRADE_LOG_PATH
        if not os.path.isfile(path):
            _tg_send("📊 X-LEG: no trade log yet")
            return
        cutoff = (_date.today() - _td(days=7)).isoformat()
        rows = []
        with open(path, "r") as f:
            for r in _trade_csv_reader(f):
                if (r.get("date") or "") >= cutoff:
                    rows.append(r)
        if not rows:
            _tg_send("📊 X-LEG: no trades in last 7 days")
            return

        def _wins_losses(group):
            w = sum(1 for r in group if float(r.get("pnl_pts", 0) or 0) > 0)
            l = len(group) - w
            wr = round(w / len(group) * 100, 1) if group else 0.0
            avg_pts = (round(sum(float(r.get("pnl_pts", 0) or 0) for r in group)
                             / len(group), 2) if group else 0.0)
            return w, l, wr, avg_pts

        pass_rows = [r for r in rows if (r.get("xleg_signal") or "") == "PASS"]
        fail_rows = [r for r in rows if (r.get("xleg_signal") or "") == "FAIL"]
        na_rows   = [r for r in rows if (r.get("xleg_signal") or "") in ("", "NA")]

        pw, pl, pwr, pavg = _wins_losses(pass_rows)
        fw, fl, fwr, favg = _wins_losses(fail_rows)
        total = len(rows)

        _verdict = "—  insufficient data (need >=5 each)"
        if len(pass_rows) >= 5 and len(fail_rows) >= 5:
            if pwr >= 55 and pwr - fwr >= 10:
                _verdict = "✅ READY TO PROMOTE — PASS-WR " + str(pwr) + "% > 55% AND > FAIL by " + str(round(pwr-fwr,1)) + "%"
            elif pwr >= 55:
                _verdict = "⚠ PASS hits 55% but edge over FAIL is thin"
            else:
                _verdict = "❌ NOT READY — PASS-WR " + str(pwr) + "% < 55%"

        _gate_on = bool(CFG.entry_ema9_band("xleg_gate_enabled", True))
        _gate_line = ("🛡 GATE ENABLED — FAIL signals blocked at entry"
                      if _gate_on else
                      "ℹ GATE DISABLED — display-only logging")
        msg = (
            "📊 <b>X-LEG ACCURACY (last 7 days)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Total trades : " + str(total) + "\n"
            "PASS (other dying) : " + str(len(pass_rows)) + "\n"
            "FAIL (other hold)  : " + str(len(fail_rows)) + "\n"
            "NA   (no data)     : " + str(len(na_rows)) + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>PASS</b>  " + str(pw) + "W / " + str(pl) + "L  ("
            + str(pwr) + "%)   avg " + ("+" if pavg>=0 else "") + str(pavg) + " pts\n"
            "<b>FAIL</b>  " + str(fw) + "W / " + str(fl) + "L  ("
            + str(fwr) + "%)   avg " + ("+" if favg>=0 else "") + str(favg) + " pts\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Verdict</b>  " + _verdict + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + _gate_line + "\n"
            "Backtest 5d: V3 (both filters) +570 pts vs +163 baseline"
        )
        _tg_send(msg)
    except Exception as e:
        _tg_send("📊 X-LEG error: " + str(e))


def _cmd_vishal_stock_fno(args):
    try:
        import csv as _csv
        _tracker = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "screener", "fno_tracker.csv"
        )
        if not os.path.isfile(_tracker):
            _tg_send("📭 F&O tracker not found.")
            return

        with open(_tracker) as _f:
            rows = list(_csv.DictReader(_f))

        # Show OPEN + T1-HIT + SL-HIT rows (all active positions)
        active_rows = [r for r in rows if
                       str(r.get("status","")).startswith("OPEN") or
                       "HIT" in str(r.get("status",""))]
        if not active_rows:
            _tg_send("📭 No F&O positions.")
            return

        lines = ""
        total_pnl = 0.0
        open_count = 0
        for r in active_rows:
            st       = str(r.get("status",""))
            is_open  = st.startswith("OPEN")
            is_t1    = "T1-HIT" in st or "T3-HIT" in st
            is_sl    = "SL-HIT" in st
            if is_open: open_count += 1
            # Use pre-calculated CSV values — correct for any lot count
            ltp      = float(r.get("current_premium") or r.get("entry_premium") or 0)
            entry    = float(r.get("entry_premium") or 0)
            sl       = float(r.get("sl_premium") or 0)
            t1       = float(r.get("t1_premium") or 0)
            pnl_pct  = float(r.get("current_return_pct") or 0)
            pnl_rs   = float(r.get("pnl_rs") or 0)
            total_pnl += pnl_rs
            sign     = "+" if pnl_rs >= 0 else ""
            dist_sl  = round(ltp - sl, 2)
            dist_t1  = round(t1 - ltp, 2)
            if is_t1:   icon = "🎯"
            elif is_sl: icon = "❌"
            elif pnl_rs >= 0: icon = "✅"
            else: icon = "⚠️"
            lines += (
                icon + " <b>" + r["symbol"] + " " + r["direction"] + "</b>"
                + (" <i>" + st + "</i>" if not is_open else "") + "\n"
                "  Entry ₹" + str(round(entry,2)) + " → Now ₹" + str(round(ltp,2)) + "\n"
                "  P&L: " + sign + str(round(pnl_pct,1)) + "%  " + sign + "₹" + str(int(pnl_rs)) + "\n"
                + ("  SL ₹" + str(sl) + " (" + str(dist_sl) + " away)  T1 ₹" + str(t1) + " (" + str(dist_t1) + " away)\n" if is_open else "")
            )

        total_sign = "+" if total_pnl >= 0 else ""
        _tg_send(
            "📊 <b>F&O POSITIONS</b> · " + str(open_count) + " open\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + lines +
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Total P&L: " + total_sign + "₹" + str(int(total_pnl)) + "\n"
            "Updated: " + _now_str()
        )
    except Exception as e:
        _tg_send("📊 F&O error: " + str(e))
        logger.error("[FNO_CMD] " + str(e))


_DISPATCH = {
    "/help"               : _cmd_help,
    "/pulse"              : _cmd_pulse,
    "/status"             : _cmd_status,
    "/trades"             : _cmd_trades,
    "/account"            : _cmd_account,
    "/pause"              : _cmd_pause,
    "/resume"             : _cmd_resume,
    "/forceexit"          : _cmd_forceexit,
    "/deploy"             : _cmd_deploy,
    "/restart"            : _cmd_restart,
    "/livecheck"          : _cmd_livecheck,
    "/download"           : _cmd_download,
    "/xleg"               : _cmd_xleg,
    "/vishal_stock_fno"   : _cmd_vishal_stock_fno,
}


# ═══════════════════════════════════════════════════════════════
# === TRADE EXECUTION (merged from VRL_TRADE) ===
# ═══════════════════════════════════════════════════════════════

def _verify_timeout(kind: str, default: int) -> int:
    try:
        v = (CFG.get().get("trade") or {}).get("verify_timeout_" + kind)
        if v is not None:
            return int(v)
    except Exception:
        pass
    return default


# verify_order_fill(kite, ...) removed — orders now via MStock.
# Fill verification handled by MSTOCK.ms_verify_fill() inside VRL_MSTOCK.py.


def place_entry(kite, symbol: str, token: int,
                option_type: str, qty: int,
                entry_price_ref: float) -> dict:
    if D.PAPER_MODE:
        logger.info("[TRADE] PAPER ENTRY: " + symbol
                    + " qty=" + str(qty)
                    + " ref=" + str(round(entry_price_ref, 2)))
        return {
            "ok": True, "fill_price": round(entry_price_ref, 2),
            "fill_qty": qty,
            "order_id": "PAPER_" + datetime.now().strftime("%H%M%S%f")[:12],
            "error": "", "slippage": 0,
        }

    _first_live_flag = os.path.expanduser("~/state/.first_live_done")
    if not os.path.isfile(_first_live_flag):
        logger.info("[TRADE] 🚀 FIRST LIVE ORDER EVER")

    buffer = max(2.0, round(entry_price_ref * 0.01, 1))
    limit_price = round(entry_price_ref + buffer, 1)

    logger.info("[TRADE] LIMIT ENTRY: ref=" + str(round(entry_price_ref, 2))
                + " buffer=" + str(buffer) + " limit=" + str(limit_price)
                + " broker=MStock")

    mc     = MSTOCK.get_mstock()
    result = MSTOCK.ms_place_buy(mc, symbol, qty, limit_price,
                                 timeout_secs=_verify_timeout("entry", 8))

    if result["ok"] and not os.path.isfile(_first_live_flag):
        try:
            with open(_first_live_flag, "w") as _f:
                _f.write(datetime.now().isoformat())
        except Exception:
            pass

    if result["ok"] and result["fill_qty"] < qty:
        logger.warning("[TRADE] Partial fill accepted: "
                       + str(result["fill_qty"]) + "/" + str(qty))

    # Re-compute slippage relative to original ref price (not limit price)
    if result["ok"]:
        result["slippage"] = round(result["fill_price"] - entry_price_ref, 2)

    return result


def place_exit(kite, symbol: str, token: int,
               option_type: str, qty: int,
               exit_price_ref: float, reason: str) -> dict:
    if D.PAPER_MODE:
        logger.info("[TRADE] PAPER EXIT: " + symbol
                    + " qty=" + str(qty)
                    + " ref=" + str(round(exit_price_ref, 2))
                    + " reason=" + reason)
        return {
            "ok": True, "fill_price": round(exit_price_ref, 2),
            "fill_qty": qty,
            "order_id": "PAPER_" + datetime.now().strftime("%H%M%S%f")[:12],
            "error": "", "slippage": 0,
        }

    logger.info("[TRADE] MARKET EXIT: " + symbol
                + " qty=" + str(qty) + " reason=" + reason + " broker=MStock")

    mc     = MSTOCK.get_mstock()
    result = MSTOCK.ms_place_sell(mc, symbol, qty,
                                  timeout_secs=_verify_timeout("exit", 8))

    if result["ok"]:
        result["slippage"] = round(exit_price_ref - result["fill_price"], 2)
        return result

    # First attempt failed — retry once
    logger.warning("[TRADE] Exit attempt 1 failed — retrying")
    time.sleep(1)
    result = MSTOCK.ms_place_sell(mc, symbol, qty,
                                  timeout_secs=_verify_timeout("exit", 8))
    if result["ok"]:
        result["slippage"] = round(exit_price_ref - result["fill_price"], 2)
        return result

    logger.critical("CRITICAL: Exit failed for " + symbol
                    + " qty=" + str(qty) + ". MANUAL ACTION REQUIRED.")
    return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
            "order_id": "", "error": "EXIT_FAILED_MANUAL_REQUIRED", "slippage": 0}


# ── Telegram listener state ───────────────────────────────────
_tg_offset         = 0
_tg_last_update_id = 0
_tg_running        = False

def _tg_get_updates(offset: int) -> list:
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 20}, timeout=30)
        if resp.ok:
            return resp.json().get("result", [])
    except Exception as e:
        logger.warning("[CTRL] getUpdates error: " + type(e).__name__)
    return []

def _tg_authorized(message: dict) -> bool:
    return str(message.get("chat", {}).get("id", "")) == str(D.TELEGRAM_CHAT_ID)

def _tg_handle_message(message: dict):
    if not _tg_authorized(message):
        return
    text = message.get("text", "").strip()
    if not text.startswith("/"):
        return
    parts   = text.split()
    raw_cmd = parts[0].split("@")[0].lower()
    args    = parts[1:] if len(parts) > 1 else []
    handler = _DISPATCH.get(raw_cmd)
    if handler:
        handler(args)
    else:
        _WATCHDOG = ("/deploy","/serverstatus","/serverlog","/gitlog")
        if raw_cmd not in _WATCHDOG:
            _tg_send("Unknown command: " + raw_cmd + "\nType /help")

def _tg_handle_callback(callback: dict):
    msg = callback.get("message", {})
    if str(msg.get("chat", {}).get("id", "")) != str(D.TELEGRAM_CHAT_ID):
        return
    query_id = callback.get("id", "")
    _tg_answer_callback(query_id, "Unknown action")

def _tg_poll_loop():
    global _tg_offset, _tg_last_update_id
    logger.info("[CTRL] Telegram listener started " + D.VERSION)
    while _tg_running:
        updates = _tg_get_updates(_tg_offset)
        for upd in updates:
            uid          = upd["update_id"]
            _tg_offset   = uid + 1
            if uid <= _tg_last_update_id:
                continue
            _tg_last_update_id = uid
            try:
                if "message" in upd:
                    _tg_handle_message(upd["message"])
                elif "callback_query" in upd:
                    _tg_handle_callback(upd["callback_query"])
            except Exception as e:
                logger.error("[CTRL] Update error: " + str(e))
        time.sleep(1)

def _start_telegram_listener():
    global _tg_running, _tg_offset
    _tg_running = True

    try:
        url  = _TG_BASE + D.TELEGRAM_TOKEN + "/getUpdates"
        resp = requests.get(url, params={"offset": -1, "timeout": 1}, timeout=5)
        if resp.ok:
            updates = resp.json().get("result", [])
            if updates:
                _tg_offset = updates[-1]["update_id"] + 1
                logger.info("[CTRL] Discarded " + str(len(updates))
                            + " pending updates on startup")
    except Exception as e:
        logger.warning("[CTRL] Startup getUpdates skip: " + type(e).__name__)

    thread = threading.Thread(target=_tg_poll_loop, name="TGListener", daemon=True)
    thread.start()
    logger.info("[CTRL] Listener thread launched")

def _stop_telegram_listener():
    global _tg_running
    _tg_running = False

# ═══════════════════════════════════════════════════════════════
#  SHUTDOWN
# ═══════════════════════════════════════════════════════════════

def _shutdown(signum, frame):
    global _running
    logger.info("[MAIN] Shutdown signal received")
    _running = False
    _stop_telegram_listener()
    if state.get("in_trade"):
        _sym   = state.get("symbol", "?")
        _entry = round(state.get("entry_price", 0), 2)
        _pk    = round(state.get("peak_pnl", 0), 1)
        logger.warning("[MAIN] Shutdown with open trade — state preserved for resume"
                       " (symbol=" + _sym
                       + " entry=" + str(_entry)
                       + " peak=" + str(_pk) + ")")
        try:
            _tg_send(
                "⚠️ VRL SHUTDOWN with open position: " + _sym
                + " entry=" + str(_entry)
                + " peak=" + str(_pk),
                priority="critical",
            )
            time.sleep(1.5)
        except Exception as _tge:
            logger.debug("[MAIN] Shutdown telegram send failed: " + str(_tge))
    _save_state()
    _remove_pid()
    logger.info("[MAIN] Clean shutdown")
    sys.exit(0)

# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    global _kite
    logger.info("[MAIN] ═══ VISHAL RAJPUT TRADE " + D.VERSION + " STARTING ═══")
    _mode_str = "PAPER" if D.PAPER_MODE else "LIVE"
    logger.info("[MAIN] Mode: " + _mode_str)
    _tg_send(
        ("🟡 <b>Bot starting in PAPER mode</b>" if D.PAPER_MODE else "🟢 <b>Bot starting in LIVE mode</b>")
        + "\nVersion: " + D.VERSION
        + "\nMode: <b>" + _mode_str + "</b>"
        + ("\n⚠️ Real orders will be placed!" if not D.PAPER_MODE else ""),
        priority="critical"
    )
    logger.info("[MAIN] Scalps: DISABLED (data-backed decision)")

    _write_pid()
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        import json as _j
        from datetime import date as _dt_date
        _tok_path = D.TOKEN_FILE_PATH
        _tok_data = {}
        if os.path.isfile(_tok_path):
            with open(_tok_path) as _tf:
                _tok_data = _j.load(_tf)
        _tok_date = _tok_data.get("date", "")
        _today = _dt_date.today().isoformat()
        if _tok_date != _today:
            logger.warning("[MAIN] Token is from " + str(_tok_date or "MISSING")
                           + ", not today (" + _today + ") — forcing fresh auth")
            _tg_send("\u26a0\ufe0f Stale token detected on startup, auto-refreshing\n"
                     "Old: " + str(_tok_date or "MISSING") + " → New: " + _today)
        else:
            logger.info("[MAIN] Token freshness check: OK (" + _today + ")")
    except Exception as _te:
        logger.warning("[MAIN] Token freshness check error: " + str(_te))

    kite = get_kite()
    _kite = kite
    D.init(kite)

    # Phase 1 health: Token + REST spot (before WS starts — WS check happens after start_websocket)
    _health_lines_pre = []
    _health_ok_pre = True
    try:
        _prof = kite.profile()
        _health_lines_pre.append("Token: ✅ " + str(_prof.get("user_name", "?")))
    except Exception as _he:
        _health_lines_pre.append("Token: ❌ " + str(_he)[:60])
        _health_ok_pre = False
    try:
        _sq = kite.ltp(["NSE:NIFTY 50"])
        _sp = float(list(_sq.values())[0]["last_price"])
        _health_lines_pre.append("Spot: ✅ " + str(round(_sp, 1)))
    except Exception as _he:
        _health_lines_pre.append("Spot: ❌ " + str(_he)[:60])
        _health_ok_pre = False

    try:
        D.set_autoheal_callback(_tg_send)
    except Exception:
        pass

    try:
        D.fetch_account_info(kite)
    except Exception:
        pass

    live_lot_size = D.get_lot_size(kite)
    D.LOT_SIZE    = live_lot_size
    logger.info("[MAIN] Lot size from broker: " + str(live_lot_size))

    _load_state()
    _load_v8_state()
    _load_shadow_state()
    _reconcile_positions(kite)
    if state.get("in_trade") and not D.is_market_open():
        logger.warning("[MAIN] Startup with in_trade=True but market is CLOSED — clearing phantom state")
        _tg_send("⚠️ Phantom trade detected on startup — state cleared\n"
                 "Symbol: " + state.get("symbol", "?") + "\n"
                 "Entry: " + str(state.get("entry_price", 0)) + "\n"
                 "Peak: " + str(state.get("peak_pnl", 0)))
        with _state_lock:
            state["in_trade"] = False
            state["symbol"] = ""
            state["token"] = None
            state["direction"] = ""
            state["entry_price"] = 0.0
            state["entry_time"] = ""
            state["peak_pnl"] = 0.0
            state["candles_held"] = 0
            state["lot1_active"] = True
            state["lot2_active"] = True
            state["lots_split"] = False
            state["_static_floor_sl"] = 0
            state["current_floor"] = 0.0
        _save_state()
        logger.info("[MAIN] Phantom trade state cleared ✓")

    try:
        D.cleanup_old_lab_data()
    except Exception as e:
        logger.warning("[MAIN] Lab cleanup failed: " + str(e))
    try:
        D.audit_log_paths()
    except Exception as _ae:
        logger.debug("[MAIN] audit_log_paths error: " + str(_ae))

    try:
        import csv as _csv
        today_iso = date.today().isoformat()
        trades_today = []

        for log_path in [D.TRADE_LOG_PATH,
                         os.path.join(D.LAB_DIR, "vrl_trade_log.csv")]:
            if not os.path.isfile(log_path):
                continue
            try:
                with open(log_path) as f:
                    raw_rows = list(_trade_csv_reader(f))

                found = []
                for r in raw_rows:
                    if r.get("date", "").strip() == today_iso:
                        found.append(r)
                    elif r.get("trade_id", "").strip() == today_iso:
                        found.append({
                            **r,
                            "date"   : r.get("trade_id", ""),
                            "pnl_pts": r.get("pnl_points", r.get("pnl_pts", "0")),
                        })
                if found:
                    trades_today = found
                    break
            except Exception:
                continue

        if trades_today:
            def _get_pnl(row):
                for k in ["pnl_pts", "pnl_points", "pnl_rs", "pnl"]:
                    if k in row:
                        try: return float(row[k])
                        except (TypeError, ValueError): pass
                return 0.0

            wins   = [t for t in trades_today if _get_pnl(t) > 0]
            losses = [t for t in trades_today if _get_pnl(t) < 0]
            pnl    = sum(_get_pnl(t) for t in trades_today)

            with _state_lock:
                state["daily_pnl"] = round(pnl, 2)

            logger.info("[MAIN] Restored: " + str(len(trades_today))
                        + " trades | " + str(len(wins)) + "W / " + str(len(losses)) + "L | pnl="
                        + str(round(pnl,1)) + "pts")
        else:
            logger.info("[MAIN] No trades found for today — starting fresh")
    except Exception as e:
        logger.warning("[MAIN] Trade log restore failed: " + str(e))
    try:
        import csv as _sync_csv
        import VRL_DB as _sync_db
        _sync_db.init_db()
        _today_iso = date.today().isoformat()
        _csv_path = D.TRADE_LOG_PATH
        _csv_trades = []
        if os.path.isfile(_csv_path):
            with open(_csv_path) as _sf:
                for _row in _trade_csv_reader(_sf):
                    _d = _row.get("date", "").strip()
                    if _d == _today_iso:
                        _csv_trades.append(_row)
        _db_trades = _sync_db.get_trades(_today_iso)
        _db_times = {t.get("entry_time", "").strip() for t in _db_trades}
        _inserted = 0
        for _ct in _csv_trades:
            _et = _ct.get("entry_time", "").strip()
            if _et and _et not in _db_times:
                _sync_db.insert_trade(_ct)
                _inserted += 1
        if _inserted > 0:
            logger.info("[SYNC] CSV→DB backfill: " + str(_inserted) + " rows inserted")
        else:
            logger.info("[SYNC] CSV/DB in sync for " + _today_iso
                        + " (CSV=" + str(len(_csv_trades)) + " DB=" + str(len(_db_trades)) + ")")
    except Exception as _se:
        logger.warning("[SYNC] CSV→DB backfill failed: " + str(_se))

    D.start_websocket()
    D.subscribe_tokens([D.NIFTY_SPOT_TOKEN, D.INDIA_VIX_TOKEN])
    time.sleep(2)

    # Phase 2 health: WS tick check (runs after WS is started + subscribed)
    try:
        import time as _time_h
        _ws_ltp = 0.0
        _market_open_now = D.is_market_open()
        _health_lines_ws = list(_health_lines_pre)
        _health_ok_ws = _health_ok_pre
        if _market_open_now:
            for _ in range(30):  # up to 30s for WS to connect and deliver first tick
                _ws_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                if _ws_ltp > 0:
                    break
                _time_h.sleep(1)
            if _ws_ltp > 0:
                _health_lines_ws.append("WS: ✅ tick=" + str(round(_ws_ltp, 1)))
            else:
                _health_lines_ws.append("WS: ⚠️ no tick after 30s (feed may be down)")
                _health_ok_ws = False
        else:
            with D._tick_lock:
                _entry = D._ticks.get(int(D.NIFTY_SPOT_TOKEN))
            if _entry:
                _age_min = int((_time_h.time() - _entry["ts"]) / 60)
                _health_lines_ws.append(
                    "WS: 💤 market closed (last tick "
                    + str(_age_min) + "m ago at "
                    + str(round(_entry["ltp"], 1)) + ")"
                )
            else:
                _health_lines_ws.append("WS: 💤 market closed (no ticks yet)")
        _icon = "✅" if _health_ok_ws else "⚠️"
        _tg_send(
            _icon + " <b>TOKEN HEALTH CHECK</b>\n"
            + "\n".join(_health_lines_ws) + "\n"
            + "Time: " + datetime.now().strftime("%H:%M:%S IST")
        )
        logger.info("[MAIN] Token health: " + (" | ".join(_health_lines_ws)))
    except Exception as _the:
        logger.warning("[MAIN] Token health check error: " + str(_the))

    try:
        _pw = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "3minute", 30)
        if _pw is not None and not _pw.empty:
            logger.info("[MAIN] Pre-warm: " + str(len(_pw))
                        + " 3-min spot candles loaded from history")
        else:
            logger.warning("[MAIN] Pre-warm: no historical 3-min data returned")
    except Exception as _pwe:
        logger.warning("[MAIN] Pre-warm failed: " + str(_pwe))

    start_lab(kite)
    _start_telegram_listener()
    _alert_bot_started()

    # ── Compute institutional levels (shadow mode — no live blocking) ──
    try:
        LEVELS.compute_today(D, kite, None)
    except Exception as _le:
        logger.warning(f"[LEVELS] startup compute failed: {_le}")

    # ── VWAP startup compute ──────────────────────────────────────
    try:
        LEVELS.update_vwap(kite)
    except Exception as _ve:
        logger.warning(f"[VWAP] startup compute failed: {_ve}")

    logger.info("[MAIN] All systems ready. Strategy loop starting.")
    _strategy_loop(kite)

if __name__ == "__main__":
    main()
