#!/home/user/kite_env/bin/python3
# ═══════════════════════════════════════════════════════════════
#  VRL_DB.py — VISHAL RAJPUT TRADE v13.1
#  SQLite database helper. WAL mode for concurrent reads.
#  All lab data + trades in ~/lab_data/vrl_data.db
# ═══════════════════════════════════════════════════════════════

import sqlite3
import os
import logging
import threading

logger = logging.getLogger("vrl_live")

DB_PATH = os.path.expanduser("~/lab_data/vrl_data.db")
_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def get_conn():
    """Get thread-local connection with WAL mode."""
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA cache_size=-8000")  # 8MB cache
    return _local.conn


def init_db():
    """Create all tables and indexes if not exist. Call at startup. Thread-safe."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = get_conn()
        c = conn.cursor()

        # ── SPOT TABLES ──
        for table in ("spot_1min", "spot_5min", "spot_15min", "spot_60min"):
            c.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                timestamp TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                ema9 REAL, ema21 REAL, ema_spread REAL, rsi REAL, adx REAL,
                UNIQUE(timestamp))""")

        c.execute("""CREATE TABLE IF NOT EXISTS spot_daily (
            date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            ema21 REAL, rsi REAL, adx REAL,
            UNIQUE(date))""")

        # ── OPTION 1-MIN ──
        c.execute("""CREATE TABLE IF NOT EXISTS option_1min (
            timestamp TEXT NOT NULL, strike INTEGER, type TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            spot_ref REAL, atm_distance REAL, dte INTEGER, session_block TEXT,
            body_pct REAL, rsi REAL, ema9 REAL, ema9_gap REAL, adx REAL,
            volume_ratio REAL, iv_pct REAL, delta REAL,
            fwd_1c REAL, fwd_3c REAL, fwd_5c REAL, fwd_outcome TEXT)""")

        # ── OPTION 3-MIN ──
        c.execute("""CREATE TABLE IF NOT EXISTS option_3min (
            timestamp TEXT NOT NULL, strike INTEGER, type TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            spot_ref REAL, atm_distance REAL, dte INTEGER, session_block TEXT,
            iv_vs_open REAL,
            body_pct REAL, adx REAL, rsi REAL, ema9 REAL, ema21 REAL,
            ema_spread REAL, ema9_gap REAL, volume_ratio REAL,
            iv_pct REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
            fwd_3c REAL, fwd_6c REAL, fwd_9c REAL, fwd_outcome TEXT)""")

        # ── OPTION 5-MIN ──
        c.execute("""CREATE TABLE IF NOT EXISTS option_5min (
            timestamp TEXT NOT NULL, strike INTEGER, type TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            spot_ref REAL, dte INTEGER, session_block TEXT,
            body_pct REAL, rsi REAL, ema9 REAL, ema21 REAL, ema_spread REAL,
            adx REAL, volume_ratio REAL, iv_pct REAL, delta REAL)""")

        # ── OPTION 15-MIN ──
        c.execute("""CREATE TABLE IF NOT EXISTS option_15min (
            timestamp TEXT NOT NULL, strike INTEGER, type TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            spot_ref REAL, dte INTEGER, session_block TEXT,
            body_pct REAL, rsi REAL, ema9 REAL, ema21 REAL, ema_spread REAL,
            macd_hist REAL, adx REAL,
            volume_ratio REAL, iv_pct REAL, delta REAL)""")

        # ── SIGNAL SCANS ──
        c.execute("""CREATE TABLE IF NOT EXISTS signal_scans (
            timestamp TEXT NOT NULL, session TEXT, dte INTEGER, atm_strike INTEGER, spot REAL,
            direction TEXT, entry_price REAL,
            rsi_1m REAL, body_pct_1m REAL, vol_ratio_1m REAL, rsi_rising_1m TEXT, spread_1m REAL,
            rsi_3m REAL, body_pct_3m REAL, ema_spread_3m REAL, conditions_3m TEXT, mode_3m TEXT,
            score REAL, fired TEXT, reject_reason TEXT,
            iv_pct REAL, delta REAL, vix REAL,
            spot_rsi_3m REAL, spot_ema_spread_3m REAL, spot_regime TEXT, spot_gap REAL,
            bias TEXT, hourly_rsi REAL, straddle_decay_pct REAL,
            near_fib_level TEXT, fib_distance REAL,
            fwd_3c REAL, fwd_5c REAL, fwd_10c REAL, fwd_outcome TEXT)""")

        # ── TRADES ──
        c.execute("""CREATE TABLE IF NOT EXISTS trades (
            date TEXT NOT NULL, entry_time TEXT, exit_time TEXT,
            symbol TEXT, direction TEXT, mode TEXT,
            entry_price REAL, exit_price REAL, pnl_pts REAL, pnl_rs REAL,
            peak_pnl REAL, trough_pnl REAL,
            exit_reason TEXT, exit_phase INTEGER,
            score REAL, iv_at_entry REAL, regime TEXT,
            dte INTEGER, candles_held INTEGER,
            session TEXT, strike INTEGER, sl_pts REAL,
            spread_1m REAL, spread_3m REAL, delta_at_entry REAL,
            bias TEXT, vix_at_entry REAL, hourly_rsi REAL, straddle_decay REAL)""")

        # ── INDEXES ──
        _indexes = [
            ("idx_spot1m_ts", "spot_1min", "timestamp"),
            ("idx_spot5m_ts", "spot_5min", "timestamp"),
            ("idx_spot15m_ts", "spot_15min", "timestamp"),
            ("idx_spot60m_ts", "spot_60min", "timestamp"),
            ("idx_spotd_date", "spot_daily", "date"),
            ("idx_opt1m_ts", "option_1min", "timestamp, type"),
            ("idx_opt3m_ts", "option_3min", "timestamp, type"),
            ("idx_opt5m_ts", "option_5min", "timestamp, type"),
            ("idx_opt15m_ts", "option_15min", "timestamp, type"),
            ("idx_scans_ts", "signal_scans", "timestamp"),
            ("idx_scans_dir", "signal_scans", "direction, fired"),
            ("idx_trades_date", "trades", "date"),
            ("idx_trades_dir", "trades", "direction, date"),
        ]
        # Functional indexes (date-based queries)
        _func_indexes = [
            ("idx_scans_date", "signal_scans", "date(timestamp)"),
            ("idx_opt1m_date", "option_1min", "date(timestamp)"),
            ("idx_opt3m_date", "option_3min", "date(timestamp)"),
            ("idx_spot1m_date", "spot_1min", "date(timestamp)"),
        ]
        for name, table, cols in _indexes:
            c.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({cols})")
        for name, table, expr in _func_indexes:
            c.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({expr})")

        # ── DASHBOARD TOKENS (subscriber access) ──
        c.execute("""CREATE TABLE IF NOT EXISTS dashboard_tokens (
            token TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_used TEXT,
            access_count INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            access_ips TEXT DEFAULT ''
        )""")

        # v13.1: Add charge columns to existing trades table
        _new_cols = [
            ("brokerage", "REAL DEFAULT 0"),
            ("stt", "REAL DEFAULT 0"),
            ("exchange_charges", "REAL DEFAULT 0"),
            ("gst", "REAL DEFAULT 0"),
            ("stamp_duty", "REAL DEFAULT 0"),
            ("total_charges", "REAL DEFAULT 0"),
            ("net_pnl_rs", "REAL DEFAULT 0"),
            ("gross_pnl_rs", "REAL DEFAULT 0"),
            ("num_exit_orders", "INTEGER DEFAULT 1"),
        ]
        for col_name, col_type in _new_cols:
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass  # column already exists

        # v13.1: Add access_ips to dashboard_tokens
        try:
            c.execute("ALTER TABLE dashboard_tokens ADD COLUMN access_ips TEXT DEFAULT ''")
        except Exception:
            pass

        conn.commit()
        _initialized = True
        logger.info("[DB] Database initialized: " + DB_PATH)


