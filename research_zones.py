#!/home/user/kite_env/bin/python3
"""
═══════════════════════════════════════════════════════════════
 research_zones.py — VISHAL RAJPUT TRADE
 Demand/Supply Zone Detection from Historical Spot Data
 
 Run: python3 research_zones.py
 
 Uses 60 days of Kite historical data (15-min spot candles)
 Detects institutional demand/supply zones
 Outputs: ~/research/zones_YYYYMMDD.csv
 
 LOGIC:
   Demand Zone: Price consolidates (tight range) → explodes UP
                (impulse candle body>60% + volume>1.5x)
                The consolidation range = demand zone
   
   Supply Zone: Price consolidates → drops DOWN with impulse
                The consolidation range = supply zone
   
   Zone Strength:
     - Impulse size (bigger = stronger)
     - Fresh (not revisited) = strongest
     - Multi-timeframe alignment = bonus
     - Each revisit weakens zone
     - 3+ revisits = zone dead
 
 STANDALONE — does NOT touch bot strategy
═══════════════════════════════════════════════════════════════
"""

import csv
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.expanduser("~/VISHAL_RAJPUT"))

import pandas as pd
from VRL_AUTH import get_kite
import VRL_DATA as D

# ── Config ────────────────────────────────────────────────────

DAYS_BACK        = 60      # How far back to scan
CONSOL_MIN       = 3       # Min candles for consolidation
CONSOL_MAX       = 8       # Max candles for consolidation
CONSOL_RANGE_MAX = 80      # Max pts range to qualify as consolidation (15-min)
IMPULSE_BODY_PCT = 50      # Min body % for impulse candle
IMPULSE_VOL_MULT = 0       # Spot index has NO volume — disabled
IMPULSE_MIN_PTS  = 30      # Min impulse move (pts)
ZONE_PROXIMITY   = 40      # Within X pts = "near zone"
ZONE_MAX_TESTS   = 3       # After X revisits, zone is dead

OUTPUT_DIR       = os.path.expanduser("~/research")
TIMEFRAMES       = ["15minute", "60minute", "3minute"]  # Detect on both


# ── Zone Detection ────────────────────────────────────────────

def detect_zones(df, timeframe="15minute"):
    """
    Scan dataframe for consolidation → impulse patterns.
    Returns list of zone dicts.
    """
    zones = []
    if df.empty or len(df) < 20:
        return zones

    # Pre-compute volume average (rolling 10)
    df["vol_avg"] = df["volume"].rolling(10).mean()
    
    i = CONSOL_MAX + 1  # Start after enough history
    
    while i < len(df) - 1:
        # Check if candle at position i is an impulse
        candle = df.iloc[i]
        o, h, l, c = float(candle["open"]), float(candle["high"]), float(candle["low"]), float(candle["close"])
        rng = h - l
        if rng <= 0:
            i += 1
            continue
        
        body = abs(c - o)
        body_pct = (body / rng) * 100
        vol = float(candle["volume"])
        vol_avg = float(candle["vol_avg"]) if candle["vol_avg"] > 0 else 1
        vol_ratio = vol / vol_avg
        
        # Is this an impulse candle?
        if body_pct < IMPULSE_BODY_PCT:  # Volume check removed — spot has no volume
            i += 1
            continue
        
        impulse_pts = body
        if impulse_pts < IMPULSE_MIN_PTS:
            i += 1
            continue
        
        bullish = c > o  # Green candle = bullish impulse
        
        # Look backward for consolidation
        best_consol = None
        
        for consol_len in range(CONSOL_MIN, CONSOL_MAX + 1):
            start_idx = i - consol_len
            if start_idx < 0:
                continue
            
            consol_candles = df.iloc[start_idx:i]
            consol_high = float(consol_candles["high"].max())
            consol_low = float(consol_candles["low"].min())
            consol_range = consol_high - consol_low
            
            if consol_range > CONSOL_RANGE_MAX:
                continue
            
            # Valid consolidation found
            best_consol = {
                "high": round(consol_high, 2),
                "low": round(consol_low, 2),
                "range": round(consol_range, 2),
                "candles": consol_len,
                "start_idx": start_idx,
            }
            break  # Take shortest valid consolidation
        
        if best_consol is None:
            i += 1
            continue
        
        # Zone found!
        zone_type = "DEMAND" if bullish else "SUPPLY"
        zone_high = best_consol["high"]
        zone_low = best_consol["low"]
        zone_mid = round((zone_high + zone_low) / 2, 2)
        
        # Impulse strength
        strength = "STRONG" if impulse_pts >= 40 else "MODERATE" if impulse_pts >= 25 else "WEAK"
        
        ts = str(df.index[i])[:16] if hasattr(df.index[i], "strftime") else str(df.index[i])[:16]
        
        zones.append({
            "zone_type": zone_type,
            "zone_high": zone_high,
            "zone_low": zone_low,
            "zone_mid": zone_mid,
            "zone_range": best_consol["range"],
            "impulse_pts": round(impulse_pts, 1),
            "impulse_body_pct": round(body_pct, 1),
            "impulse_vol_ratio": round(vol_ratio, 2),
            "strength": strength,
            "date": ts[:10],
            "time": ts[11:16],
            "timeframe": timeframe,
            "consol_candles": best_consol["candles"],
            "times_tested": 0,
            "still_active": True,
        })
        
        # Skip past this impulse
        i += 2
        continue
    
    return zones


