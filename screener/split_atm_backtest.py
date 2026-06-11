"""
Split-ATM V10 Backtest
CE = floor(spot/50)*50  (ITM call)
PE = ceil(spot/50)*50   (ITM put)
Gates tested:
  A) Original:  MOMENTUM ema9h+3.5 | OPP DECAY [-8,-4]
  B) Tuned:     MOMENTUM ema9h+5.0 | OPP DECAY [-12,-5]
Exit ladder: initial_sl=ema9_low, breakeven@+12, trail@+18 (peak-10)
"""
import pandas as pd
import numpy as np
import glob, os
from datetime import datetime, time as dtime

DATA_DIR = "/home/vishalraajput24/lab_data/options_1min"
LOT_SIZE = 65
BLACKOUT_END = dtime(9, 45)
EOD_EXIT = dtime(15, 29)
EMA_SPAN = 9

CONFIGS = {
    "Original": {"mom_gap": 3.5, "decay_lo": -8.0, "decay_hi": -4.0},
    "Tuned":    {"mom_gap": 5.0, "decay_lo": -12.0, "decay_hi": -5.0},
}


def ema9(series, span=EMA_SPAN):
    return series.ewm(span=span, adjust=False).mean()


def load_day(filepath):
    df = pd.read_csv(filepath, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def get_split_atm(spot):
    ce_strike = int((spot // 50) * 50)
    pe_strike = int(((spot + 49) // 50) * 50)
    return ce_strike, pe_strike


def build_leg(df, strike, opt_type):
    leg = df[(df["strike"] == strike) & (df["type"] == opt_type)].copy()
    leg = leg.sort_values("timestamp").reset_index(drop=True)
    leg["ema9_high"] = ema9(leg["high"])
    leg["ema9_low"]  = ema9(leg["low"])
    return leg


def simulate_trade(active_leg, entry_idx, initial_sl, direction):
    entry_price = active_leg.loc[entry_idx, "close"]
    peak_pnl = 0.0
    current_sl = initial_sl

    for i in range(entry_idx + 1, len(active_leg)):
        row = active_leg.iloc[i]
        ts = row["timestamp"].time()

        candle_high = row["high"]
        candle_low  = row["low"]
        candle_close = row["close"]

        # Update peak using candle high
        pnl_high = candle_high - entry_price
        peak_pnl = max(peak_pnl, pnl_high)

        # Compute SL tier
        if peak_pnl < 12:
            current_sl = initial_sl
        elif peak_pnl < 18:
            current_sl = max(initial_sl, entry_price)         # breakeven
        else:
            current_sl = max(initial_sl, entry_price, candle_high - 10.0)  # trail

        # SL hit: check if candle low touches SL
        if candle_low <= current_sl:
            exit_price = current_sl
            exit_reason = "SL"
            pnl_pts = exit_price - entry_price
            return pnl_pts, exit_reason, i

        # EOD exit
        if ts >= EOD_EXIT:
            exit_price = candle_close
            pnl_pts = exit_price - entry_price
            return pnl_pts, "EOD", i

    # ran out of candles
    exit_price = active_leg.iloc[-1]["close"]
    pnl_pts = exit_price - entry_price
    return pnl_pts, "EOD", len(active_leg) - 1


def run_day(df, cfg, date_str):
    trades = []
    opening_spot = df[df["timestamp"] == df["timestamp"].min()]["spot_ref"].iloc[0]
    ce_strike, pe_strike = get_split_atm(opening_spot)

    available = set(zip(df["strike"], df["type"]))
    if (ce_strike, "CE") not in available or (pe_strike, "PE") not in available:
        return trades  # skip incomplete days

    ce_leg = build_leg(df, ce_strike, "CE")
    pe_leg = build_leg(df, pe_strike, "PE")

    mom_gap  = cfg["mom_gap"]
    decay_lo = cfg["decay_lo"]
    decay_hi = cfg["decay_hi"]

    in_trade = False
    last_exit_ts = None
    last_exit_dir = None
    last_exit_candle_ts = None

    # align timestamps
    ce_by_ts = ce_leg.set_index("timestamp")
    pe_by_ts = pe_leg.set_index("timestamp")

    timestamps = sorted(set(ce_leg["timestamp"]) & set(pe_leg["timestamp"]))

    i = 0
    while i < len(timestamps):
        ts = timestamps[i]
        t  = ts.time()

        if t < BLACKOUT_END:
            i += 1
            continue
        if t >= EOD_EXIT:
            break

        if in_trade:
            i += 1
            continue

        # same-side 3-min blocker
        def blocked(direction):
            if last_exit_dir == direction and last_exit_ts is not None:
                secs = (ts - last_exit_ts).total_seconds()
                return secs < 180
            return False

        # same exit-candle cooldown
        def exit_candle_block():
            return last_exit_candle_ts is not None and ts == last_exit_candle_ts

        if exit_candle_block():
            i += 1
            continue

        ce_row = ce_by_ts.loc[ts]
        pe_row = pe_by_ts.loc[ts]

        # --- Check CE entry (momentum on CE, decay on PE) ---
        if not blocked("CE"):
            ce_mom  = ce_row["close"] >= ce_row["ema9_high"] + mom_gap
            pe_decay_val = pe_row["close"] - pe_row["ema9_low"]
            pe_decay = decay_lo <= pe_decay_val <= decay_hi

            if ce_mom and pe_decay:
                entry_idx = ce_leg[ce_leg["timestamp"] == ts].index[0]
                entry_price = ce_row["close"]
                initial_sl  = ce_row["ema9_low"]
                if initial_sl >= entry_price:
                    initial_sl = entry_price - 5.0

                pnl_pts, reason, exit_i = simulate_trade(ce_leg, entry_idx, initial_sl, "CE")
                pnl_rs = pnl_pts * LOT_SIZE
                exit_ts = ce_leg.loc[exit_i, "timestamp"]

                trades.append({
                    "date": date_str,
                    "entry_ts": ts,
                    "exit_ts": exit_ts,
                    "direction": "CE",
                    "strike": ce_strike,
                    "entry_price": entry_price,
                    "initial_sl": initial_sl,
                    "pnl_pts": round(pnl_pts, 2),
                    "pnl_rs": round(pnl_rs, 2),
                    "exit_reason": reason,
                    "spot": opening_spot,
                    "pe_decay_val": round(pe_decay_val, 2),
                })

                in_trade = True
                last_exit_ts = exit_ts
                last_exit_dir = "CE"
                last_exit_candle_ts = exit_ts

                # advance i past exit
                i = timestamps.index(exit_ts) + 1 if exit_ts in timestamps else i + 1
                in_trade = False
                continue

        # --- Check PE entry (momentum on PE, decay on CE) ---
        if not blocked("PE"):
            pe_mom  = pe_row["close"] >= pe_row["ema9_high"] + mom_gap
            ce_decay_val = ce_row["close"] - ce_row["ema9_low"]
            ce_decay = decay_lo <= ce_decay_val <= decay_hi

            if pe_mom and ce_decay:
                entry_idx = pe_leg[pe_leg["timestamp"] == ts].index[0]
                entry_price = pe_row["close"]
                initial_sl  = pe_row["ema9_low"]
                if initial_sl >= entry_price:
                    initial_sl = entry_price - 5.0

                pnl_pts, reason, exit_i = simulate_trade(pe_leg, entry_idx, initial_sl, "PE")
                pnl_rs = pnl_pts * LOT_SIZE
                exit_ts = pe_leg.loc[exit_i, "timestamp"]

                trades.append({
                    "date": date_str,
                    "entry_ts": ts,
                    "exit_ts": exit_ts,
                    "direction": "PE",
                    "strike": pe_strike,
                    "entry_price": entry_price,
                    "initial_sl": initial_sl,
                    "pnl_pts": round(pnl_pts, 2),
                    "pnl_rs": round(pnl_rs, 2),
                    "exit_reason": reason,
                    "spot": opening_spot,
                    "ce_decay_val": round(ce_decay_val, 2),
                })

                in_trade = True
                last_exit_ts = exit_ts
                last_exit_dir = "PE"
                last_exit_candle_ts = exit_ts

                i = timestamps.index(exit_ts) + 1 if exit_ts in timestamps else i + 1
                in_trade = False
                continue

        i += 1

    return trades


def summarise(trades, cfg_name):
    if not trades:
        print(f"\n[{cfg_name}] No trades fired.")
        return

    df = pd.DataFrame(trades)
    total = len(df)
    wins = (df["pnl_pts"] > 0).sum()
    losses = (df["pnl_pts"] <= 0).sum()
    acc = wins / total * 100
    avg_win  = df[df["pnl_pts"] > 0]["pnl_pts"].mean() if wins else 0
    avg_loss = df[df["pnl_pts"] <= 0]["pnl_pts"].mean() if losses else 0
    total_pnl_pts = df["pnl_pts"].sum()
    total_pnl_rs  = df["pnl_rs"].sum()
    rr = abs(avg_win / avg_loss) if avg_loss else 0

    print(f"\n{'='*55}")
    print(f"  Config: {cfg_name}")
    print(f"{'='*55}")
    print(f"  Days traded   : {df['date'].nunique()} / 23")
    print(f"  Total trades  : {total}  (W:{wins} L:{losses})")
    print(f"  Accuracy      : {acc:.1f}%")
    print(f"  Avg win       : +{avg_win:.1f} pts")
    print(f"  Avg loss      : {avg_loss:.1f} pts")
    print(f"  R:R           : {rr:.2f}")
    print(f"  Total P&L     : {total_pnl_pts:.1f} pts  |  ₹{total_pnl_rs:,.0f} (1 lot)")
    print(f"  Exit reasons  : {df['exit_reason'].value_counts().to_dict()}")
    print(f"  CE/PE split   : {df['direction'].value_counts().to_dict()}")
    print()

    # Per-day breakdown
    print(f"  {'Date':<12} {'Dir':<5} {'Entry':>7} {'SL':>7} {'P&L':>8} {'Reason'}")
    print(f"  {'-'*55}")
    for _, r in df.sort_values("entry_ts").iterrows():
        sign = "+" if r["pnl_pts"] >= 0 else ""
        print(f"  {r['date']:<12} {r['direction']:<5} {r['entry_price']:>7.1f} "
              f"{r['initial_sl']:>7.1f} {sign}{r['pnl_pts']:>7.1f} {r['exit_reason']}")


def main():
    files = sorted(glob.glob(f"{DATA_DIR}/nifty_option_1min_*.csv"))
    print(f"Loading {len(files)} days...")

    results = {k: [] for k in CONFIGS}

    for fp in files:
        date_str = os.path.basename(fp).replace("nifty_option_1min_", "").replace(".csv", "")
        df = load_day(fp)

        for cfg_name, cfg in CONFIGS.items():
            day_trades = run_day(df, cfg, date_str)
            results[cfg_name].extend(day_trades)

    for cfg_name in CONFIGS:
        summarise(results[cfg_name], cfg_name)

    # Save combined CSV
    all_rows = []
    for cfg_name, trades in results.items():
        for t in trades:
            t["config"] = cfg_name
            all_rows.append(t)
    if all_rows:
        out = pd.DataFrame(all_rows)
        out_path = "/home/vishalraajput24/VISHAL_RAJPUT/screener/split_atm_results.csv"
        out.to_csv(out_path, index=False)
        print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
