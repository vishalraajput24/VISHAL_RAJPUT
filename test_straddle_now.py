#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
 test_straddle_now.py — v15.2.4 standalone straddle diagnostic

 Purpose: confirm v15.2.3 tz-safe straddle fix produces real deltas
 live. Run directly on the server (inside kite_env), the script
 authenticates with Kite and probes three ATM strikes.

 Usage:
     python3 ~/VISHAL_RAJPUT/test_straddle_now.py
     python3 ~/VISHAL_RAJPUT/test_straddle_now.py 24200 24250 24300

 Expected output (good):
     Version: v15.2
     Time:    2026-04-17 10:15:32
     ATM 24250: straddle_delta=+2.7
     ATM 24300: straddle_delta=+4.1
     ATM 24350: straddle_delta=-1.8

 Expected output (NA still): every line reads "straddle_delta=None".
═══════════════════════════════════════════════════════════════
"""
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import VRL_DATA as D
from VRL_AUTH import get_kite


def _bootstrap_kite():
    """Attach kite to VRL_DATA so get_option_tokens() / historical_data()
    don't short-circuit. Mirrors what VRL_MAIN does on startup."""
    try:
        kite = get_kite()
    except Exception as e:
        print("[FATAL] get_kite() failed: " + str(e))
        sys.exit(2)
    try:
        D.init(kite)
    except Exception as e:
        print("[FATAL] D.init(kite) failed: " + str(e))
        sys.exit(2)
    return kite


def main():
    strikes = [int(x) for x in sys.argv[1:]] if len(sys.argv) > 1 \
              else [24250, 24300, 24350]

    print("Version: " + str(D.VERSION))
    print("Time:    " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print()

    _bootstrap_kite()

    for strike in strikes:
        try:
            delta = D.get_straddle_delta(strike)
        except Exception as e:
            delta = "ERR: " + str(e)
        # Format delta with explicit sign when numeric
        if isinstance(delta, (int, float)):
            disp = ("+" if delta >= 0 else "") + str(round(delta, 2)) + " pts"
        else:
            disp = str(delta)
        print("ATM " + str(strike) + ": straddle_delta=" + disp)


if __name__ == "__main__":
    main()