def count_zone_tests(zones, df):
    """
    For each zone, count how many times price returned to it AFTER formation.
    Each revisit weakens the zone.
    """
    for zone in zones:
        zone_date = zone["date"] + " " + zone["time"]
        tests = 0
        idx = 0

        while idx < len(df):
            ts = str(df.index[idx])[:16]
            if ts <= zone_date:
                idx += 1
                continue

            price = float(df.iloc[idx]["close"])
            if zone["zone_low"] <= price <= zone["zone_high"]:
                tests += 1
                # Don't count consecutive candles in zone as separate tests
                # Skip ahead until price leaves zone
                while idx + 1 < len(df):
                    next_price = float(df.iloc[idx + 1]["close"])
                    if zone["zone_low"] <= next_price <= zone["zone_high"]:
                        idx += 1
                    else:
                        break
            idx += 1

        zone["times_tested"] = tests
        zone["still_active"] = tests < ZONE_MAX_TESTS

    return zones


def merge_overlapping(zones):
    """Merge zones that overlap on the same timeframe."""
    if not zones:
        return zones
    
    merged = []
    zones_sorted = sorted(zones, key=lambda z: z["zone_low"])
    
    current = zones_sorted[0].copy()
    
    for z in zones_sorted[1:]:
        # Check overlap
        if (z["zone_low"] <= current["zone_high"] + 5
                and z["zone_type"] == current["zone_type"]
                and z["timeframe"] == current["timeframe"]):
            # Merge — take wider range, stronger attributes
            current["zone_high"] = max(current["zone_high"], z["zone_high"])
            current["zone_low"] = min(current["zone_low"], z["zone_low"])
            current["zone_mid"] = round((current["zone_high"] + current["zone_low"]) / 2, 2)
            current["zone_range"] = round(current["zone_high"] - current["zone_low"], 2)
            if z["impulse_pts"] > current["impulse_pts"]:
                current["strength"] = z["strength"]
                current["impulse_pts"] = z["impulse_pts"]
            current["times_tested"] = max(current["times_tested"], z["times_tested"])
        else:
            merged.append(current)
            current = z.copy()
    
    merged.append(current)
    return merged


def check_multi_tf_alignment(zones_15m, zones_60m):
    """Mark zones that appear on both timeframes."""
    for z15 in zones_15m:
        for z60 in zones_60m:
            if (z15["zone_type"] == z60["zone_type"]
                    and abs(z15["zone_mid"] - z60["zone_mid"]) <= 30):
                z15["multi_tf"] = True
                z15["strength"] = "STRONG"
                z60["multi_tf"] = True
                break
        else:
            z15["multi_tf"] = False
    
    for z60 in zones_60m:
        if "multi_tf" not in z60:
            z60["multi_tf"] = False


def get_current_proximity(zones, current_spot):
    """For each zone, calculate distance from current spot."""
    for z in zones:
        dist = current_spot - z["zone_mid"]
        z["distance_from_spot"] = round(dist, 1)
        
        if abs(dist) <= ZONE_PROXIMITY:
            z["proximity"] = "AT ZONE"
        elif abs(dist) <= ZONE_PROXIMITY * 2:
            z["proximity"] = "NEAR"
        else:
            z["proximity"] = "FAR"


# ── Output ────────────────────────────────────────────────────

FIELDNAMES = [
    "zone_type", "zone_high", "zone_low", "zone_mid", "zone_range",
    "strength", "impulse_pts", "impulse_body_pct", "impulse_vol_ratio",
    "date", "time", "timeframe", "consol_candles",
    "times_tested", "still_active", "multi_tf",
    "distance_from_spot", "proximity",
]


