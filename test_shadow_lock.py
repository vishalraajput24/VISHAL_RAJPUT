#!/home/user/kite_env/bin/python3
"""
═══════════════════════════════════════════════════════════════
 test_shadow_lock.py — Batch 5/6 BUG-N regression

 Verifies VRL_SHADOW.shadow_state mutations are serialized under
 the module's _lock. The proof is structural (every read/write
 we care about is under `with _lock:`) rather than stress-tested,
 because pytest-style thread racing against a live kite-backed
 tick() needs network and would be flaky. Structural check is
 enough to catch the "whoops I forgot a lock" regression class.
═══════════════════════════════════════════════════════════════
"""
import os
import sys
import re
from unittest.mock import MagicMock

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
        print("  FAIL " + name + ((" — " + detail if detail else "")))


print("=== BUG-N — shadow_state mutations locked ===")


import VRL_SHADOW   # noqa: E402

_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "VRL_SHADOW.py")).read()


def _fn_body(name):
    """Extract the source of function `name` for static inspection."""
    m = re.search(r"^def " + re.escape(name) + r"\(.*?\n(?:.|\n)*?(?=\n(?:def |\Z))",
                  _src, re.M)
    return m.group(0) if m else ""


# 1. Module has _lock.
test("1. _lock exists at module scope",
     hasattr(VRL_SHADOW, "_lock") and VRL_SHADOW._lock is not None,
     "VRL_SHADOW._lock missing")

# 2. Known mutation points still use `with _lock:`.
for fn in ("reset_day", "day_summary"):
    body = _fn_body(fn)
    test("2. " + fn + " uses with _lock:",
         "with _lock:" in body,
         fn + " missing lock")

# 3. tick() gates in_trade under the lock (BUG-N fix).
tick_body = _fn_body("tick")
_in_trade_guarded = bool(re.search(
    r"with _lock:\s*\n\s*_in_trade\s*=\s*shadow_state\.get\(\"in_trade\"\)",
    tick_body))
test("3. tick() reads in_trade under _lock (BUG-N)",
     _in_trade_guarded,
     "tick() must snapshot in_trade inside a `with _lock:` block")

# 4. _scan_side() snapshots cooldown fields under the lock (BUG-N fix).
scan_body = _fn_body("_scan_side")
_cd_guarded = bool(re.search(
    r"with _lock:\s*\n\s*le_ts\s*=\s*shadow_state\.get\(\"last_exit_time\"\)",
    scan_body))
test("4. _scan_side() reads last_exit_* under _lock (BUG-N)",
     _cd_guarded,
     "_scan_side() must snapshot last_exit_time/direction under _lock")

# 5. _manage() snapshots direction + token under the lock (BUG-N fix).
manage_body = _fn_body("_manage")
_mgr_guarded = bool(re.search(
    r"with _lock:\s*\n\s*direction\s*=\s*shadow_state\.get\(\"direction\"",
    manage_body))
test("5. _manage() reads direction+token under _lock (BUG-N)",
     _mgr_guarded,
     "_manage() must snapshot direction and token under _lock")

# 6. _manage() still updates running stats under the lock (regression).
_stats_guarded = (
    "with _lock:" in manage_body
    and 'shadow_state["trades_today"] += 1' in manage_body
)
test("6. _manage() running-stats update still under _lock (regression)",
     _stats_guarded,
     "stats update must remain inside `with _lock:`")


print()
print("=" * 50)
print("RESULTS: " + str(_passed) + " passed, " + str(_failed) + " failed")
sys.exit(0 if _failed == 0 else 1)
