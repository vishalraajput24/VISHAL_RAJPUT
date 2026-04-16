#!/home/user/kite_env/bin/python3
"""
═══════════════════════════════════════════════════════════════
 test_shutdown.py — v15.2.5 Batch 2/6 fix(BUG-C-tail)

 Verifies that VRL_MAIN._shutdown() sends a Telegram alert via
 _tg_send_sync (blocking) when the bot is killed with an open
 position. Uses a fresh file rather than extending test_vrl.py
 to keep Batch-1 files untouched per batch-2 constraint.
═══════════════════════════════════════════════════════════════
"""
import os
import sys
from unittest.mock import patch, MagicMock

# Stub third-party modules for dev environments that don't have them.
for _m in ("kiteconnect", "pyotp", "requests"):
    if _m not in sys.modules:
        _stub = MagicMock()
        if _m == "kiteconnect":
            _stub.KiteTicker  = MagicMock()
            _stub.KiteConnect = MagicMock()
        sys.modules[_m] = _stub

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


_passed = 0
_failed = 0


def test(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  PASS " + name)
    else:
        _failed += 1
        print("  FAIL " + name + ((" — " + detail) if detail else ""))


print("=== BUG-C-tail — shutdown Telegram on open trade ===")

import VRL_MAIN

# 1. Source-level check: the _shutdown path invokes _tg_send_sync,
#    NOT the async _tg_send, and carries the spec-exact message prefix.
_main_src = open(os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "VRL_MAIN.py")).read()
_shutdown_block = _main_src.split("def _shutdown(signum, frame):")[1]\
                           .split("\ndef ")[0]
_uses_sync  = "_tg_send_sync(" in _shutdown_block
_spec_text  = "VRL SHUTDOWN with open position:" in _shutdown_block
test("1. shutdown uses _tg_send_sync (blocking) not _tg_send (async)",
     _uses_sync,
     "shutdown block must call _tg_send_sync so the message fires "
     "before sys.exit(0)")
test("2. shutdown message matches spec prefix",
     _spec_text,
     "expected substring 'VRL SHUTDOWN with open position:' in "
     "_shutdown block")

# 3. Behavior: when _shutdown runs with in_trade=True, _tg_send_sync
#    IS called; when in_trade=False it is NOT called.
try:
    calls = {"n": 0, "last": ""}

    def _capture(text, *a, **kw):
        calls["n"] += 1
        calls["last"] = text
        return True

    # Short-circuit the rest of the shutdown path so the test doesn't
    # touch real pid files, state files, or call sys.exit.
    with patch.object(VRL_MAIN, "_tg_send_sync", side_effect=_capture), \
         patch.object(VRL_MAIN, "_stop_telegram_listener", return_value=None), \
         patch.object(VRL_MAIN, "_save_state",             return_value=None), \
         patch.object(VRL_MAIN, "_remove_pid",             return_value=None), \
         patch.object(VRL_MAIN, "sys",                     MagicMock()):

        # Scenario A: no open trade → no telegram.
        VRL_MAIN.state["in_trade"] = False
        VRL_MAIN._shutdown(signum=15, frame=None)
        _no_trade_ok = (calls["n"] == 0)

        # Scenario B: open trade → exactly one telegram with the spec text.
        VRL_MAIN.state["in_trade"]    = True
        VRL_MAIN.state["symbol"]      = "NIFTY26APR24200PE"
        VRL_MAIN.state["entry_price"] = 180.3
        VRL_MAIN.state["peak_pnl"]    = 12.4
        VRL_MAIN._shutdown(signum=15, frame=None)
        _with_trade_ok = (
            calls["n"] == 1
            and "VRL SHUTDOWN with open position:" in calls["last"]
            and "NIFTY26APR24200PE" in calls["last"]
            and "entry=180.3" in calls["last"]
            and "peak=12.4" in calls["last"]
        )

    test("3. _shutdown with no open trade → no telegram",
         _no_trade_ok,
         "calls=" + str(calls["n"]))
    test("4. _shutdown with open trade → 1 telegram with full spec text",
         _with_trade_ok,
         "last=" + calls["last"])
except SystemExit:
    # sys.exit is stubbed; any real exit here is a test bug.
    test("3/4. _shutdown behavior",  False, "SystemExit leaked past stub")
finally:
    # Clean up global state so this test doesn't pollute sibling suites.
    VRL_MAIN.state["in_trade"] = False
    VRL_MAIN.state["symbol"]   = ""


print()
print("=" * 50)
print("RESULTS: " + str(_passed) + " passed, " + str(_failed) + " failed")
sys.exit(0 if _failed == 0 else 1)
