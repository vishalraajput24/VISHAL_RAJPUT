# ═══════════════════════════════════════════════════════════════
#  VRL_CONFIG.py — VISHAL RAJPUT TRADE v14.0
#  Central config loader. Loads config.yaml, validates required
#  v14.0 sections, exposes typed accessors.
#  Immutable at runtime — restart to reload.
# ═══════════════════════════════════════════════════════════════

import os
import yaml

_CONFIG_PATH = os.environ.get(
    "VRL_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
)

_cfg = None
_CONFIG_VERSION = "14.0"


class ConfigError(Exception):
    """Raised when config.yaml is missing or invalid."""
    pass


def _deep_get(d: dict, *keys, default=None):
    """Nested dict lookup."""
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
    """Validate v14.0 required sections."""
    required = ["mode", "instrument", "lots", "entry_3min", "exit",
                "profit_floors", "strike", "risk", "market_hours"]
    for sec in required:
        if sec not in cfg:
            raise ConfigError("Missing required config section: " + sec)
    if cfg["mode"] not in ("paper", "live"):
        raise ConfigError("mode must be 'paper' or 'live', got: " + str(cfg["mode"]))
    inst = cfg["instrument"]
    for k in ("name", "lot_size", "spot_token"):
        if k not in inst:
            raise ConfigError("instrument." + k + " is required")
    if not isinstance(inst["lot_size"], int) or inst["lot_size"] <= 0:
        raise ConfigError("instrument.lot_size must be a positive integer")
    e3 = cfg["entry_3min"]
    for k in ("rsi_min", "rsi_max", "adx_min", "body_pct_min", "allowed_regimes"):
        if k not in e3:
            raise ConfigError("entry_3min." + k + " is required")


# ── Accessors ────────────────────────────────────────────────

def get() -> dict:
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


# ── Strategy v14.0 ──

def entry_3min(key: str, default=None):
    return _deep_get(get(), "entry_3min", key, default=default)


def cooldown(key: str, default=None):
    return _deep_get(get(), "cooldown", key, default=default)


def exit_cfg(key: str, default=None):
    return _deep_get(get(), "exit", key, default=default)


def rsi_exit_cfg(key: str, default=None):
    return _deep_get(get(), "rsi_exit", key, default=default)


def profit_trail(key: str, default=None):
    return _deep_get(get(), "profit_trail", key, default=default)


def profit_floors() -> list:
    """Returns list of {peak: X, lock: Y}."""
    raw = get().get("profit_floors", [])
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [{"peak": int(k), "lock": v} for k, v in raw.items()]
    return []


# ── Risk ──

def risk(key: str, default=None):
    return _deep_get(get(), "risk", key, default=default)


# ── Market Hours ──

def market_hours(key: str, default=None):
    return _deep_get(get(), "market_hours", key, default=default)


# ── Lab (untouched) ──

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


# ── Strike ──

def strike_cfg(key: str, default=None):
    return _deep_get(get(), "strike", key, default=default)


# ── Lookback (legacy compat for VRL_DATA constants) ──

def lookback(tf: str) -> int:
    """Legacy compat. Returns 50 for 1m, 60 for 3m, 10 for 5m."""
    defaults = {"1m": 50, "3m": 60, "5m": 10}
    return defaults.get(tf, 50)


# ═══════════════════════════════════════════════════════════════
#  LEGACY ACCESSORS — return defaults, no longer in config
#  These exist so VRL_DATA constants don't crash. They're dead
#  values that v14.0 strategy doesn't read. Kept as no-ops only.
# ═══════════════════════════════════════════════════════════════

def rsi(key: str, default=None):
    return default

def spread(key: str, default=None):
    return default

def scoring(key: str, default=None):
    return default

def session_score_min() -> dict:
    return {"OPEN": 5, "MORNING": 5, "AFTERNOON": 5, "LATE": 6}

def trail(key: str, default=None):
    return default

def expiry_cfg(key: str, default=None):
    return default

def dte0_cfg(key: str, default=None):
    return default

def dte_profile(dte: int) -> dict:
    return {"conv_sl_pts": 12, "conv_breakeven_pts": 10,
            "delta_min": 0.30, "delta_max": 0.70}

def prediction(regime: str, session: str) -> int:
    return 22

def regime_threshold(key: str, default=None):
    return default

def zones_enabled() -> bool:
    return False

def zones_file() -> str:
    return os.path.expanduser("~/state/vrl_zones.json")

def ml_enabled() -> bool:
    return False

def ml_model_path() -> str:
    return os.path.expanduser("~/state/ml_model.pkl")

def ml_score_weight() -> float:
    return 0.5

def adaptive_ema(level: str) -> dict:
    return {"timeframe": "5minute", "candles": 2}

def entry_cfg(key: str, default=None):
    return default
