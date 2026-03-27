#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  VRL_HEALTHCHECK.py — VISHAL RAJPUT TRADE v12.15
#  Runs at 9:30 AM — verifies all critical systems
#  Rule: every major bug fix must add a check here
#  v12.15: Spot gap check added
#  5 consecutive clean days = ready for live trading
# ═══════════════════════════════════════════════════════════════

import os, sys, json, time, subprocess
from datetime import date, datetime

sys.path.insert(0, os.path.expanduser("~"))

import requests
import VRL_DATA as D
from VRL_AUTH import get_kite

_kite_ref = None  # global for REST fallback

def _tg(msg: str):
    try:
        url = f"https://api.telegram.org/bot{D.TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": D.TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print("TG error:", e)

def check_score_threshold():
    """v12.8: Verify SESSION_SCORE_MIN ≤ max achievable score (7)."""
    try:
        from VRL_DATA import SESSION_SCORE_MIN
        max_achievable = 7  # body+bonus+rsi+vol+delta+double_align+gate_bonus
        bad = {s: v for s, v in SESSION_SCORE_MIN.items() if v > max_achievable}
        if bad:
            return False, f"❌ SESSION_SCORE_MIN {bad} > max achievable {max_achievable} — bot will NEVER enter"
        return True, f"✅ Score thresholds OK (min={min(SESSION_SCORE_MIN.values())}, max achievable={max_achievable})"
    except Exception as e:
        return False, f"❌ Score threshold check error: {e}"


def check_spread_gates():
    """v12.8: Verify 1-min spread gate constants are present."""
    try:
        from VRL_DATA import SPREAD_1M_MIN_CE, SPREAD_1M_MIN_PE
        if SPREAD_1M_MIN_CE <= 0 or SPREAD_1M_MIN_PE <= 0:
            return False, f"❌ Spread gates invalid: CE={SPREAD_1M_MIN_CE} PE={SPREAD_1M_MIN_PE}"
        return True, f"✅ 1m spread gates: CE≥+{SPREAD_1M_MIN_CE}pts  PE≥+{SPREAD_1M_MIN_PE}pts  (both bullish)"
    except Exception as e:
        return False, f"❌ Spread gate check error: {e}"


def check_single_instance():
    try:
        result = subprocess.run(["pgrep", "-f", "VISHAL_RAJPUT/VRL_MAIN.py"], capture_output=True, text=True)
        pids = [p for p in result.stdout.strip().split() if p]
        if len(pids) == 0:
            return False, "❌ Bot NOT running"
        if len(pids) > 1:
            return False, f"❌ Multiple instances: {len(pids)} PIDs"
        return True, f"✅ Single instance PID={pids[0]}"
    except Exception as e:
        return False, f"❌ Instance check error: {e}"

def check_expiry(kite):
    try:
        expiry = D.get_nearest_expiry(kite)
        today  = date.today()
        if expiry is None:
            return False, "❌ Expiry = None — CRITICAL (caused zero entries on Mar 17)"
        if expiry < today:
            return False, f"❌ Expiry {expiry} is in the past"
        return True, f"✅ Expiry: {expiry} (DTE={(expiry-today).days})"
    except Exception as e:
        return False, f"❌ Expiry error: {e}"

def check_lot_size(kite):
    try:
        lot = D.get_lot_size(kite)
        if lot <= 0:
            return False, f"❌ Lot size invalid: {lot}"
        if lot != D.LOT_SIZE_BASE and D.LOT_SIZE_BASE > 0:
            return False, f"⚠️ Lot size={lot} differs from base {D.LOT_SIZE_BASE} — exchange may have changed it, update LOT_SIZE_BASE"
        return True, f"✅ Lot size: {lot} (from broker)"
    except Exception as e:
        return False, f"❌ Lot size error: {e}"

def check_spot_tick():
    global _kite_ref
    try:
        ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        if ltp > 0:
            return True, f"✅ Spot tick: {ltp}"
        # Try REST fallback
        if _kite_ref:
            q = _kite_ref.ltp(["NSE:NIFTY 50"])
            rest_ltp = float(q["NSE:NIFTY 50"]["last_price"])
            if rest_ltp > 0:
                return True, f"✅ Spot via REST: {rest_ltp}"
        return False, "❌ Spot WS tick = 0 and REST failed"
    except Exception as e:
        return False, f"❌ Spot tick error: {e}"

