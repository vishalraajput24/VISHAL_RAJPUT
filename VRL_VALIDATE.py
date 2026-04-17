# ═══════════════════════════════════════════════════════════════
#  VRL_VALIDATE.py — VISHAL RAJPUT TRADE v15.2
#  20 live market validation checks. Run on every entry + exit.
#  Silent on PASS, alerts + logs on FAIL.
#  Zero impact on trading speed (runs after orders, not in critical path).
#
#  v15.2: VALID_ENTRY_MODES is the single allowed mode (EMA9_BREAKOUT).
#  LEGACY_MODES carries old strings — recognized for historical CSV/DB
#  replay so reports don't break, but never flagged as errors.
# ═══════════════════════════════════════════════════════════════

import os
import csv
import json
import sqlite3
import logging
from datetime import date, datetime

import VRL_DATA as D
import VRL_CONFIG as CFG

# v15.2 — single live entry mode
VALID_ENTRY_MODES = ("EMA9_BREAKOUT",)

# Old strings that may still appear in historical trades; do NOT raise
# errors on them, but they're not allowed as fresh entries either.
LEGACY_MODES = (
    "FAST", "CONFIRMED", "MOMENTUM", "3MIN",
    "BOTH", "EMA", "MINIMAL", "EXPIRY_BREAKOUT", "CONVICTION",
)

# v15.2 — exit reasons accepted for live + historical trades
VALID_EXIT_REASONS = (
    # v16.0 primary exits
    "EMERGENCY_SL", "STALE_ENTRY", "EOD_EXIT",
    "VELOCITY_STALL", "EMA1M_BREAK", "PROFIT_RATCHET",
    # v15.x historical (kept for back-compat with old trade log rows)
    "EMA9_LOW_BREAK", "BREAKEVEN_LOCK", "TRAIL_FLOOR",
    # safety / manual
    "MARKET_CLOSE", "MANUAL", "FORCE_EXIT", "CIRCUIT_BREAKER_EXIT",
    # historical (kept for back-compat with old trade log rows)
    "HARD_SL", "PROFIT_FLOOR", "FLOOR_SL",
    "RSI_BLOWOFF", "RSI_SPIKE", "ATR_TRAIL", "SCOUT_SL",
    "CANDLE_SL", "DIVERGENCE_EXIT", "WEAK_SL",
)

# ── Validation logger ─────────────────────────────────────────
_VAL_LOG_DIR  = os.path.expanduser("~/logs")
_VAL_LOG_PATH = os.path.join(_VAL_LOG_DIR, "validation.log")
os.makedirs(_VAL_LOG_DIR, exist_ok=True)

val_logger = logging.getLogger("vrl_validation")
if not val_logger.handlers:
    val_logger.setLevel(logging.INFO)
    _fh = logging.FileHandler(_VAL_LOG_PATH)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    val_logger.addHandler(_fh)
    val_logger.propagate = False

# ── Paths ─────────────────────────────────────────────────────
# Token + state paths come from VRL_DATA so AUTH/MAIN/VALIDATE all
# agree on one location. BUG-015.
import VRL_DATA as _D
_DB_PATH    = os.path.expanduser("~/lab_data/vrl_data.db")
_CSV_PATH   = os.path.expanduser("~/lab_data/vrl_trade_log.csv")
_DASH_PATH  = os.path.join(_D.STATE_DIR, "vrl_dashboard.json")
_TOKEN_PATH = _D.TOKEN_FILE_PATH


def _safe(fn, *a, **kw):
    """Run a check that might fail. Returns (ok, error_msg). Never raises."""
    try:
        return True, fn(*a, **kw)
    except Exception as e:
        return False, str(e)


# ═══════════════════════════════════════════════════════════════
#  ENTRY VALIDATION — 10 checks, runs after every entry
# ═══════════════════════════════════════════════════════════════

