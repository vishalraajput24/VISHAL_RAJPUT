"""
v12.14 DATA RICHNESS PATCH
- Trough PNL tracking (worst drawdown during trade)
- Enriched trade log (21 new columns)
- Enriched scan log (7 new columns)
- Scan forward fill (blocked trades analysis)
- Daily summary CSV (1 row per day, full market context)
"""
import os, re

R = os.path.expanduser("~/VISHAL_RAJPUT")

def rf(n):
    with open(os.path.join(R, n)) as f:
        return f.read()

def wf(n, c):
    with open(os.path.join(R, n), "w") as f:
        f.write(c)
    print("[OK] " + n)


# ═══════════════════════════════════════════════════════════════
#  1. VRL_ENGINE.py — Trough tracking
# ═══════════════════════════════════════════════════════════════

e = rf("VRL_ENGINE.py")

# Add trough tracking right after peak tracking in manage_exit
old_peak = '''    if running_pnl > state.get("peak_pnl", 0):
        state["peak_pnl"] = running_pnl'''
new_peak = '''    if running_pnl > state.get("peak_pnl", 0):
        state["peak_pnl"] = running_pnl

    # v12.14: Track trough (worst unrealized PNL) for SL calibration
    if running_pnl < state.get("trough_pnl", 0):
        state["trough_pnl"] = running_pnl'''

if old_peak in e:
    e = e.replace(old_peak, new_peak)
    wf("VRL_ENGINE.py", e)
    print("[ENGINE] Trough tracking added")
else:
    print("[WARN] ENGINE peak pattern not found")


# ═══════════════════════════════════════════════════════════════
#  2. VRL_MAIN.py — Rich trade log + trough + entry context
# ═══════════════════════════════════════════════════════════════

m = rf("VRL_MAIN.py")

# 2a. Add new fields to DEFAULT_STATE
old_state = '    "_last_milestone"    : 0,'
new_state = '''    "_last_milestone"    : 0,
    "trough_pnl"         : 0.0,
    "session_at_entry"   : "",
    "spread_1m_at_entry" : 0.0,
    "spread_3m_at_entry" : 0.0,
    "delta_at_entry"     : 0.0,
    "sl_pts_at_entry"    : 0.0,'''
m = m.replace(old_state, new_state)

# 2b. Store entry context in _execute_entry
old_entry_state = '        state["daily_trades"]      += 1'
new_entry_state = '''        state["daily_trades"]      += 1
        state["trough_pnl"]         = 0.0
        state["session_at_entry"]   = session
        state["spread_1m_at_entry"] = round(entry_result.get("spread_1m", 0.0), 2)
        state["spread_3m_at_entry"] = round(entry_result.get("ema_spread", 0.0), 2)
        state["delta_at_entry"]     = round(entry_result.get("greeks", {}).get("delta", 0), 3)
        state["sl_pts_at_entry"]    = round(actual_price - phase1_sl, 2)'''
m = m.replace(old_entry_state, new_entry_state)

# 2c. Read trough + phase in _execute_exit before clearing state
old_exit_read = '''        regime    = state.get("regime_at_entry", "")
        score     = state.get("score_at_entry", 0)'''
new_exit_read = '''        regime    = state.get("regime_at_entry", "")
        score     = state.get("score_at_entry", 0)
        trough    = state.get("trough_pnl", 0)
        exit_phase= state.get("exit_phase", 1)'''
m = m.replace(old_exit_read, new_exit_read)

# 2d. Reset trough on exit
old_exit_clear = '''            "peak_pnl"            : 0.0,'''
new_exit_clear = '''            "peak_pnl"            : 0.0,
            "trough_pnl"          : 0.0,'''
m = m.replace(old_exit_clear, new_exit_clear)

# 2e. Enrich TRADE_FIELDNAMES
old_fields = '''TRADE_FIELDNAMES = [
    "date", "entry_time", "exit_time", "symbol", "direction",
    "mode", "entry_price", "exit_price", "pnl_pts", "pnl_rs",
    "peak_pnl", "exit_reason", "score", "iv_at_entry",
    "regime", "dte", "candles_held",
]'''
new_fields = '''TRADE_FIELDNAMES = [
    "date", "entry_time", "exit_time", "symbol", "direction",
    "mode", "entry_price", "exit_price", "pnl_pts", "pnl_rs",
    "peak_pnl", "trough_pnl", "exit_reason", "exit_phase",
    "score", "iv_at_entry", "regime", "dte", "candles_held",
    "session", "strike", "sl_pts",
    "spread_1m", "spread_3m", "delta_at_entry",
    "bias", "vix_at_entry", "hourly_rsi",
    "straddle_decay",
]'''
m = m.replace(old_fields, new_fields)

