#!/home/user/kite_env/bin/python3
# ═══════════════════════════════════════════════════════════════
#  VRL_DB.py — VISHAL RAJPUT TRADE v16.5
#  Merged: VRL_DB.py + VRL_VALIDATE.py
#  SQLite database helper. WAL mode for concurrent reads.
#  All lab data + trades in ~/lab_data/vrl_data.db
#  Validation checks (validate_entry, validate_exit, manual_validate)
#  are defined at the bottom of this file.
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

# ── Error visibility ─────────────────────────────────────────
# BUG-017: DB errors used to log at DEBUG level and vanish silently.
# Now the first occurrence of each distinct error surfaces at WARNING,
# repeats are throttled to DEBUG, and malformed/corrupt errors trigger
# a one-shot Telegram alert so a broken DB can't hide for a full session.
_db_seen_errors = set()
_db_corruption_alerted = False
_db_alert_lock = threading.Lock()


def _report_db_error(context: str, err: Exception):
    """Central DB error reporter. First sighting → WARNING. Repeat → DEBUG.
    Corruption errors also trigger a one-shot Telegram alert."""
    global _db_corruption_alerted
    msg_full = "[DB] " + context + ": " + str(err)
    sig = context + "|" + type(err).__name__ + "|" + str(err)[:80]

    with _db_alert_lock:
        first_time = sig not in _db_seen_errors
        if first_time:
            _db_seen_errors.add(sig)

    if first_time:
        logger.warning(msg_full)
    else:
        logger.debug(msg_full)

    # Catastrophic: DB file corrupt. Alert once per session.
    err_str = str(err).lower()
    if ("malformed" in err_str or "corrupt" in err_str
            or "not a database" in err_str):
        with _db_alert_lock:
            if _db_corruption_alerted:
                return
            _db_corruption_alerted = True
        try:
            import VRL_DATA as _D
            import requests as _rq
            if _D.TELEGRAM_TOKEN and _D.TELEGRAM_CHAT_ID:
                _rq.post(
                    "https://api.telegram.org/bot" + _D.TELEGRAM_TOKEN + "/sendMessage",
                    json={
                        "chat_id": _D.TELEGRAM_CHAT_ID,
                        "text": ("🚨 <b>DB CORRUPT</b>\n"
                                 "File: " + DB_PATH + "\n"
                                 "Context: " + context + "\n"
                                 "Error: " + str(err)[:200] + "\n\n"
                                 "Trading continues (uses Kite API, not DB),\n"
                                 "but scans/trades aren't being logged.\n"
                                 "Recover with: sqlite3 " + DB_PATH
                                 + " '.recover' | sqlite3 recovered.db"),
                        "parse_mode": "HTML",
                    },
                    timeout=10,
                )
                logger.error("[DB] Corruption alert sent to Telegram")
        except Exception as _e:
            logger.error("[DB] Failed to send corruption alert: " + str(_e))


def get_conn():
    """Get thread-local connection with WAL mode."""
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA cache_size=-8000")  # 8MB cache
    return _local.conn


def _startup_integrity_check(conn):
    """Run PRAGMA quick_check once at startup. If the DB is corrupt,
    _report_db_error triggers the Telegram alert before the bot even
    starts scanning. Non-fatal: we let init_db continue so trading
    (which doesn't need the DB) still works.

    BUG-T v15.2.5 Batch 6: on a successful check we also clear
    _db_corruption_alerted so that IF the DB gets corrupted again
    later and then manually repaired, a FRESH corruption event will
    re-alert. Without this reset, one corruption alert per session
    was the cap — future events silent."""
    global _db_corruption_alerted
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        result = (row[0] if row else "unknown") or "unknown"
        if str(result).lower() != "ok":
            _report_db_error(
                "startup integrity",
                sqlite3.DatabaseError("quick_check returned: " + str(result)),
            )
        else:
            logger.info("[DB] Startup integrity check: ok")
            # BUG-T: reset the one-shot alert flag on a clean check.
            with _db_alert_lock:
                if _db_corruption_alerted:
                    logger.info("[DB] Corruption alert flag cleared — "
                                "integrity_check passed, future corruption "
                                "events will re-alert")
                _db_corruption_alerted = False
    except Exception as e:
        _report_db_error("startup integrity", e)


