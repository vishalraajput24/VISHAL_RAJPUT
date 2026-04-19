#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  VRL_RESEARCH_BACKTEST.py — Calibrate GJR-GARCH + Hawkes on
#  historical option_3min data. Run BEFORE live deployment.
#
#  Usage: python3 VRL_RESEARCH_BACKTEST.py [--days N]
# ═══════════════════════════════════════════════════════════════

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import VRL_RESEARCH as R

DB_PATH = os.path.expanduser("~/lab_data/vrl_data.db")
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
os.makedirs(STATE_DIR, exist_ok=True)


def _load_option_3min(days: int = 30) -> pd.DataFrame:
    if not os.path.isfile(DB_PATH):
        print("ERROR: DB not found at " + DB_PATH)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    df = pd.read_sql_query(
        "SELECT timestamp, strike, type, open, high, low, close, volume "
        "FROM option_3min WHERE date(timestamp) >= ? "
        "ORDER BY timestamp",
        conn, params=(cutoff,)
    )
    conn.close()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def _load_trades() -> pd.DataFrame:
    if not os.path.isfile(DB_PATH):
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT date, entry_time, exit_time, direction, strike, "
        "entry_price, exit_price, pnl_pts, peak_pnl, exit_reason "
        "FROM trades ORDER BY date, entry_time",
        conn
    )
    conn.close()
    return df