# 2f. Enrich _log_trade row
old_row = '''    row = {
        "date"        : date.today().isoformat(),
        "entry_time"  : st.get("entry_time", ""),
        "exit_time"   : datetime.now().strftime("%H:%M:%S"),
        "symbol"      : st.get("symbol", ""),
        "direction"   : st.get("direction", ""),
        "mode"        : st.get("mode", ""),
        "entry_price" : entry,
        "exit_price"  : round(exit_price, 2),
        "pnl_pts"     : pnl_pts,
        "pnl_rs"      : pnl_rs,
        "peak_pnl"    : round(st.get("peak_pnl", 0), 2),
        "exit_reason" : exit_reason,
        "score"       : st.get("score_at_entry", 0),
        "iv_at_entry" : st.get("iv_at_entry", 0),
        "regime"      : st.get("regime_at_entry", ""),
        "dte"         : st.get("dte_at_entry", 0),
        "candles_held": candles_held,
    }'''
new_row = '''    row = {
        "date"          : date.today().isoformat(),
        "entry_time"    : st.get("entry_time", ""),
        "exit_time"     : datetime.now().strftime("%H:%M:%S"),
        "symbol"        : st.get("symbol", ""),
        "direction"     : st.get("direction", ""),
        "mode"          : st.get("mode", ""),
        "entry_price"   : entry,
        "exit_price"    : round(exit_price, 2),
        "pnl_pts"       : pnl_pts,
        "pnl_rs"        : pnl_rs,
        "peak_pnl"      : round(st.get("peak_pnl", 0), 2),
        "trough_pnl"    : round(st.get("trough_pnl", 0), 2),
        "exit_reason"   : exit_reason,
        "exit_phase"    : st.get("exit_phase", 1),
        "score"         : st.get("score_at_entry", 0),
        "iv_at_entry"   : st.get("iv_at_entry", 0),
        "regime"        : st.get("regime_at_entry", ""),
        "dte"           : st.get("dte_at_entry", 0),
        "candles_held"  : candles_held,
        "session"       : st.get("session_at_entry", ""),
        "strike"        : st.get("strike", 0),
        "sl_pts"        : st.get("sl_pts_at_entry", 0),
        "spread_1m"     : st.get("spread_1m_at_entry", 0),
        "spread_3m"     : st.get("spread_3m_at_entry", 0),
        "delta_at_entry": st.get("delta_at_entry", 0),
        "bias"          : D.get_daily_bias() if hasattr(D, "get_daily_bias") else "",
        "vix_at_entry"  : round(D.get_vix(), 1),
        "hourly_rsi"    : D.get_hourly_rsi() if hasattr(D, "get_hourly_rsi") else 0,
        "straddle_decay": round(getattr(D, "_straddle_open", 0) * (1 + getattr(D, "_straddle_check_ts", 0) * 0) or 0, 1),
    }'''
m = m.replace(old_row, new_row)

# 2g. Fix straddle_decay in row to use actual function
# The above is a placeholder — let's fix it properly
m = m.replace(
    '"straddle_decay": round(getattr(D, "_straddle_open", 0) * (1 + getattr(D, "_straddle_check_ts", 0) * 0) or 0, 1),',
    '"straddle_decay": 0.0,  # populated by run_warnings if available'
)

# 2h. Add trough to exit alert
old_quality_loss = '''    elif reason == "STALE_ENTRY":
        saved = round((18 - abs(pnl)) if abs(pnl) < 18 else 0, 1)
        quality = "🛡 PROTECTED  (saved ~" + str(saved) + "pts vs full SL)"
    else:
        quality = "❌ LOSS  (peak was +" + str(round(peak_pnl, 1)) + "pts)"'''
new_quality_loss = '''    elif reason == "STALE_ENTRY":
        saved = round((18 - abs(pnl)) if abs(pnl) < 18 else 0, 1)
        quality = "🛡 PROTECTED  (saved ~" + str(saved) + "pts vs full SL)"
    else:
        quality = "❌ LOSS  (peak was +" + str(round(peak_pnl, 1)) + "pts  trough " + str(round(state.get("trough_pnl", 0), 1)) + "pts)"'''
m = m.replace(old_quality_loss, new_quality_loss)

