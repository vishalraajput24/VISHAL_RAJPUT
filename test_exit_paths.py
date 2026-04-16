#!/home/user/kite_env/bin/python3
"""
═══════════════════════════════════════════════════════════════
 test_exit_paths.py — Batch 3/6 BUG-D regression

 Verifies that every exit path in VRL_MAIN threads `saved_entry_price`
 through to _log_trade so PNL is computed from the REAL entry price,
 not a race-stale state read. Audit of 2026-04-16 identified two
 FAILs in the pre-fix source: _execute_exit() legacy wrapper and its
 FORCE_EXIT caller. Both paths now forward the captured entry.
═══════════════════════════════════════════════════════════════
"""
import os
import sys
import re

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


print("=== BUG-D — exit paths thread saved_entry_price ===")

_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "VRL_MAIN.py")).read()

# 1. The legacy wrapper now ACCEPTS saved_entry_price.
_sig_pat = re.compile(
    r"def _execute_exit\(\s*kite,\s*option_ltp:\s*float,"
    r"\s*reason:\s*str,\s*\n\s*saved_entry_price", re.M)
test("1. _execute_exit signature accepts saved_entry_price",
     bool(_sig_pat.search(_src)),
     "signature must be _execute_exit(kite, option_ltp, reason, "
     "saved_entry_price=None)")

# 2. Wrapper FORWARDS it to _execute_exit_v13.
_wrapper_body = _src.split("def _execute_exit(kite, option_ltp:"
                           " float, reason: str,")[1].split("\ndef ")[0]
test("2. _execute_exit forwards saved_entry_price to _v13",
     "saved_entry_price=saved_entry_price" in _wrapper_body,
     "wrapper body must pass saved_entry_price through")

# 3. FORCE_EXIT caller now passes its captured _entry_px through.
#    Find the FORCE_EXIT block and confirm saved_entry_price=_entry_px.
_fe_block_m = re.search(
    r'if _force and _in_trade:\s*\n.*?"FORCE_EXIT".*?(?=\n                continue)',
    _src, re.S)
_fe_ok = bool(_fe_block_m and "saved_entry_price=_entry_px" in _fe_block_m.group(0))
test("3. FORCE_EXIT caller passes saved_entry_price=_entry_px",
     _fe_ok,
     "FORCE_EXIT block must thread the captured _entry_px through")

# 4. EOD + manage_exit main loop already pass saved_entry_price=_saved_entry
#    (unchanged by this fix, but re-confirm it didn't regress).
test("4. main exit loop still passes saved_entry_price=_saved_entry",
     "_execute_exit_v13(kite, _exit, saved_entry_price=_saved_entry)" in _src,
     "regression — the EOD/manage_exit loop must still thread "
     "_saved_entry")

# 5. _execute_exit_v13 signature unchanged (still accepts saved_entry_price).
test("5. _execute_exit_v13 signature unchanged",
     "def _execute_exit_v13(kite, exit_info: dict, saved_entry_price: float = None):"
     in _src,
     "upstream v13 signature must remain stable")


print()
print("=" * 50)
print("RESULTS: " + str(_passed) + " passed, " + str(_failed) + " failed")
sys.exit(0 if _failed == 0 else 1)