def validate_entry(state, entry_result, kite=None):
    """Run 10 checks after every entry. Returns list of failure strings."""
    failures = []

    # CHECK 1: Entry price matches signal price (slippage < 3pts)
    try:
        signal_price = float(entry_result.get("entry_price", 0) or 0)
        fill_price   = float(state.get("entry_price", 0) or 0)
        if signal_price > 0 and fill_price > 0:
            diff = abs(signal_price - fill_price)
            if diff > 3:
                failures.append("ENTRY_SLIPPAGE: signal=" + str(signal_price)
                                + " fill=" + str(fill_price)
                                + " diff=" + str(round(diff, 2)))
    except Exception as e:
        failures.append("CHECK1_ERR: " + str(e))

    # CHECK 2: State is consistent — in_trade, symbol, token, entry_price
    if not state.get("in_trade"):
        failures.append("STATE: in_trade=False after entry")
    if not state.get("symbol"):
        failures.append("STATE: symbol is empty after entry")
    if not state.get("token"):
        failures.append("STATE: token is None after entry")
    if float(state.get("entry_price", 0) or 0) <= 0:
        failures.append("STATE: entry_price=0 after entry")

    # CHECK 3: SL is set correctly (entry - hard_sl)
    try:
        hard_sl     = CFG.get().get("exit", {}).get("hard_sl", 12)
        expected_sl = round(float(state.get("entry_price", 0) or 0) - hard_sl, 2)
        actual_sl   = round(float(state.get("phase1_sl", 0) or 0), 2)
        if abs(expected_sl - actual_sl) > 0.5:
            failures.append("SL_MISMATCH: expected=" + str(expected_sl)
                            + " actual=" + str(actual_sl))
    except Exception as e:
        failures.append("CHECK3_ERR: " + str(e))

    # CHECK 4: Qty is correct (lots × lot_size)
    try:
        lot_count    = CFG.get().get("lots", {}).get("count", 2)
        lot_size     = CFG.get().get("lots", {}).get("size", D.get_lot_size())
        expected_qty = lot_count * lot_size
        actual_qty   = int(state.get("qty", 0) or 0)
        # Allow partial fills in live mode (smaller is OK, larger is not)
        if actual_qty == 0:
            failures.append("QTY: actual=0 (no fill)")
        elif actual_qty > expected_qty:
            failures.append("QTY: actual=" + str(actual_qty)
                            + " > expected=" + str(expected_qty))
    except Exception as e:
        failures.append("CHECK4_ERR: " + str(e))

    # CHECK 5: Exchange SL order placed (live mode only)
    if not D.PAPER_MODE:
        if not state.get("_sl_order_id"):
            failures.append("EXCHANGE_SL: no SL order placed (live mode)")

    # CHECK 6: Entry mode is valid (v15.2: EMA9_BREAKOUT is the only live mode)
    mode = state.get("entry_mode", "") or state.get("mode", "")
    if mode and mode not in VALID_ENTRY_MODES and mode not in LEGACY_MODES:
        failures.append("ENTRY_MODE: invalid mode=" + str(mode))

    # CHECK 7: Direction matches option type in symbol
    direction = state.get("direction", "")
    symbol    = state.get("symbol", "")
    if direction == "CE" and symbol.endswith("PE"):
        failures.append("DIRECTION: CE but symbol ends with PE: " + symbol)
    if direction == "PE" and symbol.endswith("CE"):
        failures.append("DIRECTION: PE but symbol ends with CE: " + symbol)

    # CHECK 8: Strike is in symbol
    try:
        strike = int(state.get("strike", 0) or 0)
        if strike and str(strike) not in symbol:
            failures.append("STRIKE: " + str(strike) + " not in " + symbol)
    except Exception as e:
        failures.append("CHECK8_ERR: " + str(e))

    # CHECK 9: WebSocket has live LTP for the token
    try:
        token  = state.get("token", 0)
        ws_ltp = D.get_ltp(token) if token else 0
        if ws_ltp <= 0:
            failures.append("WEBSOCKET: no LTP for token=" + str(token))
    except Exception as e:
        failures.append("CHECK9_ERR: " + str(e))

    # CHECK 10: Trade log file is writeable (entry persists on exit)
    try:
        log_dir = os.path.dirname(_CSV_PATH)
        if not os.path.isdir(log_dir):
            failures.append("TRADE_LOG: dir missing " + log_dir)
        elif not os.access(log_dir, os.W_OK):
            failures.append("TRADE_LOG: dir not writeable " + log_dir)
    except Exception as e:
        failures.append("CHECK10_ERR: " + str(e))

    _log_result("ENTRY", 10, failures)
    return failures


