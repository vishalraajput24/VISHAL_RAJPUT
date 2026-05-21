# Vishal Session Manual
## Standard Operating Procedure — Every Session

---

## RULE #1 — NEVER DO WITHOUT PERMISSION

| Action | Rule |
|--------|------|
| Write any code | ❌ Do NOT code. Explain what you found, wait for "yes" |
| Fix any bug | ❌ Do NOT fix. Diagnose first, present findings, wait for "yes" |
| Restart the bot | ❌ STRICTLY ask permission before every restart. No exceptions. |
| Push to GitHub | ✅ Always do after any code change (mandatory) |
| Read files / logs | ✅ Always allowed — reading is free |
| Create analysis/docs | ✅ Always allowed — no code impact |

---

## CHECKLIST — Code Change / Bug Fix Session

### Step 1 — Understand the Bug First
- [ ] Read the relevant code sections
- [ ] Pull log evidence (exact timestamps, exact error messages)
- [ ] Identify the **exact root cause** (not a guess — the confirmed line of code)
- [ ] Write out the diagnosis clearly: what is broken, why, where in the code
- [ ] **STOP. Tell Vishal. Wait for "yes" before touching any code.**

### Step 2 — Code the Fix (only after permission)
- [ ] Make the smallest possible change — no refactoring, no extras
- [ ] Read back the edited lines to confirm they are correct
- [ ] Check no unintended side effects

### Step 3 — GitHub (mandatory, no exceptions)
```bash
export PATH="$HOME/bin:$PATH"
git checkout -b fix/<short-description>
git add <only changed production files — NOT backtest scripts>
git commit -m "type: reason + what and why"
git push origin fix/<short-description>
gh pr create --title "..." --body "..."
gh pr merge --squash --delete-branch
git checkout main && git pull
```

### Step 4 — Analysis
- [ ] Write today's analysis to `~/VISHAL_RAJPUT/analysis/YYYY-MM-DD_analysis.md`
- [ ] Use the Sherlock method from `VISHAL_ANALYSIS_GUIDE.md`
- [ ] Cover every signal: entry quality, PnL, verdict

### Step 5 — Restart Decision
**Ask this question first:**

> "The fix is live on main. Bot needs a restart to pick up the changes. Shall I restart?"

- [ ] Wait for explicit "yes" or "go ahead"
- [ ] Only then run: `sudo systemctl restart vrl-main.service`
- [ ] After restart, confirm alive: `tail -5 ~/logs/live/vrl_live.log`
- [ ] Confirm expected log lines appear (Token health ✅, Strikes LOCKED)

---

## CHECKLIST — Analysis-Only Session (no code change)

- [ ] Pull log: `grep "SHADOW-P1.*SIGNAL\|SHADOW-P1.*SL-HIT\|Strikes LOCKED" ~/logs/live/vrl_live.log`
- [ ] Pull errors: `grep "WARNING\|ERROR" ~/logs/live/vrl_live.log | grep -v "REJECT\|band_narrow\|below_band\|dtime\|LOGPATH"`
- [ ] Check shadow state: `cat ~/VISHAL_RAJPUT/state/vrl_shadow_state.json`
- [ ] Write analysis to `~/VISHAL_RAJPUT/analysis/YYYY-MM-DD_analysis.md`
- [ ] No restart needed (no code changed)

---

## CHECKLIST — Sanity Check (4-min loop)

1. `tail -5 ~/logs/live/vrl_live.log` — last log within 30s?
2. `grep "Token health" ~/logs/live/vrl_live.log | tail -1` — Token ✅ Spot ✅ WS ✅?
3. `grep "Strikes LOCKED" ~/logs/live/vrl_live.log | tail -1` — present?
4. `grep "REJECT-V8" ~/logs/live/vrl_live.log | tail -2` — V9 scanning?
5. `grep "SHADOW-DTF\|SHADOW-P1\|SHADOW-P2" ~/logs/live/vrl_live.log | grep "REJECT\|SIGNAL" | tail -2` — shadow scanning?
6. `cat ~/VISHAL_RAJPUT/state/vrl_shadow_state.json` — active signals correct?
7. `grep "WARNING\|ERROR" ~/logs/live/vrl_live.log | grep -v "REJECT\|SKIP\|LOGPATH\|band_narrow\|below_band\|dtime" | tail -5` — any real errors?

Report: **PASS** or flag specific issue found.

---

## Key File Paths

| Purpose | Path |
|---------|------|
| Main bot | `~/VISHAL_RAJPUT/VRL_MAIN.py` |
| Engine (gate logic) | `~/VISHAL_RAJPUT/VRL_ENGINE.py` |
| Live log | `~/logs/live/vrl_live.log` |
| Trade CSV | `~/lab_data/vrl_trade_log.csv` |
| Shadow state | `~/VISHAL_RAJPUT/state/vrl_shadow_state.json` |
| Today's analysis | `~/VISHAL_RAJPUT/analysis/YYYY-MM-DD_analysis.md` |
| Analysis guide | `~/VISHAL_RAJPUT/VISHAL_ANALYSIS_GUIDE.md` |
| This manual | `~/VISHAL_RAJPUT/VISHAL_SESSION_MANUAL.md` |
| Developer reference | `~/VISHAL_RAJPUT/CLAUDE.md` |

---

## Restart — When Is It Needed?

| Change made | Restart needed? |
|-------------|----------------|
| Code change in VRL_MAIN.py or VRL_ENGINE.py | ✅ YES — always ask permission |
| Code change in VRL_DATA.py or VRL_CONFIG.py | ✅ YES — always ask permission |
| Analysis file created/updated | ❌ NO |
| State JSON edited manually | ❌ NO (bot reads state on next tick) |
| CLAUDE.md or docs updated | ❌ NO |

**Script to restart (only after explicit permission):**
```bash
sudo systemctl restart vrl-main.service
sleep 3
tail -10 ~/logs/live/vrl_live.log
```

---

## Summary — What Claude Must NEVER Do Without Being Told

1. Write or change any code
2. Restart the bot
3. Push to GitHub without having an approved fix
4. Make assumptions about what to fix — always show evidence first
5. Block any trade or change any gate threshold without explicit instruction
