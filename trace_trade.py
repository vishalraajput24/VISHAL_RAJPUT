#!/usr/bin/env python3
"""
trace_trade.py — V11 Golden live alignment tracer.
Compares engine state vs dashboard every 1s while in a trade.
Logs all mismatches to ~/lab_data/trade_audit_trace.log.

Run: python3 trace_trade.py
"""
import os
import json
import time
import csv
from datetime import datetime
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
BASE       = Path("/home/vishalraajput24/VISHAL_RAJPUT")
STATE_FILE = BASE / "state/vrl_v11_state.json"        # engine truth
DASH_FILE  = BASE / "state/vrl_dashboard.json"        # dashboard snapshot
TRADE_LOG  = Path("/home/vishalraajput24/lab_data/vrl_trade_log.csv")
LIVE_LOG   = Path.home() / "logs/live/vrl_live.log"
AUDIT_LOG  = Path("/home/vishalraajput24/lab_data/trade_audit_trace.log")

AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _log(text, end="\n"):
    print(text, end=end, flush=True)
    with open(AUDIT_LOG, "a") as f:
        f.write(text + end)

def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}

def _read_state():
    return _read_json(STATE_FILE)

def _read_dash():
    return _read_json(DASH_FILE)

def _match(a, b, tol=0.05):
    """Numeric match within tolerance; exact match for non-numeric."""
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return str(a) == str(b)

def _ok(a, b, tol=0.05):
    return "✅" if _match(a, b, tol) else "❌ MISMATCH"

def _expected_sl_tier(peak_pnl, initial_sl, entry_price):
    """Compute the expected SL and tier from V11 Golden rules."""
    if peak_pnl >= 15.0:
        peak_ltp = entry_price + peak_pnl
        sl = max(initial_sl, entry_price + 9.0, peak_ltp - 10.0)
        return round(sl, 2), "TRAIL_10"
    elif peak_pnl >= 11.0:
        sl = max(initial_sl, entry_price + 4.0)
        return round(sl, 2), "LOCK_4"
    elif peak_pnl >= 9.0:
        sl = max(initial_sl, entry_price - 2.0)
        return round(sl, 2), "PROTECT"
    else:
        return round(initial_sl, 2), "INITIAL"

def _last_tg_lines(n=4):
    """Return last n TG sent lines from live log."""
    if not LIVE_LOG.exists():
        return []
    try:
        with open(LIVE_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 20480), 0)
            data = f.read().decode("utf-8", errors="ignore")
        lines = [l for l in data.splitlines() if "[TG] sent" in l]
        return lines[-n:]
    except Exception:
        return []


# ── Pre-trade buildup monitor ─────────────────────────────────────────────────
def print_buildup():
    """Show V11 Golden gate status while waiting for a trade."""
    dash = _read_dash()
    now  = datetime.now().strftime("%H:%M:%S")

    lines = []
    for side in ["ce", "pe"]:
        sd = dash.get(side, {})
        if not sd:
            continue
        strike = sd.get("strike", "?")
        ltp    = sd.get("ltp", 0)
        verdict = sd.get("verdict", "—")

        # V11 Golden gates — prefer new field names, fall back to old if absent
        if "momentum_ok" in sd:
            mom_ok  = bool(sd["momentum_ok"])
            mom_gap = float(sd.get("momentum_gap", 0))
            dec_ok  = bool(sd.get("decay_ok", False))
            dec_mar = float(sd.get("decay_margin", 0))
            gates_str = (
                f"MOM={'✓' if mom_ok else '✗'}({mom_gap:+.1f})  "
                f"DECAY={'✓' if dec_ok else '✗'}({dec_mar:+.1f})"
            )
            green = (1 if mom_ok else 0) + (1 if dec_ok else 0)
        else:
            # Legacy fallback (old dashboard format)
            g1 = bool(sd.get("g1_gap_ok", False))
            g5 = bool(sd.get("g5_above_ema9l", False))
            gates_str = f"G1={'✓' if g1 else '✗'}  G5={'✓' if g5 else '✗'}"
            green = (1 if g1 else 0) + (1 if g5 else 0)

        bar = "🟢" * green + "⬜" * (2 - green)
        lines.append(f"  {side.upper()} {strike}  ₹{ltp}  {bar}  {gates_str}  [{verdict}]")

    status = "  ".join(lines) if lines else "  — no gate data —"
    print(f"\r\033[K⏳ {now}  WAITING  {status}", end="", flush=True)


