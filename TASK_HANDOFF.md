# TASK_HANDOFF — VRL Bot Simplification

> Reconstructed from session decisions. Source of truth for the 20-part refactor.
> Branch: `claude/nifty-ema9-trading-bot-ZpFck`
> Starting line count: **12,707** across 12 `*.py` files
> Target: **~8,000 lines** across **6** `*.py` files (~37% reduction)

---

## What this bot does

Nifty 50 options trading bot. **EMA9 Band Breakout** on **3-min candles**. CE or PE — whichever fires first. **2 lots fixed**.

---

## Strategy decisions (authoritative)

### Entry rules — 3-min candle

- Close > EMA9-low
- Gap ≥ **3 pts** above EMA9-low
- Green candle
- Body ≥ **40 %** of range *(was 30 %)*
- Low within **3 pts** of EMA9-low
- Previous close ≤ previous EMA9-high *(fresh breakout)*
- No entry before **09:35** *(was 09:30)*
- `MAX_DAILY_TRADES` gate — **REMOVED**
- `MAX_DAILY_LOSSES` gate — **REMOVED**

### Exit rules — UNCHANGED

- Emergency stop: −10 pts
- EOD: 15:20
- **Vishal Trail**: 60 % → 85 % → 80 % → LOCK + 40

### Logic REMOVED

- Profit Lock
- Streak Gate
- VWAP Bonus
- Straddle filter bonus

### Regime detection

- Keep **Method 1** (EMA Spread) — enhance with **4 layers**: label + direction + momentum + ADX
- **Monitor-only via Telegram alert for 1 week — ZERO trade blocking**
- **Method 2** (`compute_spot_regime`) — **DELETE** (dead code)

### Telegram commands

**Keep 12:**
`/help` `/status` `/trades` `/account` `/pause` `/resume` `/forceexit` `/restart` `/alerts_on` `/alerts_off` `/reset_exit` `/livecheck` `/health` `/download`

*(note: above list is 14 — reconcile during Part 9; the session summary said "12" but named 14. Default is to keep all 14 listed unless user confirms otherwise.)*

**Remove 14:**
`/pnl` `/streak` `/slippage` `/spot` `/pivot` `/edge` `/greeks` `/score` `/regime` `/align` `/files` `/download_strategy` `/validate` `/source` `/token`

*(above is 15 — also flagged for Part 9 reconciliation.)*

### LAB

- **Keep:** 1-min CE/PE + spot, 3-min CE/PE + spot, all indicators on these
- **Remove:** 5-min, 15-min, 60-min, daily

### File consolidation — 12 → 6

| Keep           | Absorbs                            | Status              |
| -------------- | ---------------------------------- | ------------------- |
| VRL_CONFIG.py  | VRL_AUTH.py                        | **TO VERIFY** (both files still exist) |
| VRL_DB.py      | VRL_VALIDATE.py                    | **TO VERIFY** (both files still exist) |
| VRL_ENGINE.py  | VRL_CHARGES.py + VRL_ALERTS.py     | todo                |
| VRL_MAIN.py    | VRL_COMMANDS.py + VRL_TRADE.py     | todo                |
| VRL_DATA.py    | standalone                         | (trim dead code)    |
| VRL_LAB.py     | standalone                         | (trim to 1m + 3m)   |

### Dead code to remove

- 19 dead functions in `VRL_DATA.py`
- 4 dead functions in `VRL_MAIN.py`
- 2 dead functions in `VRL_ENGINE.py`
- 3 dead SL-order functions in `VRL_TRADE.py`
- All `BUG-xxx` comment blocks
- All `phase1_sl`, `phase2_sl`, `exit_phase` state fields
- All dead `DEFAULT_STATE` fields

*(exact function names to be identified during each part; list kept loose because the summary did not enumerate them)*

---

## The 20-part plan

Order = low-risk, small-blast-radius first; consolidation last so imports change once.
Each part = one commit. Run the bot's smoke test (import + `--help` or equivalent) after every part.

### Phase A — Strategy tweaks (small, high-leverage)

**Part 1 — Entry rule tweaks**
Files: `VRL_CONFIG.py`, `VRL_ENGINE.py` (and wherever entry gates live)
- Body min: 30 % → 40 %
- First-entry time: 09:30 → 09:35
- Remove `MAX_DAILY_TRADES` gate + its config/state/command surfaces
- Remove `MAX_DAILY_LOSSES` gate + its config/state/command surfaces
Commit: `Part 1: tighten entry (40% body, 09:35 start); drop daily trade/loss gates`