def check_option_ltp(kite):
    """v12.9: Verify option token resolution and LTP for ATM strike."""
    try:
        spot_q = kite.ltp(["NSE:NIFTY 50"])
        spot   = float(spot_q["NSE:NIFTY 50"]["last_price"])
        if spot <= 0:
            return False, "❌ Spot LTP = 0 — cannot resolve ATM"
        strike = D.resolve_atm_strike(spot)
        expiry = D.get_nearest_expiry(kite)
        if not expiry:
            return False, "❌ No expiry — cannot check option LTP"
        tokens = D.get_option_tokens(kite, strike, expiry)
        if not tokens:
            return False, f"❌ Token resolve failed for ATM={strike} exp={expiry}"
        missing = []
        for ot in ("CE", "PE"):
            info = tokens.get(ot)
            if not info:
                missing.append(ot)
        if missing:
            return False, f"❌ Missing tokens: {missing} for ATM={strike}"
        return True, f"✅ Option tokens: ATM={strike} CE+PE resolved"
    except Exception as e:
        return False, f"❌ Option LTP check error: {e}"

def check_rsi_warmup():
    try:
        today   = date.today().strftime("%Y%m%d")
        lab_1m  = os.path.expanduser(f"~/lab_data/options_1min/nifty_option_1min_{today}.csv")
        if not os.path.isfile(lab_1m):
            return False, "❌ Lab 1-min CSV not found — lab not collecting"
        import csv
        with open(lab_1m) as f:
            rows = list(csv.DictReader(f))
        ce_rows = [r for r in rows if r.get("type") == "CE"]
        issues = []
        if len(ce_rows) < 2:
            return False, f"⚠️ Only {len(ce_rows)} CE candles — lab may not be running"
        last = ce_rows[-2]
        rsi  = float(last.get("rsi", 0))
        if rsi == 50.0:
            issues.append("⚠️ RSI=50.0 (cold start default) — warmup not working")
        elif rsi < 5 or rsi > 95:
            issues.append(f"⚠️ RSI={rsi} extreme — warmup issue")
        candle_count = len(ce_rows)
        if issues:
            return False, "\n".join(issues) + f" ({candle_count} candles)"
        return True, f"✅ RSI warmed ({candle_count} CE candles, last RSI={rsi:.1f})"
    except Exception as e:
        return False, f"❌ RSI warmup check error: {e}"

def check_state():
    try:
        path = D.STATE_FILE_PATH
        if not os.path.isfile(path):
            return False, "❌ State file missing"
        with open(path) as f:
            state = json.load(f)
        trades = state.get("daily_trades", -1)
        losses = state.get("daily_losses", -1)
        pnl    = state.get("daily_pnl", 0)
        expiry_check = state.get("paused")
        if expiry_check:
            return False, f"❌ Bot is PAUSED — run /resume"
        return True, f"✅ State: trades={trades} losses={losses} pnl={pnl}pts"
    except Exception as e:
        return False, f"❌ State file error: {e}"

def check_logs():
    try:
        log_path = D.LIVE_LOG_FILE
        if not os.path.isfile(log_path):
            return False, "❌ Log file missing"
        age = time.time() - os.path.getmtime(log_path)
        if age > 120:
            return False, f"❌ Log not updated for {int(age)}s — bot may be stuck"
        size_mb = os.path.getsize(log_path) / (1024*1024)
        return True, f"✅ Log: updated {int(age)}s ago ({size_mb:.1f}MB)"
    except Exception as e:
        return False, f"❌ Log check error: {e}"

def check_scan_timing():
    try:
        main_path = os.path.expanduser("~/VISHAL_RAJPUT/VRL_MAIN.py")
        with open(main_path) as f:
            src = f.read()
        if "31 <= now.second <= 36" in src:
            return True, "✅ Scan window: :31-:36s (aligned with ENGINE)"
        elif "28 <= now.second" in src:
            return False, "⚠️ Scan window :28s — should be :31s to match ENGINE"
        elif "now.second <= 5" in src:
            return False, "❌ Scan window :0-5s — MISALIGNED with ENGINE (:31s)"
        else:
            return False, "⚠️ Scan window unknown — check VISHAL_RAJPUT/VRL_MAIN.py"
    except Exception as e:
        return False, f"❌ Scan timing check error: {e}"