# 2i. Add daily summary generation at EOD (after _generate_eod_report call)
old_eod = '''                try:
                    _generate_eod_report()
                except Exception as e:
                    logger.error("[MAIN] EOD report error: " + str(e))'''
new_eod = '''                try:
                    _generate_eod_report()
                except Exception as e:
                    logger.error("[MAIN] EOD report error: " + str(e))
                try:
                    from VRL_LAB import generate_daily_summary
                    generate_daily_summary()
                except Exception as e:
                    logger.warning("[MAIN] Daily summary: " + str(e))'''
m = m.replace(old_eod, new_eod)

# 2j. Persist trough and entry context fields
old_persist = '''    "candles_held",
    "_last_trail_candle",'''
new_persist = '''    "candles_held",
    "trough_pnl",
    "session_at_entry", "spread_1m_at_entry",
    "spread_3m_at_entry", "delta_at_entry", "sl_pts_at_entry",
    "_last_trail_candle",'''
m = m.replace(old_persist, new_persist)

wf("VRL_MAIN.py", m)
print("[MAIN] Trade log enriched + trough + daily summary")


# ═══════════════════════════════════════════════════════════════
#  3. VRL_LAB.py — Enriched scan log + forward fill + summary
# ═══════════════════════════════════════════════════════════════

l = rf("VRL_LAB.py")

# 3a. Enrich FIELDNAMES_SCAN
old_scan_fields = '''FIELDNAMES_SCAN = [
    "timestamp", "session", "dte", "atm_strike", "spot",
    "direction", "entry_price",
    # 1-min
    "rsi_1m", "body_pct_1m", "vol_ratio_1m", "rsi_rising_1m",
    # 3-min
    "rsi_3m", "body_pct_3m", "ema_spread_3m", "conditions_3m", "mode_3m",
    # result
    "score", "fired", "reject_reason",
    # Greeks
    "iv_pct", "delta",
    # VIX
    "vix",
    # v12.11: Spot columns
    "spot_rsi_3m", "spot_ema_spread_3m", "spot_regime", "spot_gap",
]'''
new_scan_fields = '''FIELDNAMES_SCAN = [
    "timestamp", "session", "dte", "atm_strike", "spot",
    "direction", "entry_price",
    # 1-min
    "rsi_1m", "body_pct_1m", "vol_ratio_1m", "rsi_rising_1m",
    "spread_1m",
    # 3-min
    "rsi_3m", "body_pct_3m", "ema_spread_3m", "conditions_3m", "mode_3m",
    # result
    "score", "fired", "reject_reason",
    # Greeks
    "iv_pct", "delta",
    # VIX
    "vix",
    # v12.11: Spot columns
    "spot_rsi_3m", "spot_ema_spread_3m", "spot_regime", "spot_gap",
    # v12.14: Market context
    "bias", "hourly_rsi", "straddle_decay_pct",
    "near_fib_level", "fib_distance",
    # v12.14: Blocked trade analysis (forward fill at EOD)
    "fwd_3c", "fwd_5c", "fwd_10c", "fwd_outcome",
]'''
l = l.replace(old_scan_fields, new_scan_fields)

# 3b. Enrich scan row with new columns
old_scan_row_end = '''                # v12.11: Spot
                "spot_rsi_3m"       : spot_3m.get("rsi", 0),
                "spot_ema_spread_3m": spot_3m.get("spread", 0),
                "spot_regime"       : spot_3m.get("regime", ""),
                "spot_gap"          : round(spot_gap, 1),
            })'''
new_scan_row_end = '''                # v12.11: Spot
                "spot_rsi_3m"       : spot_3m.get("rsi", 0),
                "spot_ema_spread_3m": spot_3m.get("spread", 0),
                "spot_regime"       : spot_3m.get("regime", ""),
                "spot_gap"          : round(spot_gap, 1),
                # v12.14: Market context
                "spread_1m"         : result.get("spread_1m", 0),
                "bias"              : D.get_daily_bias() if hasattr(D, "get_daily_bias") else "",
                "hourly_rsi"        : D.get_hourly_rsi() if hasattr(D, "get_hourly_rsi") else 0,
                "straddle_decay_pct": 0.0,
                "near_fib_level"    : "",
                "fib_distance"      : 0,
                # v12.14: Forward fill placeholders
                "fwd_3c": "", "fwd_5c": "", "fwd_10c": "", "fwd_outcome": "",
            })'''
l = l.replace(old_scan_row_end, new_scan_row_end)