**Part 2 — Remove Profit Lock**
Files: wherever profit-lock runs (likely `VRL_ENGINE.py` / `VRL_MAIN.py`)
Commit: `Part 2: remove Profit Lock logic`

**Part 3 — Remove Streak Gate**
Commit: `Part 3: remove Streak Gate`

**Part 4 — Remove VWAP Bonus**
Commit: `Part 4: remove VWAP bonus`

**Part 5 — Remove Straddle filter bonus**
Commit: `Part 5: remove Straddle filter bonus`

### Phase B — Regime

**Part 6 — Delete `compute_spot_regime` (Method 2)**
Commit: `Part 6: delete dead Method 2 regime code`

**Part 7 — Enhance Method 1 regime (label + direction + momentum + ADX)**
Commit: `Part 7: 4-layer regime (label/dir/momentum/ADX)`

**Part 8 — Telegram alert for regime (monitor-only, no blocking)**
Commit: `Part 8: regime Telegram alert — monitor-only for 1 week`

### Phase C — Telegram surface

**Part 9 — Remove unused commands + reconcile keep/remove lists with user**
Commit: `Part 9: trim Telegram surface to core commands`

### Phase D — LAB

**Part 10 — Trim LAB to 1-min + 3-min only**
Remove 5-min / 15-min / 60-min / daily resamplers + their indicator blocks.
Commit: `Part 10: LAB — 1m + 3m only`

### Phase E — Dead-code purge (before consolidation so diffs are clean)

**Part 11 — Remove 19 dead funcs from `VRL_DATA.py`**
Commit: `Part 11: drop 19 dead funcs from VRL_DATA`

**Part 12 — Remove 4 dead funcs from `VRL_MAIN.py`**
Commit: `Part 12: drop 4 dead funcs from VRL_MAIN`

**Part 13 — Remove 2 dead funcs from `VRL_ENGINE.py`**
Commit: `Part 13: drop 2 dead funcs from VRL_ENGINE`

**Part 14 — Remove 3 dead SL-order funcs from `VRL_TRADE.py`**
Commit: `Part 14: drop dead SL-order funcs from VRL_TRADE`

**Part 15 — Strip `BUG-xxx` blocks, `phase1_sl` / `phase2_sl` / `exit_phase`, dead `DEFAULT_STATE` fields**
Commit: `Part 15: purge BUG comments + stale state fields`

### Phase F — File consolidation (last — imports shift)

**Part 16 — VRL_AUTH → VRL_CONFIG**
Verify state; finish merge; delete `VRL_AUTH.py`; update imports everywhere.
Commit: `Part 16: absorb VRL_AUTH into VRL_CONFIG`

**Part 17 — VRL_VALIDATE → VRL_DB**
Commit: `Part 17: absorb VRL_VALIDATE into VRL_DB`

**Part 18 — VRL_CHARGES + VRL_ALERTS → VRL_ENGINE**
Commit: `Part 18: absorb VRL_CHARGES + VRL_ALERTS into VRL_ENGINE`

**Part 19 — VRL_COMMANDS + VRL_TRADE → VRL_MAIN**
Commit: `Part 19: absorb VRL_COMMANDS + VRL_TRADE into VRL_MAIN`

**Part 20 — Final sweep**
- Update all in-repo docstrings + `/help` footer to new file layout
- `python -c "import VRL_MAIN"` smoke test
- `wc -l` check — aim ~8,000 lines, 6 files
- Version bump if there is one
Commit: `Part 20: final sweep — imports, docs, smoke test`

---

## How to resume

In a new chat:

> "Read `/home/user/VISHAL_RAJPUT/TASK_HANDOFF.md` and do Part N"

Each part is self-contained. If a part turns out bigger than expected, split it and note the split in the commit message — do not silently expand scope.

## Ground rules

- One part = one commit.
- Never skip hooks. Never force-push.
- If a decision above contradicts the code in a non-trivial way, **stop and ask** — do not guess.
- Smoke test (`python -c "import VRL_MAIN"`) must pass before each commit.
- Push at the end of each part: `git push -u origin claude/nifty-ema9-trading-bot-ZpFck`.