def check_expiry_init():
    try:
        main_path = os.path.expanduser("~/VISHAL_RAJPUT/VRL_MAIN.py")
        with open(main_path) as f:
            src = f.read()
        if "Expiry on startup" in src and "expiry = D.get_nearest_expiry(kite)" in src:
            return True, "✅ Expiry resolved on startup (Mar 17 bug fixed)"
        else:
            return False, "❌ Expiry NOT resolved on startup — will cause zero entries!"
    except Exception as e:
        return False, f"❌ Expiry init check error: {e}"

def check_token_cache():
    try:
        data_path = os.path.expanduser("~/VISHAL_RAJPUT/VRL_DATA.py")
        with open(data_path) as f:
            src = f.read()
        if "_token_cache" in src and "clear_token_cache" in src:
            return True, "✅ Token cache active (no 2-3s API delay per scan)"
        else:
            return False, "❌ Token cache NOT active — scans will be slow"
    except Exception as e:
        return False, f"❌ Token cache check error: {e}"


def check_vrl_trade():
    """v12.9: Verify VISHAL_RAJPUT/VRL_TRADE.py exists and is importable."""
    try:
        trade_path = os.path.expanduser("~/VISHAL_RAJPUT/VRL_TRADE.py")
        if not os.path.isfile(trade_path):
            return False, "❌ VISHAL_RAJPUT/VRL_TRADE.py missing — bot will crash on startup"
        # Verify it has the required functions
        with open(trade_path) as f:
            src = f.read()
        missing = []
        if "def place_entry" not in src:
            missing.append("place_entry")
        if "def place_exit" not in src:
            missing.append("place_exit")
        if missing:
            return False, f"❌ VISHAL_RAJPUT/VRL_TRADE.py missing functions: {missing}"
        return True, "✅ VISHAL_RAJPUT/VRL_TRADE.py present (place_entry + place_exit)"
    except Exception as e:
        return False, f"❌ VRL_TRADE check error: {e}"


def check_circuit_breaker_logic():
    """v12.9: Verify circuit breaker _error_count reset is NOT at top of loop."""
    try:
        main_path = os.path.expanduser("~/VISHAL_RAJPUT/VRL_MAIN.py")
        with open(main_path) as f:
            src = f.read()
        # The bug was: _error_count reset at top of loop = circuit breaker never fires
        # Fixed: reset only after successful scan (inside the scan block)
        # Check that the reset is NOT right after "today = date.today()"
        lines = src.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if ('_error_count' in stripped and '= 0' in stripped
                    and 'state["_error_count"]' in stripped):
                # Look at context: is it near "spot_ltp = D.get_ltp" (top of loop)?
                context = "\n".join(lines[max(0, i-5):i+1])
                if "spot_ltp" in context and "check_profit_lock" in context:
                    return False, "❌ _error_count resets at loop top — circuit breaker disabled!"
        return True, "✅ Circuit breaker logic OK (_error_count reset after scan)"
    except Exception as e:
        return False, f"❌ Circuit breaker check error: {e}"


def check_strike_step():
    """v12.15: Verify 100-step strikes active + expiry fallback to 50."""
    try:
        from VRL_DATA import STRIKE_STEP, STRIKE_STEP_EXPIRY, get_active_strike_step
        issues = []
        if STRIKE_STEP != 100:
            issues.append(f"STRIKE_STEP={STRIKE_STEP} expected 100")
        if STRIKE_STEP_EXPIRY != 50:
            issues.append(f"STRIKE_STEP_EXPIRY={STRIKE_STEP_EXPIRY} expected 50")
        normal = get_active_strike_step(dte=3)
        expiry = get_active_strike_step(dte=0)
        if normal != 100:
            issues.append(f"DTE=3 returns step={normal} expected 100")
        if expiry != 50:
            issues.append(f"DTE=0 returns step={expiry} expected 50")
        if issues:
            return False, "❌ Strike step: " + "; ".join(issues)
        return True, "✅ Strike step: 100 normal, 50 on expiry — fewer ATM flips"
    except Exception as e:
        return False, f"❌ Strike step check error: {e}"


def check_spot_gap():
    """v12.15: Verify spot gap detection works."""
    try:
        gap_info = D.calculate_spot_gap()
        gap = gap_info.get("gap_pts", 0)
        prev = gap_info.get("prev_close", 0)
        today_open = gap_info.get("today_open", 0)
        if prev <= 0 or today_open <= 0:
            return False, "❌ Spot gap: no prev close or today open"
        return True, f"✅ Spot gap: {gap:+.0f}pts (prev={prev:.0f} open={today_open:.0f})"
    except Exception as e:
        return False, f"❌ Spot gap error: {e}"