# ═══════════════════════════════════════════════════════════════
#  INSERT HELPERS — one per table type
# ═══════════════════════════════════════════════════════════════

def _insert(table, row, fields):
    """Generic insert. row is a dict, fields is ordered list of column names."""
    conn = get_conn()
    vals = [row.get(f) for f in fields]
    placeholders = ",".join(["?"] * len(fields))
    cols = ",".join(fields)
    try:
        conn.execute(f"INSERT OR IGNORE INTO {table}({cols}) VALUES ({placeholders})", vals)
        conn.commit()
    except Exception as e:
        logger.debug("[DB] Insert " + table + ": " + str(e))


def _insert_many(table, rows, fields):
    """Bulk insert. rows is list of dicts."""
    if not rows:
        return
    conn = get_conn()
    placeholders = ",".join(["?"] * len(fields))
    cols = ",".join(fields)
    data = [[r.get(f) for f in fields] for r in rows]
    try:
        conn.executemany(f"INSERT OR IGNORE INTO {table}({cols}) VALUES ({placeholders})", data)
        conn.commit()
    except Exception as e:
        logger.debug("[DB] Insert many " + table + ": " + str(e))


def _insert_many_fn(table, fields):
    """Return a function that bulk-inserts rows into a table. Used by import script."""
    def fn(rows):
        _insert_many(table, rows, fields)
    return fn