# ═══════════════════════════════════════════════════════════════
#  EXIT VALIDATION — 10 checks, runs after every exit
# ═══════════════════════════════════════════════════════════════

def validate_exit(state, exit_pnl, exit_price, exit_reason,
                  entry_price, qty_exited, kite=None):
    """Run 10 checks after every exit. Returns list of failure strings."""
    failures = []
    today    = date.today().isoformat()

    # CHECK 11: PNL calculation matches (exit - entry)
    try:
        expected_pnl = round(float(exit_price) - float(entry_price), 2)
        if abs(expected_pnl - float(exit_pnl)) > 0.5:
            failures.append("PNL_CALC: expected=" + str(expected_pnl)
                            + " actual=" + str(exit_pnl))
    except Exception as e:
        failures.append("CHECK11_ERR: " + str(e))

    # CHECK 12: Exit reason is in the known set
    if exit_reason not in VALID_EXIT_REASONS:
        failures.append("EXIT_REASON: unknown=" + str(exit_reason))

    # CHECK 13: Charges calculator returns sensible numbers (if module exists)
    try:
        import VRL_CHARGES as CH  # type: ignore
        try:
            charges = CH.calculate_charges(
                float(entry_price), float(exit_price), int(qty_exited), 1)
            if charges.get("total_charges", 0) <= 0:
                failures.append("CHARGES: zero or negative")
        except Exception as ce:
            failures.append("CHARGES_ERR: " + str(ce))
    except ImportError:
        # Charges module not present in this build — skip silently
        pass

    # CHECK 14: DB trade row exists for today
    try:
        if os.path.isfile(_DB_PATH):
            conn = sqlite3.connect(_DB_PATH, timeout=5)
            try:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE date=?", (today,)
                ).fetchone()[0]
            finally:
                conn.close()
            if cnt < 1:
                failures.append("DB: no trade row for today")
    except Exception as e:
        failures.append("CHECK14_ERR: " + str(e))

    # CHECK 15: CSV trade row exists for today
    csv_count = 0
    try:
        if os.path.isfile(_CSV_PATH):
            with open(_CSV_PATH) as f:
                csv_count = sum(1 for r in csv.DictReader(f)
                                if r.get("date") == today)
            if csv_count < 1:
                failures.append("CSV: no trade row for today")
    except Exception as e:
        failures.append("CHECK15_ERR: " + str(e))

    # CHECK 16: DB and CSV trade counts match
    try:
        if os.path.isfile(_DB_PATH) and os.path.isfile(_CSV_PATH):
            conn2 = sqlite3.connect(_DB_PATH, timeout=5)
            try:
                db_count = conn2.execute(
                    "SELECT COUNT(*) FROM trades WHERE date=?", (today,)
                ).fetchone()[0]
            finally:
                conn2.close()
            if db_count != csv_count:
                failures.append("SYNC: DB=" + str(db_count)
                                + " CSV=" + str(csv_count))
    except Exception as e:
        failures.append("CHECK16_ERR: " + str(e))

    # CHECK 17: State reset after a fully-closed exit
    if state.get("in_trade"):
        failures.append("STATE: in_trade=True after exit")
    if float(state.get("entry_price", 0) or 0) != 0:
        failures.append("STATE: entry_price not reset")
    if float(state.get("peak_pnl", 0) or 0) != 0:
        failures.append("STATE: peak_pnl not reset")

    # CHECK 18: Daily counters updated
    daily_trades = int(state.get("daily_trades", 0) or 0)
    if daily_trades < 1:
        failures.append("STATE: daily_trades=0 after exit")

    # CHECK 19: Exchange SL cancelled (live mode only)
    if not D.PAPER_MODE:
        sl_id = state.get("_sl_order_id")
        if sl_id and sl_id != "PAPER_SL":
            failures.append("EXCHANGE_SL: SL order_id=" + str(sl_id)
                            + " not cleared after exit")

    # CHECK 20: Dashboard JSON reflects the same trade count + PNL
    try:
        if os.path.isfile(_DASH_PATH):
            with open(_DASH_PATH) as f:
                dash = json.load(f)
            today_block = dash.get("today", {}) or {}
            dash_trades = int(today_block.get("trades", 0) or 0)
            if dash_trades != daily_trades:
                failures.append("DASHBOARD: trades=" + str(dash_trades)
                                + " state=" + str(daily_trades))

            # Alignment cross-checks (Telegram == Dashboard == State)
            try:
                tg_pnl   = round(float(state.get("daily_pnl", 0) or 0), 1)
                dash_pnl = round(float(today_block.get("pnl", 0) or 0), 1)
                if abs(tg_pnl - dash_pnl) > 1.0:
                    failures.append("ALIGN_PNL: state=" + str(tg_pnl)
                                    + " dashboard=" + str(dash_pnl))
            except Exception:
                pass
            try:
                tg_wins   = daily_trades - int(state.get("daily_losses", 0) or 0)
                dash_wins = int(today_block.get("wins", 0) or 0)
                if tg_wins != dash_wins:
                    failures.append("ALIGN_WINS: state=" + str(tg_wins)
                                    + " dashboard=" + str(dash_wins))
            except Exception:
                pass
    except Exception as e:
        failures.append("CHECK20_ERR: " + str(e))

    _log_result("EXIT ", 10, failures)
    return failures


