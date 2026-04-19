# ═══════════════════════════════════════════════════════════════
#  VRL_RESEARCH.py — GJR-GARCH volatility + Hawkes intensity
#  Pure functions. No state mutation, no side effects.
# ═══════════════════════════════════════════════════════════════

import json
import logging
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger("VRL")

_THRESHOLDS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "state", "research_thresholds.json"
)

_DEFAULT_THRESHOLDS = {
    "sigma_p25": 1.0,
    "sigma_p50": 2.0,
    "sigma_p75": 4.0,
    "sigma_p95": 8.0,
    "lambda_p50": 0.5,
    "lambda_p75": 1.2,
    "updated": "",
}


def load_thresholds() -> dict:
    try:
        if os.path.isfile(_THRESHOLDS_PATH):
            with open(_THRESHOLDS_PATH) as f:
                t = json.load(f)
            updated = t.get("updated", "")
            if updated:
                age = (datetime.now() - datetime.fromisoformat(updated)).days
                if age <= 30:
                    return t
    except Exception as e:
        logger.debug("[RESEARCH] threshold load: " + str(e))
    return dict(_DEFAULT_THRESHOLDS)


def classify_vol_regime(sigma: float, thresholds: dict = None) -> str:
    if thresholds is None:
        thresholds = load_thresholds()
    if sigma <= 0:
        return "LOW"
    if sigma <= thresholds.get("sigma_p25", 1.0):
        return "LOW"
    if sigma <= thresholds.get("sigma_p75", 4.0):
        return "NORMAL"
    if sigma <= thresholds.get("sigma_p95", 8.0):
        return "HIGH"
    return "EXTREME"


def classify_cluster_state(lambda_now: float, recent_lambdas: list = None,
                           thresholds: dict = None) -> str:
    if thresholds is None:
        thresholds = load_thresholds()
    p75 = thresholds.get("lambda_p75", 1.2)
    p50 = thresholds.get("lambda_p50", 0.5)
    if lambda_now <= p50:
        return "CALM"
    if recent_lambdas and len(recent_lambdas) >= 2:
        if recent_lambdas[-1] < recent_lambdas[-2] and lambda_now > p50:
            return "COOLING"
    if lambda_now > p75:
        return "ACTIVE"
    return "CALM"


def _gk_realized_variance(o, h, l, c):
    """Garman-Klass realized variance per candle.
    RV = 0.5 * (log(H/L))^2 - (2*log(2) - 1) * (log(C/O))^2
    All inputs are arrays of equal length (open, high, low, close).
    Rows with non-positive values are filtered upstream.
    """
    term1 = 0.5 * (np.log(h / l)) ** 2
    term2 = (2.0 * np.log(2.0) - 1.0) * (np.log(c / o)) ** 2
    rv = term1 - term2
    # GK can be slightly negative on pathological bars; clip to tiny positive
    return np.maximum(rv, 1e-12)


def gjr_garch_forecast_gk(ohlc_df: pd.DataFrame,
                          min_candles: int = 30) -> dict:
    """v2: GJR-GARCH on Garman-Klass realized vol instead of log-returns.
    Captures intrabar movement — works for option premium series where
    close-to-close variance is too stable to fit.

    ohlc_df: DataFrame with columns open, high, low, close (last row is
    the bar we forecast FROM; horizon-1 sigma is the forecast AT/AFTER).
    """
    result = {
        "sigma_forecast": 0.0,
        "vol_regime": "INSUFFICIENT",
        "gjr_asymmetry": 0.0,
        "fit_success": False,
        "error": None,
    }

    if ohlc_df is None or len(ohlc_df) < min_candles:
        result["error"] = "insufficient data: " + str(len(ohlc_df) if ohlc_df is not None else 0)
        return result

    try:
        d = ohlc_df[["open", "high", "low", "close"]].dropna()
        d = d[(d["open"] > 0) & (d["high"] > 0) & (d["low"] > 0) & (d["close"] > 0)]
        if len(d) < min_candles:
            result["error"] = "insufficient after filter: " + str(len(d))
            return result

        o = d["open"].values.astype(float)
        h = d["high"].values.astype(float)
        l = d["low"].values.astype(float)
        c = d["close"].values.astype(float)

        gk = _gk_realized_variance(o, h, l, c)
        # Signed realized return: magnitude from GK sqrt, sign from candle body.
        # Flat candles (close==open) get zero sign → replace with tiny noise to
        # keep GARCH fit numerically stable.
        sign = np.sign(c - o)
        sign = np.where(sign == 0, 1.0, sign)
        signed_rv = sign * np.sqrt(gk) * 100.0   # scale to percent for fit

        if np.std(signed_rv) == 0 or np.unique(signed_rv).size == 1:
            result["vol_regime"] = "LOW"
            result["sigma_forecast"] = 0.0
            result["fit_success"] = True
            return result

        series = pd.Series(signed_rv)
        from arch import arch_model
        model = arch_model(series, vol="GARCH", p=1, o=1, q=1, dist="t", rescale=False)
        res = model.fit(disp="off", show_warning=False)
        fcst = res.forecast(horizon=1, reindex=False)
        sigma = float(np.sqrt(fcst.variance.iloc[-1, 0]))
        gamma = float(res.params.get("gamma[1]", 0.0))

        thresholds = load_thresholds()
        result["sigma_forecast"] = round(sigma, 4)
        result["gjr_asymmetry"] = round(gamma, 4)
        result["vol_regime"] = classify_vol_regime(sigma, thresholds)
        result["fit_success"] = True
        return result

    except Exception as e:
        result["error"] = str(e)[:200]
        return result