# ── Spot ──

_SPOT_FIELDS = ["timestamp", "open", "high", "low", "close", "volume",
                "ema9", "ema21", "ema_spread", "rsi", "adx"]

_SPOT_DAILY_FIELDS = ["date", "open", "high", "low", "close", "volume",
                      "ema21", "rsi", "adx"]

def insert_spot_1min(row):
    _insert("spot_1min", row, _SPOT_FIELDS)

def insert_spot_5min(row):
    _insert("spot_5min", row, _SPOT_FIELDS)

def insert_spot_15min(row):
    _insert("spot_15min", row, _SPOT_FIELDS)

def insert_spot_60min(row):
    _insert("spot_60min", row, _SPOT_FIELDS)

def insert_spot_daily(row):
    _insert("spot_daily", row, _SPOT_DAILY_FIELDS)


# ── Options ──

_OPT_1M_FIELDS = [
    "timestamp", "strike", "type", "open", "high", "low", "close", "volume",
    "spot_ref", "atm_distance", "dte", "session_block",
    "body_pct", "rsi", "ema9", "ema9_gap", "adx",
    "volume_ratio", "iv_pct", "delta",
    "fwd_1c", "fwd_3c", "fwd_5c", "fwd_outcome",
]

_OPT_3M_FIELDS = [
    "timestamp", "strike", "type", "open", "high", "low", "close", "volume",
    "spot_ref", "atm_distance", "dte", "session_block", "iv_vs_open",
    "body_pct", "adx", "rsi", "ema9", "ema21", "ema_spread", "ema9_gap",
    "volume_ratio", "iv_pct", "delta", "gamma", "theta", "vega",
    "fwd_3c", "fwd_6c", "fwd_9c", "fwd_outcome",
]

_OPT_5M_FIELDS = [
    "timestamp", "strike", "type", "open", "high", "low", "close", "volume",
    "spot_ref", "dte", "session_block",
    "body_pct", "rsi", "ema9", "ema21", "ema_spread", "adx",
    "volume_ratio", "iv_pct", "delta",
]

_OPT_15M_FIELDS = [
    "timestamp", "strike", "type", "open", "high", "low", "close", "volume",
    "spot_ref", "dte", "session_block",
    "body_pct", "rsi", "ema9", "ema21", "ema_spread", "macd_hist", "adx",
    "volume_ratio", "iv_pct", "delta",
]

def insert_option_1min(row):
    _insert("option_1min", row, _OPT_1M_FIELDS)

