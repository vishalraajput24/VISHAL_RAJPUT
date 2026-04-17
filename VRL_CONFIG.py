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
    """Validate v15.2 required sections (nested entry: / exit: format)."""
    required = ["mode", "instrument", "lots", "entry", "exit",
                "strike", "risk", "market_hours"]
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
    eb = (cfg.get("entry") or {}).get("ema9_band") or {}
    for k in ("body_pct_min", "warmup_until", "cutoff_after"):
        if k not in eb:
            raise ConfigError("entry.ema9_band." + k + " is required")
    if "cooldown_minutes_same_dir" not in eb and "cooldown_minutes" not in eb:
        raise ConfigError("entry.ema9_band.cooldown_minutes_same_dir is required")
    xb = (cfg.get("exit") or {}).get("ema9_band") or {}
    for k in ("emergency_sl_pts", "stale_candles", "stale_peak_max", "eod_exit_time"):
        if k not in xb:
            raise ConfigError("exit.ema9_band." + k + " is required")


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


# ── Strategy v15.2 (nested entry: / exit: / filters: paths) ──

def entry_ema9_band(key: str, default=None):
    """Read entry.ema9_band.<key>. Special-case cooldown_minutes so callers
    that still ask for the old name pick up the new `cooldown_minutes_same_dir`."""
    eb = (get().get("entry") or {}).get("ema9_band") or {}
    if key == "cooldown_minutes":
        if "cooldown_minutes_same_dir" in eb:
            return eb["cooldown_minutes_same_dir"]
    if key in eb:
        return eb[key]
    return default


def exit_ema9_band(key: str, default=None):
    xb = (get().get("exit") or {}).get("ema9_band") or {}
    return xb.get(key, default)


def straddle_filter(key: str, default=None):
    """v16: straddle is display-only. Reads entry.filters.straddle_display."""
    sf = ((get().get("entry") or {}).get("filters") or {}).get("straddle_display") or {}
    return sf.get(key, default)


def vwap_bonus(key: str, default=None):
    vb = ((get().get("entry") or {}).get("filters") or {}).get("vwap_bonus") or {}
    return vb.get(key, default)


def cooldown(key: str, default=None):
    return _deep_get(get(), "cooldown", key, default=default)


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


# ── Lookback ──

def lookback(tf: str) -> int:
    defaults = {"1m": 50, "3m": 60, "5m": 10}
    return defaults.get(tf, 50)