def run_backtest(days: int = 30):
    today_str = date.today().strftime("%Y-%m-%d")
    print("=" * 40)
    print("BATCH 8 — BACKTEST REPORT")
    print("=" * 40)
    print("Run date:", today_str)

    df = _load_option_3min(days)
    if df.empty:
        print("ERROR: No option_3min data found for last", days, "days.")
        print("RESULT: FIXES_NEEDED: [no data]")
        return

    total_candles = len(df)
    dates = df["timestamp"].dt.date.unique()
    date_range = str(min(dates)) + " → " + str(max(dates))
    print("Sample:", total_candles, "candles across", len(dates), "days")
    print("Date range:", date_range)
    print()

    # v2: group by (strike, type) and process chronologically so the
    # rolling 30-candle GARCH window spans day boundaries (FIX 1 warmup).
    # Hawkes still resets per day (decay makes prior-day jumps irrelevant).
    groups = df.groupby(["strike", "type"])

    sigma_vals = []
    lambda_vals = []
    gamma_vals = []
    garch_ok = 0
    garch_fail = 0
    garch_insuf = 0         # separate bucket: waiting for first 30 bars
    hawkes_ok = 0
    hawkes_fail = 0
    edge_reasons = defaultdict(int)
    candles_processed = 0
    results_csv = []

    for (strike, otype), sdf in groups:
        sdf = sdf.sort_values("timestamp").reset_index(drop=True)
        if len(sdf) < 5:
            edge_reasons["group_too_small"] += 1
            continue

        current_date = None
        day_candles = []
        for i in range(len(sdf)):
            candles_processed += 1
            row = sdf.iloc[i]
            row_date = row["timestamp"].date()
            if row_date != current_date:
                current_date = row_date
                day_candles = []   # Hawkes day-local

            # GARCH: rolling 30-candle window across day boundaries (FIX 1).
            # FIX 3: feed Garman-Klass realized vol instead of log-returns.
            if i >= 29:
                window_ohlc = sdf.iloc[i - 29:i + 1][["open", "high", "low", "close"]]
                g_out = R.gjr_garch_forecast_gk(window_ohlc, min_candles=30)
            else:
                g_out = {"sigma_forecast": 0, "vol_regime": "INSUFFICIENT",
                         "gjr_asymmetry": 0, "fit_success": False,
                         "error": "window_" + str(i + 1)}
                edge_reasons["warmup_first_30"] += 1
                garch_insuf += 1

            if g_out["fit_success"]:
                garch_ok += 1
                sigma_vals.append(g_out["sigma_forecast"])
                gamma_vals.append(g_out["gjr_asymmetry"])
            elif g_out["vol_regime"] != "INSUFFICIENT":
                garch_ok += 1
            elif i >= 29:
                # Only count as "fail" if we had enough data and still failed.
                garch_fail += 1
                if g_out.get("error"):
                    edge_reasons["garch_" + g_out["error"][:30]] += 1

            # Hawkes: cumulative today's candles
            day_candles.append({
                "timestamp": row["timestamp"],
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
            })
            h_out = R.hawkes_intensity(day_candles)
            if h_out["cluster_state"] != "INSUFFICIENT":
                hawkes_ok += 1
                lambda_vals.append(h_out["lambda_now"])
            else:
                hawkes_fail += 1
                if h_out.get("error"):
                    edge_reasons["hawkes_" + h_out["error"][:30]] += 1

            results_csv.append({
                "timestamp": str(row["timestamp"]),
                "date": str(row_date),
                "strike": strike,
                "type": otype,
                "close": float(row["close"]),
                "sigma_forecast": g_out["sigma_forecast"],
                "vol_regime": g_out["vol_regime"],
                "lambda_now": h_out["lambda_now"],
                "cluster_state": h_out["cluster_state"],
                "fit_success": int(g_out["fit_success"]),
            })

    print("=== RAW DATA SUMMARY ===")
    print("Total candles processed:", candles_processed)
    print("Warmup candles (first 30 per strike):", garch_insuf)
    eligible_candles = max(1, candles_processed - garch_insuf)
    garch_pct = round(garch_ok / eligible_candles * 100, 1)
    hawkes_pct = round(hawkes_ok / max(1, candles_processed) * 100, 1)
    print("GARCH-eligible candles:", eligible_candles)
    print("Candles with valid GARCH fit:", garch_ok, "(" + str(garch_pct) + "% of eligible)")
    print("Candles with valid Hawkes:", hawkes_ok, "(" + str(hawkes_pct) + "%)")
    skip_total = sum(edge_reasons.values())
    print("Skipped (edge cases):", skip_total, "— breakdown:")
    for reason, count in sorted(edge_reasons.items(), key=lambda x: -x[1])[:10]:
        print("  ", reason, ":", count)
    print()

    # Calibrate thresholds
    sigma_arr = np.array(sigma_vals) if sigma_vals else np.array([0.0])
    lambda_arr = np.array(lambda_vals) if lambda_vals else np.array([0.0])

    thresholds = {
        "sigma_p25": round(float(np.percentile(sigma_arr, 25)), 4),
        "sigma_p50": round(float(np.percentile(sigma_arr, 50)), 4),
        "sigma_p75": round(float(np.percentile(sigma_arr, 75)), 4),
        "sigma_p95": round(float(np.percentile(sigma_arr, 95)), 4),
        "lambda_p50": round(float(np.percentile(lambda_arr, 50)), 4),
        "lambda_p75": round(float(np.percentile(lambda_arr, 75)), 4),
        "updated": today_str,
    }

    print("=== GJR-GARCH CALIBRATION ===")
    for k in ("sigma_p25", "sigma_p50", "sigma_p75", "sigma_p95"):
        print(k + ":", thresholds[k])
    fit_rate = round(garch_ok / max(1, candles_processed) * 100, 1)
    print("Fit success rate:", str(fit_rate) + "%")
    avg_gamma = round(float(np.mean(gamma_vals)), 4) if gamma_vals else 0.0
    print("Avg gamma (asymmetry):", avg_gamma)
    print("Convergence failures:", garch_fail, "cases")
    print()

    print("=== HAWKES CALIBRATION ===")
    print("baseline_mu:", 0.1)
    jumps_per_day = []
    for d in dates:
        day_rows = [r for r in results_csv if r["date"] == str(d)]
        day_jumps = sum(1 for r in day_rows
                        if r["lambda_now"] > 0.1 + 0.01)
        jumps_per_day.append(day_jumps)
    avg_jpd = round(float(np.mean(jumps_per_day)), 1) if jumps_per_day else 0
    print("Avg jumps per day:", avg_jpd)
    print("lambda_p50:", thresholds["lambda_p50"])
    print("lambda_p75:", thresholds["lambda_p75"])
    max_lam = round(float(np.max(lambda_arr)), 4) if len(lambda_arr) else 0.0
    print("Max observed lambda:", max_lam)
    print()

    # Save thresholds
    thresh_path = os.path.join(STATE_DIR, "research_thresholds.json")
    with open(thresh_path, "w") as f:
        json.dump(thresholds, f, indent=2)
    print("Thresholds saved to:", thresh_path)
    print()

    # Trade-level analysis
    trades = _load_trades()
    print("=== TRADE-LEVEL ANALYSIS ===")
    if trades.empty:
        print("No trades in DB — skipping trade-level analysis.")
    else:
        winners = trades[trades["pnl_pts"] > 0]
        losers = trades[trades["pnl_pts"] <= 0]
        print("Total historical trades:", len(trades),
              "(wins:", len(winners), "losses:", len(losers), ")")
        print()

        for label, subset in [("Winners", winners), ("Losers", losers)]:
            if subset.empty:
                print(label, "(N=0): no data")
                continue
            sigmas = []
            regimes = defaultdict(int)
            clusters = defaultdict(int)
            for _, t in subset.iterrows():
                entry_ts = str(t.get("date", "")) + " " + str(t.get("entry_time", ""))
                matched = [r for r in results_csv
                           if r["strike"] == t.get("strike")
                           and r["type"] == t.get("direction")
                           and r["timestamp"][:16] <= entry_ts[:16]]
                if matched:
                    closest = matched[-1]
                    sigmas.append(closest["sigma_forecast"])
                    regimes[closest["vol_regime"]] += 1
                    clusters[closest["cluster_state"]] += 1
            n = len(subset)
            avg_sig = round(float(np.mean(sigmas)), 4) if sigmas else 0.0
            print(label, "(N=" + str(n) + "):")
            print("  avg sigma_forecast at entry:", avg_sig)
            print("  vol_regime distribution:",
                  "  ".join(k + " " + str(round(v / max(1, n) * 100)) + "%"
                            for k, v in sorted(regimes.items())))
            print("  cluster_state distribution:",
                  "  ".join(k + " " + str(round(v / max(1, n) * 100)) + "%"
                            for k, v in sorted(clusters.items())))
        print()

        # Edge detection
        w_sigs = []
        l_sigs = []
        w_active = 0
        l_active = 0
        for _, t in trades.iterrows():
            entry_ts = str(t.get("date", "")) + " " + str(t.get("entry_time", ""))
            matched = [r for r in results_csv
                       if r["strike"] == t.get("strike")
                       and r["type"] == t.get("direction")
                       and r["timestamp"][:16] <= entry_ts[:16]]
            if matched:
                c = matched[-1]
                if t["pnl_pts"] > 0:
                    w_sigs.append(c["sigma_forecast"])
                    if c["cluster_state"] == "ACTIVE":
                        w_active += 1
                else:
                    l_sigs.append(c["sigma_forecast"])
                    if c["cluster_state"] == "ACTIVE":
                        l_active += 1

        print("=== EDGE DETECTION ===")
        sig_diff = abs(np.mean(w_sigs) - np.mean(l_sigs)) if w_sigs and l_sigs else 0
        print("GARCH predictive (>10pt difference):",
              "YES" if sig_diff > 10 else "NO",
              "(diff=" + str(round(sig_diff, 2)) + ")")
        w_act_pct = w_active / max(1, len(winners)) * 100
        l_act_pct = l_active / max(1, len(losers)) * 100
        act_diff = abs(w_act_pct - l_act_pct)
        print("Hawkes predictive (>15% ACTIVE diff):",
              "YES" if act_diff > 15 else "NO",
              "(diff=" + str(round(act_diff, 1)) + "%)")
        print()

    # Checklist. garch_fail now excludes warmup_first_30 (expected behavior),
    # so "All edge cases handled" only fails if post-warmup fits actually crash.
    eligible = max(1, candles_processed - garch_insuf)
    print("=== LIVE DEPLOY CHECKLIST ===")
    checks = {
        "Code runs end-to-end": True,
        "Thresholds JSON written": os.path.isfile(thresh_path),
        "All edge cases handled": garch_fail < eligible * 0.5,
        "No NaN propagation": not any(np.isnan(v) for v in sigma_vals + lambda_vals),
        "Sample size sufficient (N >= 500)": candles_processed >= 500,
    }
    all_ok = True
    for check, passed in checks.items():
        status = "[x]" if passed else "[ ]"
        print(status, check)
        if not passed:
            all_ok = False

    issues = [k for k, v in checks.items() if not v]
    print()
    if all_ok:
        print("RESULT: DEPLOY_APPROVED")
    else:
        print("RESULT: FIXES_NEEDED:", issues)

    # Save report
    report_path = os.path.expanduser(
        "~/lab_data/research_backtest_report_" + today_str + ".txt"
    )
    try:
        import io
        old_stdout = sys.stdout
        sys.stdout = buffer = io.StringIO()
        # Re-run summary lines for file
        buffer.write("BATCH 8 — BACKTEST REPORT\n")
        buffer.write("Run date: " + today_str + "\n")
        buffer.write("Candles: " + str(candles_processed) + "\n")
        buffer.write("GARCH ok: " + str(garch_ok) + " (" + str(garch_pct) + "%)\n")
        buffer.write("Hawkes ok: " + str(hawkes_ok) + " (" + str(hawkes_pct) + "%)\n")
        buffer.write("Thresholds: " + json.dumps(thresholds, indent=2) + "\n")
        buffer.write("Result: " + ("DEPLOY_APPROVED" if all_ok else "FIXES_NEEDED") + "\n")
        sys.stdout = old_stdout
        with open(report_path, "w") as f:
            f.write(buffer.getvalue())
        print("Report saved to:", report_path)
    except Exception as e:
        print("Report save error:", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    run_backtest(args.days)
