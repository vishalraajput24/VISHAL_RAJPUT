# ═══════════════════════════════════════════════════════════════
#  VRL_CONFIG.py — Central Configuration Loader
#  Loads config.yaml, validates all required keys, exposes
#  typed accessors. Immutable at runtime — restart to reload.
# ═══════════════════════════════════════════════════════════════

import os
import sys
import yaml

_CONFIG_PATH = os.environ.get(
    "VRL_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
)

_cfg = None
_CONFIG_VERSION = "13.2"


class ConfigError(Exception):
    """Raised when config.yaml is missing or invalid."""
    pass


def _deep_get(d: dict, *keys, default=None):
    """Nested dict lookup: _deep_get(cfg, 'strategy', 'rsi', '1m_low')"""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
        if d is default:
            return default
    return d


def load(path: str = None) -> dict:
    """Load and validate config.yaml. Called once at startup."""
    global _cfg
    p = path or _CONFIG_PATH
    if not os.path.isfile(p):
        raise ConfigError("Config file not found: " + p)
    with open(p) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ConfigError("Config file is empty or not a valid YAML dict")
    _validate(raw)
    _cfg = raw
    return _cfg


def _validate(cfg: dict):
    """Validate all required top-level sections and critical keys."""
    required_sections = [
        "mode", "instrument", "strategy", "risk", "trail",
        "market_hours", "dte_profiles", "prediction_table",
    ]
    for sec in required_sections:
        if sec not in cfg:
            raise ConfigError("Missing required config section: " + sec)

    # Validate mode
    if cfg["mode"] not in ("paper", "live"):
        raise ConfigError("mode must be 'paper' or 'live', got: " + str(cfg["mode"]))

    # Validate instrument
    inst = cfg["instrument"]
    for k in ("name", "lot_size", "spot_token"):
        if k not in inst:
            raise ConfigError("instrument." + k + " is required")
    if not isinstance(inst["lot_size"], int) or inst["lot_size"] <= 0:
        raise ConfigError("instrument.lot_size must be a positive integer")

    # Validate strategy RSI
    rsi = _deep_get(cfg, "strategy", "rsi")
    if rsi is None:
        raise ConfigError("strategy.rsi section is required")
    for k in ("1m_low", "1m_high_normal", "1m_high_strong", "3m_low", "3m_high"):
        if k not in rsi:
            raise ConfigError("strategy.rsi." + k + " is required")

    # Validate risk
    risk = cfg["risk"]
    for k in ("sl_max", "sl_dte0", "sl_multiplier"):
        if k not in risk:
            raise ConfigError("risk." + k + " is required")

    # Validate DTE profiles
    profiles = cfg["dte_profiles"]
    for dte_key in ("0", "1", "2", "3-5", "6+"):
        if dte_key not in profiles:
            raise ConfigError("dte_profiles." + dte_key + " is required")
        p = profiles[dte_key]
        for k in ("conv_sl_pts", "conv_breakeven_pts", "delta_min", "delta_max"):
            if k not in p:
                raise ConfigError("dte_profiles." + dte_key + "." + k + " is required")

    # Validate prediction table
    pred = cfg["prediction_table"]
    for regime in ("TRENDING_STRONG", "TRENDING", "NEUTRAL", "CHOPPY"):
        if regime not in pred:
            raise ConfigError("prediction_table." + regime + " is required")


# ── Typed accessors ──────────────────────────────────────────

def get() -> dict:
    """Return the full config dict. Raises if not loaded."""
    if _cfg is None:
        raise ConfigError("Config not loaded. Call VRL_CONFIG.load() first.")
    return _cfg


def mode() -> str:
    return get()["mode"]


def is_paper() -> bool:
    return mode() == "paper"


def is_live() -> bool:
    return mode() == "live"


# ── Instrument ──

def instrument_name() -> str:
    return get()["instrument"]["name"]


def lot_size() -> int:
    return get()["instrument"]["lot_size"]


def spot_token() -> int:
    return get()["instrument"]["spot_token"]


def vix_token() -> int:
    return get()["instrument"].get("vix_token", 264969)


# ── Strategy ──

def rsi(key: str, default=None):
    return _deep_get(get(), "strategy", "rsi", key, default=default)


def spread(key: str, default=None):
    return _deep_get(get(), "strategy", "spread", key, default=default)