def insert_option_1min_many(rows):
    _insert_many("option_1min", rows, _OPT_1M_FIELDS)

def insert_option_3min(row):
    _insert("option_3min", row, _OPT_3M_FIELDS)

def insert_option_3min_many(rows):
    _insert_many("option_3min", rows, _OPT_3M_FIELDS)

def insert_option_5min(row):
    _insert("option_5min", row, _OPT_5M_FIELDS)

def insert_option_5min_many(rows):
    _insert_many("option_5min", rows, _OPT_5M_FIELDS)

def insert_option_15min(row):
    _insert("option_15min", row, _OPT_15M_FIELDS)

def insert_option_15min_many(rows):
    _insert_many("option_15min", rows, _OPT_15M_FIELDS)


# ── Scans ──

_SCAN_FIELDS = [
    "timestamp", "session", "dte", "atm_strike", "spot",
    "direction", "entry_price",
    "rsi_1m", "body_pct_1m", "vol_ratio_1m", "rsi_rising_1m", "spread_1m",
    "rsi_3m", "body_pct_3m", "ema_spread_3m", "conditions_3m", "mode_3m",
    "score", "fired", "reject_reason",
    "iv_pct", "delta", "vix",
    "spot_rsi_3m", "spot_ema_spread_3m", "spot_regime", "spot_gap",
    "bias", "hourly_rsi", "straddle_decay_pct",
    "near_fib_level", "fib_distance",
    "fwd_3c", "fwd_5c", "fwd_10c", "fwd_outcome",
]

def insert_scan(row):
    _insert("signal_scans", row, _SCAN_FIELDS)

def insert_scan_many(rows):
    _insert_many("signal_scans", rows, _SCAN_FIELDS)


# ── Trades ──

_TRADE_FIELDS = [
    "date", "entry_time", "exit_time", "symbol", "direction", "mode",
    "entry_price", "exit_price", "pnl_pts", "pnl_rs",
    "peak_pnl", "trough_pnl", "exit_reason", "exit_phase",
    "score", "iv_at_entry", "regime", "dte", "candles_held",
    "session", "strike", "sl_pts",
    "spread_1m", "spread_3m", "delta_at_entry",
    "bias", "vix_at_entry", "hourly_rsi", "straddle_decay",
    "brokerage", "stt", "exchange_charges", "gst", "stamp_duty",
    "total_charges", "net_pnl_rs", "gross_pnl_rs", "num_exit_orders",
]

def insert_trade(row):
    _insert("trades", row, _TRADE_FIELDS)


# ═══════════════════════════════════════════════════════════════
#  FORWARD FILL — update existing rows at EOD
# ═══════════════════════════════════════════════════════════════

def update_scan_fwd(timestamp, direction, fwd_3c, fwd_5c, fwd_10c, outcome):
    """Update forward-fill columns for a scan row."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE signal_scans SET fwd_3c=?, fwd_5c=?, fwd_10c=?, fwd_outcome=? "
            "WHERE timestamp=? AND direction=?",
            (fwd_3c, fwd_5c, fwd_10c, outcome, timestamp, direction)
        )
        conn.commit()
    except Exception as e:
        logger.debug("[DB] Update scan fwd: " + str(e))


def update_option_1min_fwd(timestamp, opt_type, fwd_1c, fwd_3c, fwd_5c, outcome):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE option_1min SET fwd_1c=?, fwd_3c=?, fwd_5c=?, fwd_outcome=? "
            "WHERE timestamp=? AND type=?",
            (fwd_1c, fwd_3c, fwd_5c, outcome, timestamp, opt_type)
        )
        conn.commit()
    except Exception as e:
        logger.debug("[DB] Update opt1m fwd: " + str(e))


def update_option_3min_fwd(timestamp, opt_type, fwd_3c, fwd_6c, fwd_9c, outcome):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE option_3min SET fwd_3c=?, fwd_6c=?, fwd_9c=?, fwd_outcome=? "
            "WHERE timestamp=? AND type=?",
            (fwd_3c, fwd_6c, fwd_9c, outcome, timestamp, opt_type)
        )
        conn.commit()
    except Exception as e:
        logger.debug("[DB] Update opt3m fwd: " + str(e))


# ═══════════════════════════════════════════════════════════════
#  QUERY HELPERS
# ═══════════════════════════════════════════════════════════════

def query(sql, params=None):
    """Generic query — returns list of dicts."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, params or ())
        rows = [dict(r) for r in cur.fetchall()]
        conn.row_factory = None
        return rows
    except Exception as e:
        conn.row_factory = None
        logger.debug("[DB] Query error: " + str(e))
        return []


