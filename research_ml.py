# ═══════════════════════════════════════════════════════════════
#  research_ml.py — VISHAL RAJPUT TRADE v12.16
#  ML research: Train decision tree + gradient boosting on scan data.
#  Output win probability for each entry configuration.
#  Run daily after market close to retrain on latest data.
#
#  Phase 1: Research only — DO NOT integrate with bot yet.
#  Crontab: 40 15 * * 1-5 cd ~/VISHAL_RAJPUT && python3 research_ml.py >> ~/logs/ml.log 2>&1
# ═══════════════════════════════════════════════════════════════

import pandas as pd
import numpy as np
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score
import glob, json, os, pickle


# ═══════════════════════════════════════════════════════════════
#  STEP 1: LOAD ALL SCAN DATA
# ═══════════════════════════════════════════════════════════════

print("=" * 60)
print("  LOADING SCAN DATA")
print("=" * 60)

scan_files = sorted(glob.glob(os.path.expanduser(
    "~/lab_data/options_1min/nifty_signal_scan_*.csv")))
dfs = []
for f in scan_files:
    try:
        df = pd.read_csv(f, on_bad_lines="skip")
        dfs.append(df)
    except Exception as e:
        print(f"  Skip {f}: {e}")

if not dfs:
    print("  No scan data found. Exiting.")
    exit(0)

all_scans = pd.concat(dfs, ignore_index=True)

# Only rows with forward fill data
all_scans = all_scans[all_scans["fwd_5c"].notna() &
                       (all_scans["fwd_5c"] != "")]
all_scans["fwd_5c"] = pd.to_numeric(all_scans["fwd_5c"], errors="coerce")
all_scans["entry_price"] = pd.to_numeric(all_scans["entry_price"], errors="coerce")
all_scans = all_scans.dropna(subset=["fwd_5c", "entry_price"])
all_scans = all_scans[all_scans["entry_price"] > 0]

print(f"  Scan files: {len(scan_files)}")
print(f"  Total samples with forward fill: {len(all_scans)}")

if len(all_scans) < 100:
    print("  Not enough data (need 100+). Exiting.")
    exit(0)


# ═══════════════════════════════════════════════════════════════
#  STEP 2: CREATE FEATURES AND LABELS
# ═══════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  FEATURE ENGINEERING")
print("=" * 60)

# Label: use fwd_outcome if available, else PNL-based
# WIN = max move >= 10pts in 3/5/10 candles
# Also compute PNL at each forward point
all_scans["pnl_3c"] = pd.to_numeric(all_scans.get("fwd_3c", 0), errors="coerce").fillna(0) - all_scans["entry_price"]
all_scans["pnl_5c"] = all_scans["fwd_5c"] - all_scans["entry_price"]
all_scans["fwd_10c_num"] = pd.to_numeric(all_scans.get("fwd_10c", 0), errors="coerce").fillna(0)
all_scans["pnl_10c"] = all_scans["fwd_10c_num"] - all_scans["entry_price"]

# Win = gained at least 5pts by candle 10 (realistic for options)
all_scans["best_pnl"] = all_scans[["pnl_3c", "pnl_5c", "pnl_10c"]].max(axis=1)
all_scans["win"] = (all_scans["best_pnl"] >= 5).astype(int)

# Features — all numeric columns from scan log
feature_cols = [
    "rsi_1m", "body_pct_1m", "vol_ratio_1m", "spread_1m",
    "rsi_3m", "body_pct_3m", "ema_spread_3m", "conditions_3m",
    "iv_pct", "delta", "vix",
    "spot_rsi_3m", "spot_ema_spread_3m",
    "hourly_rsi", "fib_distance"
]

# Encode categorical features
all_scans["direction_code"] = (all_scans["direction"] == "CE").astype(int)
all_scans["session_code"] = all_scans["session"].map(
    {"OPEN": 0, "MORNING": 1, "MID": 2, "LATE": 3}).fillna(1)
all_scans["regime_code"] = all_scans.get("spot_regime", pd.Series(dtype=str)).map(
    {"CHOPPY": 0, "NEUTRAL": 1, "TRENDING": 2,
     "TRENDING_STRONG": 3, "UNKNOWN": 0}).fillna(0)
all_scans["bias_code"] = all_scans.get("bias", pd.Series(dtype=str)).map(
    {"BEAR": -1, "NEUTRAL": 0, "BULL": 1,
     "SIDEWAYS": 0, "UNKNOWN": 0}).fillna(0)

# Extract time features
all_scans["hour"] = pd.to_datetime(
    all_scans["timestamp"]).dt.hour
all_scans["minute"] = pd.to_datetime(
    all_scans["timestamp"]).dt.minute
all_scans["time_mins"] = all_scans["hour"] * 60 + all_scans["minute"]

# DTE
all_scans["dte"] = pd.to_numeric(all_scans["dte"], errors="coerce").fillna(5)

# Premium bucket
all_scans["premium_bucket"] = pd.cut(
    all_scans["entry_price"],
    bins=[0, 50, 100, 150, 200, 300, 500],
    labels=[0, 1, 2, 3, 4, 5]).astype(float).fillna(2)

# RSI relative to 3m (alignment check)
all_scans["rsi_gap"] = all_scans["rsi_1m"] - all_scans["rsi_3m"]

# All features
all_features = feature_cols + [
    "direction_code", "session_code", "regime_code",
    "bias_code", "hour", "time_mins", "dte",
    "premium_bucket", "rsi_gap"
]

