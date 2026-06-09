"""
orion_news_filter_backtest.py
─────────────────────────────
ORION Strategy — News/Earnings Filter Backtest
Measures how dangerous the ±30 min window around earnings is at STOCK level.

For each stock in FNO_UNIVERSE:
  1. Fetch earnings/result announcement dates from NSE corporate calendar
  2. Pull intraday (5-min) candles from Kite for those dates
  3. Measure the max price swing in the ±6 candles (30 min) around announcement
  4. Compare to average swing on non-event days (same time window, same stocks)

Output:
  screener/orion_news_filter_results.csv   — per-stock per-event detail
  screener/orion_news_filter_summary.csv   — per-stock accuracy summary
  screener/orion_news_filter_report.md     — human-readable report

Usage:
  python3 orion_news_filter_backtest.py
  python3 orion_news_filter_backtest.py --quick   # first 20 stocks only

Requires active Kite access token (same as fno_collector).
"""

import os, sys, json, time, warnings, argparse
import urllib.request, urllib.parse
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta

warnings.filterwarnings("ignore")

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(dotenv_path=None, **_kw):
        _p = os.path.expanduser(dotenv_path or "~/.env")
        if not os.path.isfile(_p): return False
        with open(_p) as _fh:
            for _ln in _fh:
                _ln = _ln.strip()
                if not _ln or _ln.startswith("#") or "=" not in _ln: continue
                _k, _, _v = _ln.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
        return True

from kiteconnect import KiteConnect

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(os.path.expanduser("~"), "VISHAL_RAJPUT", "state", "access_token.json")
INST_CACHE = os.path.join(BASE_DIR, "inst_cache_nse.csv")

RESULTS_FILE = os.path.join(BASE_DIR, "orion_news_filter_results.csv")
SUMMARY_FILE = os.path.join(BASE_DIR, "orion_news_filter_summary.csv")
REPORT_FILE  = os.path.join(BASE_DIR, "orion_news_filter_report.md")

# Dangerous move threshold: if stock moves ≥ this % in ±30 min → event is "dangerous"
DANGER_THRESHOLD_PCT = 1.5

# How many candles (5-min) either side of announcement = 30 min
WINDOW_CANDLES = 6

# Lookback for intraday data — Kite allows ~60 days of 5-min data
INTRADAY_LOOKBACK_DAYS = 60

# Lookback for NSE announcements (quarters)
ANNOUNCEMENT_LOOKBACK_DAYS = 400   # ~5 quarters

# ─────────────────────────────────────────────────────────────────────────────

load_dotenv(os.path.expanduser("~/.env"))
API_KEY = os.getenv("KITE_API_KEY", "")


def get_kite():
    if not API_KEY:
        print("KITE_API_KEY not found in ~/.env"); sys.exit(1)
    kite = KiteConnect(api_key=API_KEY)
    with open(TOKEN_FILE) as f:
        saved = json.load(f)
    if saved.get("date") != date.today().isoformat():
        print("Kite token expired — run fno_collector.py --morning first"); sys.exit(1)
    kite.set_access_token(saved["access_token"])
    kite.profile()
    return kite


def load_nse_instruments():
    today = date.today().isoformat()
    if os.path.exists(INST_CACHE):
        tmp = pd.read_csv(INST_CACHE, nrows=1)
        if "_date" in tmp.columns and str(tmp["_date"].iloc[0]) == today:
            return pd.read_csv(INST_CACHE)
    print("  Instrument cache miss — loading from Kite...")
    kite = get_kite()
    df = pd.DataFrame(kite.instruments("NSE"))
    df["_date"] = today
    df.to_csv(INST_CACHE, index=False)
    return df


def get_token(nse_df, symbol):
    row = nse_df[(nse_df["tradingsymbol"] == symbol) & (nse_df["instrument_type"] == "EQ")]
    if row.empty:
        return None
    return int(row.iloc[0]["instrument_token"])


# ─────────────────────────────────────────────────────────────────────────────
# NSE Corporate Announcements  (board meetings / quarterly results)
# ─────────────────────────────────────────────────────────────────────────────

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

# Session cookie — NSE blocks direct API calls without a prior GET to the homepage
_nse_session_cookies = {}

