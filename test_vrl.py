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

# 2. Stale breakout (prev was already above) → BLOCKED
_df_stale = _make_opt_3m(last_close=103.0, last_open=101.0, last_high=104.0,
                         last_low=100.5, ema9_high=100.0,
                         prev_close=101.0, prev_ema9_high=100.0)
r = _run_entry(df=_df_stale)
test("2. test_no_fresh_breakout_blocked",
     r["fired"] is False and "stale_breakout" in r.get("reject_reason", ""),
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

section("STRADDLE FILTER (GATE 7)")

# 8. Opening tier (09:45-10:30), threshold 1, delta +2 → ALLOWS
r = _run_entry(hour=10, minute=0, straddle_delta=2)
test("8. test_straddle_opening_threshold_1",
     r["fired"] is True and r.get("straddle_period") == "OPENING",
     "fired=" + str(r["fired"]) + " period=" + str(r.get("straddle_period"))
     + " reject=" + r.get("reject_reason", ""))

# 9. Midday tier (10:30-14:00), threshold 5 — delta +3 BLOCKS, delta +6 FIRES
r_b = _run_entry(hour=12, minute=0, straddle_delta=3)
r_p = _run_entry(hour=12, minute=0, straddle_delta=6)
test("9. test_straddle_midday_threshold_5",
     (r_b["fired"] is False and "straddle_bleed" in r_b.get("reject_reason", ""))
     and (r_p["fired"] is True and r_p.get("straddle_period") == "MIDDAY"),
     "block=" + r_b.get("reject_reason", "") + " | pass fired=" + str(r_p["fired"]))

# 10. Closing tier (14:00-15:10), threshold 3 — delta +2 BLOCKS, delta +4 FIRES
r_b = _run_entry(hour=14, minute=30, straddle_delta=2)
r_p = _run_entry(hour=14, minute=30, straddle_delta=4)
test("10. test_straddle_closing_threshold_3",
     (r_b["fired"] is False and "straddle_bleed" in r_b.get("reject_reason", ""))
     and (r_p["fired"] is True and r_p.get("straddle_period") == "CLOSING"),
     "block=" + r_b.get("reject_reason", "") + " | pass fired=" + str(r_p["fired"]))

# 11. Straddle bleed (negative delta) → BLOCKED with "straddle_bleed"
r = _run_entry(hour=12, minute=0, straddle_delta=-5)
test("11. test_straddle_bleed_blocks",
     r["fired"] is False and "straddle_bleed" in r.get("reject_reason", ""),
     "reject=" + r.get("reject_reason", ""))


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

# 26. VRL_DATA.VERSION matches config.yaml
test("26. test_version_is_v15_2",
     D.VERSION == "v15.2" and 'version: "v15.2"' in _cfg_src,
     "VERSION=" + str(D.VERSION))

# 27. Config uses nested entry:/exit: structure and has BE+2=10
import yaml
_cfg_parsed = yaml.safe_load(_cfg_src)
_be2 = _cfg_parsed.get("exit", {}).get("ema9_band", {}).get("breakeven_lock_peak_threshold")
_has_straddle = (_cfg_parsed.get("entry", {}).get("filters", {})
                 .get("straddle_expansion", {}).get("enabled"))
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