def get_trades(date_str=None):
    """Get trades for a date (default today)."""
    if not date_str:
        from datetime import date
        date_str = date.today().isoformat()
    return query("SELECT * FROM trades WHERE date=? ORDER BY entry_time", (date_str,))


def get_scans(date_str=None, direction=None):
    """Get signal scans for a date, optionally filtered by direction."""
    if not date_str:
        from datetime import date
        date_str = date.today().isoformat()
    if direction:
        return query(
            "SELECT * FROM signal_scans WHERE date(timestamp)=? AND direction=? ORDER BY timestamp",
            (date_str, direction))
    return query(
        "SELECT * FROM signal_scans WHERE date(timestamp)=? ORDER BY timestamp",
        (date_str,))


def get_spot(table="spot_1min", from_ts=None, to_ts=None):
    """Get spot data between timestamps."""
    if from_ts and to_ts:
        return query(f"SELECT * FROM {table} WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
                     (from_ts, to_ts))
    return query(f"SELECT * FROM {table} ORDER BY timestamp DESC LIMIT 500")


def get_stats(date_str=None):
    """Get trade stats for a date."""
    if not date_str:
        from datetime import date
        date_str = date.today().isoformat()
    rows = query(
        "SELECT count(*) as cnt, sum(pnl_pts) as total_pts, avg(pnl_pts) as avg_pts, "
        "sum(CASE WHEN pnl_pts > 0 THEN 1 ELSE 0 END) as wins, "
        "sum(CASE WHEN pnl_pts <= 0 THEN 1 ELSE 0 END) as losses "
        "FROM trades WHERE date=?",
        (date_str,))
    return rows[0] if rows else {}


def close():
    """Close thread-local connection."""
    if hasattr(_local, "conn") and _local.conn:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD TOKENS
# ═══════════════════════════════════════════════════════════════

def create_token(name: str, days: int = 30) -> str:
    """Create subscriber access token. Returns token string."""
    import secrets
    from datetime import datetime, timedelta
    token = secrets.token_hex(8)
    now = datetime.now().isoformat()
    expires = (datetime.now() + timedelta(days=days)).isoformat()
    conn = get_conn()
    try:
        conn.execute("INSERT INTO dashboard_tokens (token, name, created_at, expires_at) VALUES (?,?,?,?)",
                     (token, name, now, expires))
        conn.commit()
    except Exception as e:
        logger.debug("[DB] create_token: " + str(e))
    return token