def gjr_garch_forecast(premium_series: pd.Series,
                       min_candles: int = 30) -> dict:
    result = {
        "sigma_forecast": 0.0,
        "vol_regime": "INSUFFICIENT",
        "gjr_asymmetry": 0.0,
        "fit_success": False,
        "error": None,
    }

    if premium_series is None or len(premium_series) < min_candles:
        result["error"] = "insufficient data: " + str(len(premium_series) if premium_series is not None else 0)
        return result

    try:
        s = premium_series.dropna()
        if len(s) < min_candles:
            result["error"] = "insufficient after NaN drop: " + str(len(s))
            return result

        if s.std() == 0 or s.nunique() == 1:
            result["vol_regime"] = "LOW"
            result["sigma_forecast"] = 0.0
            result["fit_success"] = True
            return result

        scale = 100.0 if s.median() < 5 else 1.0
        returns = (scale * np.log(s)).diff().dropna()

        if len(returns) < min_candles - 1:
            result["error"] = "insufficient returns: " + str(len(returns))
            return result

        if returns.std() == 0:
            result["vol_regime"] = "LOW"
            result["sigma_forecast"] = 0.0
            result["fit_success"] = True
            return result

        from arch import arch_model
        model = arch_model(returns, vol="GARCH", p=1, o=1, q=1, dist="t")
        res = model.fit(disp="off", show_warning=False)
        fcst = res.forecast(horizon=1, reindex=False)
        sigma = float(np.sqrt(fcst.variance.iloc[-1, 0]))
        gamma = float(res.params.get("gamma[1]", 0.0))

        thresholds = load_thresholds()
        result["sigma_forecast"] = round(sigma, 4)
        result["gjr_asymmetry"] = round(gamma, 4)
        result["vol_regime"] = classify_vol_regime(sigma, thresholds)
        result["fit_success"] = True
        return result

    except Exception as e:
        result["error"] = str(e)[:200]
        return result


def hawkes_intensity(candle_history: list, jump_threshold_pct: float = 8.0,
                     alpha: float = 0.4, beta: float = 0.3) -> dict:
    """v2: threshold lowered 15 → 8%, jumps now size-weighted.
    λ(t) = μ + Σ (range_pct_i / threshold) * α * exp(−β(t − t_i))
    A 16% candle contributes 2x a single 8% candle.
    """
    result = {
        "lambda_now": 0.0,
        "baseline_mu": 0.1,
        "cluster_state": "INSUFFICIENT",
        "recent_jumps": 0,
        "error": None,
    }

    if not candle_history:
        result["error"] = "empty candle history"
        return result

    mu = 0.1
    result["baseline_mu"] = mu

    try:
        # jumps[i] = (timestamp, range_pct) for bars exceeding threshold
        jumps = []
        now_ts = None
        for c in candle_history:
            close = float(c.get("close", 0) or 0)
            high = float(c.get("high", 0) or 0)
            low = float(c.get("low", 0) or 0)
            if close <= 0 or high <= 0:
                continue

            ts = c.get("timestamp") or c.get("date")
            if ts is None:
                ts_key = None
                for k in c:
                    if hasattr(c[k], "timestamp") or isinstance(c[k], (datetime, pd.Timestamp)):
                        ts_key = k
                        break
                if ts_key:
                    ts = c[ts_key]
            if ts is None:
                continue

            if isinstance(ts, str):
                try:
                    ts = pd.Timestamp(ts)
                except Exception:
                    continue
            elif not isinstance(ts, (datetime, pd.Timestamp)):
                try:
                    ts = pd.Timestamp(ts)
                except Exception:
                    continue

            now_ts = ts
            range_pct = (high - low) / close * 100.0
            if range_pct > jump_threshold_pct:
                jumps.append((ts, range_pct))

        if now_ts is None:
            result["error"] = "no valid timestamps"
            return result

        lambda_now = mu
        for jt, rpct in jumps:
            dt_minutes = (now_ts - jt).total_seconds() / 60.0
            if dt_minutes < 0:
                continue
            weight = rpct / jump_threshold_pct  # size-weighted contribution
            lambda_now += weight * alpha * np.exp(-beta * dt_minutes)

        thirty_ago = now_ts - timedelta(minutes=30)
        recent = sum(1 for jt, _ in jumps if jt >= thirty_ago)

        thresholds = load_thresholds()
        result["lambda_now"] = round(float(lambda_now), 4)
        result["recent_jumps"] = recent
        result["cluster_state"] = classify_cluster_state(
            lambda_now, thresholds=thresholds)
        return result

    except Exception as e:
        result["error"] = str(e)[:200]
        return result
