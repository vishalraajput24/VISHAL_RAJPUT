"""
One-time DB migration: add expiry column to option_3min.
expiry = next Tuesday (inclusive) from each row's date.
NIFTY50 weekly options expire every Tuesday (changed from Thursday,
effective September 2025 per NSE circular).

Run once:
    ~/kite_env/bin/python3 ~/VISHAL_RAJPUT/fix_db_expiry.py
"""
import sqlite3, os

DB = os.path.expanduser("~/lab_data/vrl_data.db")
con = sqlite3.connect(DB)
cur = con.cursor()

# Check if column already exists
cur.execute("PRAGMA table_info(option_3min)")
cols = [r[1] for r in cur.fetchall()]

if 'expiry' in cols:
    print("expiry column exists — resetting to recompute with Tuesday expiry...")
    con.execute("UPDATE option_3min SET expiry = NULL")
    con.commit()
else:
    print("Adding expiry column...")
    con.execute("ALTER TABLE option_3min ADD COLUMN expiry TEXT")

# SQLite strftime: %w = 0(Sun) 1(Mon) 2(Tue) 3(Wed) 4(Thu) 5(Fri) 6(Sat)
# Next Tuesday inclusive = timestamp_date + ((2 - weekday + 7) % 7) days
print("Populating expiry dates (next Tuesday for each row)...")
con.execute("""
    UPDATE option_3min
    SET expiry = date(
        timestamp,
        '+' || ((2 - cast(strftime('%w', timestamp) as integer) + 7) % 7) || ' days'
    )
    WHERE expiry IS NULL
""")
rows_updated = con.execute("SELECT changes()").fetchone()[0]
print(f"Updated {rows_updated:,} rows")

con.commit()

# Verify — show date → expiry mapping with day names
print("\n=== Verification: date → expiry mapping ===")
cur.execute("""
    SELECT date(timestamp) as dt,
           strftime('%w', timestamp) as wday,
           expiry,
           COUNT(*) as n
    FROM option_3min
    GROUP BY date(timestamp)
    ORDER BY date(timestamp)
""")
day_names = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat']
print(f"  {'Date':12} {'Day':4} {'Expiry':12} {'Rows':>6}")
print(f"  {'─'*38}")
for r in cur.fetchall():
    wday = int(r[1])
    flag = " ← EXPIRY DAY" if r[0] == r[2] else ""
    print(f"  {r[0]:12} {day_names[wday]:4} {r[2]:12} {r[3]:6}{flag}")

# Check price discontinuity is now within contracts
print("\n=== 23800 CE — expiry-split price check ===")
cur.execute("""
    SELECT expiry, date(timestamp) as dt, MIN(close), MAX(close), AVG(close), COUNT(*)
    FROM option_3min
    WHERE strike=23800 AND type='CE'
    GROUP BY expiry, date(timestamp)
    ORDER BY expiry, date(timestamp)
""")
last_expiry = None
for r in cur.fetchall():
    if r[0] != last_expiry:
        print(f"\n  Contract expiry={r[0]}:")
        last_expiry = r[0]
    print(f"    {r[1]}  min={r[2]:.1f} max={r[3]:.1f} avg={r[4]:.1f} n={r[5]}")

# Add index for faster groupby queries
print("\n=== Adding index on (strike, type, expiry) ===")
cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_option_3min_expiry
    ON option_3min (strike, type, expiry, timestamp)
""")
con.commit()
print("Index created.")

con.close()
print("\nDone. Run backtests again — EMA will now reset correctly per contract.")