# ── Main alignment audit ──────────────────────────────────────────────────────
def run_audit_cycle():
    st   = _read_state()
    dash = _read_dash()
    pos  = dash.get("position", {})
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Pull engine fields
    v_sym    = st.get("symbol", "—")
    v_dir    = st.get("direction", "—")
    v_strike = st.get("strike", 0)
    v_entry  = float(st.get("entry_price", 0))
    v_peak   = float(st.get("peak_pnl", 0))
    v_sl     = float(st.get("active_ratchet_sl", 0))
    v_tier   = st.get("active_ratchet_tier", "—")
    v_cndl   = int(st.get("candles_held", 0))
    v_isl    = float(st.get("initial_sl", 0))
    v_qty    = int(st.get("qty", 0))

    # Pull dashboard position fields
    d_sym    = pos.get("symbol", "—")
    d_dir    = pos.get("direction", "—")
    d_strike = pos.get("strike", 0)
    d_entry  = float(pos.get("entry", 0))
    d_ltp    = float(pos.get("ltp", 0))
    d_peak   = float(pos.get("peak", 0))
    d_sl     = float(pos.get("sl", 0))
    d_tier   = pos.get("active_ratchet_tier", "—")
    d_cndl   = int(pos.get("candles", 0))
    d_qty    = int(pos.get("qty", 0))

    # V11 Golden SL/tier expected from engine truth
    exp_sl, exp_tier = _expected_sl_tier(v_peak, v_isl, v_entry)

    mismatches = []

    def row(label, v_val, d_val, tol=0.05):
        result = _ok(v_val, d_val, tol)
        flag = "" if "✅" in result else f"  ← {label} MISMATCH"
        if flag:
            mismatches.append(f"{label}: state={v_val} dash={d_val}")
        return f"  {label:<22} state={str(v_val):<18} dash={str(d_val):<18} {result}{flag}"

    _log("\n" + "=" * 80)
    _log(f"📊 AUDIT  {now}  [{v_dir} {v_strike}  peak=+{v_peak:.1f}pts]")
    _log("=" * 80)

    # Surface alignment
    _log(row("Symbol",        v_sym,    d_sym,    0))
    _log(row("Direction",     v_dir,    d_dir,    0))
    _log(row("Strike",        v_strike, d_strike, 0))
    _log(row("Entry price",   v_entry,  d_entry))
    _log(f"  {'LTP (dash only)':<22} dash=₹{d_ltp}")
    _log(row("Peak PnL",      v_peak,   d_peak))
    _log(row("Active SL",     v_sl,     d_sl))
    _log(row("SL tier",       v_tier,   d_tier,   0))
    _log(row("Candles held",  v_cndl,   d_cndl,   0))

    # SL tier correctness vs V11 Golden rules
    tier_correct = (v_tier == exp_tier) and _match(v_sl, exp_sl)
    tier_flag = "✅" if tier_correct else f"❌  expected tier={exp_tier} SL=₹{exp_sl}"
    if not tier_correct:
        mismatches.append(f"SL logic: state tier={v_tier} SL={v_sl} but expected {exp_tier} SL={exp_sl}")
    _log(f"  {'SL rule check':<22} peak={v_peak:.1f}  initial_sl=₹{v_isl}  {tier_flag}")

    # Position size
    _log("-" * 80)
    _log(f"  Entry  ₹{v_entry} × {v_qty} qty  (market fill)")
    _log(row("Qty", v_qty, d_qty, 0))

    # Telegram
    _log("-" * 80)
    tg = _last_tg_lines(3)
    if tg:
        _log("  Last TG alerts:")
        for line in tg:
            clean = line.split("sent ok — ")[-1] if "sent ok — " in line else line
            _log(f"    📢 {clean[:120]}")
    else:
        _log("  Last TG: (none found)")

    # Summary
    _log("-" * 80)
    if mismatches:
        _log(f"  ⚠️  {len(mismatches)} MISMATCH(ES) FOUND:")
        for m in mismatches:
            _log(f"     • {m}")
    else:
        _log("  ✅ ALL SURFACES ALIGNED")
    _log("=" * 80)

    return mismatches