def save_zones(zones, current_spot):
    """Save zones to CSV and print summary."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    today = date.today().strftime("%Y%m%d")
    path = os.path.join(OUTPUT_DIR, "zones_" + today + ".csv")
    
    # Sort: active first, then by distance from spot
    zones.sort(key=lambda z: (not z["still_active"], abs(z.get("distance_from_spot", 999))))
    
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(zones)
    
    print("\n" + "=" * 60)
    print("  ZONES SAVED: " + path)
    print("  Total: " + str(len(zones)) + " zones detected")
    print("=" * 60)


def print_summary(zones, current_spot):
    """Print readable zone summary."""
    active = [z for z in zones if z["still_active"]]
    demand = [z for z in active if z["zone_type"] == "DEMAND"]
    supply = [z for z in active if z["zone_type"] == "SUPPLY"]
    
    print("\n" + "=" * 60)
    print("  DEMAND/SUPPLY ZONE ANALYSIS")
    print("  Spot: " + str(round(current_spot, 1))
          + "  Date: " + date.today().isoformat())
    print("=" * 60)
    
    print("\n  📊 SUMMARY")
    print("  Active zones : " + str(len(active)) + " / " + str(len(zones)))
    print("  Demand zones : " + str(len(demand)))
    print("  Supply zones : " + str(len(supply)))
    
    # Nearest zones
    near_demand = [z for z in demand if abs(z.get("distance_from_spot", 999)) <= 100]
    near_supply = [z for z in supply if abs(z.get("distance_from_spot", 999)) <= 100]
    
    if near_demand:
        print("\n  🟢 DEMAND ZONES WITHIN 100pts (support — CE friendly)")
        print("  " + "-" * 56)
        for z in sorted(near_demand, key=lambda x: abs(x.get("distance_from_spot", 0))):
            mtf = " 🔥MTF" if z.get("multi_tf") else ""
            print("  " + str(z["zone_low"]) + " - " + str(z["zone_high"])
                  + "  [" + z["strength"] + "]"
                  + "  impulse=" + str(z["impulse_pts"]) + "pts"
                  + "  tested=" + str(z["times_tested"]) + "x"
                  + "  dist=" + str(z.get("distance_from_spot", 0)) + "pts"
                  + "  " + z.get("proximity", "")
                  + mtf)
    else:
        print("\n  🟢 No demand zones within 100pts")
    
    if near_supply:
        print("\n  🔴 SUPPLY ZONES WITHIN 100pts (resistance — PE friendly)")
        print("  " + "-" * 56)
        for z in sorted(near_supply, key=lambda x: abs(x.get("distance_from_spot", 0))):
            mtf = " 🔥MTF" if z.get("multi_tf") else ""
            print("  " + str(z["zone_low"]) + " - " + str(z["zone_high"])
                  + "  [" + z["strength"] + "]"
                  + "  impulse=" + str(z["impulse_pts"]) + "pts"
                  + "  tested=" + str(z["times_tested"]) + "x"
                  + "  dist=" + str(z.get("distance_from_spot", 0)) + "pts"
                  + "  " + z.get("proximity", "")
                  + mtf)
    else:
        print("\n  🔴 No supply zones within 100pts")
    
    # Trading implication
    print("\n  📋 TRADING IMPLICATION")
    nearest_d = min(demand, key=lambda z: abs(z.get("distance_from_spot", 999))) if demand else None
    nearest_s = min(supply, key=lambda z: abs(z.get("distance_from_spot", 999))) if supply else None
    
    if nearest_d and abs(nearest_d.get("distance_from_spot", 999)) <= ZONE_PROXIMITY:
        print("  ⚠️  SPOT AT DEMAND ZONE " + str(nearest_d["zone_low"]) + "-" + str(nearest_d["zone_high"]))
        print("     CE gets +1 score bonus (institutional buy zone)")
        print("     PE risky — institutions buying here")
    elif nearest_s and abs(nearest_s.get("distance_from_spot", 999)) <= ZONE_PROXIMITY:
        print("  ⚠️  SPOT AT SUPPLY ZONE " + str(nearest_s["zone_low"]) + "-" + str(nearest_s["zone_high"]))
        print("     PE gets +1 score bonus (institutional sell zone)")
        print("     CE risky — institutions selling here")
    else:
        print("  ✅ Spot in open territory — no zone conflict")
        if nearest_d:
            print("     Nearest demand: " + str(nearest_d["zone_low"]) + "-" + str(nearest_d["zone_high"])
                  + " (" + str(abs(nearest_d.get("distance_from_spot", 0))) + "pts away)")
        if nearest_s:
            print("     Nearest supply: " + str(nearest_s["zone_low"]) + "-" + str(nearest_s["zone_high"])
                  + " (" + str(abs(nearest_s.get("distance_from_spot", 0))) + "pts away)")
    
    print("\n" + "=" * 60)


# ── Telegram Alert ────────────────────────────────────────────

def send_zone_alert(zones, current_spot):
    """Send zone summary to Telegram."""
    try:
        import requests
        active = [z for z in zones if z["still_active"]]
        demand = [z for z in active if z["zone_type"] == "DEMAND"]
        supply = [z for z in active if z["zone_type"] == "SUPPLY"]
        
        near = [z for z in active if abs(z.get("distance_from_spot", 999)) <= 50]
        
        msg = ("🗺 <b>ZONE ANALYSIS</b>  " + date.today().isoformat() + "\n"
               "Spot: " + str(round(current_spot, 1)) + "\n"
               "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
               "Active: " + str(len(active)) + " zones\n"
               "Demand: " + str(len(demand)) + "  Supply: " + str(len(supply)) + "\n")
        
        if near:
            msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            msg += "<b>NEARBY ZONES (±50pts)</b>\n"
            for z in near:
                icon = "🟢" if z["zone_type"] == "DEMAND" else "🔴"
                mtf = " MTF🔥" if z.get("multi_tf") else ""
                msg += (icon + " " + z["zone_type"][:3] + " "
                        + str(z["zone_low"]) + "-" + str(z["zone_high"])
                        + " [" + z["strength"][:3] + "]"
                        + " " + str(z.get("distance_from_spot", 0)) + "pts"
                        + mtf + "\n")
        else:
            msg += "✅ No zones within 50pts\n"
        
        url = "https://api.telegram.org/bot" + D.TELEGRAM_TOKEN + "/sendMessage"
        requests.post(url, json={
            "chat_id": D.TELEGRAM_CHAT_ID,
            "text": msg, "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print("[TG] " + str(e))


# ── Main ──────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  DEMAND/SUPPLY ZONE DETECTOR")
    print("  Scanning " + str(DAYS_BACK) + " days of Nifty spot data")
    print("=" * 60)
    
    kite = get_kite()
    
    now = datetime.now()
    from_dt = now - timedelta(days=DAYS_BACK)
    
    all_zones = []
    
    for tf in TIMEFRAMES:
        print("\n[FETCH] " + tf + " candles...")
        time.sleep(0.5)
        
        try:
            raw = kite.historical_data(
                instrument_token=D.NIFTY_SPOT_TOKEN,
                from_date=from_dt, to_date=now,
                interval=tf, continuous=False, oi=False)
            
            if not raw:
                print("[WARN] No data for " + tf)
                continue
            
            df = pd.DataFrame(raw)
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            
            for col in ("open", "high", "low", "close"):
                df[col] = df[col].astype(float)
            df["volume"] = df["volume"].astype(int)
            
            print("[OK] " + str(len(df)) + " candles loaded")
            
            # Detect zones
            zones = detect_zones(df, tf)
            print("[ZONES] " + str(len(zones)) + " raw zones on " + tf)
            
            # Count revisits
            zones = count_zone_tests(zones, df)
            
            all_zones.extend(zones)
            
        except Exception as e:
            print("[ERROR] " + tf + ": " + str(e))
    
    if not all_zones:
        print("\n[RESULT] No zones detected. Check Kite connection.")
        return
    
    # Merge overlapping
    all_zones = merge_overlapping(all_zones)
    print("\n[MERGED] " + str(len(all_zones)) + " unique zones")
    
    # Multi-TF alignment
    z15 = [z for z in all_zones if z["timeframe"] == "15minute"]
    z60 = [z for z in all_zones if z["timeframe"] == "60minute"]
    check_multi_tf_alignment(z15, z60)
    
    # Current spot
    try:
        D.init(kite)
        D.start_websocket()
        time.sleep(3)
        current_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        if current_spot <= 0:
            q = kite.ltp(["NSE:NIFTY 50"])
            current_spot = float(list(q.values())[0]["last_price"])
    except Exception:
        # Fallback — use last close from data
        current_spot = float(raw[-1]["close"]) if raw else 0
    
    print("[SPOT] Current: " + str(round(current_spot, 1)))
    
    # Calculate proximity
    get_current_proximity(all_zones, current_spot)
    
    # Output
    save_zones(all_zones, current_spot)
    print_summary(all_zones, current_spot)
    send_zone_alert(all_zones, current_spot)
    
    # Also save as JSON for dashboard
    try:
        import json
        json_path = os.path.join(os.path.expanduser("~/state"), "vrl_zones.json")
        active = [z for z in all_zones if z["still_active"]]
        with open(json_path, "w") as f:
            json.dump({
                "date": date.today().isoformat(),
                "spot": round(current_spot, 1),
                "total_zones": len(all_zones),
                "active_zones": len(active),
                "zones": active,
            }, f, indent=2, default=str)
        print("[JSON] " + json_path)
    except Exception as e:
        print("[JSON] Error: " + str(e))


if __name__ == "__main__":
    main()