# Clean: fill NaN with 0, convert to numeric
for col in all_features:
    all_scans[col] = pd.to_numeric(all_scans[col], errors="coerce").fillna(0)

X = all_scans[all_features]
y = all_scans["win"]

print(f"  Features: {len(all_features)}")
print(f"  Samples: {len(X)}")
print(f"  Win rate: {y.mean()*100:.1f}%")


# ═══════════════════════════════════════════════════════════════
#  STEP 3: TRAIN DECISION TREE + GRADIENT BOOSTING
# ═══════════════════════════════════════════════════════════════

# Time-series split — never train on future, test on past
tscv = TimeSeriesSplit(n_splits=5)

# Decision Tree — interpretable, shows exact rules
dt = DecisionTreeClassifier(
    max_depth=5,
    min_samples_leaf=20,
    min_samples_split=40,
    class_weight="balanced"
)

# Gradient Boosting — more accurate, less interpretable
gb = GradientBoostingClassifier(
    n_estimators=100,
    max_depth=3,
    min_samples_leaf=15,
    learning_rate=0.1,
    subsample=0.8
)

print("\n" + "=" * 60)
print("  WALK-FORWARD VALIDATION")
print("=" * 60)

for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
    X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
    y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

    dt.fit(X_train, y_train)
    gb.fit(X_train, y_train)

    dt_acc = accuracy_score(y_test, dt.predict(X_test))
    gb_acc = accuracy_score(y_test, gb.predict(X_test))

    dt_proba = dt.predict_proba(X_test)[:, 1]
    gb_proba = gb.predict_proba(X_test)[:, 1]

    # Profit simulation: only trade when probability > 70%
    test_data = all_scans.iloc[test_idx]
    high_conf_dt = dt_proba > 0.70
    high_conf_gb = gb_proba > 0.70

    dt_pnl = test_data.iloc[high_conf_dt]["pnl_5c"].sum() if high_conf_dt.any() else 0
    gb_pnl = test_data.iloc[high_conf_gb]["pnl_5c"].sum() if high_conf_gb.any() else 0
    dt_trades = high_conf_dt.sum()
    gb_trades = high_conf_gb.sum()

    print(f"\n  Fold {fold+1}:")
    print(f"    Train: {len(X_train)} Test: {len(X_test)}")
    print(f"    DT accuracy: {dt_acc:.1%} | trades@70%: {dt_trades} | PNL: {dt_pnl:+.1f}pts")
    print(f"    GB accuracy: {gb_acc:.1%} | trades@70%: {gb_trades} | PNL: {gb_pnl:+.1f}pts")


# ═══════════════════════════════════════════════════════════════
#  STEP 4: TRAIN FINAL MODEL ON ALL DATA
# ═══════════════════════════════════════════════════════════════

gb.fit(X, y)
dt.fit(X, y)

# Feature importance — what matters most?
print("\n" + "=" * 60)
print("  FEATURE IMPORTANCE (what drives wins)")
print("=" * 60)

importances = gb.feature_importances_
for feat, imp in sorted(zip(all_features, importances),
                         key=lambda x: -x[1]):
    bar = "█" * int(imp * 50)
    print(f"  {feat:25s} {imp:.3f} {bar}")


# ═══════════════════════════════════════════════════════════════
#  STEP 5: EXTRACT DECISION TREE RULES
# ═══════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  DECISION TREE RULES")
print("=" * 60)
tree_rules = export_text(dt, feature_names=all_features,
                          max_depth=5)
print(tree_rules)


# ═══════════════════════════════════════════════════════════════
#  STEP 6: SAVE MODEL + GENERATE PREDICTIONS FILE
# ═══════════════════════════════════════════════════════════════

# Ensure state directory exists
os.makedirs(os.path.expanduser("~/state"), exist_ok=True)

# Save model
model_path = os.path.expanduser("~/state/ml_model.pkl")
with open(model_path, "wb") as f:
    pickle.dump({"gb": gb, "dt": dt, "features": all_features}, f)
print(f"\nModel saved: {model_path}")

# Generate prediction thresholds
print("\n" + "=" * 60)
print("  ML THRESHOLDS")
print("=" * 60)

for thresh in [0.5, 0.6, 0.7, 0.8]:
    proba = gb.predict_proba(X)[:, 1]
    mask = proba >= thresh
    trades = mask.sum()
    if trades > 0:
        pnl = all_scans.iloc[mask]["pnl_5c"].sum()
        wins = all_scans.iloc[mask]["win"].sum()
        wr = wins / trades * 100
        print(f"  Threshold {thresh:.0%}: {trades} trades, "
              f"{wins} wins ({wr:.0f}%), PNL: {pnl:+.1f}pts")

# Save thresholds to JSON for bot to read
thresholds = {
    "model": "gradient_boosting",
    "features": all_features,
    "optimal_threshold": 0.70,
    "trained_on": len(all_scans),
    "overall_accuracy": float(accuracy_score(y, gb.predict(X))),
    "trained_date": str(pd.Timestamp.now().date())
}
thresh_path = os.path.expanduser("~/state/ml_thresholds.json")
with open(thresh_path, "w") as f:
    json.dump(thresholds, f, indent=2)

print(f"\nThresholds saved: {thresh_path}")
print("\nDone. Run daily after 15:35 to retrain on new data.")