# ═══════════════════════════════════════════════════════════════
#  /validate COMMAND — manual on-demand system check
# ═══════════════════════════════════════════════════════════════

def manual_validate(state):
    """
    Run a fresh ad-hoc validation. Returns dict:
        {"checks": [(name, ok, detail), ...], "passed": int, "total": int}
    """
    checks = []

    # 1. DB exists and has trades table
    try:
        if os.path.isfile(_DB_PATH):
            conn = sqlite3.connect(_DB_PATH, timeout=5)
            try:
                cnt = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            finally:
                conn.close()
            size_mb = round(os.path.getsize(_DB_PATH) / (1024 * 1024), 1)
            checks.append(("DB", True, str(cnt) + " trades, " + str(size_mb) + "MB"))
        else:
            checks.append(("DB", False, "missing " + _DB_PATH))
    except Exception as e:
        checks.append(("DB", False, str(e)))

    # 2. CSV exists
    try:
        if os.path.isfile(_CSV_PATH):
            with open(_CSV_PATH) as f:
                csv_rows = sum(1 for _ in csv.DictReader(f))
            checks.append(("CSV", True, str(csv_rows) + " rows"))
        else:
            checks.append(("CSV", False, "missing"))
    except Exception as e:
        checks.append(("CSV", False, str(e)))

    # 3. DB == CSV (TODAY only — historical drift from pre-DB days is
    #    not a bug we can fix retroactively).
    try:
        today_iso = date.today().isoformat()
        db_today  = -1
        csv_today = -1
        if os.path.isfile(_DB_PATH):
            conn = sqlite3.connect(_DB_PATH, timeout=5)
            try:
                db_today = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE date=?",
                    (today_iso,)).fetchone()[0]
            finally:
                conn.close()
        if os.path.isfile(_CSV_PATH):
            with open(_CSV_PATH) as f:
                csv_today = sum(1 for r in csv.DictReader(f)
                                if r.get("date") == today_iso)
        if db_today < 0 or csv_today < 0:
            checks.append(("DB = CSV (today)", False,
                           "db=" + str(db_today) + " csv=" + str(csv_today)))
        else:
            checks.append(("DB = CSV (today)", db_today == csv_today,
                           "db=" + str(db_today) + " csv=" + str(csv_today)))
    except Exception as e:
        checks.append(("DB = CSV (today)", False, str(e)))

    # 4. State sanity
    try:
        in_trade = bool(state.get("in_trade"))
        checks.append(("State", True,
                       "in_trade" if in_trade else "not in trade"))
    except Exception as e:
        checks.append(("State", False, str(e)))

    # 5. WebSocket tick freshness — market-aware. A stale tick at
    #    23:00 IST is normal, not a fault.
    try:
        ws_ok = D.is_tick_live(D.NIFTY_SPOT_TOKEN)
        if not ws_ok and not D.is_market_open():
            checks.append(("WebSocket", True, "idle (market closed)"))
        else:
            checks.append(("WebSocket", ws_ok,
                           "connected" if ws_ok else "stale or closed"))
    except Exception as e:
        checks.append(("WebSocket", False, str(e)))

    # 6. Kite auth — token file present and dated today
    try:
        if os.path.isfile(_TOKEN_PATH):
            with open(_TOKEN_PATH) as f:
                tok = json.load(f)
            today_str = date.today().isoformat()
            ok = tok.get("date") == today_str and bool(tok.get("access_token"))
            checks.append(("Kite auth", ok,
                           "valid" if ok else "stale (date=" + str(tok.get("date", "—")) + ")"))
        else:
            checks.append(("Kite auth", False, "no token file"))
    except Exception as e:
        checks.append(("Kite auth", False, str(e)))

    # 7. Dashboard JSON freshness
    try:
        if os.path.isfile(_DASH_PATH):
            age = int(datetime.now().timestamp() - os.path.getmtime(_DASH_PATH))
            ok  = age < 300  # less than 5 minutes old
            checks.append(("Dashboard", ok,
                           "updated " + str(age) + "s ago"))
        else:
            checks.append(("Dashboard", False, "missing"))
    except Exception as e:
        checks.append(("Dashboard", False, str(e)))

    # 8. Spot LTP available — market-aware. Zero LTP at 11pm is
    #    expected (WebSocket idle); treat as PASS with "market closed".
    try:
        spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        if spot_ltp > 0:
            checks.append(("Spot LTP", True, str(round(spot_ltp, 1))))
        elif not D.is_market_open():
            checks.append(("Spot LTP", True, "0 (market closed)"))
        else:
            checks.append(("Spot LTP", False, "0 — market open but no tick"))
    except Exception as e:
        checks.append(("Spot LTP", False, str(e)))

    # 9. Config version stamp
    try:
        ver = CFG.get().get("version", "—")
        checks.append(("Config", True, ver))
    except Exception as e:
        checks.append(("Config", False, str(e)))

    # 10. Daily reconcile — state vs CSV trade count
    try:
        today = date.today().isoformat()
        csv_today = 0
        if os.path.isfile(_CSV_PATH):
            with open(_CSV_PATH) as f:
                csv_today = sum(1 for r in csv.DictReader(f)
                                if r.get("date") == today)
        state_today = int(state.get("daily_trades", 0) or 0)
        ok = csv_today == state_today
        checks.append(("Reconcile", ok,
                       "state=" + str(state_today) + " csv=" + str(csv_today)))
    except Exception as e:
        checks.append(("Reconcile", False, str(e)))

    passed = sum(1 for _n, ok, _d in checks if ok)
    return {"checks": checks, "passed": passed, "total": len(checks)}


# ═══════════════════════════════════════════════════════════════
#  INTERNAL — single-line summary log per validation run
# ═══════════════════════════════════════════════════════════════

def _log_result(phase: str, total: int, failures: list):
    passed = total - len(failures)
    if failures:
        msg = ("| " + phase + " | " + str(passed) + "/" + str(total) + " PASS"
               " | FAIL: " + "; ".join(failures))
    else:
        msg = "| " + phase + " | " + str(total) + "/" + str(total) + " PASS"
    try:
        val_logger.info(msg)
    except Exception:
        pass