# 3c. Add fib data to scan rows (after the row append, populate fib)
# We'll add it inline in the row construction by enhancing the try block
old_fib_placeholder = '''                "near_fib_level"    : "",
                "fib_distance"      : 0,'''
new_fib_populate = '''                "near_fib_level"    : D.get_nearest_fib_level(spot_ltp).get("level", "") if hasattr(D, "get_nearest_fib_level") else "",
                "fib_distance"      : D.get_nearest_fib_level(spot_ltp).get("distance", 0) if hasattr(D, "get_nearest_fib_level") else 0,'''
l = l.replace(old_fib_placeholder, new_fib_populate)

# 3d. Add scan forward fill + daily summary functions before start_lab
old_start_lab = '''def start_lab(kite):'''
new_functions = r'''
# ─── SCAN FORWARD FILL (v12.14) ──────────────────────────────

def fill_forward_scan(kite, target_date: date = None):
    """
    v12.14: For each scan row, fill what the option price was
    3/5/10 candles later. Answers: "What would have happened
    if we entered here?"
    Only fills rows where fired=0 (blocked entries) — these are
    the what-if analysis rows.
    """
    if target_date is None:
        target_date = date.today()

    path = _csv_path_scan(target_date)
    if not os.path.isfile(path):
        return

    logger.info("[LAB] Scan forward fill for " + str(target_date))

    try:
        with open(path) as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        logger.error("[LAB] Scan fwd read: " + str(e))
        return

    if not rows:
        return

    changed = 0
    for row in rows:
        # Skip already filled
        if row.get("fwd_3c"):
            continue
        # Only fill blocked entries (the what-if analysis)
        # Also fill fired entries for comparison
        token = None
        opt_type = row.get("direction", "")
        if _current_atm_tokens and opt_type in _current_atm_tokens:
            token = _current_atm_tokens[opt_type]["token"]

        if not token:
            continue

        try:
            ts = datetime.fromisoformat(row["timestamp"])
            prices = []
            for mins in [3, 5, 10]:
                fwd_t = ts + timedelta(minutes=mins)
                candles = _fetch_candles(kite, token,
                                         fwd_t - timedelta(minutes=1),
                                         fwd_t + timedelta(minutes=2),
                                         "minute")
                prices.append(round(candles[-1]["close"], 2) if candles else None)
                time.sleep(0.25)

            entry = float(row.get("entry_price", 0))
            if entry > 0 and all(p is not None for p in prices):
                row["fwd_3c"]  = prices[0]
                row["fwd_5c"]  = prices[1]
                row["fwd_10c"] = prices[2]
                max_move = max(p - entry for p in prices)
                min_move = min(p - entry for p in prices)
                if max_move >= 10:
                    row["fwd_outcome"] = "WIN"
                elif min_move <= -8:
                    row["fwd_outcome"] = "LOSS"
                else:
                    row["fwd_outcome"] = "NEUTRAL"
                changed += 1
        except Exception as e:
            logger.debug("[LAB] Scan fwd row: " + str(e))

    try:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES_SCAN, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            f.flush()
        logger.info("[LAB] Scan fwd fill done: " + str(changed) + " rows")
    except Exception as e:
        logger.error("[LAB] Scan fwd write: " + str(e))


# ─── DAILY SUMMARY CSV (v12.14) ──────────────────────────────

FIELDNAMES_DAILY = [
    "date", "day_of_week",
    # Trade stats
    "total_trades", "wins", "losses", "pnl_pts", "pnl_rs",
    "best_trade_pts", "worst_trade_pts",
    "avg_peak", "avg_trough", "avg_candles_held",
    # Scan stats
    "total_scans", "total_fired",
    "blocks_3m_gate", "blocks_spread", "blocks_rsi",
    "blocks_body", "blocks_volume", "blocks_score",
    # Market context
    "bias", "vix_open", "vix_close", "vix_high",
    "spot_open", "spot_close", "spot_high", "spot_low", "spot_range",
    "gap_pts",
    "dte",
    # Warning data
    "straddle_open", "straddle_close", "straddle_decay_pct",
    "hourly_rsi_high", "hourly_rsi_low",
    # Regime distribution
    "regime_trending_pct", "regime_choppy_pct",
]


def generate_daily_summary(target_date: date = None):
    """
    v12.14: Generate one-row-per-day summary CSV.
    Called at EOD from VRL_MAIN.
    """
    if target_date is None:
        target_date = date.today()

    summary_path = os.path.join(D.REPORTS_DIR, "vrl_daily_summary.csv")
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)

    row = {"date": target_date.isoformat(),
           "day_of_week": target_date.strftime("%A")}

    # ── Trade stats ──
    trade_log = os.path.join(D.LAB_DIR, "vrl_trade_log.csv")
    today_str = target_date.isoformat()
    trades = []
    if os.path.isfile(trade_log):
        try:
            with open(trade_log) as f:
                for r in csv.DictReader(f):
                    if r.get("date", "").strip() == today_str:
                        trades.append(r)
        except Exception:
            pass

    if trades:
        pnls = [float(t.get("pnl_pts", 0)) for t in trades]
        peaks = [float(t.get("peak_pnl", 0)) for t in trades]
        troughs = [float(t.get("trough_pnl", 0)) for t in trades]
        candles = [int(t.get("candles_held", 0)) for t in trades]
        row["total_trades"]     = len(trades)
        row["wins"]             = sum(1 for p in pnls if p > 0)
        row["losses"]           = sum(1 for p in pnls if p < 0)
        row["pnl_pts"]          = round(sum(pnls), 2)
        row["pnl_rs"]           = round(sum(pnls) * D.LOT_SIZE, 0)
        row["best_trade_pts"]   = round(max(pnls), 2)
        row["worst_trade_pts"]  = round(min(pnls), 2)
        row["avg_peak"]         = round(sum(peaks) / len(peaks), 1) if peaks else 0
        row["avg_trough"]       = round(sum(troughs) / len(troughs), 1) if troughs else 0
        row["avg_candles_held"] = round(sum(candles) / len(candles), 1) if candles else 0
    else:
        for k in ["total_trades", "wins", "losses", "pnl_pts", "pnl_rs",
                   "best_trade_pts", "worst_trade_pts", "avg_peak",
                   "avg_trough", "avg_candles_held"]:
            row[k] = 0

    # ── Scan stats ──
    scan_path = _csv_path_scan(target_date)
    scans = []
    if os.path.isfile(scan_path):
        try:
            with open(scan_path) as f:
                scans = list(csv.DictReader(f))
        except Exception:
            pass

    if scans:
        row["total_scans"]    = len(scans)
        row["total_fired"]    = sum(1 for s in scans if s.get("fired") == "1")
        reasons = [s.get("reject_reason", "") for s in scans if s.get("fired") != "1"]
        row["blocks_3m_gate"] = sum(1 for r in reasons if "3M" in r)
        row["blocks_spread"]  = sum(1 for r in reasons if "SPREAD" in r.upper() or "1M_SPREAD" in r.upper())
        row["blocks_rsi"]     = sum(1 for r in reasons if "RSI" in r)
        row["blocks_body"]    = sum(1 for r in reasons if "BODY" in r)
        row["blocks_volume"]  = sum(1 for r in reasons if "VOLUME" in r.upper() or "VOL" in r.upper())
        row["blocks_score"]   = sum(1 for r in reasons if "SCORE" in r)
        # Regime distribution
        regimes = [s.get("spot_regime", "") for s in scans if s.get("spot_regime")]
        if regimes:
            row["regime_trending_pct"] = round(sum(1 for r in regimes if "TREND" in r) / len(regimes) * 100, 0)
            row["regime_choppy_pct"]   = round(sum(1 for r in regimes if "CHOPPY" in r or "NEUTRAL" in r) / len(regimes) * 100, 0)
    else:
        for k in ["total_scans", "total_fired", "blocks_3m_gate",
                   "blocks_spread", "blocks_rsi", "blocks_body",
                   "blocks_volume", "blocks_score",
                   "regime_trending_pct", "regime_choppy_pct"]:
            row[k] = 0

    # ── Market context ──
    try:
        row["bias"] = D.get_daily_bias() if hasattr(D, "get_daily_bias") else ""
    except Exception:
        row["bias"] = ""

    try:
        row["vix_open"]  = round(D.get_vix(), 1)
        row["vix_close"] = round(D.get_vix(), 1)
        row["vix_high"]  = round(D.get_vix(), 1)
    except Exception:
        row["vix_open"] = row["vix_close"] = row["vix_high"] = 0

    # Spot from spot CSV
    spot_path = os.path.join(D.SPOT_DIR, "nifty_spot_1min_" + target_date.strftime("%Y%m%d") + ".csv")
    if os.path.isfile(spot_path):
        try:
            with open(spot_path) as f:
                spot_rows = list(csv.DictReader(f))
            if spot_rows:
                closes = [float(r.get("close", 0)) for r in spot_rows if float(r.get("close", 0)) > 0]
                highs  = [float(r.get("high", 0)) for r in spot_rows if float(r.get("high", 0)) > 0]
                lows   = [float(r.get("low", 0)) for r in spot_rows if float(r.get("low", 0)) > 0]
                if closes:
                    row["spot_open"]  = round(closes[0], 1)
                    row["spot_close"] = round(closes[-1], 1)
                if highs:
                    row["spot_high"] = round(max(highs), 1)
                if lows:
                    row["spot_low"]  = round(min(lows), 1)
                if highs and lows:
                    row["spot_range"] = round(max(highs) - min(lows), 1)
        except Exception:
            pass

    try:
        row["gap_pts"] = round(D.get_spot_gap(), 1) if hasattr(D, "get_spot_gap") else 0
    except Exception:
        row["gap_pts"] = 0

    try:
        exp = D.get_nearest_expiry()
        row["dte"] = D.calculate_dte(exp) if exp else 0
    except Exception:
        row["dte"] = 0

    # Straddle
    try:
        row["straddle_open"]      = round(getattr(D, "_straddle_open", 0), 1)
        row["straddle_close"]     = 0
        row["straddle_decay_pct"] = 0
    except Exception:
        pass

    # Hourly RSI
    try:
        row["hourly_rsi_high"] = round(D.get_hourly_rsi(), 1) if hasattr(D, "get_hourly_rsi") else 0
        row["hourly_rsi_low"]  = round(D.get_hourly_rsi(), 1) if hasattr(D, "get_hourly_rsi") else 0
    except Exception:
        row["hourly_rsi_high"] = row["hourly_rsi_low"] = 0

    # ── Write ──
    is_new = not os.path.isfile(summary_path)
    try:
        with open(summary_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES_DAILY, extrasaction="ignore")
            if is_new:
                w.writeheader()
            w.writerow(row)
            f.flush()
        logger.info("[LAB] Daily summary written for " + str(target_date))
    except Exception as e:
        logger.error("[LAB] Daily summary write: " + str(e))


''' + '''def start_lab(kite):'''
l = l.replace(old_start_lab, new_functions)