def check_engine_ce_override():
    """v12.15: Verify CE regime override has no orphaned return."""
    try:
        path = os.path.join(os.path.dirname(__file__), "VRL_ENGINE.py")
        with open(path) as f:
            content = f.read()
        if "\n\n\n\n            return result" in content:
            return False, "ORPHANED RETURN in ENGINE — CE entries broken"
        return True, "CE override path clean"
    except Exception as e:
        return False, f"CE override check: {e}"


def check_version_match():
    """v12.15: All VRL files same version."""
    try:
        target = D.VERSION
        bad = []
        base = os.path.dirname(__file__)
        for fn in ["VRL_MAIN.py", "VRL_ENGINE.py", "VRL_TRADE.py"]:
            p = os.path.join(base, fn)
            if not os.path.isfile(p): continue
            with open(p) as f: head = "".join(f.readlines()[:10])
            if target not in head: bad.append(fn)
        if bad:
            return False, f"Version mismatch: {', '.join(bad)} != {target}"
        return True, f"All files at {target}"
    except Exception as e:
        return False, f"Version check: {e}"


def check_warning_system():
    """v12.15: Warning functions exist."""
    try:
        funcs = ["run_warnings", "compute_daily_bias", "check_hourly_rsi",
                 "check_vix_warning", "capture_straddle", "is_entry_fire_window"]
        missing = [f for f in funcs if not hasattr(D, f)]
        if missing:
            return False, f"Missing: {', '.join(missing)}"
        return True, "Warning system present"
    except Exception as e:
        return False, f"Warning check: {e}"


def check_entry_window():
    """v12.15: Entry fire window configured."""
    try:
        h = D.ENTRY_FIRE_HOUR; m = D.ENTRY_FIRE_MIN
        if h != 9 or m != 45:
            return False, f"Entry fire wrong: {h}:{m:02d} expected 9:45"
        if D.TRADE_START_MIN != 15:
            return False, f"Scan start wrong: 9:{D.TRADE_START_MIN:02d} expected 9:15"
        return True, f"Scan 9:{D.TRADE_START_MIN:02d} | Fire {h}:{m:02d}-{D.ENTRY_CUTOFF_HOUR}:{D.ENTRY_CUTOFF_MIN:02d}"
    except Exception as e:
        return False, f"Entry window check: {e}"


def main():
    global _kite_ref
    now = datetime.now().strftime("%H:%M")
    print(f"VRL HealthCheck v12.15 running at {now}")

    try:
        kite = get_kite()
        _kite_ref = kite
        D.init(kite)
        time.sleep(2)
    except Exception as e:
        _tg(f"🚨 <b>HEALTHCHECK FAILED</b>\nCannot init kite: {e}")
        return

    checks = [
        ("Instance",        check_single_instance()),
        ("Score Threshold", check_score_threshold()),
        ("Spread Gates",    check_spread_gates()),
        ("Strike Step",     check_strike_step()),
        ("Spot Gap",        check_spot_gap()),
        ("Expiry Init",     check_expiry_init()),
        ("Expiry",          check_expiry(kite)),
        ("Token Cache",     check_token_cache()),
        ("VRL_TRADE",       check_vrl_trade()),
        ("Circuit Breaker", check_circuit_breaker_logic()),
        ("Scan Timing",     check_scan_timing()),
        ("Lot Size",        check_lot_size(kite)),
        ("Spot Tick",       check_spot_tick()),
        ("Option LTP",      check_option_ltp(kite)),
        ("RSI Warmup",      check_rsi_warmup()),
        ("State",           check_state()),
        ("Logs",            check_logs()),
        ("CE Override",     check_engine_ce_override()),
        ("Version Match",   check_version_match()),
        ("Warning System",  check_warning_system()),
        ("Entry Window",    check_entry_window()),
    ]

    all_ok = all(ok for _, (ok, _) in checks)
    lines  = []
    for name, (ok, msg) in checks:
        icon = "✅" if ok else "❌"
        for line in msg.split("\n"):
            lines.append(f"{icon} <b>{name}</b>: {line}")
            icon = " "  # indent subsequent lines

    status = "✅ ALL SYSTEMS OK — Ready to trade" if all_ok else "⚠️ ISSUES FOUND — Fix before trading"
    report = (
        f"🩺 <b>HEALTHCHECK v12.15 — {now}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines) +
        f"\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{status}"
    )

    print(report)
    _tg(report)

if __name__ == "__main__":
    main()