def init_db():
    """Create all tables and indexes if not exist. Call at startup. Thread-safe."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = get_conn()
        _startup_integrity_check(conn)
        c = conn.cursor()

        # ── Schema version tracking (BUG-F1) ──
        c.execute("CREATE TABLE IF NOT EXISTS schema_meta "
                  "(key TEXT PRIMARY KEY, value TEXT)")
        _schema_v = 0
        try:
            _sv_row = c.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()
            _schema_v = int(_sv_row[0]) if _sv_row else 0
        except Exception:
            pass

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

        # v13.3: migrate trades table — add columns that may not exist yet
        # BUG-F1: skip if schema v15+ (migration already dropped these)
        if _schema_v >= 15:
            logger.info("[DB] Schema v" + str(_schema_v) + " — legacy "
                        "ALTER TABLE blocks skipped")
        _new_trade_cols = [
            ("brokerage",       "REAL DEFAULT 0"),
            ("stt",             "REAL DEFAULT 0"),
            ("exchange_charges","REAL DEFAULT 0"),
            ("gst",             "REAL DEFAULT 0"),
            ("stamp_duty",      "REAL DEFAULT 0"),
            ("total_charges",   "REAL DEFAULT 0"),
            ("net_pnl_rs",      "REAL DEFAULT 0"),
            ("gross_pnl_rs",    "REAL DEFAULT 0"),
            ("num_exit_orders", "INTEGER DEFAULT 1"),
            ("entry_slippage",  "REAL DEFAULT 0"),
            ("exit_slippage",   "REAL DEFAULT 0"),
            ("signal_price",    "REAL DEFAULT 0"),
            ("lot_id",          "TEXT DEFAULT 'ALL'"),
            ("bonus_vwap",      "INTEGER DEFAULT 0"),
            ("bonus_fib_level", "TEXT DEFAULT ''"),
            ("bonus_fib_dist",  "REAL DEFAULT 0"),
            ("bonus_vol_spike", "INTEGER DEFAULT 0"),
            ("bonus_vol_ratio", "REAL DEFAULT 0"),
            ("bonus_pdh_break", "INTEGER DEFAULT 0"),
            ("qty_exited",      "INTEGER DEFAULT 0"),
            ("entry_mode",      "TEXT DEFAULT ''"),
            ("momentum_pts",    "REAL DEFAULT 0"),
            ("rsi_rising",      "INTEGER DEFAULT 0"),
            ("spot_confirms",   "INTEGER DEFAULT 0"),
            ("spot_move",       "REAL DEFAULT 0"),
            ("spike_ratio",     "REAL DEFAULT 0"),
            ("momentum_tf",     "TEXT DEFAULT ''"),
            ("other_falling",   "INTEGER DEFAULT 0"),
            ("other_move",      "REAL DEFAULT 0"),
        ]
        if _schema_v < 15:
            _existing = {row[1] for row in c.execute("PRAGMA table_info(trades)")}
            for _cname, _ctype in _new_trade_cols:
                if _cname not in _existing:
                    try:
                        c.execute("ALTER TABLE trades ADD COLUMN " + _cname + " " + _ctype)
                    except Exception:
                        pass

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

        # Migrate dashboard_tokens for databases created before access_ips existed
        try:
            c.execute("ALTER TABLE dashboard_tokens ADD COLUMN access_ips TEXT DEFAULT ''")
        except Exception:
            pass

        # v13.2 + v15.0 ALTER TABLE blocks — skipped on v15+ schema
        if _schema_v < 15:
            for _sc, _st in [("entry_slippage", "REAL DEFAULT 0"),
                              ("exit_slippage", "REAL DEFAULT 0"),
                              ("signal_price", "REAL DEFAULT 0"),
                              ("lot_id", "TEXT DEFAULT 'ALL'"),
                              ("bonus_vwap", "INTEGER DEFAULT 0"),
                              ("bonus_fib_level", "TEXT DEFAULT ''"),
                              ("bonus_fib_dist", "REAL DEFAULT 0"),
                              ("bonus_vol_spike", "INTEGER DEFAULT 0"),
                              ("bonus_vol_ratio", "REAL DEFAULT 0"),
                              ("bonus_pdh_break", "INTEGER DEFAULT 0"),
                              ("qty_exited", "INTEGER DEFAULT 130"),
                              ("entry_mode", "TEXT DEFAULT 'EMA'"),
                              ("momentum_pts", "REAL DEFAULT 0"),
                              ("rsi_rising", "INTEGER DEFAULT 0"),
                              ("spot_confirms", "INTEGER DEFAULT 0"),
                              ("spot_move", "REAL DEFAULT 0")]:
                try:
                    c.execute(f"ALTER TABLE trades ADD COLUMN {_sc} {_st}")
                except Exception:
                    pass

        if _schema_v < 15:
            for _tbl, _col, _typ in [
                ("option_3min",  "ema9_high",       "REAL DEFAULT 0"),
                ("option_3min",  "ema9_low",        "REAL DEFAULT 0"),
                ("signal_scans", "ema9_high",       "REAL DEFAULT 0"),
                ("signal_scans", "ema9_low",        "REAL DEFAULT 0"),
                ("signal_scans", "band_position",   "TEXT DEFAULT ''"),
                ("signal_scans", "body_pct",        "REAL DEFAULT 0"),
                # v15.2 — straddle filter columns
                ("signal_scans", "straddle_delta",     "REAL DEFAULT 0"),
                ("signal_scans", "straddle_threshold", "REAL DEFAULT 0"),
                ("signal_scans", "straddle_period",    "TEXT DEFAULT ''"),
                ("signal_scans", "atm_strike_used",    "INTEGER DEFAULT 0"),
                ("signal_scans", "band_width",         "REAL DEFAULT 0"),
                # v15.2 — VWAP bonus columns
                ("signal_scans", "spot_vwap",     "REAL DEFAULT 0"),
                ("signal_scans", "spot_vs_vwap",  "REAL DEFAULT 0"),
                ("signal_scans", "vwap_bonus",    "TEXT DEFAULT ''"),
                # BUG-N3 v15.2.5: distinguishes "signal passed all gates"
                # (fired=1) from "trade was actually opened" (trade_taken=1).
                ("signal_scans", "trade_taken",   "INTEGER DEFAULT 0"),
                ("trades",       "entry_ema9_high", "REAL DEFAULT 0"),
                ("trades",       "entry_ema9_low",  "REAL DEFAULT 0"),
                ("trades",       "exit_ema9_high",  "REAL DEFAULT 0"),
                ("trades",       "exit_ema9_low",   "REAL DEFAULT 0"),
                ("trades",       "entry_band_position", "TEXT DEFAULT ''"),
                ("trades",       "exit_band_position",  "TEXT DEFAULT ''"),
                ("trades",       "entry_body_pct",  "REAL DEFAULT 0"),
                # v15.2 — straddle/VWAP captured at entry (replayed at exit)
                ("trades",       "entry_straddle_delta",     "REAL DEFAULT 0"),
                ("trades",       "entry_straddle_threshold", "REAL DEFAULT 0"),
                ("trades",       "entry_straddle_period",    "TEXT DEFAULT ''"),
                ("trades",       "entry_atm_strike",         "INTEGER DEFAULT 0"),
                ("trades",       "entry_band_width",         "REAL DEFAULT 0"),
                ("trades",       "entry_spot_vwap",          "REAL DEFAULT 0"),
                ("trades",       "entry_spot_vs_vwap",       "REAL DEFAULT 0"),
                ("trades",       "entry_vwap_bonus",         "TEXT DEFAULT ''"),
                # v15.2.5 Fix 5: STRONG / NEUTRAL / WEAK / NA classification
                ("trades",       "entry_straddle_info",      "TEXT DEFAULT ''"),
            ]:
                try:
                    c.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_typ}")
                    logger.info(f"[DB] migrate: added {_tbl}.{_col}")
                except Exception:
                    pass  # column already exists

        conn.commit()
        _initialized = True
        logger.info("[DB] Database initialized: " + DB_PATH)

        # BUG-N6 + N7: one-shot migration to drop dead v13 columns.
        # BUG-F1: gated by schema version so it doesn't re-run.
        if _schema_v < 15:
            try:
                migrate_schema_v15()
                # Stamp schema version AFTER successful migration so
                # the next restart skips all legacy ALTER TABLE blocks
                # AND the migration itself.
                c.execute("INSERT OR REPLACE INTO schema_meta "
                          "VALUES ('version', '15')")
                conn.commit()
                logger.info("[DB] Schema version stamped: 15")
            except Exception as _me:
                logger.warning("[DB] Schema v15 migration: " + str(_me))
        elif _schema_v < 16:
            try:
                migrate_schema_v16()
            except Exception as _me:
                logger.warning("[DB] Schema v16 migration: " + str(_me))
        else:
            logger.info("[DB] Schema v" + str(_schema_v)
                        + " — migration already done, skipped")


# ═══════════════════════════════════════════════════════════════
#  v15.2.5 BUG-N6/N7 — SCHEMA MIGRATION: drop dead v13 columns
#  SQLite < 3.35 has no ALTER TABLE DROP COLUMN. Strategy:
#    1. Backup DB
#    2. Rename table → _legacy
#    3. CREATE new table with live columns only
#    4. INSERT SELECT from _legacy → new
#    5. DROP _legacy
#  Idempotent: skips if _legacy table already gone AND the new
#  table has the expected column count.
# ═══════════════════════════════════════════════════════════════

# Live columns for signal_scans (v15.2.5 — dead v13 fields removed)
_SCAN_LIVE_COLS = [
    "timestamp TEXT NOT NULL",
    "session TEXT", "dte INTEGER", "atm_strike INTEGER", "spot REAL",
    "direction TEXT", "entry_price REAL",
    # v15.2 indicator fields (populated by engine + LAB)
    "ema9_high REAL DEFAULT 0", "ema9_low REAL DEFAULT 0",
    "band_position TEXT DEFAULT ''", "body_pct REAL DEFAULT 0",
    "body_pct_3m REAL DEFAULT 0", "ema_spread_3m REAL DEFAULT 0",
    "mode_3m TEXT DEFAULT ''",
    # v15.2 straddle + VWAP (display-only after Fix 5)
    "straddle_delta REAL DEFAULT 0", "straddle_period TEXT DEFAULT ''",
    "atm_strike_used INTEGER DEFAULT 0", "band_width REAL DEFAULT 0",
    "spot_vwap REAL DEFAULT 0", "spot_vs_vwap REAL DEFAULT 0",
    "vwap_bonus TEXT DEFAULT ''",
    # Market context
    "vix REAL DEFAULT 0", "spot_rsi_3m REAL DEFAULT 0",
    "spot_ema_spread_3m REAL DEFAULT 0", "spot_regime TEXT DEFAULT ''",
    "spot_gap REAL DEFAULT 0", "bias TEXT DEFAULT ''",
    "hourly_rsi REAL DEFAULT 0",
    # Result
    "fired TEXT DEFAULT '0'", "trade_taken INTEGER DEFAULT 0",
    "reject_reason TEXT DEFAULT ''",
    # Forward fill (populated EOD)
    "fwd_3c REAL", "fwd_5c REAL", "fwd_10c REAL",
    "fwd_outcome TEXT DEFAULT ''",
]

# Live columns for trades (v15.2.5 — dead v13 fields removed)
_TRADES_LIVE_COLS = [
    "date TEXT NOT NULL", "entry_time TEXT", "exit_time TEXT",
    "symbol TEXT", "direction TEXT", "strike INTEGER",
    "entry_price REAL", "exit_price REAL",
    "pnl_pts REAL", "pnl_rs REAL", "gross_pnl_rs REAL DEFAULT 0",
    "net_pnl_rs REAL DEFAULT 0",
    "peak_pnl REAL", "trough_pnl REAL",
    "exit_reason TEXT", "exit_phase INTEGER DEFAULT 1",
    "dte INTEGER", "candles_held INTEGER", "session TEXT",
    "sl_pts REAL DEFAULT 0", "bias TEXT DEFAULT ''",
    "vix_at_entry REAL DEFAULT 0", "hourly_rsi REAL DEFAULT 0",
    "entry_mode TEXT DEFAULT ''",
    # Charges
    "brokerage REAL DEFAULT 0", "stt REAL DEFAULT 0",
    "exchange_charges REAL DEFAULT 0", "gst REAL DEFAULT 0",
    "stamp_duty REAL DEFAULT 0", "total_charges REAL DEFAULT 0",
    "num_exit_orders INTEGER DEFAULT 1", "qty_exited INTEGER DEFAULT 0",
    "entry_slippage REAL DEFAULT 0", "exit_slippage REAL DEFAULT 0",
    "lot_id TEXT DEFAULT 'ALL'",
    # v15.2 entry/exit context
    "entry_ema9_high REAL DEFAULT 0", "entry_ema9_low REAL DEFAULT 0",
    "exit_ema9_high REAL DEFAULT 0", "exit_ema9_low REAL DEFAULT 0",
    "entry_band_position TEXT DEFAULT ''",
    "exit_band_position TEXT DEFAULT ''",
    "entry_body_pct REAL DEFAULT 0",
    "entry_straddle_delta REAL DEFAULT 0",
    "entry_straddle_threshold REAL DEFAULT 0",
    "entry_straddle_period TEXT DEFAULT ''",
    "entry_straddle_info TEXT DEFAULT ''",
    "entry_atm_strike INTEGER DEFAULT 0",
    "entry_band_width REAL DEFAULT 0",
    "entry_spot_vwap REAL DEFAULT 0",
    "entry_spot_vs_vwap REAL DEFAULT 0",
    "entry_vwap_bonus TEXT DEFAULT ''",
]


def _col_name(col_def):
    return col_def.strip().split()[0]


def _backup_db():
    """Back up DB before schema migration. Returns backup path."""
    import shutil
    backup = DB_PATH + ".backup_" + date.today().strftime("%Y%m%d")
    if not os.path.isfile(backup):
        shutil.copy2(DB_PATH, backup)
        logger.info("[DB] Migration backup created: " + backup)
    return backup


def _migrate_table(conn, table, live_cols_defs):
    """Migrate a table by creating a new version with only the live columns
    and copying data from the existing table. Idempotent."""
    from datetime import date as _d
    live_names = [_col_name(c) for c in live_cols_defs]
    legacy_name = table + "_legacy"

    # Check: does the legacy table already exist? If so, we crashed
    # mid-migration last time. Drop the new table (if partial) and redo.
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if legacy_name in tables and table in tables:
        # Both exist — previous migration was partial. Drop the new one
        # and re-run from the rename step.
        conn.execute("DROP TABLE IF EXISTS " + table)
        tables.discard(table)
    if legacy_name in tables and table not in tables:
        # Normal: legacy exists, new doesn't. Create new + copy.
        pass
    elif table in tables and legacy_name not in tables:
        # First migration: rename current → legacy.
        existing_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(" + table + ")").fetchall()}
        # Idempotent: if the existing table already has ONLY the live
        # columns (±2 tolerance for newly-added cols), skip migration.
        dead = existing_cols - set(live_names)
        if len(dead) <= 2:
            logger.info("[DB] " + table + " already clean ("
                        + str(len(existing_cols)) + " cols, "
                        + str(len(dead)) + " extra). Migration skipped.")
            return
        conn.execute("ALTER TABLE " + table + " RENAME TO " + legacy_name)
        logger.info("[DB] Renamed " + table + " → " + legacy_name
                    + " (had " + str(len(existing_cols)) + " cols, "
                    + str(len(dead)) + " dead)")
    else:
        logger.info("[DB] " + table + " migration: nothing to do")
        return

    # Create new table with live columns only.
    col_defs = ", ".join(live_cols_defs)
    conn.execute("CREATE TABLE IF NOT EXISTS " + table
                 + " (" + col_defs + ")")

    # INSERT SELECT: copy only columns that exist in BOTH legacy and new.
    legacy_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(" + legacy_name + ")").fetchall()}
    common = [c for c in live_names if c in legacy_cols]
    common_str = ", ".join(common)
    conn.execute("INSERT OR IGNORE INTO " + table
                 + " (" + common_str + ") SELECT " + common_str
                 + " FROM " + legacy_name)
    n_migrated = conn.execute(
        "SELECT COUNT(*) FROM " + table).fetchone()[0]
    n_legacy = conn.execute(
        "SELECT COUNT(*) FROM " + legacy_name).fetchone()[0]

    # Drop legacy only if counts match (data integrity check).
    if n_migrated >= n_legacy:
        conn.execute("DROP TABLE " + legacy_name)
        logger.info("[DB] " + table + " migration complete: "
                    + str(n_migrated) + " rows, "
                    + str(len(live_names)) + " cols (was "
                    + str(len(legacy_cols)) + ")")
    else:
        logger.error("[DB] " + table + " migration MISMATCH: "
                     + str(n_migrated) + " vs " + str(n_legacy)
                     + " rows. Legacy table preserved for manual review: "
                     + legacy_name)


def migrate_schema_v15():
    """BUG-N6 + BUG-N7: one-shot migration that drops dead v13 columns
    from signal_scans and trades. Call from init_db() gated by a
    version check. Idempotent — running twice is a no-op if the first
    run completed."""
    if not os.path.isfile(DB_PATH):
        return
    try:
        _backup_db()
    except Exception as e:
        logger.error("[DB] Migration backup failed: " + str(e)
                     + " — aborting migration for safety")
        return
    conn = get_conn()
    try:
        _migrate_table(conn, "signal_scans", _SCAN_LIVE_COLS)
        _migrate_table(conn, "trades", _TRADES_LIVE_COLS)
        conn.commit()
        # Re-create indexes that were dropped with the old tables.
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scans_ts "
                         "ON signal_scans(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scans_dir "
                         "ON signal_scans(direction, fired)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scans_date "
                         "ON signal_scans(date(timestamp))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_date "
                         "ON trades(date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_dir "
                         "ON trades(direction, date)")
            conn.commit()
        except Exception as _ie:
            logger.warning("[DB] Index recreation: " + str(_ie))
    except Exception as e:
        logger.error("[DB] Schema migration error: " + str(e))


def migrate_schema_v16():
    """v16.0: drop straddle_threshold from signal_scans + clean up
    any leftover _legacy tables. Stamps schema_meta version=16."""
    if not os.path.isfile(DB_PATH):
        return
    conn = get_conn()
    try:
        for tbl in ("signal_scans_legacy", "trades_legacy"):
            try:
                conn.execute("DROP TABLE IF EXISTS " + tbl)
            except Exception:
                pass
        live = {r[1] for r in conn.execute("PRAGMA table_info(signal_scans)")}
        if "straddle_threshold" in live:
            new_cols = [c for c in _SCAN_LIVE_COLS
                        if _col_name(c) != "straddle_threshold"]
            _migrate_table(conn, "signal_scans", new_cols)
            logger.info("[DB] v16: dropped straddle_threshold from signal_scans")
        conn.execute("INSERT OR REPLACE INTO schema_meta VALUES ('version', '16')")
        conn.commit()
        logger.info("[DB] Schema version stamped: 16")
    except Exception as e:
        logger.warning("[DB] v16 migration error: " + str(e))


from datetime import date

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
        _report_db_error("Insert " + table, e)


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
        _report_db_error("Insert many " + table, e)


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
    "volume_ratio", "ema9_high", "ema9_low",   # v15.0 bands
    "iv_pct", "delta", "gamma", "theta", "vega",
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

# v15.2.5 BUG-N6: live columns only. Dead v13 fields (rsi_1m, body_pct_1m,
# vol_ratio_1m, rsi_rising_1m, spread_1m, rsi_3m, conditions_3m, score,
# iv_pct, delta, straddle_decay_pct, straddle_threshold, near_fib_level,
# fib_distance) removed after schema migration in migrate_schema_v15().
_SCAN_FIELDS = [
    "timestamp", "session", "dte", "atm_strike", "spot",
    "direction", "entry_price",
    "ema9_high", "ema9_low", "band_position", "body_pct",
    "body_pct_3m", "ema_spread_3m", "mode_3m",
    "straddle_delta", "straddle_period",
    "atm_strike_used", "band_width",
    "spot_vwap", "spot_vs_vwap", "vwap_bonus",
    "vix", "spot_rsi_3m", "spot_ema_spread_3m", "spot_regime",
    "spot_gap", "bias", "hourly_rsi",
    "fired", "trade_taken", "reject_reason",
    "fwd_3c", "fwd_5c", "fwd_10c", "fwd_outcome",
]

def insert_scan(row):
    _insert("signal_scans", row, _SCAN_FIELDS)

def insert_scan_many(rows):
    _insert_many("signal_scans", rows, _SCAN_FIELDS)


# ── Trades ──

# v15.2.5 BUG-N7: live columns only. Dead v13 fields (mode, score,
# iv_at_entry, regime, spread_1m, spread_3m, delta_at_entry,
# straddle_decay, signal_price, bonus_*, momentum_pts, rsi_rising,
# spot_confirms, spot_move, spike_ratio, other_falling, other_move,
# momentum_tf) removed after schema migration in migrate_schema_v15().
_TRADE_FIELDS = [
    "date", "entry_time", "exit_time", "symbol", "direction", "strike",
    "entry_price", "exit_price", "pnl_pts", "pnl_rs",
    "gross_pnl_rs", "net_pnl_rs",
    "peak_pnl", "trough_pnl", "exit_reason", "exit_phase",
    "dte", "candles_held", "session", "sl_pts",
    "bias", "vix_at_entry", "hourly_rsi", "entry_mode",
    "brokerage", "stt", "exchange_charges", "gst", "stamp_duty",
    "total_charges", "num_exit_orders", "qty_exited",
    "entry_slippage", "exit_slippage", "lot_id",
    # v15.2 entry/exit context
    "entry_ema9_high", "entry_ema9_low",
    "exit_ema9_high", "exit_ema9_low",
    "entry_band_position", "exit_band_position",
    "entry_body_pct",
    "entry_straddle_delta", "entry_straddle_threshold",
    "entry_straddle_period", "entry_straddle_info",
    "entry_atm_strike", "entry_band_width",
    "entry_spot_vwap", "entry_spot_vs_vwap", "entry_vwap_bonus",
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
        _report_db_error("Update scan fwd", e)


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
        _report_db_error("Update opt1m fwd", e)


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
        _report_db_error("Update opt3m fwd", e)


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
        _report_db_error("Query error", e)
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
        _report_db_error("create_token", e)
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
            _report_db_error("Cleanup " + t, e)
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
        _report_db_error("Vacuum error", e)


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


# ═══════════════════════════════════════════════════════════════
#  VALIDATION SECTION (formerly VRL_VALIDATE.py)
#  20 live market validation checks. Run on every entry + exit.
#  Import fix: VRL_CHARGES merged into VRL_ENGINE — use
#    from VRL_ENGINE import calculate_charges
# ═══════════════════════════════════════════════════════════════

import csv as _csv_mod
import json as _json_mod
import sqlite3 as _sqlite3_mod
from datetime import date as _val_date, datetime as _val_datetime

import VRL_DATA as D
import VRL_CONFIG as CFG

# v15.2 — single live entry mode
VALID_ENTRY_MODES = ("EMA9_BREAKOUT",)

# Old strings that may still appear in historical trades; do NOT raise
# errors on them, but they're not allowed as fresh entries either.
LEGACY_MODES = (
    "FAST", "CONFIRMED", "MOMENTUM", "3MIN",
    "BOTH", "EMA", "MINIMAL", "EXPIRY_BREAKOUT", "CONVICTION",
)

# v15.2 — exit reasons accepted for live + historical trades
VALID_EXIT_REASONS = (
    # v16.2 primary exits
    "EMERGENCY_SL", "STALE_ENTRY", "EOD_EXIT",
    "VELOCITY_STALL", "VISHAL_TRAIL", "STAGE2_UNCONFIRMED",
    # v16.0 historical
    "EMA1M_BREAK", "PROFIT_RATCHET",
    # v15.x historical
    "EMA9_LOW_BREAK", "BREAKEVEN_LOCK", "TRAIL_FLOOR",
    # safety / manual
    "MARKET_CLOSE", "MANUAL", "FORCE_EXIT", "CIRCUIT_BREAKER_EXIT",
    "EOD_SAFETY",
    # historical (back-compat)
    "HARD_SL", "PROFIT_FLOOR", "FLOOR_SL",
    "RSI_BLOWOFF", "RSI_SPIKE", "ATR_TRAIL", "SCOUT_SL",
    "CANDLE_SL", "DIVERGENCE_EXIT", "WEAK_SL",
)

# ── Validation logger ─────────────────────────────────────────
_VAL_LOG_DIR  = os.path.expanduser("~/logs")
_VAL_LOG_PATH = os.path.join(_VAL_LOG_DIR, "validation.log")
os.makedirs(_VAL_LOG_DIR, exist_ok=True)

val_logger = logging.getLogger("vrl_validation")
if not val_logger.handlers:
    val_logger.setLevel(logging.INFO)
    _fh = logging.FileHandler(_VAL_LOG_PATH)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    val_logger.addHandler(_fh)
    val_logger.propagate = False

# ── Paths ─────────────────────────────────────────────────────
_VAL_DB_PATH    = DB_PATH  # reuse VRL_DB constant
_VAL_CSV_PATH   = os.path.expanduser("~/lab_data/vrl_trade_log.csv")
_VAL_DASH_PATH  = os.path.join(D.STATE_DIR, "vrl_dashboard.json")
_VAL_TOKEN_PATH = D.TOKEN_FILE_PATH


def _safe_run(fn, *a, **kw):
    """Run a check that might fail. Returns (ok, error_msg). Never raises."""
    try:
        return True, fn(*a, **kw)
    except Exception as e:
        return False, str(e)


# ═══════════════════════════════════════════════════════════════
#  ENTRY VALIDATION — 10 checks, runs after every entry
# ═══════════════════════════════════════════════════════════════

def validate_entry(state, entry_result, kite=None):
    """Run 10 checks after every entry. Returns list of failure strings."""
    failures = []

    # CHECK 1: Entry price matches signal price (slippage < 3pts)
    try:
        signal_price = float(entry_result.get("entry_price", 0) or 0)
        fill_price   = float(state.get("entry_price", 0) or 0)
        if signal_price > 0 and fill_price > 0:
            diff = abs(signal_price - fill_price)
            if diff > 3:
                failures.append("ENTRY_SLIPPAGE: signal=" + str(signal_price)
                                + " fill=" + str(fill_price)
                                + " diff=" + str(round(diff, 2)))
    except Exception as e:
        failures.append("CHECK1_ERR: " + str(e))

    # CHECK 2: State is consistent — in_trade, symbol, token, entry_price
    if not state.get("in_trade"):
        failures.append("STATE: in_trade=False after entry")
    if not state.get("symbol"):
        failures.append("STATE: symbol is empty after entry")
    if not state.get("token"):
        failures.append("STATE: token is None after entry")
    if float(state.get("entry_price", 0) or 0) <= 0:
        failures.append("STATE: entry_price=0 after entry")

    # CHECK 3: SL is set correctly (entry - hard_sl)
    try:
        hard_sl     = CFG.get().get("exit", {}).get("hard_sl", 12)
        expected_sl = round(float(state.get("entry_price", 0) or 0) - hard_sl, 2)
        actual_sl   = round(float(state.get("phase1_sl", 0) or 0), 2)
        if abs(expected_sl - actual_sl) > 0.5:
            failures.append("SL_MISMATCH: expected=" + str(expected_sl)
                            + " actual=" + str(actual_sl))
    except Exception as e:
        failures.append("CHECK3_ERR: " + str(e))

    # CHECK 4: Qty is correct (lots × lot_size)
    try:
        lot_count    = CFG.get().get("lots", {}).get("count", 2)
        lot_size_val = CFG.get().get("lots", {}).get("size", D.get_lot_size())
        expected_qty = lot_count * lot_size_val
        actual_qty   = int(state.get("qty", 0) or 0)
        if actual_qty == 0:
            failures.append("QTY: actual=0 (no fill)")
        elif actual_qty > expected_qty:
            failures.append("QTY: actual=" + str(actual_qty)
                            + " > expected=" + str(expected_qty))
    except Exception as e:
        failures.append("CHECK4_ERR: " + str(e))

    # CHECK 5: Exchange SL order placed (live mode only)
    if not D.PAPER_MODE:
        if not state.get("_sl_order_id"):
            failures.append("EXCHANGE_SL: no SL order placed (live mode)")

    # CHECK 6: Entry mode is valid
    mode_val = state.get("entry_mode", "") or state.get("mode", "")
    if mode_val and mode_val not in VALID_ENTRY_MODES and mode_val not in LEGACY_MODES:
        failures.append("ENTRY_MODE: invalid mode=" + str(mode_val))

    # CHECK 7: Direction matches option type in symbol
    direction = state.get("direction", "")
    symbol    = state.get("symbol", "")
    if direction == "CE" and symbol.endswith("PE"):
        failures.append("DIRECTION: CE but symbol ends with PE: " + symbol)
    if direction == "PE" and symbol.endswith("CE"):
        failures.append("DIRECTION: PE but symbol ends with CE: " + symbol)

    # CHECK 8: Strike is in symbol
    try:
        strike = int(state.get("strike", 0) or 0)
        if strike and str(strike) not in symbol:
            failures.append("STRIKE: " + str(strike) + " not in " + symbol)
    except Exception as e:
        failures.append("CHECK8_ERR: " + str(e))

    # CHECK 9: WebSocket has live LTP for the token
    try:
        token  = state.get("token", 0)
        ws_ltp = D.get_ltp(token) if token else 0
        if ws_ltp <= 0:
            failures.append("WEBSOCKET: no LTP for token=" + str(token))
    except Exception as e:
        failures.append("CHECK9_ERR: " + str(e))

    # CHECK 10: Trade log file is writeable
    try:
        log_dir = os.path.dirname(_VAL_CSV_PATH)
        if not os.path.isdir(log_dir):
            failures.append("TRADE_LOG: dir missing " + log_dir)
        elif not os.access(log_dir, os.W_OK):
            failures.append("TRADE_LOG: dir not writeable " + log_dir)
    except Exception as e:
        failures.append("CHECK10_ERR: " + str(e))

    _log_result("ENTRY", 10, failures)
    return failures


# ═══════════════════════════════════════════════════════════════
#  EXIT VALIDATION — 10 checks, runs after every exit
# ═══════════════════════════════════════════════════════════════

def validate_exit(state, exit_pnl, exit_price, exit_reason,
                  entry_price, qty_exited, kite=None):
    """Run 10 checks after every exit. Returns list of failure strings."""
    failures = []
    today    = _val_date.today().isoformat()

    # CHECK 11: PNL calculation matches (exit - entry)
    try:
        expected_pnl = round(float(exit_price) - float(entry_price), 2)
        if abs(expected_pnl - float(exit_pnl)) > 0.5:
            failures.append("PNL_CALC: expected=" + str(expected_pnl)
                            + " actual=" + str(exit_pnl))
    except Exception as e:
        failures.append("CHECK11_ERR: " + str(e))

    # CHECK 12: Exit reason is in the known set
    if exit_reason not in VALID_EXIT_REASONS:
        failures.append("EXIT_REASON: unknown=" + str(exit_reason))

    # CHECK 13: Charges calculator returns sensible numbers
    # FIX: VRL_CHARGES merged into VRL_ENGINE — import from there
    try:
        from VRL_ENGINE import calculate_charges
        charges = calculate_charges(
            float(entry_price), float(exit_price), int(qty_exited), 1)
        if charges.get("total_charges", 0) <= 0:
            failures.append("CHARGES: zero or negative")
    except ImportError:
        pass  # Charges module not present — skip silently
    except Exception as ce:
        failures.append("CHARGES_ERR: " + str(ce))

    # CHECK 14: DB trade row exists for today
    try:
        if os.path.isfile(_VAL_DB_PATH):
            conn = _sqlite3_mod.connect(_VAL_DB_PATH, timeout=5)
            try:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE date=?", (today,)
                ).fetchone()[0]
            finally:
                conn.close()
            if cnt < 1:
                failures.append("DB: no trade row for today")
    except Exception as e:
        failures.append("CHECK14_ERR: " + str(e))

    # CHECK 15: CSV trade row exists for today
    csv_count = 0
    try:
        if os.path.isfile(_VAL_CSV_PATH):
            with open(_VAL_CSV_PATH) as f:
                csv_count = sum(1 for r in _csv_mod.DictReader(f)
                                if r.get("date") == today)
            if csv_count < 1:
                failures.append("CSV: no trade row for today")
    except Exception as e:
        failures.append("CHECK15_ERR: " + str(e))

    # CHECK 16: DB and CSV trade counts match
    try:
        if os.path.isfile(_VAL_DB_PATH) and os.path.isfile(_VAL_CSV_PATH):
            conn2 = _sqlite3_mod.connect(_VAL_DB_PATH, timeout=5)
            try:
                db_count = conn2.execute(
                    "SELECT COUNT(*) FROM trades WHERE date=?", (today,)
                ).fetchone()[0]
            finally:
                conn2.close()
            if db_count != csv_count:
                failures.append("SYNC: DB=" + str(db_count)
                                + " CSV=" + str(csv_count))
    except Exception as e:
        failures.append("CHECK16_ERR: " + str(e))

    # CHECK 17: State reset after a fully-closed exit
    if state.get("in_trade"):
        failures.append("STATE: in_trade=True after exit")
    if float(state.get("entry_price", 0) or 0) != 0:
        failures.append("STATE: entry_price not reset")
    if float(state.get("peak_pnl", 0) or 0) != 0:
        failures.append("STATE: peak_pnl not reset")

    # CHECK 18: Daily counters updated
    daily_trades = int(state.get("daily_trades", 0) or 0)
    if daily_trades < 1:
        failures.append("STATE: daily_trades=0 after exit")

    # CHECK 19: Exchange SL cancelled (live mode only)
    if not D.PAPER_MODE:
        sl_id = state.get("_sl_order_id")
        if sl_id and sl_id != "PAPER_SL":
            failures.append("EXCHANGE_SL: SL order_id=" + str(sl_id)
                            + " not cleared after exit")

    # CHECK 20: Dashboard JSON reflects the same trade count + PNL
    try:
        if os.path.isfile(_VAL_DASH_PATH):
            with open(_VAL_DASH_PATH) as f:
                dash = _json_mod.load(f)
            today_block = dash.get("today", {}) or {}
            dash_trades = int(today_block.get("trades", 0) or 0)
            if dash_trades != daily_trades:
                failures.append("DASHBOARD: trades=" + str(dash_trades)
                                + " state=" + str(daily_trades))
            try:
                tg_pnl   = round(float(state.get("daily_pnl", 0) or 0), 1)
                dash_pnl = round(float(today_block.get("pnl", 0) or 0), 1)
                if abs(tg_pnl - dash_pnl) > 1.0:
                    failures.append("ALIGN_PNL: state=" + str(tg_pnl)
                                    + " dashboard=" + str(dash_pnl))
            except Exception:
                pass
            try:
                tg_wins   = daily_trades - int(state.get("daily_losses", 0) or 0)
                dash_wins = int(today_block.get("wins", 0) or 0)
                if tg_wins != dash_wins:
                    failures.append("ALIGN_WINS: state=" + str(tg_wins)
                                    + " dashboard=" + str(dash_wins))
            except Exception:
                pass
    except Exception as e:
        failures.append("CHECK20_ERR: " + str(e))

    _log_result("EXIT ", 10, failures)
    return failures


# ═══════════════════════════════════════════════════════════════
#  /validate COMMAND — manual on-demand system check
# ═══════════════════════════════════════════════════════════════

def manual_validate(state):
    """
    Run a fresh ad-hoc validation. Returns dict:
        {"checks": [(name, ok, detail), ...], "passed": int, "total": int}
    """
    checks = []

    # 1. DB exists and has trades table
    try:
        if os.path.isfile(_VAL_DB_PATH):
            conn = _sqlite3_mod.connect(_VAL_DB_PATH, timeout=5)
            try:
                cnt = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            finally:
                conn.close()
            size_mb = round(os.path.getsize(_VAL_DB_PATH) / (1024 * 1024), 1)
            checks.append(("DB", True, str(cnt) + " trades, " + str(size_mb) + "MB"))
        else:
            checks.append(("DB", False, "missing " + _VAL_DB_PATH))
    except Exception as e:
        checks.append(("DB", False, str(e)))

    # 2. CSV exists
    try:
        if os.path.isfile(_VAL_CSV_PATH):
            with open(_VAL_CSV_PATH) as f:
                csv_rows = sum(1 for _ in _csv_mod.DictReader(f))
            checks.append(("CSV", True, str(csv_rows) + " rows"))
        else:
            checks.append(("CSV", False, "missing"))
    except Exception as e:
        checks.append(("CSV", False, str(e)))

    # 3. DB == CSV (TODAY only)
    try:
        today_iso = _val_date.today().isoformat()
        db_today  = -1
        csv_today = -1
        if os.path.isfile(_VAL_DB_PATH):
            conn = _sqlite3_mod.connect(_VAL_DB_PATH, timeout=5)
            try:
                db_today = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE date=?",
                    (today_iso,)).fetchone()[0]
            finally:
                conn.close()
        if os.path.isfile(_VAL_CSV_PATH):
            with open(_VAL_CSV_PATH) as f:
                csv_today = sum(1 for r in _csv_mod.DictReader(f)
                                if r.get("date") == today_iso)
        if db_today < 0 or csv_today < 0:
            checks.append(("DB = CSV (today)", False,
                           "db=" + str(db_today) + " csv=" + str(csv_today)))
        else:
            checks.append(("DB = CSV (today)", db_today == csv_today,
                           "db=" + str(db_today) + " csv=" + str(csv_today)))
    except Exception as e:
        checks.append(("DB = CSV (today)", False, str(e)))

    # 4. State sanity
    try:
        in_trade = bool(state.get("in_trade"))
        checks.append(("State", True,
                       "in_trade" if in_trade else "not in trade"))
    except Exception as e:
        checks.append(("State", False, str(e)))

    # 5. WebSocket tick freshness
    try:
        ws_ok = D.is_tick_live(D.NIFTY_SPOT_TOKEN)
        if not ws_ok and not D.is_market_open():
            checks.append(("WebSocket", True, "idle (market closed)"))
        else:
            checks.append(("WebSocket", ws_ok,
                           "connected" if ws_ok else "stale or closed"))
    except Exception as e:
        checks.append(("WebSocket", False, str(e)))

    # 6. Kite auth — token file present and dated today
    try:
        if os.path.isfile(_VAL_TOKEN_PATH):
            with open(_VAL_TOKEN_PATH) as f:
                tok = _json_mod.load(f)
            today_str = _val_date.today().isoformat()
            ok = tok.get("date") == today_str and bool(tok.get("access_token"))
            checks.append(("Kite auth", ok,
                           "valid" if ok else "stale (date=" + str(tok.get("date", "—")) + ")"))
        else:
            checks.append(("Kite auth", False, "no token file"))
    except Exception as e:
        checks.append(("Kite auth", False, str(e)))

    # 7. Dashboard JSON freshness
    try:
        if os.path.isfile(_VAL_DASH_PATH):
            age = int(_val_datetime.now().timestamp() - os.path.getmtime(_VAL_DASH_PATH))
            ok  = age < 300
            checks.append(("Dashboard", ok, "updated " + str(age) + "s ago"))
        else:
            checks.append(("Dashboard", False, "missing"))
    except Exception as e:
        checks.append(("Dashboard", False, str(e)))

    # 8. Spot LTP available
    try:
        spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        if spot_ltp > 0:
            checks.append(("Spot LTP", True, str(round(spot_ltp, 1))))
        elif not D.is_market_open():
            checks.append(("Spot LTP", True, "0 (market closed)"))
        else:
            checks.append(("Spot LTP", False, "0 — market open but no tick"))
    except Exception as e:
        checks.append(("Spot LTP", False, str(e)))

    # 9. Config version stamp
    try:
        ver = CFG.get().get("version", "—")
        checks.append(("Config", True, ver))
    except Exception as e:
        checks.append(("Config", False, str(e)))

    # 10. Daily reconcile — state vs CSV trade count
    try:
        today = _val_date.today().isoformat()
        csv_today = 0
        if os.path.isfile(_VAL_CSV_PATH):
            with open(_VAL_CSV_PATH) as f:
                csv_today = sum(1 for r in _csv_mod.DictReader(f)
                                if r.get("date") == today)
        state_today = int(state.get("daily_trades", 0) or 0)
        ok = csv_today == state_today
        checks.append(("Reconcile", ok,
                       "state=" + str(state_today) + " csv=" + str(csv_today)))
    except Exception as e:
        checks.append(("Reconcile", False, str(e)))

    passed = sum(1 for _n, ok, _d in checks if ok)
    return {"checks": checks, "passed": passed, "total": len(checks)}


# ═══════════════════════════════════════════════════════════════
#  INTERNAL — single-line summary log per validation run
# ═══════════════════════════════════════════════════════════════

def _log_result(phase: str, total: int, failures: list):
    passed = total - len(failures)
    if failures:
        msg = ("| " + phase + " | " + str(passed) + "/" + str(total) + " PASS"
               " | FAIL: " + "; ".join(failures))
    else:
        msg = "| " + phase + " | " + str(total) + "/" + str(total) + " PASS"
    try:
        val_logger.info(msg)
    except Exception:
        pass
    close()