def strike_cfg(key: str, default=None):
    return _deep_get(get(), "strategy", "strike", key, default=default)


def scoring(key: str, default=None):
    return _deep_get(get(), "strategy", "scoring", key, default=default)


def session_score_min() -> dict:
    return _deep_get(get(), "strategy", "scoring", "session_min",
                     default={"OPEN": 5, "MORNING": 5, "AFTERNOON": 5, "LATE": 6})


def lookback(tf: str) -> int:
    return _deep_get(get(), "strategy", "lookback", tf, default=50)


# ── Risk ──

def risk(key: str, default=None):
    return _deep_get(get(), "risk", key, default=default)


# ── Trail ──

def trail(key: str, default=None):
    return _deep_get(get(), "trail", key, default=default)


def profit_floors() -> list:
    """Returns list of {peak: X, lock: Y} from config.yaml profit_floors section."""
    try:
        raw = get().get("profit_floors", [])
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return [{"peak": int(k), "lock": v} for k, v in raw.items()]
        return []
    except Exception:
        return []


def adaptive_ema(level: str) -> dict:
    """level = 'low', 'mid', or 'high'"""
    try:
        return _deep_get(get(), "trail", "adaptive_ema", level,
                         default={"timeframe": "5minute", "candles": 2})
    except Exception:
        return {"timeframe": "5minute", "candles": 2}


def cooldown(key: str, default=None):
    """Safe accessor for cooldown config."""
    try:
        return _deep_get(get(), "cooldown", key, default=default)
    except Exception:
        return default


def entry_cfg(key: str, default=None):
    """Safe accessor for entry config."""
    try:
        return _deep_get(get(), "entry", key, default=default)
    except Exception:
        return default


def exit_cfg(key: str, default=None):
    """Safe accessor for exit config."""
    try:
        return _deep_get(get(), "exit", key, default=default)
    except Exception:
        return default


def rsi_exit_cfg(key: str, default=None):
    """Safe accessor for rsi_exit config."""
    try:
        return _deep_get(get(), "rsi_exit", key, default=default)
    except Exception:
        return default


# ── DTE Profiles ──

def dte_profile(dte: int) -> dict:
    profiles = get()["dte_profiles"]
    if dte >= 6:   return profiles["6+"]
    elif dte >= 3: return profiles["3-5"]
    elif dte == 2: return profiles["2"]
    elif dte == 1: return profiles["1"]
    else:          return profiles["0"]


# ── Market Hours ──

def market_hours(key: str, default=None):
    return _deep_get(get(), "market_hours", key, default=default)


# ── Expiry ──

def expiry_cfg(key: str, default=None):
    return _deep_get(get(), "expiry", key, default=default)


# ── DTE0 ──

def dte0_cfg(key: str, default=None):
    return _deep_get(get(), "dte0", key, default=default)


# ── Prediction Table ──

def prediction(regime: str, session: str) -> int:
    pred = get()["prediction_table"]
    return _deep_get(pred, regime, session,
                     default=_deep_get(pred, "TRENDING", "MORNING", default=22))


# ── Regime thresholds ──

def regime_threshold(key: str, default=None):
    return _deep_get(get(), "regime", key, default=default)


# ── Zones ──

def zones_enabled() -> bool:
    return _deep_get(get(), "zones", "enabled", default=False)


def zones_file() -> str:
    return os.path.expanduser(_deep_get(get(), "zones", "file",
                                         default="~/state/vrl_zones.json"))


# ── ML ──

def ml_enabled() -> bool:
    return _deep_get(get(), "ml", "enabled", default=False)


def ml_model_path() -> str:
    return os.path.expanduser(_deep_get(get(), "ml", "model_path",
                                         default="~/state/ml_model.pkl"))


def ml_score_weight() -> float:
    return _deep_get(get(), "ml", "score_weight", default=0.5)


# ── Lab ──

def lab(key: str, default=None):
    return _deep_get(get(), "lab", key, default=default)


# ── Websocket ──

def ws_reconnect_delay() -> int:
    return _deep_get(get(), "websocket", "reconnect_delay", default=5)


def ws_tick_stale_secs() -> int:
    return _deep_get(get(), "websocket", "tick_stale_secs", default=8)


# ── Web ──

def web_port() -> int:
    return _deep_get(get(), "web", "port", default=8080)


def web_auth() -> bool:
    return _deep_get(get(), "web", "auth", default=False)