# ── Post-exit reconciliation ──────────────────────────────────────────────────
def post_exit_reconciliation(entry_snapshot):
    """Compare final CSV row vs last TG exit vs entry snapshot."""
    _log("\n" + "#" * 80)
    _log(f"🏁 EXIT DETECTED — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _log("Waiting 5s for data sync...")
    _log("#" * 80)
    time.sleep(5)

    # Read last CSV row
    last_row = {}
    if TRADE_LOG.exists():
        try:
            with open(TRADE_LOG, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4096), 0)
                data = f.read().decode("utf-8", errors="ignore")
            rows = list(csv.DictReader(data.splitlines()))
            if rows:
                last_row = rows[-1]
        except Exception:
            pass

    # Last TG exit message
    tg_lines = _last_tg_lines(6)
    exit_tg  = [l for l in tg_lines if "EXIT" in l or "TRAIL" in l or "LOCK_4" in l]

    mismatches = []

    _log("\n" + "=" * 80)
    _log("🏁 POST-EXIT RECONCILIATION")
    _log("=" * 80)

    if last_row:
        csv_entry  = float(last_row.get("entry_price", 0))
        csv_exit   = float(last_row.get("exit_price", 0))
        csv_pnl    = float(last_row.get("pnl_pts", 0))
        csv_peak   = float(last_row.get("peak_pnl", 0))
        csv_reason = last_row.get("exit_reason", "—")
        csv_dir    = last_row.get("direction", "—")
        csv_strike = last_row.get("strike", "—")
        csv_qty    = last_row.get("qty_exited", "—")

        _log(f"  CSV row  : {csv_dir} {csv_strike}  entry=₹{csv_entry}  exit=₹{csv_exit}  pnl={csv_pnl:+.1f}pts  peak={csv_peak:+.1f}  reason={csv_reason}  qty={csv_qty}")

        # Cross-check CSV entry vs what we captured at entry
        snap_entry = float(entry_snapshot.get("entry_price", 0))
        if snap_entry > 0 and not _match(csv_entry, snap_entry):
            mismatches.append(f"Entry price: captured={snap_entry} CSV={csv_entry}")

        # Expected PnL from entry/exit prices
        if csv_entry > 0 and csv_exit > 0:
            exp_pnl = round(csv_exit - csv_entry, 2)
            if not _match(csv_pnl, exp_pnl, 0.1):
                mismatches.append(f"PnL calc: CSV pnl_pts={csv_pnl} but exit({csv_exit})-entry({csv_entry})={exp_pnl}")

        # entry_mode should be V11_CE/V11_PE (V10_* tolerated for pre-rename rows)
        em = last_row.get("entry_mode", "")
        if em and "V11" not in em and "V10" not in em:
            mismatches.append(f"entry_mode={em} — expected V11_CE or V11_PE")
    else:
        _log("  CSV row  : not found")
        mismatches.append("CSV row missing after exit")

    _log("")
    if exit_tg:
        _log("  Last exit TG alerts:")
        for l in exit_tg[-2:]:
            clean = l.split("sent ok — ")[-1] if "sent ok — " in l else l
            _log(f"    📢 {clean[:140]}")
    else:
        _log("  TG exit alerts: (none captured)")

    _log("-" * 80)
    if mismatches:
        _log(f"  ❌ {len(mismatches)} MISMATCH(ES):")
        for m in mismatches:
            _log(f"     • {m}")
    else:
        _log("  ✅ RECONCILIATION PASSED — all surfaces aligned")
    _log("=" * 80 + "\n")


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    os.system("clear")
    _log("=" * 80)
    _log(f"🚀 V11 Golden Alignment Tracer  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _log(f"   State   : {STATE_FILE}")
    _log(f"   Dashboard: {DASH_FILE}")
    _log(f"   Audit log: {AUDIT_LOG}")
    _log("   Press Ctrl+C to stop.")
    _log("=" * 80 + "\n")

    in_trade       = False
    entry_snapshot = {}

    while True:
        try:
            st = _read_state()
            current_in_trade = bool(st.get("in_trade", False))

            if not in_trade and current_in_trade:
                # ── New trade entered ──
                in_trade       = True
                entry_snapshot = dict(st)
                _log("\n" + "#" * 80)
                _log(f"🚀 ENTRY DETECTED  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                _log(f"   {st.get('direction','?')} {st.get('strike','?')}  entry=₹{st.get('entry_price',0)}"
                     f"  initial_sl=₹{st.get('initial_sl',0)}"
                     f"  qty={st.get('qty',0)}")
                _log("#" * 80)
                run_audit_cycle()
                time.sleep(1)

            elif in_trade and not current_in_trade:
                # ── Trade exited ──
                in_trade = False
                post_exit_reconciliation(entry_snapshot)
                entry_snapshot = {}

            elif in_trade:
                # ── Active trade: audit every cycle ──
                run_audit_cycle()
                time.sleep(1)

            else:
                # ── Waiting: show gate buildup ──
                print_buildup()
                time.sleep(1)

        except KeyboardInterrupt:
            print("\n\n👋 Tracer stopped.")
            break
        except Exception as e:
            print(f"\n⚠️  Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