def _init_nse_session():
    """Prime NSE cookies with a homepage hit."""
    global _nse_session_cookies
    try:
        req = urllib.request.Request("https://www.nseindia.com", headers=NSE_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.headers.get("Set-Cookie", "")
            for part in raw.split(";"):
                part = part.strip()
                if "=" in part:
                    k, _, v = part.partition("=")
                    _nse_session_cookies[k.strip()] = v.strip()
    except Exception:
        pass


def fetch_nse_announcements(symbol: str) -> list[dict]:
    """
    Returns list of dicts: {"date": date, "time": "HH:MM", "subject": "..."}
    for quarterly result/board meeting announcements.
    """
    global _nse_session_cookies
    if not _nse_session_cookies:
        _init_nse_session()

    url = (
        f"https://www.nseindia.com/api/corporate-announcements"
        f"?index=equities&symbol={urllib.parse.quote(symbol)}"
    )
    cookies_str = "; ".join(f"{k}={v}" for k, v in _nse_session_cookies.items())
    headers = {**NSE_HEADERS, "Cookie": cookies_str}

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return []

    cutoff = date.today() - timedelta(days=ANNOUNCEMENT_LOOKBACK_DAYS)
    result_keywords = [
        "financial result", "quarterly result", "board meeting", "outcome of board",
        "unaudited result", "audited result", "q1", "q2", "q3", "q4",
        "half year", "annual result",
    ]
    events = []
    for item in data:
        subject = (item.get("desc") or item.get("subject") or "").lower()
        if not any(kw in subject for kw in result_keywords):
            continue
        ann_str = item.get("an_dt") or ""
        if not ann_str:
            continue
        # Format: "18-Apr-2026 15:08:52"
        try:
            ann_dt = datetime.strptime(ann_str[:20].strip(), "%d-%b-%Y %H:%M:%S")
        except ValueError:
            try:
                ann_dt = datetime.strptime(ann_str[:10], "%Y-%m-%d")
            except ValueError:
                continue
        if ann_dt.date() < cutoff:
            continue
        events.append({
            "date": ann_dt.date(),
            "time": ann_dt.strftime("%H:%M"),
            "subject": item.get("desc", ""),
        })

    # Keep earliest announcement per day (first release wins)
    by_day: dict = {}
    for e in sorted(events, key=lambda x: (x["date"], x["time"])):
        if e["date"] not in by_day:
            by_day[e["date"]] = e
    return sorted(by_day.values(), key=lambda x: x["date"])


# ─────────────────────────────────────────────────────────────────────────────
# Kite intraday data
# ─────────────────────────────────────────────────────────────────────────────

def fetch_5min_candles(kite, token, target_date: date) -> pd.DataFrame | None:
    """5-min candles for the full trading session on target_date."""
    from_dt = datetime(target_date.year, target_date.month, target_date.day, 9, 0, 0)
    to_dt   = datetime(target_date.year, target_date.month, target_date.day, 15, 35, 0)
    try:
        candles = kite.historical_data(token, from_dt, to_dt, "5minute", continuous=False)
        if not candles:
            return None
        df = pd.DataFrame(candles)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        return df
    except Exception:
        return None


def compute_swing_pct(df: pd.DataFrame, around_time: str = "15:30") -> float | None:
    """
    Max % swing (high-low / open) within ±WINDOW_CANDLES 5-min candles
    around `around_time` (HH:MM).
    Returns None if not enough data.
    """
    if df is None or df.empty:
        return None
    h, m = map(int, around_time.split(":"))
    center = df.index[0].replace(hour=h, minute=m, second=0)
    window = pd.Timedelta(minutes=WINDOW_CANDLES * 5)
    mask = (df.index >= center - window) & (df.index <= center + window)
    sub = df[mask]
    if sub.empty or len(sub) < 2:
        return None
    ref = float(sub["open"].iloc[0])
    if ref <= 0:
        return None
    swing = (sub["high"].max() - sub["low"].min()) / ref * 100
    return round(swing, 3)


def compute_daily_avg_swing(df: pd.DataFrame) -> float | None:
    """
    Average 5-min bar range (high-low / open * 100) across the full session.
    Proxy for "normal intraday volatility" on that day.
    """
    if df is None or df.empty:
        return None
    bar_swings = (df["high"] - df["low"]) / df["open"].replace(0, np.nan) * 100
    return round(float(bar_swings.mean()), 3)


# ─────────────────────────────────────────────────────────────────────────────
# Core backtest logic
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(symbols: list[str], kite, nse_df) -> tuple[pd.DataFrame, pd.DataFrame]:
    all_events = []    # per-event detail rows
    summary    = []    # per-stock summary rows

    intraday_cutoff = date.today() - timedelta(days=INTRADAY_LOOKBACK_DAYS)

    total = len(symbols)
    for idx, sym in enumerate(symbols, 1):
        print(f"\n[{idx:3}/{total}] {sym}", flush=True)

        token = get_token(nse_df, sym)
        if token is None:
            print(f"        SKIP — not in NSE instruments")
            continue

        # 1. Fetch NSE announcements
        events = fetch_nse_announcements(sym)
        time.sleep(0.3)   # be polite to NSE
        print(f"        Announcements found: {len(events)}")

        if not events:
            summary.append({
                "symbol": sym, "events_found": 0,
                "events_with_intraday": 0, "events_with_data": 0, "dangerous_events": 0,
                "filter_accuracy_pct": None,
                "avg_event_swing_pct": None, "avg_normal_swing_pct": None,
                "swing_ratio": None, "note": "no_announcements"
            })
            continue

        event_swings   = []
        normal_swings  = []
        event_rows     = []

        for ev in events:
            ev_date = ev["date"]
            if ev_date.weekday() >= 5:   # skip weekends (announcement on weekend → skip)
                continue

            # Use actual announcement time; if after 15:30 (post-market) the move shows
            # as a next-day open gap instead.
            ann_time = ev.get("time", "15:00")
            is_post_market = ann_time >= "15:30"
            # Clamp intra-session window: min 09:35, max 15:25
            if ann_time < "09:35":
                ann_time = "09:35"
            elif ann_time > "15:25":
                ann_time = "15:00"   # use pre-close window for post-market releases

            # Intraday analysis only possible within Kite's 60-day window
            if ev_date < intraday_cutoff:
                # Only have daily data — measure open gap vs prev close
                ev_row = {
                    "symbol": sym, "event_date": ev_date.isoformat(),
                    "ann_time": ev.get("time", ""),
                    "subject": ev["subject"][:80],
                    "data_type": "daily_only",
                    "swing_pct": None, "normal_avg_pct": None,
                    "is_dangerous": None, "gap_pct": None
                }
                # Try to get daily close
                try:
                    from_d = datetime(ev_date.year, ev_date.month, ev_date.day) - timedelta(days=5)
                    to_d   = datetime(ev_date.year, ev_date.month, ev_date.day) + timedelta(days=3)
                    candles = kite.historical_data(token, from_d, to_d, "day", continuous=False)
                    time.sleep(0.2)
                    if candles and len(candles) >= 2:
                        df_d = pd.DataFrame(candles)
                        df_d["date"] = pd.to_datetime(df_d["date"]).dt.date
                        df_d = df_d.set_index("date").sort_index()
                        # Find event day and next day
                        dates = list(df_d.index)
                        if ev_date in dates:
                            loc = dates.index(ev_date)
                            if loc + 1 < len(dates):
                                next_open  = float(df_d.iloc[loc + 1]["open"])
                                ev_close   = float(df_d.iloc[loc]["close"])
                                if ev_close > 0:
                                    gap = (next_open - ev_close) / ev_close * 100
                                    ev_row["gap_pct"] = round(gap, 3)
                                    ev_row["is_dangerous"] = abs(gap) >= DANGER_THRESHOLD_PCT
                except Exception:
                    pass
                event_rows.append(ev_row)
                continue

            # Intraday window available
            df5 = fetch_5min_candles(kite, token, ev_date)
            time.sleep(0.3)

            swing = compute_swing_pct(df5, around_time=ann_time)
            norm  = compute_daily_avg_swing(df5)

            is_dangerous = (swing is not None) and (swing >= DANGER_THRESHOLD_PCT)

            if swing is not None:
                event_swings.append(swing)
            if norm is not None:
                normal_swings.append(norm)

            ev_row = {
                "symbol": sym, "event_date": ev_date.isoformat(),
                "ann_time": ev.get("time", ""),
                "subject": ev["subject"][:80],
                "data_type": "intraday_5min",
                "swing_pct": swing, "normal_avg_pct": norm,
                "is_dangerous": is_dangerous, "gap_pct": None
            }

            # Also measure next-day open gap (common for post-market results)
            next_bday = ev_date + timedelta(days=1)
            while next_bday.weekday() >= 5:
                next_bday += timedelta(days=1)
            if next_bday <= date.today():
                try:
                    from_d = datetime(ev_date.year, ev_date.month, ev_date.day)
                    to_d   = datetime(next_bday.year, next_bday.month, next_bday.day, 15, 35)
                    cd = kite.historical_data(token, from_d, to_d, "day", continuous=False)
                    time.sleep(0.2)
                    if cd and len(cd) >= 2:
                        ev_close  = float(cd[-2]["close"])
                        next_open = float(cd[-1]["open"])
                        if ev_close > 0:
                            gap = (next_open - ev_close) / ev_close * 100
                            ev_row["gap_pct"] = round(gap, 3)
                            if abs(gap) >= DANGER_THRESHOLD_PCT:
                                ev_row["is_dangerous"] = True
                except Exception:
                    pass

            event_rows.append(ev_row)

        all_events.extend(event_rows)

        # Build per-stock summary
        intraday_rows = [r for r in event_rows if r["data_type"] == "intraday_5min" and r["swing_pct"] is not None]
        all_rows_with_data = [r for r in event_rows if r["is_dangerous"] is not None]
        dangerous = [r for r in all_rows_with_data if r["is_dangerous"]]

        accuracy = None
        if all_rows_with_data:
            accuracy = round(len(dangerous) / len(all_rows_with_data) * 100, 1)

        avg_ev_swing = round(np.mean([r["swing_pct"] for r in intraday_rows]), 3) if intraday_rows else None
        avg_norm     = round(np.mean([r["normal_avg_pct"] for r in intraday_rows if r["normal_avg_pct"] is not None]), 3) if intraday_rows else None
        ratio        = round(avg_ev_swing / avg_norm, 2) if (avg_ev_swing and avg_norm and avg_norm > 0) else None

        print(f"        Events measured: {len(all_rows_with_data)} | Dangerous: {len(dangerous)} | Accuracy: {accuracy}%")

        summary.append({
            "symbol": sym,
            "events_found": len(events),
            "events_with_intraday": len(intraday_rows),
            "events_with_data": len(all_rows_with_data),
            "dangerous_events": len(dangerous),
            "filter_accuracy_pct": accuracy,
            "avg_event_swing_pct": avg_ev_swing,
            "avg_normal_swing_pct": avg_norm,
            "swing_ratio": ratio,
            "note": ""
        })

    events_df  = pd.DataFrame(all_events) if all_events else pd.DataFrame()
    summary_df = pd.DataFrame(summary) if summary else pd.DataFrame()
    return events_df, summary_df


# ─────────────────────────────────────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_report(events_df: pd.DataFrame, summary_df: pd.DataFrame) -> str:
    lines = []
    lines.append("# ORION — News Filter Backtest Report")
    lines.append(f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Danger threshold: ≥{DANGER_THRESHOLD_PCT}% swing in ±30 min window or next-day gap\n")

    if summary_df.empty:
        lines.append("No data collected.")
        return "\n".join(lines)

    valid = summary_df[summary_df["filter_accuracy_pct"].notna()]
    overall_acc = None
    if not valid.empty:
        total_ev   = valid["events_with_data"].sum()
        total_dang = valid["dangerous_events"].sum()
        overall_acc = round(total_dang / total_ev * 100, 1) if total_ev > 0 else None

    lines.append(f"## Overall Filter Accuracy: **{overall_acc}%**")
    lines.append(f"*(% of earnings events where stock moved ≥{DANGER_THRESHOLD_PCT}% in the ±30 min window — these are the trades the filter would skip)*\n")

    lines.append("## Summary by Stock\n")
    lines.append("| Stock | Events | Dangerous | Accuracy% | Avg Event Swing | Avg Normal | Ratio |")
    lines.append("|-------|--------|-----------|-----------|-----------------|------------|-------|")
    for _, row in summary_df.sort_values("filter_accuracy_pct", ascending=False, na_position="last").iterrows():
        acc = f"{row['filter_accuracy_pct']:.1f}%" if pd.notna(row["filter_accuracy_pct"]) else "—"
        ev_sw = f"{row['avg_event_swing_pct']:.2f}%" if pd.notna(row["avg_event_swing_pct"]) else "—"
        n_sw  = f"{row['avg_normal_swing_pct']:.2f}%" if pd.notna(row["avg_normal_swing_pct"]) else "—"
        ratio = f"{row['swing_ratio']:.1f}x" if pd.notna(row["swing_ratio"]) else "—"
        lines.append(
            f"| {row['symbol']:<14} | {int(row['events_with_data']) if pd.notna(row['events_with_data']) else 0:6} "
            f"| {int(row['dangerous_events']) if pd.notna(row['dangerous_events']) else 0:9} "
            f"| {acc:9} | {ev_sw:15} | {n_sw:10} | {ratio:5} |"
        )

    lines.append("\n## High-Risk Stocks (accuracy ≥ 60%)")
    high_risk = valid[valid["filter_accuracy_pct"] >= 60].sort_values("filter_accuracy_pct", ascending=False)
    if high_risk.empty:
        lines.append("None above threshold.")
    else:
        for _, row in high_risk.iterrows():
            lines.append(f"- **{row['symbol']}**: {row['filter_accuracy_pct']:.1f}% dangerous events ({row['dangerous_events']}/{row['events_with_data']})")

    lines.append("\n## Stocks Relatively Safe Around Results (accuracy < 30%)")
    low_risk = valid[valid["filter_accuracy_pct"] < 30].sort_values("filter_accuracy_pct")
    if low_risk.empty:
        lines.append("None below threshold.")
    else:
        for _, row in low_risk.iterrows():
            lines.append(f"- **{row['symbol']}**: {row['filter_accuracy_pct']:.1f}% ({row['dangerous_events']}/{row['events_with_data']})")

    if not events_df.empty and "gap_pct" in events_df.columns:
        gap_rows = events_df[events_df["gap_pct"].notna()]
        if not gap_rows.empty:
            avg_gap = gap_rows["gap_pct"].abs().mean()
            big_gaps = (gap_rows["gap_pct"].abs() >= DANGER_THRESHOLD_PCT).sum()
            lines.append(f"\n## Next-Day Gap Analysis")
            lines.append(f"- Events with gap data: {len(gap_rows)}")
            lines.append(f"- Avg absolute gap: {avg_gap:.2f}%")
            lines.append(f"- Events with gap ≥ {DANGER_THRESHOLD_PCT}%: {big_gaps} ({big_gaps/len(gap_rows)*100:.1f}%)")

    lines.append("\n---")
    lines.append(f"Universe: {len(summary_df)} stocks · Threshold: {DANGER_THRESHOLD_PCT}% · Window: ±30 min")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ORION News Filter Backtest")
    parser.add_argument("--quick", action="store_true", help="First 20 stocks only")
    args = parser.parse_args()

    sys.path.insert(0, BASE_DIR)
    from vishal_fno_screener import FNO_UNIVERSE
    symbols = FNO_UNIVERSE[:20] if args.quick else FNO_UNIVERSE

    print("=" * 60)
    print(f"  ORION News Filter Backtest — {date.today()}")
    print(f"  Universe: {len(symbols)} stocks")
    print(f"  Danger threshold: ≥{DANGER_THRESHOLD_PCT}% in ±30 min")
    print(f"  Intraday lookback: {INTRADAY_LOOKBACK_DAYS} days")
    print("=" * 60)

    kite   = get_kite()
    nse_df = load_nse_instruments()

    print(f"\n  Loaded {len(nse_df)} NSE instruments")
    print(f"  Fetching NSE announcements + Kite intraday...\n")

    events_df, summary_df = run_backtest(symbols, kite, nse_df)

    # Save outputs
    if not events_df.empty:
        events_df.to_csv(RESULTS_FILE, index=False)
        print(f"\n  Events saved  → {RESULTS_FILE}")

    if not summary_df.empty:
        summary_df.to_csv(SUMMARY_FILE, index=False)
        print(f"  Summary saved → {SUMMARY_FILE}")

    report = generate_report(events_df, summary_df)
    with open(REPORT_FILE, "w") as f:
        f.write(report)
    print(f"  Report saved  → {REPORT_FILE}")

    # Print the headline number
    print("\n" + "=" * 60)
    valid = summary_df[summary_df["filter_accuracy_pct"].notna()] if not summary_df.empty else pd.DataFrame()
    if not valid.empty:
        total_ev   = valid["events_with_data"].sum()
        total_dang = valid["dangerous_events"].sum()
        overall    = round(total_dang / total_ev * 100, 1) if total_ev > 0 else 0
        print(f"  OVERALL FILTER ACCURACY : {overall}%")
        print(f"  Total events measured   : {int(total_ev)}")
        print(f"  Dangerous events caught : {int(total_dang)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
