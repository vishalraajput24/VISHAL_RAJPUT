# 📘 Multibagger Screener — User Manual
**Version**: 1.0 | **Author**: Claude (for Vishal Rajput) | **Date**: 2026-05-26

---

## Table of Contents
1. [What This Does](#1-what-this-does)
2. [Setup — First Time](#2-setup--first-time)
3. [How to Run](#3-how-to-run)
4. [Understanding the Output](#4-understanding-the-output)
5. [The 7 Filters Explained](#5-the-7-filters-explained)
6. [Tuning Filters for Your Style](#6-tuning-filters-for-your-style)
7. [Kite Connect Integration](#7-kite-connect-integration)
8. [Adding Your Own Stocks](#8-adding-your-own-stocks)
9. [After the Screener — What Next](#9-after-the-screener--what-next)
10. [Troubleshooting](#10-troubleshooting)
11. [Important Warnings](#11-important-warnings)

---

## 1. What This Does

This screener automatically scans 150+ NSE stocks every time you run it and finds stocks that have multibagger potential — stocks that can return 3x, 5x, 10x or more over 3–10 years.

**Data sources used:**
- **Screener.in** — fundamental data (ROE, ROCE, debt, growth, promoter holding)
- **Kite Connect API** — live price and volume (optional)

**What it produces:**
- Console table of all stocks that passed filters — ranked by quality score
- CSV file with all 150 stocks — passed and failed — with every metric

**Time to run:** ~8 minutes for 150 stocks (respects Screener.in rate limits)

---

## 2. Setup — First Time

### Step 1: Install Python packages
```bash
pip install requests beautifulsoup4 pandas tabulate colorama kiteconnect
```

### Step 2: Navigate to screener folder
```bash
cd ~/VISHAL_RAJPUT/screener
```

### Step 3: Verify installation
```bash
python3 -c "import requests, pandas, bs4, tabulate, colorama; print('All OK')"
```

### Step 4 (Optional): Get Screener.in session cookie
Without login — you get most data (ROE, ROCE, growth, D/E, promoter).
With login — you may get additional pre-computed growth rates.

To get cookie:
1. Open Chrome → screener.in → Login
2. Press F12 → Application → Cookies → screener.in
3. Copy value of `sessionid`
4. Paste into `SCREENER_SESSION_COOKIE = "paste_here"` in the script

---

## 3. How to Run

### Basic Run (no Kite, no login needed)
```bash
cd ~/VISHAL_RAJPUT/screener
python3 multibagger_screener.py
```

### Run with Kite live price
```bash
# Step 1: Get today's access token (do this once every morning)
python3 multibagger_screener.py --login
# Follow the link, login, paste request_token, copy the access token

# Step 2: Open the script, paste the token
# KITE_ACCESS_TOKEN = "paste_token_here"
# USE_KITE = True

# Step 3: Run
python3 multibagger_screener.py
```

### Scan fewer stocks (for quick test)
Open script → change:
```python
MAX_STOCKS_TO_SCAN = 20   # scan only first 20 stocks
```

---

## 4. Understanding the Output

### Console Output — While Running
```
[  1/150] BAJFINANCE       ❌ FAIL          ₹579077cr  ROE=18%  PG=22%  score=8
[  2/150] CHOLAFIN         ❌ FAIL          ₹71000cr   ROE=19%  PG=25%  score=6
[  3/150] PERSISTENT       ✅ BUY           ₹45000cr   ROE=27%  PG=26%  score=11
```

### Final Table — Passed Stocks
```
Grade          | Symbol    | Name              | Price  | MCap    | ROE   | ROCE  | D/E  | Promoter | ProfitGr | PEG  | Score
🔥 STRONG BUY | TRENTLTD  | Trent Ltd         | ₹5200  | ₹18000cr| 32%   | 28%   | 0.10 | 68%      | 35%      | 1.2  | 15
✅ BUY        | PERSISTENT| Persistent Systems| ₹4100  | ₹32000cr| 27%   | 34%   | 0.06 | 30%      | 26%      | 1.1  | 11
```

### Grade System

| Grade | Score | What It Means |
|-------|-------|--------------|
| 🔥 STRONG BUY | ≥14/17 | Exceptional — all pillars strong |
| ✅ BUY | ≥11/17 | Good — most pillars strong |
| ⚠️ WATCH | ≥8/17 | Decent — some weakness, monitor |
| ❌ FAIL | <8 or failed core filter | Did not pass mandatory filters |

### CSV Output
Saved to: `~/VISHAL_RAJPUT/screener/multibagger_YYYYMMDD.csv`

Columns in CSV:
| Column | What It Is |
|--------|-----------|
| Symbol | NSE ticker |
| Name | Company full name |
| Passed | TRUE/FALSE — passed all filters |
| Score | 0–17 quality score |
| Grade | 🔥/✅/⚠️/❌ |
| ROE_% | Return on Equity |
| ROCE_% | Return on Capital Employed |
| DebtEquity | Total debt / Equity |
| PromoterHolding | Promoter % (latest quarter) |
| Pledge_% | Pledged shares % |
| SalesGrowth3Y | Revenue CAGR 3 years |
| ProfitGrowth3Y | PAT CAGR 3 years |
| PEG | PE / Growth ratio |
| FCF_Cr | Free Cash Flow (₹ crore) |
| FailReasons | Why it failed (if it did) |

---

## 5. The 7 Filters Explained

### Filter 1 — Business Quality (ROE + ROCE)
```
ROE  > 20%  — How much profit per ₹100 of shareholder money
ROCE > 20%  — How much profit per ₹100 of total capital (debt+equity)
```
**Why 20%?** A business earning 20%+ on capital can reinvest profits and compound. Banks give 7%. The gap IS the multibagger engine.

**Exception**: NBFCs (Bajaj Finance, Chola) — ROE can be 18–22% with high D/E. That's structural. Lower threshold to 15% for NBFC universe.

---

### Filter 2 — Growth (Sales + Profit 3Y CAGR)
```
Sales  growth 3Y > 15%
Profit growth 3Y > 15%
```
**Why both?** Revenue growth without profit growth = competitive pressure eroding margins. Profit growing faster than sales = improving efficiency. Ideal = profit growing 1.5x–2x sales growth rate.

---

### Filter 3 — Financial Health (D/E)
```
Debt / Equity < 0.5
```
**Why?** A debt-heavy company is a ticking bomb in recessions. When interest rates rise, margins collapse. Zero-debt companies can self-fund growth — no dependency on markets.

**Exception**: NBFCs and banks — D/E of 5–8 is normal (they borrow to lend). For them, look at NIM and NPA instead.

---

### Filter 4 — Management (Promoter + Pledge)
```
Promoter holding > 45%
Pledged shares   < 5%
```
**Why promoter > 45%?** Promoter has majority skin in the game. They can't easily dilute or exit. They win when stock goes up — aligned with you.

**Why pledge < 5%?** Pledged shares = promoter borrowed money against their shares. If stock falls, lender sells shares → stock crashes more → death spiral. AVOID.

**Red flag**: Promoter holding falling quarter after quarter = promoter selling. Run.

---

### Filter 5 — Profitability (OPM + FCF)
```
Operating Margin > 10%
Free Cash Flow   > 0 (positive)
```
**OPM > 10%**: Below 10% means thin margins — one bad quarter wipes profits. High OPM companies (20%+) have pricing power (moat).

**FCF positive**: PAT can be manipulated. Cash flow cannot. If company shows ₹100 cr profit but FCF is negative — profits are fake or all going to working capital. FCF = real money.

---

### Filter 6 — Valuation (P/E + PEG)
```
P/E  < 80
PEG  < 2.0
```
**P/E**: Don't overpay. P/E > 80 means market has already priced in 10 years of perfection.

**PEG ratio = P/E ÷ Growth rate**:
- PEG < 1.0 = Growing faster than you're paying for ✅
- PEG 1.0–1.5 = Fairly valued ✅
- PEG 1.5–2.0 = Slightly expensive ⚠️
- PEG > 2.0 = Overvalued ❌

**Example**: Stock at P/E 40, growing 40% → PEG = 1.0 (fair). Stock at P/E 15, growing 5% → PEG = 3.0 (expensive for the growth).

---

### Filter 7 — Size Sweet Spot
```
Market Cap: ₹200 cr – ₹30,000 cr
```
**Why ₹200 cr min?** Below this = micro-cap. Illiquid, manipulated, no institutional coverage.

**Why ₹30,000 cr max?** Above this = already large-cap. 10x from ₹50,000 cr = ₹5 lakh cr company — very hard. Best multibaggers come from ₹500–₹10,000 cr range (small/mid cap).

**Adjust** mcap_max_cr to 50,000 if you want mid-to-large cap stocks.

---

## 6. Tuning Filters for Your Style

Open `multibagger_screener.py` → find the `FILTERS` block → change values:

### For Aggressive / Small-cap Focus
```python
FILTERS = {
    "roe_min"          : 18.0,   # slightly relaxed
    "roce_min"         : 18.0,   # slightly relaxed
    "sales_growth_3y"  : 20.0,   # higher growth required
    "profit_growth_3y" : 20.0,   # higher growth required
    "debt_equity_max"  : 0.3,    # stricter — want zero debt
    "promoter_min"     : 50.0,   # majority holding
    "pledge_max"       : 2.0,    # almost zero pledge
    "mcap_min_cr"      : 200,
    "mcap_max_cr"      : 5000,   # small cap only
    "pe_max"           : 60.0,
    "peg_max"          : 1.5,    # stricter valuation
}
```

### For Conservative / Large-cap
```python
FILTERS = {
    "roe_min"          : 20.0,
    "roce_min"         : 20.0,
    "sales_growth_3y"  : 12.0,   # lower growth ok for large caps
    "profit_growth_3y" : 12.0,
    "debt_equity_max"  : 0.5,
    "promoter_min"     : 35.0,   # large caps have lower promoter
    "pledge_max"       : 5.0,
    "mcap_min_cr"      : 5000,   # mid to large only
    "mcap_max_cr"      : 200000,
    "pe_max"           : 50.0,
    "peg_max"          : 2.0,
}
```

### For NBFC / Financial Sector
```python
FILTERS = {
    "roe_min"          : 15.0,   # NBFCs: 15-20% is good
    "roce_min"         : 8.0,    # NBFCs have lower ROCE
    "debt_equity_max"  : 8.0,    # NBFCs borrow to lend — high D/E is normal
    "promoter_min"     : 35.0,
    "pledge_max"       : 5.0,
    "opm_min"          : 0.0,    # NBFCs don't report OPM same way
    "fcf_positive"     : False,  # NBFCs always have negative FCF (loans are outflows)
}
```

---

## 7. Kite Connect Integration

### What Kite Adds
- **Live price** at the moment you run the screener (instead of previous close from Screener.in)
- **Volume** — useful to check if there's unusual activity
- Can be extended to get **52-week high/low**, **historical data**

### Setup Steps

**Step 1**: Create Kite Connect app
- Go to: https://kite.trade/connect/login
- Create developer account → create new app
- Note down `api_key` and `api_secret`

**Step 2**: Set redirect URL
- In your app settings → set redirect URL to: `http://127.0.0.1`

**Step 3**: Paste keys in script
```python
KITE_API_KEY    = "your_api_key"
KITE_API_SECRET = "your_api_secret"
USE_KITE        = True
```

**Step 4**: Get access token (every morning before market open)
```bash
python3 multibagger_screener.py --login
```
- Browser opens → Login with Zerodha
- You'll be redirected to 127.0.0.1 with URL like:
  `http://127.0.0.1/?request_token=XXXXX&action=login&status=success`
- Copy the `XXXXX` part → paste into terminal
- You'll get the access token → paste into `KITE_ACCESS_TOKEN` in script

**Step 5**: Run with live data
```bash
python3 multibagger_screener.py
```

### Cost of Kite Connect API
- Kite Connect API: ₹2,000/month
- If you only use it for this screener: Not worth it
- If you already have it for algo trading: Enable it for free

---

## 8. Adding Your Own Stocks

### Option A: Add individual stocks
Open script → find `UNIVERSE` list → add your symbols:
```python
UNIVERSE = [
    # Your additions
    "TATAELXSI", "ASTRAL", "POLYCAB",
    # ... existing list ...
]
```

### Option B: Load NSE 500 full list from CSV
Download NSE 500 list from NSE website → save as `nse500.csv`
```python
import pandas as pd
UNIVERSE = pd.read_csv("nse500.csv")["Symbol"].tolist()
MAX_STOCKS_TO_SCAN = 500  # increase limit
```

### Option C: Use Screener.in custom screen
1. Go to screener.in → Explore → create your own screen
2. Export to CSV
3. Load that CSV:
```python
df = pd.read_csv("my_screener_export.csv")
UNIVERSE = df["Ticker"].tolist()
```

### Correct symbol format
- Use NSE ticker without `.NS` suffix
- Example: `BAJFINANCE` not `BAJFINANCE.NS`
- For BSE-only stocks, Screener.in still uses the NSE format in URL
- If a stock gives 404 error — try the BSE symbol version

---

## 9. After the Screener — What Next

The screener gives you a shortlist. **This is only Step 1.**

### Step 2 — Deep Dive Each Stock (30 min per stock)

For every stock that passed:

**Check on Screener.in:**
- [ ] 10-year revenue trend — is growth consistent or one-year blip?
- [ ] 10-year profit trend — same question
- [ ] Return ratios improving or deteriorating? (Ratios section)
- [ ] Debt trend — taking more debt or reducing?
- [ ] Working capital cycle — improving or worsening?

**Read the Annual Report (BSE filings):**
- [ ] MD&A section — what is management saying about the business?
- [ ] Are they investing in new capacity? Where?
- [ ] Risks section — what risks do they acknowledge?
- [ ] Related party transactions — are they reasonable?

**Check Management:**
- [ ] Concall transcripts (last 4 quarters) — on Trendlyne / Tickertape
- [ ] Are they consistent in guidance vs actual delivery?
- [ ] Promoter buying/selling — check BSE bulk/block deals

### Step 3 — Check Technical Setup (Streak on Kite)

After fundamental shortlist:
- Is stock near 52-week high breakout? (momentum)
- Is volume above average? (institutions accumulating)
- Is it in an uptrend or sideways? (don't fight trend)

### Step 4 — Set Entry Price

Don't buy at market. Set GTT alert in Kite:
```
Kite → Holdings → GTT → Create GTT
Set trigger price = your target entry
Type = Single (buy when price hits)
```

### Step 5 — Position Size

```
Never put > 10% in one stock (diversification)
Never put > 20% in one sector
Start with 5% position → add on conviction
Minimum hold period = 3 years
```

### Step 6 — Review Quarterly

- Run screener again every quarter
- Check if fundamentals still hold
- If ROE/ROCE declining for 2 quarters → investigate
- If promoter selling → investigate
- Don't sell just because price fell — check if business is fine

---

## 10. Troubleshooting

### "Error: 404 Not Found" for a stock
- Symbol not found on Screener.in
- Try: Some stocks use different ticker on Screener.in vs NSE
- Example: `MCDOWELL-N` → try `MCDOUGL` or check screener.in URL manually

### "All fields = None" for a stock
- Screener.in page structure may have changed
- Run: `python3 -c "from multibagger_screener import ScreenerScraper; s=ScreenerScraper(); print(s.get_company_data('BAJFINANCE'))"`
- If broken, the HTML selectors need updating

### "0 stocks passed" when filters are strict
- Lower the thresholds — see Section 6
- Or add more stocks to UNIVERSE
- Market cycle matters — in bear market, fewer stocks pass

### Kite login fails
- Access token expires every day at midnight
- Run `--login` fresh every morning
- Make sure redirect URL in Kite app settings = `http://127.0.0.1`

### Script is slow
- Lower `MAX_STOCKS_TO_SCAN = 50` for quick test
- Lower `DELAY_BETWEEN_REQUESTS = 1` (risky — may get rate-limited)
- Run after market hours — Screener.in is faster

---

## 11. Important Warnings

```
⚠️  THIS IS A SCREENING TOOL — NOT A BUY SIGNAL

The screener filters stocks mechanically.
It does NOT:
  - Know about promoter fraud
  - Know about accounting manipulation
  - Know about sector disruption risk
  - Know about governance issues
  - Predict future earnings

ALWAYS:
  - Read the annual report
  - Listen to concalls
  - Check for recent news
  - Verify with your own judgement

Past ROE/growth does NOT guarantee future performance.
A stock passing all 7 filters can still go to zero.

Invest only what you can afford to lose.
This tool is for educational and research purposes.
```

---

## Quick Reference Card

```
COMMAND                              WHAT IT DOES
─────────────────────────────────────────────────────────
python3 multibagger_screener.py      Run full scan (150 stocks, ~8 min)
python3 multibagger_screener.py --login   Get Kite access token

OUTPUT FILE
~/VISHAL_RAJPUT/screener/multibagger_YYYYMMDD.csv

KEY CONFIG (top of script)
USE_KITE = False/True
MAX_STOCKS_TO_SCAN = 150
DELAY_BETWEEN_REQUESTS = 2
SCREENER_SESSION_COOKIE = "..."
KITE_ACCESS_TOKEN = "..."

GRADE SYSTEM
🔥 STRONG BUY  score ≥ 14/17
✅ BUY         score ≥ 11/17
⚠️  WATCH       score ≥  8/17
❌ FAIL         failed core filter

CORE FILTERS (all must pass)
ROE > 20% | ROCE > 20% | D/E < 0.5 | Promoter > 45% | Pledge < 5%
```

---

*Manual v1.0 — Keep this file alongside multibagger_screener.py*