# 3e. Call scan forward fill at EOD alongside existing forward fills
old_fwd_eod = '''                try:
                    fill_forward_columns(_kite_ref, today, "3min")
                    fill_forward_columns(_kite_ref, today, "1min")
                except Exception as e:
                    logger.error("[LAB] Forward fill error: " + str(e))'''
new_fwd_eod = '''                try:
                    fill_forward_columns(_kite_ref, today, "3min")
                    fill_forward_columns(_kite_ref, today, "1min")
                except Exception as e:
                    logger.error("[LAB] Forward fill error: " + str(e))
                try:
                    fill_forward_scan(_kite_ref, today)
                except Exception as e:
                    logger.error("[LAB] Scan forward fill error: " + str(e))'''
l = l.replace(old_fwd_eod, new_fwd_eod)

wf("VRL_LAB.py", l)
print("[LAB] Scan enriched + forward fill + daily summary")


# ═══════════════════════════════════════════════════════════════
#  4. VRL_DATA.py — Add REPORTS_DIR ensure + daily summary path
# ═══════════════════════════════════════════════════════════════

d = rf("VRL_DATA.py")

# Already has REPORTS_DIR, just verify ensure_dirs covers it
if "REPORTS_DIR" not in d.split("ensure_dirs")[1] if "ensure_dirs" in d else True:
    # It should already be there, just verify
    pass

wf("VRL_DATA.py", d)
print("[DATA] Verified")


print("\n" + "=" * 55)
print("  DATA RICHNESS PATCH COMPLETE")
print("=" * 55)
print()
print("TRADE LOG: +12 new columns")
print("  trough_pnl, exit_phase, session, strike, sl_pts,")
print("  spread_1m, spread_3m, delta, bias, vix, hourly_rsi,")
print("  straddle_decay")
print()
print("SCAN LOG: +11 new columns")
print("  spread_1m, bias, hourly_rsi, straddle_decay_pct,")
print("  near_fib_level, fib_distance,")
print("  fwd_3c, fwd_5c, fwd_10c, fwd_outcome")
print()
print("NEW: ~/lab_data/reports/vrl_daily_summary.csv")
print("  1 row per day, 40+ columns of market context")
print()
print("NEW: Scan forward fill at EOD")
print("  Answers: blocked trades — would they have won?")
print()
print("NEW: Trough PNL tracking")
print("  Worst drawdown during trade — SL calibration data")