def validate_token(token: str, ip: str = "") -> dict:
    """Check token validity. Tracks IP. Returns {valid, name, expired, sharing_alert} or None."""
    from datetime import datetime
    rows = query("SELECT * FROM dashboard_tokens WHERE token=?", (token,))
    if not rows:
        return None
    r = rows[0]
    if not r.get("active"):
        return {"valid": False, "name": r["name"], "expired": False, "revoked": True}
    try:
        exp = datetime.fromisoformat(r["expires_at"])
        if datetime.now() > exp:
            return {"valid": False, "name": r["name"], "expired": True, "revoked": False}
    except Exception:
        pass
    # Valid — update access stats + track IP
    conn = get_conn()
    sharing_alert = False
    unique_ips = []
    try:
        existing_ips = r.get("access_ips", "") or ""
        ip_list = [x.strip() for x in existing_ips.split(",") if x.strip()]
        if ip and ip not in ip_list:
            ip_list.append(ip)
        unique_ips = ip_list
        new_ips = ",".join(ip_list)
        conn.execute("UPDATE dashboard_tokens SET access_count=access_count+1, last_used=?, access_ips=? WHERE token=?",
                     (datetime.now().isoformat(), new_ips, token))
        conn.commit()
        # Alert if 4+ unique IPs (mobile+laptop+tablet = 3 is normal)
        if len(ip_list) >= 4:
            sharing_alert = True
    except Exception:
        pass
    return {"valid": True, "name": r["name"], "expired": False, "revoked": False,
            "sharing_alert": sharing_alert, "unique_ips": len(unique_ips)}


def list_tokens() -> list:
    return query("SELECT * FROM dashboard_tokens ORDER BY created_at DESC")


def revoke_token(name: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("UPDATE dashboard_tokens SET active=0 WHERE name=? AND active=1", (name,))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        return False


def extend_token(name: str, days: int) -> bool:
    from datetime import datetime, timedelta
    rows = query("SELECT * FROM dashboard_tokens WHERE name=? AND active=1", (name,))
    if not rows:
        return False
    try:
        old_exp = datetime.fromisoformat(rows[0]["expires_at"])
        new_exp = max(old_exp, datetime.now()) + timedelta(days=days)
        conn = get_conn()
        conn.execute("UPDATE dashboard_tokens SET expires_at=? WHERE name=? AND active=1",
                     (new_exp.isoformat(), name))
        conn.commit()
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
#  MAINTENANCE
# ═══════════════════════════════════════════════════════════════

def cleanup_old_db_data(retention_days=30):
    """Delete rows older than N days from candle/scan tables. Never touches trades or spot_daily."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
    conn = get_conn()
    tables = ["spot_1min", "spot_5min", "spot_15min", "spot_60min",
              "option_1min", "option_3min", "option_5min", "option_15min",
              "signal_scans"]
    total = 0
    for t in tables:
        try:
            cur = conn.execute(f"DELETE FROM {t} WHERE timestamp < ?", (cutoff,))
            n = cur.rowcount
            if n > 0:
                total += n
                logger.info("[DB] Cleaned " + t + ": " + str(n) + " rows (before " + cutoff + ")")
        except Exception as e:
            logger.debug("[DB] Cleanup " + t + ": " + str(e))
    if total > 0:
        conn.commit()
        logger.info("[DB] Total cleaned: " + str(total) + " rows")


def vacuum_db():
    """Optimize and vacuum the database. Run weekly."""
    conn = get_conn()
    try:
        before = db_size_mb()
        conn.execute("PRAGMA optimize")
        conn.execute("VACUUM")
        after = db_size_mb()
        logger.info("[DB] Vacuum done: " + str(before) + "MB → " + str(after) + "MB")
    except Exception as e:
        logger.debug("[DB] Vacuum error: " + str(e))


def db_size_mb() -> float:
    """Return database file size in MB."""
    try:
        return round(os.path.getsize(DB_PATH) / (1024 * 1024), 2)
    except Exception:
        return 0.0


def db_stats() -> dict:
    """Return row counts for all tables + file size."""
    result = {"size_mb": db_size_mb(), "tables": {}}
    try:
        tables = query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        for t in tables:
            cnt = query(f"SELECT count(*) as n FROM {t['name']}")
            result["tables"][t["name"]] = cnt[0]["n"] if cnt else 0
    except Exception:
        pass
    return result


# ═══════════════════════════════════════════════════════════════
#  INIT ON IMPORT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    print("Database initialized at " + DB_PATH)
    conn = get_conn()
    tables = query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    for t in tables:
        cnt = query(f"SELECT count(*) as n FROM {t['name']}")
        print(f"  {t['name']}: {cnt[0]['n']} rows")
    close()
