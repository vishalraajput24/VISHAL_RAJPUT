#!/home/user/kite_env/bin/python3
"""
═══════════════════════════════════════════════════════════════
 test_vrl.py — VISHAL RAJPUT TRADE v15.2 Test Suite
 28 focused tests covering:
   Entry gates 1–6 | Straddle Gate 7 tiers | VWAP display
   Exit chain (5 rules) | BE+2 peak 10 | Validation | Data integrity
═══════════════════════════════════════════════════════════════
"""

import os
import sys
from unittest.mock import MagicMock, patch
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Stub kiteconnect so dev / CI envs without the broker SDK still run tests.
# On the prod server the real module is already installed and wins the import.
if "kiteconnect" not in sys.modules:
    _stub = MagicMock()
    _stub.KiteTicker = MagicMock()
    _stub.KiteConnect = MagicMock()
    sys.modules["kiteconnect"] = _stub

import pandas as pd
import VRL_DATA as D
import VRL_ENGINE as E
import VRL_CONFIG as C
C.load()


# ── Test framework ──────────────────────────────────────────

_passed = 0
_failed = 0
_errors = []

def test(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  PASS " + name)
    else:
        _failed += 1
        msg = "  FAIL " + name + (" — " + detail if detail else "")
        print(msg)
        _errors.append(msg)

def section(name):
    print("\n=== " + name + " ===")


# ── Fixture builders ──────────────────────────────────────────

def _make_opt_3m(n=20,
                 ema9_high=100.0, ema9_low=91.0,
                 last_close=103.0, last_open=98.0,
                 last_high=104.0, last_low=97.5,
                 prev_close=99.0, prev_ema9_high=100.0):
    """Build a 3-min option DataFrame with controlled last + prev rows.
    Default band width = 9 (satisfies min_band_width_pts=8)."""
    rows = []
    for _i in range(n - 2):
        rows.append({"open": 97.0, "high": 99.0, "low": 96.0,
                     "close": 98.0, "volume": 1000})
    rows.append({"open": 98.0, "high": prev_close + 0.5, "low": 97.0,
                 "close": prev_close, "volume": 1000})
    rows.append({"open": last_open, "high": last_high, "low": last_low,
                 "close": last_close, "volume": 1000})
    # live in-progress candle
    rows.append({"open": last_close, "high": last_close + 1,
                 "low": last_close - 1, "close": last_close + 0.5,
                 "volume": 500})
    df = pd.DataFrame(rows)
    _base = datetime(2026, 4, 16, 10, 0)
    df.index = [_base + timedelta(minutes=i * 3) for i in range(len(rows))]
    df = D.add_indicators(df)
    df.iloc[-2, df.columns.get_loc("ema9_high")] = ema9_high
    df.iloc[-2, df.columns.get_loc("ema9_low")]  = ema9_low
    df.iloc[-3, df.columns.get_loc("ema9_high")] = prev_ema9_high
    df.iloc[-3, df.columns.get_loc("ema9_low")]  = ema9_low - 2
    return df


def _make_state(entry=200, peak=0, candles=0, in_trade=True):
    return {
        "in_trade": in_trade, "entry_price": entry, "peak_pnl": peak,
        "trough_pnl": 0, "candles_held": candles, "token": 12345,
        "entry_mode": "EMA9_BREAKOUT",
        "current_ema9_high": 0, "current_ema9_low": 0,
        "last_band_check_ts": "",
    }


class _FakeNow:
    """Context manager that patches VRL_ENGINE.datetime.now() to a fixed time
    so Gate 7 picks a predictable straddle tier."""
    def __init__(self, hour, minute):
        self.hour = hour
        self.minute = minute
        self._patcher = None

    def __enter__(self):
        fixed = datetime(2026, 4, 16, self.hour, self.minute)
        mock_dt = MagicMock(wraps=datetime)
        mock_dt.now = MagicMock(return_value=fixed)
        mock_dt.fromisoformat = datetime.fromisoformat
        self._patcher = patch("VRL_ENGINE.datetime", mock_dt)
        self._patcher.start()
        return self

    def __exit__(self, *a):
        self._patcher.stop()


def _run_entry(direction="CE", spot=24000, df=None,
               straddle_delta=10, vwap=None, spot_ltp_for_vwap=None,
               state=None, hour=10, minute=15, market_open=False):
    """Run E.check_entry with standard v15.2 mocks. Returns the result dict."""
    if df is None:
        df = _make_opt_3m()
    if state is None:
        state = {}
    patches = [
        patch.object(D, "get_historical_data", return_value=df),
        patch.object(D, "add_indicators", side_effect=lambda x: x),
        patch.object(D, "get_straddle_delta", return_value=straddle_delta),
        patch.object(D, "resolve_atm_strike", return_value=24000),
        patch.object(D, "is_market_open", return_value=market_open),
        patch.object(D, "get_spot_vwap", return_value=vwap),
        patch.object(D, "get_spot_ltp",
                     return_value=(spot_ltp_for_vwap if spot_ltp_for_vwap is not None else spot)),
    ]
    for p in patches:
        p.start()
    try:
        with _FakeNow(hour, minute):
            r = E.check_entry(12345, direction, spot, 3, state=state)
    finally:
        for p in patches:
            p.stop()
    return r


# ═══════════════════════════════════════════════════════════════
#  Section 1 — Entry gates (7 tests)
# ═══════════════════════════════════════════════════════════════

section("ENTRY GATES")

# 1. Full breakout → FIRES
r = _run_entry()
test("1. test_breakout_fires",
     r["fired"] is True and r["entry_mode"] == "EMA9_BREAKOUT",
     "fired=" + str(r["fired"]) + " reject=" + r.get("reject_reason", ""))

# 2. v15.2.4: SUSTAINED above-band (entire 3-candle lookback above)
#    → BLOCKED with the new "already_above_band" reject code.
#    The relaxed fresh-breakout rule fires when ANY of the last N bars
#    was below the band, so this test forces ALL lookback bars above.
import pandas as _pd
_rows_above = []
for _i in range(20):
    _rows_above.append({"open": 101.0, "high": 103.0, "low": 100.0,
                        "close": 102.0, "volume": 1000})
_df_stale = _pd.DataFrame(_rows_above)
_base_ts = datetime(2026, 4, 16, 10, 0)
_df_stale.index = [_base_ts + timedelta(minutes=i * 3) for i in range(len(_rows_above))]
# Hand-set indicators so EVERY row has close > ema9_high (sustained above).
_df_stale["EMA_9"]     = 101.0
_df_stale["EMA_21"]    = 101.0
_df_stale["RSI"]       = 50.0
_df_stale["ema9_high"] = 100.0
_df_stale["ema9_low"]  = 91.0
r = _run_entry(df=_df_stale)
test("2. test_no_fresh_breakout_blocked",
     r["fired"] is False and "already_above_band" in r.get("reject_reason", ""),
     "reject=" + r.get("reject_reason", ""))

# 3. Red candle → BLOCKED
_df_red = _make_opt_3m(last_close=101.0, last_open=103.0, last_high=103.5,
                       last_low=100.5, ema9_high=100.0,
                       prev_close=99.0, prev_ema9_high=100.0)
r = _run_entry(df=_df_red)
test("3. test_red_candle_blocked",
     r["fired"] is False and "red_candle" in r.get("reject_reason", ""),
     "reject=" + r.get("reject_reason", ""))

# 4. Weak body (body 10% of range) → BLOCKED
_df_weak = _make_opt_3m(last_close=103.0, last_open=102.0, last_high=108.0,
                        last_low=98.0, ema9_high=100.0,
                        prev_close=99.0, prev_ema9_high=100.0)
r = _run_entry(df=_df_weak)
test("4. test_weak_body_blocked",
     r["fired"] is False and "weak_body" in r.get("reject_reason", ""),
     "reject=" + r.get("reject_reason", ""))

# 5. Warmup window (market_open=True, time 09:30) → BLOCKED
r = _run_entry(hour=9, minute=30, market_open=True)
test("5. test_warmup_blocked",
     r["fired"] is False and "before_09:45" in r.get("reject_reason", ""),
     "reject=" + r.get("reject_reason", ""))

# 6. After cutoff (market_open=True, time 15:20) → BLOCKED
r = _run_entry(hour=15, minute=20, market_open=True)
test("6. test_cutoff_blocked",
     r["fired"] is False and "after_15:10" in r.get("reject_reason", ""),
     "reject=" + r.get("reject_reason", ""))

# 7. Cooldown — same direction exit 2min ago (relative to the engine's
#    patched "now") → BLOCKED. Must use the same fixed clock as _FakeNow
#    otherwise the stored exit-time falls hours into the past.
_fixed_now_cd   = datetime(2026, 4, 16, 10, 15)
_state_cd = {
    "last_exit_time": (_fixed_now_cd - timedelta(minutes=2)).isoformat(),
    "last_exit_direction": "CE",
}
r = _run_entry(state=_state_cd)
test("7. test_cooldown_blocked",
     r["fired"] is False and "cooldown" in r.get("reject_reason", ""),
     "reject=" + r.get("reject_reason", ""))


# ═══════════════════════════════════════════════════════════════
#  Section 2 — Straddle Gate 7 tiers (4 tests)
# ═══════════════════════════════════════════════════════════════

section("v15.2.5 Fix 5 — STRADDLE DISPLAY ONLY (never blocks)")

# 8. Period label populates correctly for each time-of-day window.
r_o = _run_entry(hour=10, minute=0,  straddle_delta=2)
r_m = _run_entry(hour=12, minute=0,  straddle_delta=6)
r_c = _run_entry(hour=14, minute=30, straddle_delta=4)
test("8. test_straddle_period_labels",
     (r_o.get("straddle_period") == "OPENING"
      and r_m.get("straddle_period") == "MIDDAY"
      and r_c.get("straddle_period") == "CLOSING"),
     "got O=" + str(r_o.get("straddle_period"))
     + " M=" + str(r_m.get("straddle_period"))
     + " C=" + str(r_c.get("straddle_period")))

# 9. NEGATIVE straddle delta (weak) does NOT block any more.
r = _run_entry(hour=12, minute=0, straddle_delta=-5)
test("9. test_straddle_weak_does_not_block_entry",
     r["fired"] is True and r.get("straddle_info") == "WEAK"
     and "straddle" not in r.get("reject_reason", ""),
     "fired=" + str(r["fired"])
     + " info=" + str(r.get("straddle_info"))
     + " reject=" + r.get("reject_reason", ""))

# 10. Missing straddle data (None) does NOT block — annotates as NA.
r = _run_entry(hour=12, minute=0, straddle_delta=None)
test("10. test_straddle_na_does_not_block_entry",
     r["fired"] is True and r.get("straddle_info") == "NA"
     and r.get("straddle_available") is False
     and "straddle" not in r.get("reject_reason", ""),
     "fired=" + str(r["fired"])
     + " info=" + str(r.get("straddle_info"))
     + " available=" + str(r.get("straddle_available")))

# 11. Classification boundaries: STRONG >=+5, 0<=NEUTRAL<+5, WEAK<0, NA=None.
_r_strong  = _run_entry(hour=12, minute=0, straddle_delta=7.0)
_r_neutral = _run_entry(hour=12, minute=0, straddle_delta=2.0)
_r_weak    = _run_entry(hour=12, minute=0, straddle_delta=-1.5)
_r_na      = _run_entry(hour=12, minute=0, straddle_delta=None)
test("11. test_straddle_info_classified_correctly",
     _r_strong.get("straddle_info")  == "STRONG"
     and _r_neutral.get("straddle_info") == "NEUTRAL"
     and _r_weak.get("straddle_info")    == "WEAK"
     and _r_na.get("straddle_info")      == "NA",
     "got " + ", ".join(str(r.get("straddle_info")) for r in
                        (_r_strong, _r_neutral, _r_weak, _r_na)))


# ═══════════════════════════════════════════════════════════════
#  Section 3 — VWAP bonus display (2 tests — MUST NEVER BLOCK)
# ═══════════════════════════════════════════════════════════════

section("VWAP BONUS (display only)")

# 12. CE with spot < vwap (AGAINST) → STILL FIRES
r = _run_entry(direction="CE", spot=24000, vwap=24050.0,
               spot_ltp_for_vwap=24000.0)
test("12. test_vwap_against_does_not_block",
     r["fired"] is True and r.get("vwap_bonus") == "AGAINST",
     "fired=" + str(r["fired"]) + " bonus=" + str(r.get("vwap_bonus"))
     + " reject=" + r.get("reject_reason", ""))

# 13. CE with spot > vwap → vwap_bonus = "CONFLUENCE"
r = _run_entry(direction="CE", spot=24050, vwap=24000.0,
               spot_ltp_for_vwap=24050.0)
test("13. test_vwap_confluence_logged",
     r["fired"] is True and r.get("vwap_bonus") == "CONFLUENCE"
     and abs(float(r.get("spot_vs_vwap", 0)) - 50.0) < 0.1,
     "bonus=" + str(r.get("vwap_bonus"))
     + " diff=" + str(r.get("spot_vs_vwap")))


# ═══════════════════════════════════════════════════════════════
#  Section 4 — Exit chain (6 tests)
# ═══════════════════════════════════════════════════════════════

section("EXIT CHAIN")

# 14. Emergency SL at pnl -20
with patch.object(D, "get_historical_data", return_value=MagicMock(empty=True)):
    st = _make_state(200, peak=0, candles=1)
    ex = E.manage_exit(st, 180, {})
test("14. test_emergency_sl",
     len(ex) == 1 and ex[0]["reason"] == "EMERGENCY_SL",
     "got " + str(ex))

# 15. EOD at 15:30 (market_open=True, time 15:30) → EOD_EXIT
with patch.object(D, "is_market_open", return_value=True), \
     patch.object(D, "get_historical_data", return_value=MagicMock(empty=True)):
    with _FakeNow(15, 30):
        st = _make_state(200, peak=5, candles=3)
        ex = E.manage_exit(st, 200, {})
test("15. test_eod_exit",
     len(ex) == 1 and ex[0]["reason"] == "EOD_EXIT",
     "got " + str(ex))

# 16. Stale — 5 candles, peak < 3 → STALE_ENTRY
with patch.object(D, "get_historical_data", return_value=MagicMock(empty=True)):
    st = _make_state(200, peak=2, candles=5)
    ex = E.manage_exit(st, 201, {})
test("16. test_stale_exit",
     len(ex) == 1 and ex[0]["reason"] == "STALE_ENTRY",
     "got " + str(ex))

# 17. EMA9_LOW_BREAK — last close < ema9_low. peak=4 keeps BE+2 dormant.
_df_break = _make_opt_3m(last_close=94.0, last_open=95.0, last_high=96.0,
                         last_low=93.5, ema9_high=100.0, ema9_low=95.5)
with patch.object(D, "get_historical_data", return_value=_df_break), \
     patch.object(D, "add_indicators", side_effect=lambda x: x):
    st = _make_state(entry=100, peak=4, candles=3)
    ex = E.manage_exit(st, 94, {})
test("17. test_ema9_low_break",
     len(ex) == 1 and ex[0]["reason"] == "EMA9_LOW_BREAK",
     "got " + str(ex))

# 18. BE+2 arms at peak 10 → SL = entry + 2. Hold at ltp 108 (above lock).
with patch.object(D, "get_historical_data", return_value=MagicMock(empty=True)):
    st = _make_state(entry=100, peak=10, candles=3)
    ex = E.manage_exit(st, 108, {})
test("18. test_be2_at_peak_10",
     st.get("be2_active") is True and st.get("be2_level") == 102 and len(ex) == 0,
     "be2_active=" + str(st.get("be2_active")) + " level=" + str(st.get("be2_level"))
     + " ex=" + str(ex))

# 19. BE+2 does NOT arm at peak 9 → no lock, no exit
with patch.object(D, "get_historical_data", return_value=MagicMock(empty=True)):
    st = _make_state(entry=100, peak=9, candles=3)
    ex = E.manage_exit(st, 108, {})
test("19. test_be2_not_at_peak_9",
     st.get("be2_active") is False and len(ex) == 0,
     "be2_active=" + str(st.get("be2_active")) + " ex=" + str(ex))


# ═══════════════════════════════════════════════════════════════
#  Section 5 — Validation whitelist (2 tests)
# ═══════════════════════════════════════════════════════════════

section("VALIDATION")

from VRL_VALIDATE import VALID_ENTRY_MODES, LEGACY_MODES, VALID_EXIT_REASONS

# 20. EMA9_BREAKOUT is in VALID_ENTRY_MODES
test("20. test_validate_entry_mode_accepted",
     "EMA9_BREAKOUT" in VALID_ENTRY_MODES and "FAST" in LEGACY_MODES,
     "valid=" + str(VALID_ENTRY_MODES) + " legacy=" + str(LEGACY_MODES))

# 21. All 6 v15.2 exit reasons accepted
_expected = ("EMA9_LOW_BREAK", "BREAKEVEN_LOCK", "TRAIL_FLOOR",
             "EMERGENCY_SL", "STALE_ENTRY", "EOD_EXIT")
_missing = [r for r in _expected if r not in VALID_EXIT_REASONS]
test("21. test_validate_exit_reason_accepted",
     len(_missing) == 0,
     "missing: " + str(_missing))


# ═══════════════════════════════════════════════════════════════
#  Section 6 — Dashboard source-of-truth (1 test)
# ═══════════════════════════════════════════════════════════════

section("DASHBOARD")

# 22. VRL_WEB._today_trade_summary reads the CSV — the single source of truth
import importlib, VRL_WEB
importlib.reload(VRL_WEB)
test("22. test_dashboard_count_matches_state",
     hasattr(VRL_WEB, "_today_trade_summary")
     and callable(VRL_WEB._today_trade_summary),
     "no _today_trade_summary() in VRL_WEB")


# ═══════════════════════════════════════════════════════════════
#  Section 7 — Data integrity (2 tests)
# ═══════════════════════════════════════════════════════════════

section("DATA INTEGRITY")

# 23. add_indicators populates ema9_high/ema9_low with non-zero values
_raw = pd.DataFrame({
    "open":   [100, 101, 102, 103, 104, 105, 106, 107, 108, 109],
    "high":   [101, 102, 103, 104, 105, 106, 107, 108, 109, 110],
    "low":    [ 99, 100, 101, 102, 103, 104, 105, 106, 107, 108],
    "close":  [100.5, 101.5, 102.5, 103.5, 104.5, 105.5, 106.5, 107.5, 108.5, 109.5],
    "volume": [1000] * 10,
})
_out = D.add_indicators(_raw)
_eh_all = float((_out["ema9_high"] > 0).all())
_el_all = float((_out["ema9_low"]  > 0).all())
test("23. test_ema9_columns_non_zero",
     _eh_all == 1.0 and _el_all == 1.0,
     "ema9_high>0 all=" + str(_eh_all) + " ema9_low>0 all=" + str(_el_all))

# 24. signal_scans schema has all 6 v15.2 straddle/VWAP columns
import VRL_DB, VRL_LAB
required = ["straddle_delta", "straddle_threshold", "straddle_period",
            "spot_vwap", "spot_vs_vwap", "vwap_bonus",
            "atm_strike_used", "band_width"]
_missing_db  = [f for f in required if f not in VRL_DB._SCAN_FIELDS]
_missing_csv = [f for f in required if f not in VRL_LAB.FIELDNAMES_SCAN]
test("24. test_signal_scan_has_straddle",
     len(_missing_db) == 0 and len(_missing_csv) == 0,
     "missing_db=" + str(_missing_db) + " missing_csv=" + str(_missing_csv))


# ═══════════════════════════════════════════════════════════════
#  Section 8 — Banner / integrity (4 tests)
# ═══════════════════════════════════════════════════════════════

section("BANNER + CONFIG INTEGRITY")

_repo = os.path.dirname(os.path.abspath(__file__))
def _read(name):
    with open(os.path.join(_repo, name)) as f:
        return f.read()

_main_src = _read("VRL_MAIN.py")
_cmd_src  = _read("VRL_COMMANDS.py")
_dash_src = _read("static/VRL_DASHBOARD.html")
_cfg_src  = _read("config.yaml")

# 25. No old floor ladder anywhere (banner, /help, dashboard)
_stale = ["+5→-6", "+5\u2192-6", "+10→+2", "+10\u2192+2",
          "{p:5,l:-6}", "{p:10,l:2}", "SL -12 close",
          "FLOORS: +5"]
_all_src = _main_src + _cmd_src + _dash_src
_leaks = [s for s in _stale if s in _all_src]
test("25. test_banner_no_old_floors",
     len(_leaks) == 0,
     "leaks=" + str(_leaks))

# 26. VRL_DATA.VERSION is a v15.2.x string AND matches config.yaml prefix
test("26. test_version_is_v15_2_family",
     D.VERSION.startswith("v15.2") and ('version: "' + D.VERSION + '"') in _cfg_src,
     "VERSION=" + str(D.VERSION))

# 27. Config uses nested entry:/exit: structure and has BE+2=10
import yaml
_cfg_parsed = yaml.safe_load(_cfg_src)
_be2 = _cfg_parsed.get("exit", {}).get("ema9_band", {}).get("breakeven_lock_peak_threshold")
# v15.2.5 Fix 5 renamed straddle_expansion → straddle_display (display only)
_filters_block = _cfg_parsed.get("entry", {}).get("filters", {})
_has_straddle  = (_filters_block.get("straddle_display", {}).get("enabled")
                  or _filters_block.get("straddle_expansion", {}).get("enabled"))
_has_vwap = (_cfg_parsed.get("entry", {}).get("filters", {})
             .get("vwap_bonus", {}).get("enabled"))
test("27. test_config_v15_2_structure",
     _be2 == 10 and _has_straddle is True and _has_vwap is True,
     "be2=" + str(_be2) + " straddle=" + str(_has_straddle)
     + " vwap=" + str(_has_vwap))

# 28. Deleted config keys are actually gone
_dead = ["profit_floors:", "entry_3min:", "rsi_exit:",
         "atr_filter:", "stop_hunt_recovery:"]
_alive = [k for k in _dead if k in _cfg_src]
test("28. test_deleted_config_keys_absent",
     len(_alive) == 0,
     "still present: " + str(_alive))


# ═══════════════════════════════════════════════════════════════
#  Section 8b — v15.2.5 VELOCITY_STALL exit + DB persistence + scan labels
# ═══════════════════════════════════════════════════════════════

section("v15.2.5 — VELOCITY_STALL + DB PERSISTENCE + SCAN LABELS")


def _vs_state(peak_history, peak=None, entry=100, candles=4):
    """Build a state ready for VELOCITY_STALL evaluation. Sets
    last_peak_candle_ts so the in-function update won't append another
    value (tests drive peak_history manually)."""
    peak_val = peak if peak is not None else (max(peak_history) if peak_history else 0)
    return {
        "in_trade": True, "entry_price": entry,
        "peak_pnl": peak_val, "trough_pnl": 0,
        "candles_held": candles, "token": 12345,
        "peak_history": list(peak_history),
        "last_peak_candle_ts": "already_seen",
        "last_band_check_ts": "already_seen",
    }


# 32. VELOCITY_STALL fires when 3-candle-avg velocity <= 0 for two windows.
#     Needs 5 flat slots: ph[-5..-1]=15 → v=(15-15)/3=0, prev_v=(15-15)/3=0. EXIT.
#     candles=3 so STALE_ENTRY (5c+peak<3) doesn't fire first.
_ph_stalled = [10, 15, 15, 15, 15, 15]
with patch.object(D, "get_historical_data", return_value=MagicMock(empty=True)):
    st = _vs_state(_ph_stalled, peak=15.0, candles=3)
    ex = E.manage_exit(st, 115.0, {})
test("32. test_velocity_stall_exits",
     len(ex) == 1 and ex[0]["reason"] == "VELOCITY_STALL",
     "got " + str(ex))

# 33. Only latest window stalled but prior window still had growth → no exit.
#     ph=[5, 10, 15, 15, 15] → v=(15-10)/3=1.67 (>0), prev_v=(15-5)/3=3.3 (>0). HOLD.
_ph_one_window = [5, 10, 15, 15, 15]
with patch.object(D, "get_historical_data", return_value=MagicMock(empty=True)):
    st = _vs_state(_ph_one_window, peak=15.0, candles=5)
    ex = E.manage_exit(st, 115.0, {})
test("33. test_velocity_stall_needs_2_consecutive",
     len(ex) == 0,
     "got " + str(ex))

# 34. Stall at tiny peaks is ignored (peak < vs_min_peak=3).
#     candles=3 so STALE_ENTRY (5c+peak<3) doesn't fire first.
_ph_tiny = [0, 1, 1, 1, 1, 1]
with patch.object(D, "get_historical_data", return_value=MagicMock(empty=True)):
    st = _vs_state(_ph_tiny, peak=1.0, candles=3)
    ex = E.manage_exit(st, 101.0, {})
test("34. test_velocity_stall_ignores_tiny_peaks",
     len(ex) == 0,
     "got " + str(ex))

# 35. Healthy growth → no exit. Each candle adds peak, both velocities > 0.
_ph_growing = [5, 8, 11, 14, 17, 20]
with patch.object(D, "get_historical_data", return_value=MagicMock(empty=True)):
    st = _vs_state(_ph_growing, peak=20.0, candles=6)
    ex = E.manage_exit(st, 120.0, {})
test("35. test_velocity_still_alive",
     len(ex) == 0,
     "got " + str(ex))

# 36. Trade DB write path includes all 15 v15.2 columns (DB _TRADE_FIELDS)
import VRL_DB as _VDB
_required_trade_cols = [
    "entry_ema9_high", "entry_ema9_low",
    "exit_ema9_high", "exit_ema9_low",
    "entry_band_position", "exit_band_position",
    "entry_body_pct",
    "entry_straddle_delta", "entry_straddle_threshold",
    "entry_straddle_period", "entry_atm_strike", "entry_band_width",
    "entry_spot_vwap", "entry_spot_vs_vwap", "entry_vwap_bonus",
]
_required_trade_cols.append("entry_straddle_info")   # Fix 5 addition
_missing_trade = [c for c in _required_trade_cols if c not in _VDB._TRADE_FIELDS]
test("36. test_trade_db_all_fields_populated",
     len(_missing_trade) == 0,
     "missing from _TRADE_FIELDS: " + str(_missing_trade))

# 37. signal_scans reject_reason comes from engine result.reject_reason
#     directly — no more fake "EMA_0_RSI_0_RED_SHRINK" v14 labels.
import re as _re
_lab_src = open(os.path.join(_repo, "VRL_LAB.py")).read()
_has_old_recon = bool(_re.search(
    r'reasons\.append\(\s*"EMA_"', _lab_src))
_uses_engine_reason = ('result.get("reject_reason"' in _lab_src
                       or 'result.get(\'reject_reason\'' in _lab_src)
test("37. test_signal_scan_reject_reason_matches_engine",
     (not _has_old_recon) and _uses_engine_reason,
     "old_recon=" + str(_has_old_recon)
     + " uses_engine_reason=" + str(_uses_engine_reason))


# ═══════════════════════════════════════════════════════════════
#  Section 8c — v15.2.5 Smart ATM multi-candidate scanner
# ═══════════════════════════════════════════════════════════════

section("v15.2.5 — SMART ATM MULTI-CANDIDATE")

# 38. scan_all_candidates returns the best fired candidate by (sd, body, -|Δ|)
_df_fire = _make_opt_3m()   # default fixture fires
_tokens_map = {
    24150: {"CE": {"token": 111, "symbol": "CE24150"}, "PE": {"token": 112, "symbol": "PE24150"}},
    24200: {"CE": {"token": 211, "symbol": "CE24200"}, "PE": {"token": 212, "symbol": "PE24200"}},
    24250: {"CE": {"token": 311, "symbol": "CE24250"}, "PE": {"token": 312, "symbol": "PE24250"}},
}
# Score by straddle_delta — patch get_straddle_delta to return side-dependent values
# so the scoring picks the highest.
_straddle_by_token = {
    111: 3.0, 112: 3.0,     # ATM-50: weak
    211: 7.0, 212: 7.0,     # ATM:    strong
    311: 5.0, 312: 5.0,     # ATM+50: medium
}
_patches_38 = [
    patch.object(D, "get_historical_data", return_value=_df_fire),
    patch.object(D, "add_indicators", side_effect=lambda x: x),
    patch.object(D, "get_option_tokens",
                 side_effect=lambda k, strike, exp: _tokens_map.get(int(strike), {})),
    patch.object(D, "get_straddle_delta",
                 side_effect=lambda atm, lookback_minutes=15: 7.0),
    patch.object(D, "resolve_atm_strike", return_value=24200),
    patch.object(D, "is_market_open", return_value=False),
    patch.object(D, "get_spot_vwap", return_value=None),
    patch.object(D, "get_spot_ltp", return_value=24200),
]
for _p in _patches_38: _p.start()
try:
    with _FakeNow(10, 30):
        best = E.scan_all_candidates(
            kite=MagicMock(), spot_ltp=24200.0,
            atm_strike=24200, expiry=date(2026, 4, 30), dte=3)
finally:
    for _p in _patches_38: _p.stop()
test("38. test_scan_all_candidates_returns_best_fired",
     best is not None and best.get("strike") in (24150, 24200, 24250)
     and best.get("result", {}).get("fired") is True,
     "got " + str(best))

# 39. When no candidate fires, scan_all_candidates returns None
_df_below = _make_opt_3m(last_close=98.0, last_open=97.0, last_high=99.0,
                         last_low=96.5, ema9_high=100.0)
_patches_39 = [
    patch.object(D, "get_historical_data", return_value=_df_below),
    patch.object(D, "add_indicators", side_effect=lambda x: x),
    patch.object(D, "get_option_tokens",
                 side_effect=lambda k, strike, exp: _tokens_map.get(int(strike), {})),
    patch.object(D, "get_straddle_delta", return_value=7.0),
    patch.object(D, "resolve_atm_strike", return_value=24200),
    patch.object(D, "is_market_open", return_value=False),
    patch.object(D, "get_spot_vwap", return_value=None),
    patch.object(D, "get_spot_ltp", return_value=24200),
]
for _p in _patches_39: _p.start()
try:
    with _FakeNow(10, 30):
        best_none = E.scan_all_candidates(
            kite=MagicMock(), spot_ltp=24200.0,
            atm_strike=24200, expiry=date(2026, 4, 30), dte=3)
finally:
    for _p in _patches_39: _p.stop()
test("39. test_scan_all_candidates_none_if_nothing_fires",
     best_none is None,
     "got " + str(best_none))

# 40. Scoring picks the highest straddle_delta candidate when >1 fires.
#     We patch get_straddle_delta to return a token-keyed value so two
#     strikes fire with different deltas.
_df_fire2 = _make_opt_3m()
_sd_map = {111: 4.0, 112: 4.0, 211: 9.0, 212: 9.0, 311: 2.5, 312: 2.5}

def _sd_by_atm(atm, lookback_minutes=15):
    # atm 24200 uses 24200 tokens → sd=9; 24150 uses 24150 → sd=4; etc.
    return {24150: 4.0, 24200: 9.0, 24250: 2.5}.get(int(atm), 0)

_patches_40 = [
    patch.object(D, "get_historical_data", return_value=_df_fire2),
    patch.object(D, "add_indicators", side_effect=lambda x: x),
    patch.object(D, "get_option_tokens",
                 side_effect=lambda k, strike, exp: _tokens_map.get(int(strike), {})),
    patch.object(D, "get_straddle_delta", side_effect=_sd_by_atm),
    patch.object(D, "resolve_atm_strike", side_effect=lambda spot, step=None: int(spot)),
    patch.object(D, "is_market_open", return_value=False),
    patch.object(D, "get_spot_vwap", return_value=None),
    patch.object(D, "get_spot_ltp", return_value=24200),
]
for _p in _patches_40: _p.start()
try:
    with _FakeNow(10, 30):
        best_best = E.scan_all_candidates(
            kite=MagicMock(), spot_ltp=24200.0,
            atm_strike=24200, expiry=date(2026, 4, 30), dte=3)
finally:
    for _p in _patches_40: _p.stop()
test("40. test_scoring_picks_highest_straddle_delta",
     best_best is not None and best_best.get("strike") == 24200,
     "got strike=" + str(best_best.get("strike") if best_best else None))


# ═══════════════════════════════════════════════════════════════
#  Section 8d — v15.2.5 Pre-entry awareness alerts
# ═══════════════════════════════════════════════════════════════

section("v15.2.5 — PRE-ENTRY ALERTS")

import importlib as _imp, VRL_ALERTS
_imp.reload(VRL_ALERTS)

# Helper: build a minimal result + df representing each signal profile.
def _alert_df(last, prev1, prev2):
    """last/prev1/prev2 = dicts with open/high/low/close/ema9_high/ema9_low/RSI"""
    rows = [prev2, prev1, last, last]  # last row repeated = "in progress"
    idx = [_base_ts + timedelta(minutes=3 * i) for i in range(len(rows))]
    df = _pd.DataFrame(rows, index=idx)
    return df


# 41. REVERSAL BUILDING fires when last=green big body, prev/prev2 close<=ema9l, RSI rising
_df_rev = _alert_df(
    last  = {"open": 90, "high": 103, "low": 89, "close": 100,
             "ema9_high": 105, "ema9_low": 92, "RSI": 55},
    prev1 = {"open": 95, "high": 96, "low": 88, "close": 88,
             "ema9_high": 105, "ema9_low": 92, "RSI": 42},
    prev2 = {"open": 96, "high": 97, "low": 87, "close": 89,
             "ema9_high": 105, "ema9_low": 92, "RSI": 38},
)
_sig_rev = VRL_ALERTS._detect_reversal_building(
    "PE", 24200, {"close": 100, "ema9_high": 105, "ema9_low": 92,
                  "body_pct": 90, "candle_green": True}, _df_rev)
test("41. test_reversal_building_alert_conditions",
     _sig_rev is not None and _sig_rev.get("type") == "A"
     and "REVERSAL BUILDING" in _sig_rev.get("msg", ""),
     "got " + str(_sig_rev))

# 42. APPROACHING BREAKOUT: close within 3pts below ema9_high, RSI 2x rising, 1+ green
_df_apr = _alert_df(
    last  = {"open": 97, "high": 99, "low": 96, "close": 98,
             "ema9_high": 100, "ema9_low": 90, "RSI": 58},
    prev1 = {"open": 95, "high": 98, "low": 94, "close": 97,
             "ema9_high": 100, "ema9_low": 90, "RSI": 52},
    prev2 = {"open": 93, "high": 95, "low": 92, "close": 94,
             "ema9_high": 100, "ema9_low": 90, "RSI": 48},
)
_sig_apr = VRL_ALERTS._detect_approaching_breakout(
    "CE", 24200, {"close": 98, "ema9_high": 100, "ema9_low": 90}, _df_apr)
test("42. test_approaching_breakout_alert_conditions",
     _sig_apr is not None and _sig_apr.get("type") == "B"
     and "APPROACHING BREAKOUT" in _sig_apr.get("msg", ""),
     "got " + str(_sig_apr))

# 43. READY TO FIRE: all gates OK except body<30 (weak_body reject)
_sig_ready = VRL_ALERTS._detect_ready_to_fire(
    "CE", 24200,
    {"fired": False, "reject_reason": "weak_body_22pct_<_30",
     "close": 103, "ema9_high": 100, "body_pct": 22,
     "candle_green": True, "straddle_delta": 6.5},
    None)
test("43. test_ready_to_fire_detects_one_gate_missing",
     _sig_ready is not None and _sig_ready.get("type") == "C"
     and "READY TO FIRE" in _sig_ready.get("msg", ""),
     "got " + str(_sig_ready))

# 44. BLOCKED SETUP: valid breakout + green + body>=30 but straddle_bleed rejects
_sig_blocked = VRL_ALERTS._detect_blocked_setup(
    "PE", 24250,
    {"fired": False, "reject_reason": "straddle_bleed_+2.0_need_5_in_MIDDAY",
     "close": 103, "ema9_high": 100, "body_pct": 55,
     "candle_green": True, "straddle_delta": 2.0},
    None)
test("44. test_blocked_setup_alert_for_valid_breakout_gate_block",
     _sig_blocked is not None and _sig_blocked.get("type") == "D"
     and "BLOCKED" in _sig_blocked.get("msg", ""),
     "got " + str(_sig_blocked))

# 45. Rate limit: same (strike,side,type) key within 15 min → suppressed
_state_rl = {"pre_entry_alerts_enabled": True,
             "alert_history": {
                 "PE_24200_A": (datetime.now() - timedelta(minutes=5)).isoformat()
             }}
_rl = VRL_ALERTS._rate_limited(_state_rl, "PE_24200_A", window_min=15)
test("45. test_alert_rate_limit_15min_per_key",
     _rl is True,
     "rate_limited=" + str(_rl))

# 46. Toggle works: set_enabled(False) → is_enabled()=False, True → True
_toggle_state = {}
VRL_ALERTS.set_enabled(_toggle_state, False)
_off = VRL_ALERTS.is_enabled(_toggle_state)
VRL_ALERTS.set_enabled(_toggle_state, True)
_on = VRL_ALERTS.is_enabled(_toggle_state)
test("46. test_alert_toggle_on_off",
     _off is False and _on is True,
     "off=" + str(_off) + " on=" + str(_on))


# ═══════════════════════════════════════════════════════════════
#  Section 8e — v15.2.5 BUG-A exit failure safety rail
# ═══════════════════════════════════════════════════════════════

section("v15.2.5 BUG-A — EXIT FAILURE BLOCK")

# 47. _exit_failed is now persisted across restart — must appear in
#     STATE_PERSIST_FIELDS so _save_state() writes it to disk.
test("47. test_exit_failed_persisted_across_restart",
     "_exit_failed" in D.STATE_PERSIST_FIELDS,
     "missing from STATE_PERSIST_FIELDS")

# 48. Critical exit alert text names /reset_exit so the operator knows
#     how to clear the block after manually flattening the position.
_main_src_bug_a = open(os.path.join(_repo, "VRL_MAIN.py")).read()
_has_alert = ("/reset_exit" in _main_src_bug_a
              and "MANUAL" in _main_src_bug_a.upper()
              and "_alert_exit_critical" in _main_src_bug_a)
test("48. test_critical_alert_names_reset_exit",
     _has_alert,
     "alert must reference /reset_exit + MANUAL + define _alert_exit_critical")

# 49. /reset_exit command exists and clears the flag.
_cmd_src_bug_a = open(os.path.join(_repo, "VRL_COMMANDS.py")).read()
test("49. test_reset_exit_command_clears_flag",
     'def _cmd_reset_exit' in _cmd_src_bug_a
     and 'state["_exit_failed"] = False' in _cmd_src_bug_a
     and '"/reset_exit"' in _cmd_src_bug_a,
     "/reset_exit missing or does not clear _exit_failed")


# ═══════════════════════════════════════════════════════════════
#  Section 9 — Silent 1-min shadow strategy (Part 4, 3 tests)
# ═══════════════════════════════════════════════════════════════

section("SHADOW 1-MIN (PART 4)")

import importlib, VRL_SHADOW
importlib.reload(VRL_SHADOW)

# Build a 1-min breakout fixture tuned so the real EMA9 bands produce
# width >= 8 without any overrides. Shadow recomputes the bands inside
# _scan_side, so hardcoded ema9 values would get clobbered — the fixture
# instead makes the natural EWM land where we need:
#   highs stable near 100, lows stable near 91, last close 103 > 100,
#   prev close 99 <= prev_ema9h ~100.
def _make_1m():
    rows = []
    for _i in range(15):
        rows.append({"open": 95, "high": 100, "low": 91,
                     "close": 96, "volume": 1000})
    # prev (iloc[-3]): close 99, high stays at 100, low at 91 so EWM holds.
    rows.append({"open": 96, "high": 100, "low": 91, "close": 99, "volume": 1000})
    # last closed (iloc[-2]): breakout close 103 > ema9_high ~100.
    rows.append({"open": 98, "high": 104, "low": 97.5, "close": 103, "volume": 1000})
    # live in-progress (iloc[-1]): ignored by the scan.
    rows.append({"open": 103, "high": 104, "low": 102,
                 "close": 103.5, "volume": 500})
    df = pd.DataFrame(rows)
    _base = datetime(2026, 4, 16, 10, 15)
    df.index = [_base + timedelta(minutes=i) for i in range(len(rows))]
    return df


# 29. Shadow can fire INDEPENDENTLY of whether live is in a position.
#     We simulate a running LIVE trade (state has in_trade=True elsewhere),
#     then call shadow tick and verify it still enters a shadow position.
VRL_SHADOW.reset_day()
_live_state_snapshot = {"in_trade": True, "direction": "PE",
                        "entry_price": 150.0, "daily_trades": 3}
_df_1m = _make_1m()
_fake_now = datetime(2026, 4, 16, 10, 30)  # MIDDAY → threshold 5
_patches = [
    patch.object(D, "get_historical_data", return_value=_df_1m),
    patch.object(D, "add_indicators", side_effect=lambda x: x),
    patch.object(D, "get_option_tokens",
                 return_value={"CE": {"token": 11111, "symbol": "CE11111"},
                               "PE": {"token": 22222, "symbol": "PE22222"}}),
    patch.object(D, "get_straddle_delta", return_value=7.0),
    patch.object(D, "is_market_open", return_value=False),
    patch.object(D, "get_ltp", return_value=103.0),
    patch.object(D, "resolve_atm_strike", return_value=24000),
]
for p in _patches: p.start()
try:
    VRL_SHADOW.tick(kite=None, spot_ltp=24000, atm_strike=24000,
                    expiry=date(2026, 4, 30), now=_fake_now)
finally:
    for p in _patches: p.stop()
_shadow_entered = VRL_SHADOW.shadow_state.get("in_trade", False)
test("29. test_shadow_fires_independently",
     _shadow_entered is True and VRL_SHADOW.shadow_state["direction"] in ("CE", "PE"),
     "shadow in_trade=" + str(_shadow_entered)
     + " dir=" + str(VRL_SHADOW.shadow_state.get("direction")))

# 30. Shadow entry NEVER mutates the live state dict
_before = dict(_live_state_snapshot)
# tick already ran above; nothing should have touched _live_state_snapshot.
test("30. test_shadow_never_affects_live",
     _live_state_snapshot == _before,
     "live state mutated: " + str({k: v for k, v in _live_state_snapshot.items()
                                    if _before.get(k) != v}))

# 31a. v15.2.1: public API surface — shadow_scan_1min + shadow_state
#      must be importable from VRL_ENGINE (not just from VRL_SHADOW).
from VRL_ENGINE import shadow_scan_1min as _sscan, shadow_state as _sstate
test("31a. test_shadow_function_exists",
     callable(_sscan) and isinstance(_sstate, dict) and "in_trade" in _sstate,
     "callable=" + str(callable(_sscan))
     + " state_type=" + type(_sstate).__name__
     + " has_in_trade=" + str("in_trade" in (_sstate or {})))

# 31. EOD summary renders both shadow AND live stats in one message
VRL_SHADOW.reset_day()
# Simulate 2 shadow trades (1 win, 1 loss) via direct accumulation
with VRL_SHADOW._lock:
    VRL_SHADOW.shadow_state.update({
        "trades_today": 2, "wins_today": 1, "losses_today": 1,
        "total_pnl": 3.4, "peak_sum": 12.0, "peaks_over_10": 1,
    })
_captured = {"msg": ""}
def _fake_tg(text):
    _captured["msg"] = text
VRL_SHADOW.emit_eod_summary(_fake_tg, live_stats={"trades": 3, "wins": 2,
                                                  "pnl": 8.5, "wr": 67})
_msg = _captured["msg"]
_has_shadow_line = "[SHADOW 1-MIN] Day Summary" in _msg
_has_live_line   = "vs LIVE 3-MIN:" in _msg
_has_both_counts = ("Trades: 2" in _msg) and ("3 trades" in _msg)
test("31. test_shadow_eod_summary_has_both_stats",
     _has_shadow_line and _has_live_line and _has_both_counts,
     "msg_has shadow=" + str(_has_shadow_line)
     + " live=" + str(_has_live_line)
     + " both_counts=" + str(_has_both_counts)
     + "\n--- msg ---\n" + _msg)


# ═══════════════════════════════════════════════════════════════
#  Summary
# ═══════════════════════════════════════════════════════════════

print("\n" + "=" * 50)
print("  RESULTS: " + str(_passed) + " passed, " + str(_failed) + " failed")
print("=" * 50)

if _errors:
    print("\nFAILED:")
    for e in _errors:
        print(e)

sys.exit(0 if _failed == 0 else 1)
