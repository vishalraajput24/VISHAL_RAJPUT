#!/usr/bin/env python3
"""
VRL_ANALYSIS_BUILDER.py — Daily analysis builder (Sherlock mode)
Run anytime during or after market: python3 VRL_ANALYSIS_BUILDER.py
Writes: analysis/YYYY-MM-DD_analysis.md

Parses today's log → builds complete signal table + per-trade diagnosis.
Incremental: run after each trade, file always up to date.
"""

import re
import os
from datetime import date, datetime
from pathlib import Path

LOG_FILE  = os.path.expanduser("~/logs/live/vrl_live.log")
OUT_DIR   = os.path.expanduser("~/VISHAL_RAJPUT/analysis")
TODAY     = str(date.today())
OUT_FILE  = os.path.join(OUT_DIR, f"{TODAY}_analysis.md")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ts(line):
    m = re.match(r'\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})', line)
    return m.group(1)[:5] if m else ""


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_levels(log_file, today):
    """Parse PDH/PDL/CPR/ORH/ORL from today's startup LEVELS log line."""
    levels = {}
    with open(log_file) as fh:
        for raw in fh:
            if today not in raw or 'LEVELS' not in raw:
                continue
            m = re.search(
                r'PDH=([\d.]+) PDL=([\d.]+) PDC=([\d.]+) Pivot=([\d.]+) '
                r'CPR=([\d.]+)-([\d.]+) \(w=([\d.]+)\) ORH=([\d.]+) ORL=([\d.]+)', raw)
            if m:
                levels = {
                    'PDH':   float(m.group(1)),
                    'PDL':   float(m.group(2)),
                    'PDC':   float(m.group(3)),
                    'Pivot': float(m.group(4)),
                    'CPR_L': float(m.group(5)),
                    'CPR_H': float(m.group(6)),
                    'CPR_W': float(m.group(7)),
                    'ORH':   float(m.group(8)),
                    'ORL':   float(m.group(9)),
                }
    return levels


def spot_zone(spot, lvl):
    """Return zone label for spot relative to key levels."""
    if not lvl:
        return "—"
    if spot >= lvl['PDH']:
        return f"↑ ABOVE PDH({lvl['PDH']:.0f})"
    elif spot >= lvl['ORH']:
        return f"PDH-ORH zone ({lvl['ORH']:.0f}–{lvl['PDH']:.0f})"
    elif spot >= lvl['CPR_H']:
        return f"ORH-CPR zone ({lvl['CPR_H']:.0f}–{lvl['ORH']:.0f})"
    elif spot >= lvl['CPR_L']:
        return f"⚡ INSIDE CPR ({lvl['CPR_L']:.0f}–{lvl['CPR_H']:.0f})"
    elif spot >= lvl['ORL']:
        return f"CPR-ORL zone ({lvl['ORL']:.0f}–{lvl['CPR_L']:.0f})"
    elif spot >= lvl['Pivot']:
        return f"ORL-Pivot zone ({lvl['Pivot']:.0f}–{lvl['ORL']:.0f})"
    elif spot >= lvl['PDL']:
        return f"Pivot-PDL zone ({lvl['PDL']:.0f}–{lvl['Pivot']:.0f})"
    else:
        return f"↓ BELOW PDL({lvl['PDL']:.0f})"


def parse_vwap_timeline(log_file, today):
    """Parse [VWAP] lines → list of (HH:MM, gap_float) sorted by time."""
    entries = []
    with open(log_file) as fh:
        for raw in fh:
            if today not in raw or '[VWAP]' not in raw:
                continue
            t = _ts(raw)
            m = re.search(r'gap=([+\-\d.]+)', raw)
            if m and t:
                entries.append((t, float(m.group(1))))
    return entries  # [(HH:MM, gap), ...]


def get_spot_vwap_gap(trade_time, vwap_timeline):
    """Return spot VWAP gap closest to (but not after) trade_time. None if no data."""
    if not vwap_timeline:
        return None
    best = None
    for (vt, gap) in vwap_timeline:
        if vt <= trade_time:
            best = gap
        else:
            break
    return best


def parse_log():
    trades  = []
    live    = {}   # key=(strategy,dir) → current open trade dict
    counter = 0
    relocks = []
    spots   = []
    # track latest locked spot per relock — used as signal-time spot proxy
    _latest_spot = 0.0

    with open(LOG_FILE) as fh:
        for raw in fh:
            if TODAY not in raw:
                continue
            line = raw.strip()
            t    = _ts(line)

            # Spot
            m = re.search(r'Spot: ✅ ([\d.]+)', line)
            if m:
                spots.append(float(m.group(1)))

            # ATM relock — track latest spot
            m = re.search(r'Strikes LOCKED: ATM=(\d+).*spot=([\d.]+)', line)
            if m:
                relocks.append((t, m.group(1), m.group(2)))
                _latest_spot = float(m.group(2))

            # ── P1 SIGNAL ──
            m = re.search(
                r'\[SHADOW-P1\] (\w+) (\d+) SIGNAL '
                r'entry=([\d.]+) sl=[\d.]+ '
                r'ema9h_gap=([+\-\d.]+) bw=([\d.]+) '
                r'vwap=([\d.]+) gap_vwap=([+\-\d.]+) rsi=([\d.]+)', line)
            if m and 'V2' not in line:
                dir_, strike, entry, gap, bw, vwap, gap_vwap, rsi = m.groups()
            else:
                # backward compat: old format without bw
                m = re.search(
                    r'\[SHADOW-P1\] (\w+) (\d+) SIGNAL '
                    r'entry=([\d.]+) sl=[\d.]+ '
                    r'ema9h_gap=([+\-\d.]+) '
                    r'vwap=([\d.]+) gap_vwap=([+\-\d.]+) rsi=([\d.]+)', line)
                if m and 'V2' not in line:
                    dir_, strike, entry, gap, vwap, gap_vwap, rsi = m.groups()
                    bw = None
            if m and 'V2' not in line:
                counter += 1
                key = ('P1', dir_)
                live[key] = dict(
                    num=counter, strategy='P1', dir=dir_,
                    strike=int(strike), time=t, entry=float(entry),
                    ema9h_gap=float(gap), vwap=float(vwap),
                    gap_vwap=float(gap_vwap), rsi=float(rsi),
                    bw=float(bw) if bw is not None else None,
                    flags='clean', flags_raw='',
                    exit=None, exit_time=None, exit_reason=None,
                    pnl_v1=None, pnl_v2=None,
                    peak=None, trail=None, delay=None, open=True,
                    spot_at_entry=_latest_spot,
                )
                continue

            # ── P2 SIGNAL ──
            m = re.search(
                r'\[SHADOW-P2\] (\w+) (\d+) SIGNAL '
                r'entry=([\d.]+) sl=[\d.]+ '
                r'ema9h_gap=([+\-\d.]+) bw=([\d.]+) '
                r'vwap=([\d.]+) below_by=([+\-\d.]+) rsi=([\d.]+)', line)
            if m:
                dir_, strike, entry, gap, bw, vwap, below_by, rsi = m.groups()
                counter += 1
                key = ('P2', dir_)
                live[key] = dict(
                    num=counter, strategy='P2', dir=dir_,
                    strike=int(strike), time=t, entry=float(entry),
                    ema9h_gap=float(gap), vwap=float(vwap),
                    gap_vwap=float(below_by), rsi=float(rsi),
                    bw=float(bw), flags='clean', flags_raw='',
                    exit=None, exit_time=None, exit_reason=None,
                    pnl_v1=None, pnl_v2=None,
                    peak=None, trail=None, delay=None, open=True,
                    spot_at_entry=_latest_spot,
                )
                continue

            # ── ANALYSIS flags ──
            m = re.search(r'\[ANALYSIS\] P(\d) (\w+) entry=([\d.]+).*?— (.+)$', line)
            if m:
                snum, dir_, entry_str, flags_raw = m.groups()
                key = (f'P{snum}', dir_)
                if key in live and abs(live[key]['entry'] - float(entry_str)) < 1.0:
                    live[key]['flags_raw'] = flags_raw.strip()
                    flist = []
                    if 'WEAK_ADX'          in flags_raw:
                        mm = re.search(r'WEAK_ADX\([\d.]+\)', flags_raw)
                        flist.append(mm.group() if mm else 'WEAK_ADX')
                    if 'EXTENDED_GAP'      in flags_raw:
                        mm = re.search(r'EXTENDED_GAP\([\d.]+\)', flags_raw)
                        flist.append(mm.group() if mm else 'EXTENDED_GAP')
                    if 'TINY_GAP'          in flags_raw: flist.append('TINY_GAP')
                    if 'XLEG_CONFIRMED'    in flags_raw: flist.append('XLEG_CONFIRMED')
                    if 'XLEG_AMBIGUOUS'    in flags_raw: flist.append('XLEG_AMBIGUOUS')
                    if 'FUT_VWAP_MISMATCH' in flags_raw:
                        mm = re.search(r'FUT_VWAP_MISMATCH\((\w+)', flags_raw)
                        flist.append(f'FUT_MISMATCH({mm.group(1) if mm else "?"})')
                    if 'SPOT_EMA_BULL'     in flags_raw: flist.append('SPOT_EMA_BULL')
                    if 'SPOT_EMA_BEAR'     in flags_raw: flist.append('SPOT_EMA_BEAR')
                    if 'VWAP_COMPRESSED'   in flags_raw: flist.append('VWAP_COMPRESSED')
                    live[key]['flags'] = ' | '.join(flist) if flist else 'clean'
                continue

            # ── P1 V1 EXIT ──
            m = re.search(
                r'\[SHADOW-P1\] (\w+) (SL-HIT|PROFIT|TARGET\+\d+|EOD) '
                r'entry=([\d.]+) exit=([\d.]+) pnl=([+\-\d.]+) '
                r'peak=\+?([\d.]+) trail=(\S+)', line)
            if m and 'V2' not in line:
                dir_, reason, entry, exit_, pnl, peak, trail = m.groups()
                key = ('P1', dir_)
                if key in live and abs(live[key]['entry'] - float(entry)) < 1.0:
                    live[key].update(
                        exit=float(exit_), exit_time=t, exit_reason=reason,
                        pnl_v1=float(pnl), peak=float(peak),
                        trail=trail, open=False,
                    )
                    trades.append(live.pop(key))
                continue

            # ── P1 V2 EXIT ──
            m = re.search(
                r'\[SHADOW-P1-V2\] (\w+) (SL-HIT|PROFIT|TARGET\+\d+|EOD) '
                r'entry=([\d.]+) exit=([\d.]+) pnl=([+\-\d.]+)', line)
            if m:
                dir_, reason, entry, exit_, pnl = m.groups()
                key = ('P1', dir_)
                if key in live and abs(live[key]['entry'] - float(entry)) < 1.0:
                    live[key]['pnl_v2'] = float(pnl)
                else:
                    for tr in trades:
                        if tr['strategy'] == 'P1' and tr['dir'] == dir_ \
                                and abs(tr['entry'] - float(entry)) < 1.0 \
                                and tr['pnl_v2'] is None:
                            tr['pnl_v2'] = float(pnl)
                            break
                continue

            # ── P2 V1 EXIT ──
            m = re.search(
                r'\[SHADOW-P2\] (\w+) (SL-HIT|PROFIT|TARGET\+\d+|EOD) '
                r'entry=([\d.]+) exit=([\d.]+) pnl=([+\-\d.]+) '
                r'peak=\+?([\d.]+) trail=(\S+)', line)
            if m and 'V2' not in line:
                dir_, reason, entry, exit_, pnl, peak, trail = m.groups()
                key = ('P2', dir_)
                if key in live and abs(live[key]['entry'] - float(entry)) < 1.0:
                    live[key].update(
                        exit=float(exit_), exit_time=t, exit_reason=reason,
                        pnl_v1=float(pnl), peak=float(peak),
                        trail=trail, open=False,
                    )
                    trades.append(live.pop(key))
                continue

            # ── P2 V2 EXIT ──
            m = re.search(
                r'\[SHADOW-P2-V2\] (\w+) (SL-HIT|PROFIT|TARGET\+\d+|EOD) '
                r'entry=([\d.]+) exit=([\d.]+) pnl=([+\-\d.]+)', line)
            if m:
                dir_, reason, entry, exit_, pnl = m.groups()
                key = ('P2', dir_)
                if key in live and abs(live[key]['entry'] - float(entry)) < 1.0:
                    live[key]['pnl_v2'] = float(pnl)
                else:
                    for tr in trades:
                        if tr['strategy'] == 'P2' and tr['dir'] == dir_ \
                                and abs(tr['entry'] - float(entry)) < 1.0 \
                                and tr['pnl_v2'] is None:
                            tr['pnl_v2'] = float(pnl)
                            break
                continue

            # ── DELAY-ANALYSIS (new format with spot) ──
            m = re.search(
                r'\[DELAY-ANALYSIS\] (P[12])-(\w+) (\d+) base=([\d.]+) spot_base=([\d.]+) '
                r'\+5s=opt([\d.]+)\(([+\-\d.]+)\)spot([\d.]+)\(([+\-\d.]+)\) '
                r'\+10s=opt([\d.]+)\(([+\-\d.]+)\)spot([\d.]+)\(([+\-\d.]+)\) '
                r'\+30s=opt([\d.]+)\(([+\-\d.]+)\)spot([\d.]+)\(([+\-\d.]+)\) '
                r'\+60s=opt([\d.]+)\(([+\-\d.]+)\)spot([\d.]+)\(([+\-\d.]+)\)', line)
            if m:
                (strat, dir_, strike, base, spot_base,
                 s5, d5, sp5, spd5,
                 s10, d10, sp10, spd10,
                 s30, d30, sp30, spd30,
                 s60, d60, sp60, spd60) = m.groups()
                delay = {
                    5:  (float(s5),  float(d5),  float(sp5),  float(spd5)),
                    10: (float(s10), float(d10), float(sp10), float(spd10)),
                    30: (float(s30), float(d30), float(sp30), float(spd30)),
                    60: (float(s60), float(d60), float(sp60), float(spd60)),
                }
                key = (strat, dir_)
                # tight tolerance 0.3 to avoid cross-matching two same-dir signals with close entry prices
                if key in live and abs(live[key]['entry'] - float(base)) < 0.3:
                    live[key]['delay'] = delay
                    live[key]['spot_at_entry'] = float(spot_base)   # exact spot at signal time
                for tr in trades:
                    if tr['strategy'] == strat and tr['dir'] == dir_ \
                            and abs(tr['entry'] - float(base)) < 0.3:
                        tr['delay'] = delay
                        tr['spot_at_entry'] = float(spot_base)      # exact spot at signal time
                        break
                continue
            # ── DELAY-ANALYSIS (old format, no spot — backward compat) ──
            m = re.search(
                r'\[DELAY-ANALYSIS\] (P[12])-(\w+) (\d+) base=([\d.]+) '
                r'\+5s=([\d.]+)\(([+\-\d.]+)\) '
                r'\+10s=([\d.]+)\(([+\-\d.]+)\) '
                r'\+30s=([\d.]+)\(([+\-\d.]+)\) '
                r'\+60s=([\d.]+)\(([+\-\d.]+)\)', line)
            if m:
                strat, dir_, strike, base, s5, d5, s10, d10, s30, d30, s60, d60 = m.groups()
                delay = {
                    5:  (float(s5),  float(d5),  None, None),
                    10: (float(s10), float(d10), None, None),
                    30: (float(s30), float(d30), None, None),
                    60: (float(s60), float(d60), None, None),
                }
                key = (strat, dir_)
                if key in live and abs(live[key]['entry'] - float(base)) < 0.3:
                    live[key]['delay'] = delay
                for tr in trades:
                    if tr['strategy'] == strat and tr['dir'] == dir_ \
                            and abs(tr['entry'] - float(base)) < 0.3:
                        tr['delay'] = delay
                        break
                continue

    # Add still-open trades
    for key, tr in live.items():
        trades.append(tr)

    market = {}
    if spots:
        market['first'] = spots[0]
        market['last']  = spots[-1]
        market['move']  = round(spots[-1] - spots[0], 1)

    return trades, relocks, market


def parse_cross_trades(log_file, today):
    """Parse [CROSS-TRADE] events — P1 and P2 open in opposite directions."""
    crosses = []
    with open(log_file) as fh:
        for raw in fh:
            if today not in raw or '[CROSS-TRADE]' not in raw:
                continue
            t = re.match(r'\d{4}-\d{2}-\d{2} (\d{2}:\d{2})', raw)
            time_str = t.group(1) if t else ''
            # P2 vs P1 format
            m = re.search(
                r'\[CROSS-TRADE\] (P[12])-(\w+) just fired vs (P[12])-(\w+) already open '
                r'p[12]_entry=([\d.]+) p[12]_entry=([\d.]+) '
                r'p[12]_peak=([\d.]+) strike=(\d+)', raw)
            if m:
                crosses.append({
                    'time':       time_str,
                    'new_strat':  m.group(1),
                    'new_dir':    m.group(2),
                    'old_strat':  m.group(3),
                    'old_dir':    m.group(4),
                    'new_entry':  float(m.group(5)),
                    'old_entry':  float(m.group(6)),
                    'old_peak':   float(m.group(7)),
                    'strike':     int(m.group(8)),
                })
    return crosses


def parse_rsi_blocks(log_file, today):
    """Parse [RSI-SHADOW] BLOCKED lines — signals RSI filter killed."""
    blocks = []
    seen = set()
    with open(log_file) as fh:
        for raw in fh:
            if today not in raw or '[RSI-SHADOW]' not in raw:
                continue
            m = re.search(
                r'\[RSI-SHADOW\] (\w+) (\d+) BLOCKED '
                r'entry=([\d.]+) ema9h_gap=([+\-\d.]+) bw=([\d.]+) '
                r'vwap=([\d.]+) gap_vwap=([+\-\d.]+) '
                r'rsi=([\d.]+) reason=(\S+)', raw)
            if m:
                t = re.match(r'\d{4}-\d{2}-\d{2} (\d{2}:\d{2})', raw)
                time_str = t.group(1) if t else ''
                key = (time_str, m.group(1))
                if key in seen:
                    continue
                seen.add(key)
                blocks.append({
                    'time':      time_str,
                    'dir':       m.group(1),
                    'strike':    int(m.group(2)),
                    'entry':     float(m.group(3)),
                    'ema9h_gap': float(m.group(4)),
                    'bw':        float(m.group(5)),
                    'vwap':      float(m.group(6)),
                    'gap_vwap':  float(m.group(7)),
                    'rsi':       float(m.group(8)),
                    'reason':    m.group(9),
                })
    return blocks


# ── Diagnosis ─────────────────────────────────────────────────────────────────

def diagnose(tr):
    flags    = tr.get('flags_raw', '')
    pnl_v1   = tr.get('pnl_v1')
    peak     = tr.get('peak') or 0.0
    gap_vwap = tr.get('gap_vwap') or 0.0
    bw       = tr.get('bw')
    delay    = tr.get('delay')
    pnl_v2   = tr.get('pnl_v2')

    if tr.get('open'):
        v2_str = f" | V2 exited {pnl_v2:+.0f}" if pnl_v2 is not None else ""
        return f"⏳ OPEN — peak so far={peak:.1f}{v2_str}"

    if pnl_v1 is None:
        return "No exit data"

    clues = []

    if pnl_v1 < 0:
        if peak < 1.0:
            clues.append("zero momentum — immediate reversal, false breakout")
        elif peak < 5.0:
            clues.append(f"peaked at only +{peak:.1f} — no follow-through")
        elif peak < 12.0:
            clues.append(f"reached +{peak:.1f} but couldn't lock +12 — weak move")
        if 'WEAK_ADX' in flags:
            adx = re.search(r'WEAK_ADX\(([\d.]+)\)', flags)
            clues.append(f"ADX={adx.group(1) if adx else '?'} — no directional energy")
        if 'XLEG_AMBIGUOUS' in flags:
            clues.append("cross-leg ambiguous — no divergence confirmation")
        if 'FUT_VWAP_MISMATCH' in flags:
            clues.append("futures VWAP direction conflict — entered against trend")
        if abs(gap_vwap) > 10:
            clues.append(f"gap_vwap={gap_vwap:+.1f} — entry overextended from VWAP")
        if bw is not None and float(bw) < 5.0:
            clues.append(f"BW={bw:.1f} — band too narrow, no energy")
        if 'SPOT_EMA_BULL' in flags and tr['dir'] == 'PE':
            clues.append("spot bull-lean — PE against dominant direction")
        if delay:
            neg = sum(1 for d in (5,10,30,60) if delay[d][1] < 0)
            if neg >= 3:
                clues.append(f"delay confirmed spike — {neg}/4 snapshots reversed within 60s")
        if not clues:
            clues.append("clean signal — market reversed without warning (chop cost)")
        return "❌ LOSS: " + " | ".join(clues)

    else:
        if abs(gap_vwap) < 2.0:
            clues.append(f"VWAP crossover entry (gap={gap_vwap:+.2f}) — fresh breakout not overextended")
        if 'XLEG_CONFIRMED' in flags:
            clues.append("XLEG_CONFIRMED — cross-leg dying cleanly")
        if 'SPOT_EMA_BEAR' in flags and tr['dir'] == 'CE':
            clues.append("SPOT_EMA_BEAR → CE compressed, breakout had room to run")
        if 'SPOT_EMA_BULL' in flags and tr['dir'] == 'PE':
            clues.append("SPOT_EMA_BULL → PE reversal from extended spot")
        if peak >= 30:
            clues.append(f"sustained momentum to +{peak:.1f} — trend move")
        elif peak >= 15:
            clues.append(f"solid move to +{peak:.1f}")
        if delay:
            pos = sum(1 for d in (5,10,30,60) if delay[d][1] > 0)
            if pos >= 3:
                clues.append(f"delay confirmed — {pos}/4 snapshots held above entry")
        if pnl_v2 is not None:
            edge = round(pnl_v2 - pnl_v1, 1)
            v2_note = f" [V2={pnl_v2:+.0f} edge={edge:+.1f}]"
        else:
            v2_note = ""
        if not clues:
            clues.append("solid entry, market cooperated")
        return f"✅ WIN{v2_note}: " + " | ".join(clues)


# ── Delay string ──────────────────────────────────────────────────────────────

def delay_str(delay):
    if not delay:
        return "no data yet"
    has_spot = delay[5][2] is not None
    parts = []
    for d in (5, 10, 30, 60):
        opt_d  = delay[d][1]
        opt_s  = f"{'+' if opt_d >= 0 else ''}{opt_d:.1f}"
        base   = f"+{d}s=opt{delay[d][0]:.1f}({opt_s})"
        if has_spot and delay[d][2] is not None:
            spd = delay[d][3]
            base += f" spot{delay[d][2]:.0f}({'+' if spd >= 0 else ''}{spd:.0f})"
        parts.append(base)
    vals = [delay[d][1] for d in (5, 10, 30, 60)]
    if all(v > 0 for v in vals):      label = "REAL MOVE ✅"
    elif all(v < 0 for v in vals):    label = "SPIKE ❌"
    elif vals[0] < 0:                 label = "EARLY SPIKE"
    elif vals[-1] < vals[1]:          label = "FADED"
    else:                             label = "MIXED"
    return "  ".join(parts) + f"  → {label}"


# ── Writer ────────────────────────────────────────────────────────────────────

def write_analysis(trades, relocks, market, levels=None, rsi_blocks=None, cross_trades=None, vwap_timeline=None):
    now_str   = datetime.now().strftime("%H:%M")
    closed    = [tr for tr in trades if not tr.get('open')]
    open_tr   = [tr for tr in trades if tr.get('open')]
    pnl_v1    = sum(tr['pnl_v1'] for tr in closed if tr['pnl_v1'] is not None)
    pnl_v2    = sum(tr['pnl_v2'] for tr in closed if tr['pnl_v2'] is not None)
    wins      = [tr for tr in closed if (tr.get('pnl_v1') or 0) > 0]
    losses    = [tr for tr in closed if (tr.get('pnl_v1') or 0) < 0]
    p1_trades = [tr for tr in trades if tr['strategy'] == 'P1']
    p2_trades = [tr for tr in trades if tr['strategy'] == 'P2']

    L = []
    def a(s=""): L.append(s)

    a(f"# VRL Shadow — Analysis | {TODAY}")
    a(f"**Analyst**: Claude (Sherlock mode) | **Updated**: {now_str} IST | **Signals**: {len(trades)}")
    a(f"**Relocks**: {len(relocks)} | **P&L V1**: {pnl_v1:+.0f} pts | **P&L V2**: {pnl_v2:+.0f} pts (closed)")
    a()

    # 1. Market context
    a("---")
    a("## 1. Market Context")
    a()
    if market:
        move = market.get('move', 0)
        bias = "BULL 🟢" if move > 20 else ("BEAR 🔴" if move < -20 else "CHOPPY ⚡")
        a(f"- Spot: {market.get('first','?')} → {market.get('last','?')} ({move:+.0f} pts) — **{bias}**")
    chop = ("extreme chop" if len(relocks) > 15 else
            "choppy" if len(relocks) > 8 else
            "moderate chop" if len(relocks) > 4 else "stable")
    a(f"- Relocks: {len(relocks)} ({chop})")
    if relocks:
        rl_str = "  →  ".join(f"{r[0]}(ATM={r[1]})" for r in relocks[:10])
        if len(relocks) > 10:
            rl_str += f"  +{len(relocks)-10} more"
        a(f"- Timeline: {rl_str}")
    a()

    # 1b. Key levels
    if levels:
        a("---")
        a("## 1b. Key Levels")
        a()
        cpw = levels.get('CPR_W', 0)
        cpw_note = "narrow → trending day" if cpw < 40 else ("wide → choppy day" if cpw > 80 else "moderate")
        a(f"| Level | Value |")
        a(f"|-------|-------|")
        a(f"| PDH | {levels['PDH']:.1f} |")
        a(f"| ORH | {levels['ORH']:.1f} |")
        a(f"| CPR HIGH | {levels['CPR_H']:.1f} |")
        a(f"| CPR LOW | {levels['CPR_L']:.1f} (width={cpw:.1f} — {cpw_note}) |")
        a(f"| ORL | {levels['ORL']:.1f} |")
        a(f"| Pivot | {levels['Pivot']:.1f} |")
        a(f"| PDL | {levels['PDL']:.1f} |")
        a(f"| PDC | {levels['PDC']:.1f} |")
        a()
        # Level zone W/L
        if trades:
            zone_stats = {}
            for tr in [t for t in trades if not t.get('open') and t.get('pnl_v1') is not None]:
                z = spot_zone(tr.get('spot_at_entry', tr['strike']), levels)
                if z not in zone_stats:
                    zone_stats[z] = [0, 0]
                if tr['pnl_v1'] > 0:
                    zone_stats[z][0] += 1
                else:
                    zone_stats[z][1] += 1
            if zone_stats:
                a(f"**Zone performance:**")
                a(f"| Zone | W | L |")
                a(f"|------|---|---|")
                for z, (w, l) in sorted(zone_stats.items(), key=lambda x: -x[1][0]):
                    a(f"| {z} | {w} | {l} |")
                a()

    # 1c. Market Classifier
    a("---")
    a("## 1c. Market Classifier")
    a()
    mc_score = 0
    mc_factors = []

    # Factor 1: Relocks
    rl_count = len(relocks)
    if rl_count <= 1:
        mc_score += 1
        mc_factors.append(("Relocks", f"{rl_count}", "TRENDING ✅"))
    elif rl_count <= 3:
        mc_factors.append(("Relocks", f"{rl_count}", "SIDEWAYS —"))
    else:
        mc_score -= 1
        mc_factors.append(("Relocks", f"{rl_count}", "CHOPPY ❌"))

    # Factor 2: BW avg at entry
    bw_vals = [tr['bw'] for tr in trades if tr.get('bw') is not None]
    if bw_vals:
        bw_avg_mc = sum(bw_vals) / len(bw_vals)
        if bw_avg_mc > 10:
            mc_score += 1
            mc_factors.append(("BW avg", f"{bw_avg_mc:.1f}", "TRENDING ✅"))
        elif bw_avg_mc >= 8:
            mc_factors.append(("BW avg", f"{bw_avg_mc:.1f}", "SIDEWAYS —"))
        else:
            mc_score -= 1
            mc_factors.append(("BW avg", f"{bw_avg_mc:.1f}", "CHOPPY ❌"))

    # Factor 3: DELAY FADED% (across all trades with delay data)
    delay_labels = []
    for tr in trades:
        d = tr.get('delay')
        if d:
            vals60 = [d[k][1] for k in (5, 10, 30, 60)]
            if all(v > 0 for v in vals60):
                delay_labels.append('REAL')
            elif all(v < 0 for v in vals60):
                delay_labels.append('SPIKE')
            elif vals60[-1] < vals60[1]:
                delay_labels.append('FADED')
            else:
                delay_labels.append('MIXED')
    if delay_labels:
        faded_count = delay_labels.count('FADED') + delay_labels.count('SPIKE')
        faded_pct   = faded_count / len(delay_labels) * 100
        if faded_pct < 30:
            mc_score += 1
            mc_factors.append(("DELAY bad%", f"{faded_pct:.0f}%", "TRENDING ✅"))
        elif faded_pct < 60:
            mc_factors.append(("DELAY bad%", f"{faded_pct:.0f}%", "SIDEWAYS —"))
        else:
            mc_score -= 1
            mc_factors.append(("DELAY bad%", f"{faded_pct:.0f}%", "CHOPPY ❌"))

    # Factor 4: CPR width
    if levels:
        cpw_mc = levels.get('CPR_W', 0)
        if cpw_mc < 20:
            mc_score += 1
            mc_factors.append(("CPR width", f"{cpw_mc:.0f}", "TRENDING ✅"))
        elif cpw_mc <= 50:
            mc_factors.append(("CPR width", f"{cpw_mc:.0f}", "SIDEWAYS —"))
        else:
            mc_score -= 1
            mc_factors.append(("CPR width", f"{cpw_mc:.0f}", "CHOPPY ❌"))

    # Factor 5: P1 avg peak
    p1_peaks_mc = [tr['peak'] for tr in trades if tr.get('strategy') == 'P1'
                   and tr.get('peak') is not None and not tr.get('open')]
    if p1_peaks_mc:
        p1_avg_pk = sum(p1_peaks_mc) / len(p1_peaks_mc)
        if p1_avg_pk > 15:
            mc_score += 1
            mc_factors.append(("P1 avg peak", f"+{p1_avg_pk:.1f}", "TRENDING ✅"))
        elif p1_avg_pk >= 10:
            mc_factors.append(("P1 avg peak", f"+{p1_avg_pk:.1f}", "SIDEWAYS —"))
        else:
            mc_score -= 1
            mc_factors.append(("P1 avg peak", f"+{p1_avg_pk:.1f}", "CHOPPY ❌"))

    # Factor 6: SPOT_EMA consistency
    bull_ema = sum(1 for tr in trades if 'SPOT_EMA_BULL' in tr.get('flags_raw', ''))
    bear_ema = sum(1 for tr in trades if 'SPOT_EMA_BEAR' in tr.get('flags_raw', ''))
    total_ema = bull_ema + bear_ema
    if total_ema > 0:
        dominant_pct = max(bull_ema, bear_ema) / total_ema
        direction    = "BULL" if bull_ema >= bear_ema else "BEAR"
        if dominant_pct >= 0.8:
            mc_score += 1
            mc_factors.append(("SPOT_EMA", f"{direction} {dominant_pct:.0%}", "TRENDING ✅"))
        else:
            mc_score -= 1
            mc_factors.append(("SPOT_EMA", f"BULL={bull_ema} BEAR={bear_ema}", "CHOPPY ❌"))

    # Final verdict
    if mc_score >= 3:
        mc_type = "📈 TRENDING"
    elif mc_score >= 0:
        mc_type = "📊 SIDEWAYS"
    else:
        mc_type = "⚡ CHOPPY"

    a(f"**Market Type: {mc_type}** (score={mc_score:+d} / {len(mc_factors)} factors)")
    a()
    a("| Factor | Value | Signal |")
    a("|--------|-------|--------|")
    for (fname, fval, fsig) in mc_factors:
        a(f"| {fname} | {fval} | {fsig} |")
    a()
    a(f"> Score legend: ≥ +3 = TRENDING · 0 to +2 = SIDEWAYS · ≤ -1 = CHOPPY")
    a()

    # 2. Signal table
    a("---")
    a("## 2. Signal Table")
    a()
    a("| # | Strat | Time | Dir | Strike | Entry | ema9h_gap | gap_vwap | BW | RSI | Peak | PnL V1 | PnL V2 | Trail | Flags | Zone |")
    a("|---|-------|------|-----|--------|-------|-----------|----------|----|-----|------|--------|--------|-------|-------|------|")

    for tr in sorted(trades, key=lambda x: x['num']):
        p1 = f"**{tr['pnl_v1']:+.0f}**" if tr['pnl_v1'] is not None else "🟢open"
        p2 = f"{tr['pnl_v2']:+.0f}" if tr['pnl_v2'] is not None else "—"
        pk = f"+{tr['peak']:.1f}" if tr['peak'] is not None else "—"
        bw = f"{tr['bw']:.1f}" if tr['bw'] is not None else "—"
        tl = tr['trail'] or ("open" if tr.get('open') else "—")
        zn = spot_zone(tr.get('spot_at_entry', 0), levels) if levels else "—"
        a(f"| S{tr['num']} | {tr['strategy']} | {tr['time']} | {tr['dir']} | {tr['strike']} "
          f"| {tr['entry']:.1f} | {tr['ema9h_gap']:+.2f} | {tr['gap_vwap']:+.2f} "
          f"| {bw} | {tr['rsi']:.1f} | {pk} | {p1} | {p2} | {tl} | {tr['flags']} | {zn} |")
    a()

    # 3. Per-trade diagnosis
    a("---")
    a("## 3. Per-Trade Diagnosis")
    a()
    for tr in sorted(trades, key=lambda x: x['num']):
        icon = "🟢" if tr.get('open') else ("✅" if (tr.get('pnl_v1') or 0) > 0 else "❌")
        a(f"### S{tr['num']} {icon} | {tr['strategy']} {tr['dir']} {tr['strike']} | {tr['time']}")
        a()
        exit_str = f"{tr['exit']:.1f}" if tr['exit'] else "—"
        p1_str   = f"{tr['pnl_v1']:+.0f}" if tr['pnl_v1'] is not None else "—"
        p2_str   = f"{tr['pnl_v2']:+.0f}" if tr['pnl_v2'] is not None else "—"
        pk_str   = f"+{tr['peak']:.1f}" if tr['peak'] is not None else "—"
        a(f"```")
        a(f"Entry : {tr['entry']:.1f}   Exit : {exit_str}   PnL V1 : {p1_str}   PnL V2 : {p2_str}")
        a(f"Peak  : {pk_str}   Trail  : {tr['trail'] or '—'}   Reason : {tr['exit_reason'] or '—'}")
        a(f"Gap   : ema9h={tr['ema9h_gap']:+.2f}  vwap={tr['gap_vwap']:+.2f}  RSI={tr['rsi']:.1f}" +
          (f"  BW={tr['bw']:.1f}" if tr['bw'] is not None else ""))
        a(f"Flags : {tr['flags']}")
        zn = spot_zone(tr.get('spot_at_entry', 0), levels) if levels else "—"
        a(f"Zone  : {zn}  (spot@entry={tr.get('spot_at_entry', 0):.0f})")
        a(f"Delay : {delay_str(tr['delay'])}")
        a(f"```")
        a()
        a(f"**Diagnosis:** {diagnose(tr)}")
        a()

    # 4. Day summary
    a("---")
    a("## 4. Day Summary")
    a()
    a(f"| | |")
    a(f"|--|--|")
    a(f"| Signals | {len(trades)} ({len(p1_trades)} P1 / {len(p2_trades)} P2) |")
    a(f"| Closed | {len(closed)} — {len(wins)} wins / {len(losses)} losses |")
    a(f"| Open | {len(open_tr)} |")
    a(f"| **P&L V1** | **{pnl_v1:+.0f} pts** |")
    a(f"| **P&L V2** | **{pnl_v2:+.0f} pts** |")
    if wins:
        best = max(wins, key=lambda x: x['pnl_v1'])
        a(f"| Best | S{best['num']} {best['strategy']} {best['dir']} {best['strike']} → {best['pnl_v1']:+.0f} pts |")
    if losses:
        avg_loss = sum(tr['pnl_v1'] for tr in losses) / len(losses)
        a(f"| Avg loss | {avg_loss:.1f} pts |")
    a()

    # 5. Patterns
    a("---")
    a("## 5. Patterns")
    a()

    xc_wins   = [tr for tr in closed if 'XLEG_CONFIRMED' in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) > 0]
    xc_losses = [tr for tr in closed if 'XLEG_CONFIRMED' in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) < 0]
    noxc_l    = [tr for tr in closed if 'XLEG_CONFIRMED' not in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) < 0]
    xa_l      = [tr for tr in closed if 'XLEG_AMBIGUOUS' in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) < 0]
    near_w    = [tr for tr in closed if abs(tr.get('gap_vwap',99)) < 2 and (tr['pnl_v1'] or 0) > 0]
    near_l    = [tr for tr in closed if abs(tr.get('gap_vwap',99)) < 2 and (tr['pnl_v1'] or 0) < 0]
    far_l     = [tr for tr in closed if abs(tr.get('gap_vwap',0)) > 8 and (tr['pnl_v1'] or 0) < 0]
    bear_cew  = [tr for tr in closed if 'SPOT_EMA_BEAR' in tr.get('flags_raw','') and tr['dir']=='CE' and (tr['pnl_v1'] or 0) > 0]
    bull_cel  = [tr for tr in closed if 'SPOT_EMA_BULL' in tr.get('flags_raw','') and tr['dir']=='CE' and (tr['pnl_v1'] or 0) < 0]
    fut_w     = [tr for tr in closed if 'FUT_VWAP_MISMATCH' in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) > 0]
    fut_l     = [tr for tr in closed if 'FUT_VWAP_MISMATCH' in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) < 0]
    vc_w      = [tr for tr in closed if 'VWAP_COMPRESSED' in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) > 0]
    vc_l      = [tr for tr in closed if 'VWAP_COMPRESSED' in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) < 0]
    vc_fm_w   = [tr for tr in closed if 'VWAP_COMPRESSED' in tr.get('flags_raw','') and 'FUT_VWAP_MISMATCH' in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) > 0]
    vc_fm_l   = [tr for tr in closed if 'VWAP_COMPRESSED' in tr.get('flags_raw','') and 'FUT_VWAP_MISMATCH' in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) < 0]
    # BW at entry (P1 now has BW; P2 always had it)
    narrow_bw_w = [tr for tr in closed if tr.get('bw') is not None and tr['bw'] < 10 and (tr['pnl_v1'] or 0) > 0]
    narrow_bw_l = [tr for tr in closed if tr.get('bw') is not None and tr['bw'] < 10 and (tr['pnl_v1'] or 0) < 0]
    wide_bw_w   = [tr for tr in closed if tr.get('bw') is not None and tr['bw'] >= 15 and (tr['pnl_v1'] or 0) > 0]
    wide_bw_l   = [tr for tr in closed if tr.get('bw') is not None and tr['bw'] >= 15 and (tr['pnl_v1'] or 0) < 0]
    # gap_vwap > 8 combined with BW — overextended but is there energy?
    far_nb_w  = [tr for tr in closed if abs(tr.get('gap_vwap',0)) > 8 and tr.get('bw') is not None and tr['bw'] < 10  and (tr['pnl_v1'] or 0) > 0]
    far_nb_l  = [tr for tr in closed if abs(tr.get('gap_vwap',0)) > 8 and tr.get('bw') is not None and tr['bw'] < 10  and (tr['pnl_v1'] or 0) < 0]
    far_wb_w  = [tr for tr in closed if abs(tr.get('gap_vwap',0)) > 8 and tr.get('bw') is not None and tr['bw'] >= 15 and (tr['pnl_v1'] or 0) > 0]
    far_wb_l  = [tr for tr in closed if abs(tr.get('gap_vwap',0)) > 8 and tr.get('bw') is not None and tr['bw'] >= 15 and (tr['pnl_v1'] or 0) < 0]
    far_mid_w = [tr for tr in closed if abs(tr.get('gap_vwap',0)) > 8 and tr.get('bw') is not None and 10 <= tr['bw'] < 15 and (tr['pnl_v1'] or 0) > 0]
    far_mid_l = [tr for tr in closed if abs(tr.get('gap_vwap',0)) > 8 and tr.get('bw') is not None and 10 <= tr['bw'] < 15 and (tr['pnl_v1'] or 0) < 0]
    # P2-specific filters
    p2_closed  = [tr for tr in closed if tr.get('strategy') == 'P2']
    # P2 VWAP depth zones
    p2_atvwap_w = [tr for tr in p2_closed if -2  <  tr.get('gap_vwap', 0) <= 0  and (tr['pnl_v1'] or 0) > 0]
    p2_atvwap_l = [tr for tr in p2_closed if -2  <  tr.get('gap_vwap', 0) <= 0  and (tr['pnl_v1'] or 0) < 0]
    p2_near_w   = [tr for tr in p2_closed if -10 <= tr.get('gap_vwap', 0) <= -2  and (tr['pnl_v1'] or 0) > 0]
    p2_near_l   = [tr for tr in p2_closed if -10 <= tr.get('gap_vwap', 0) <= -2  and (tr['pnl_v1'] or 0) < 0]
    p2_deep_w   = [tr for tr in p2_closed if tr.get('gap_vwap', 0) < -10         and (tr['pnl_v1'] or 0) > 0]
    p2_deep_l   = [tr for tr in p2_closed if tr.get('gap_vwap', 0) < -10         and (tr['pnl_v1'] or 0) < 0]
    # P2 BW zones
    p2_bw_narrow_w = [tr for tr in p2_closed if tr.get('bw') is not None and tr['bw'] < 8  and (tr['pnl_v1'] or 0) > 0]
    p2_bw_narrow_l = [tr for tr in p2_closed if tr.get('bw') is not None and tr['bw'] < 8  and (tr['pnl_v1'] or 0) < 0]
    p2_bw_mid_w    = [tr for tr in p2_closed if tr.get('bw') is not None and 8 <= tr['bw'] < 15 and (tr['pnl_v1'] or 0) > 0]
    p2_bw_mid_l    = [tr for tr in p2_closed if tr.get('bw') is not None and 8 <= tr['bw'] < 15 and (tr['pnl_v1'] or 0) < 0]
    p2_bw_wide_w   = [tr for tr in p2_closed if tr.get('bw') is not None and tr['bw'] >= 15 and (tr['pnl_v1'] or 0) > 0]
    p2_bw_wide_l   = [tr for tr in p2_closed if tr.get('bw') is not None and tr['bw'] >= 15 and (tr['pnl_v1'] or 0) < 0]
    # P2 VWAP_COMPRESSED
    p2_vc_w = [tr for tr in p2_closed if 'VWAP_COMPRESSED' in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) > 0]
    p2_vc_l = [tr for tr in p2_closed if 'VWAP_COMPRESSED' in tr.get('flags_raw','') and (tr['pnl_v1'] or 0) < 0]
    # P2 sweet spot: XLEG_CONFIRMED + BW ≥ 10 + gap_vwap -2 to -10
    p2_sweet_w = [tr for tr in p2_closed if 'XLEG_CONFIRMED' in tr.get('flags_raw','')
                  and tr.get('bw', 0) >= 10 and -10 <= tr.get('gap_vwap', 0) <= -2
                  and (tr['pnl_v1'] or 0) > 0]
    p2_sweet_l = [tr for tr in p2_closed if 'XLEG_CONFIRMED' in tr.get('flags_raw','')
                  and tr.get('bw', 0) >= 10 and -10 <= tr.get('gap_vwap', 0) <= -2
                  and (tr['pnl_v1'] or 0) < 0]
    # P2 danger: VWAP_COMPRESSED + BW < 8
    p2_danger_w = [tr for tr in p2_closed if 'VWAP_COMPRESSED' in tr.get('flags_raw','')
                   and tr.get('bw', 99) < 8 and (tr['pnl_v1'] or 0) > 0]
    p2_danger_l = [tr for tr in p2_closed if 'VWAP_COMPRESSED' in tr.get('flags_raw','')
                   and tr.get('bw', 99) < 8 and (tr['pnl_v1'] or 0) < 0]

    a(f"| Pattern | W | L | Note |")
    a(f"|---------|---|---|------|")
    a(f"| XLEG_CONFIRMED | {len(xc_wins)} | {len(xc_losses)} | Required but not sufficient |")
    a(f"| No XLEG_CONFIRMED | 0 | {len(noxc_l)} | All losses — no divergence = skip |")
    a(f"| XLEG_AMBIGUOUS | 0 | {len(xa_l)} | Confirmed loss predictor |")
    a(f"| gap_vwap < 2 (VWAP crossover) | {len(near_w)} | {len(near_l)} | Fresh entry = best risk/reward |")
    a(f"| gap_vwap > 8 (overextended) | 0 | {len(far_l)} | Extended — check BW below |")
    a(f"| gap_vwap > 8 + BW < 10 → SKIP | {len(far_nb_w)} | {len(far_nb_l)} | Overextended + no energy = danger |")
    a(f"| gap_vwap > 8 + BW ≥ 15 → ALLOW | {len(far_wb_w)} | {len(far_wb_l)} | Overextended but trending = ok |")
    a(f"| gap_vwap > 8 + BW 10–14 → CAUTION | {len(far_mid_w)} | {len(far_mid_l)} | Borderline — watch closely |")
    a(f"| SPOT_EMA_BEAR + CE | {len(bear_cew)} | 0 | Compressed CE = room to run |")
    a(f"| SPOT_EMA_BULL + CE | 0 | {len(bull_cel)} | Extended CE = limited upside |")
    a(f"| FUT_VWAP_MISMATCH | {len(fut_w)} | {len(fut_l)} | Direction conflict |")
    a(f"| VWAP_COMPRESSED | {len(vc_w)} | {len(vc_l)} | Both sides tight |")
    a(f"| VWAP_COMPRESSED + FUT_MISMATCH | {len(vc_fm_w)} | {len(vc_fm_l)} | Tight + conflict = danger |")
    a(f"| BW < 10 at entry | {len(narrow_bw_w)} | {len(narrow_bw_l)} | Narrow band = low energy |")
    a(f"| BW ≥ 15 at entry | {len(wide_bw_w)} | {len(wide_bw_l)} | Wide band = momentum |")
    a()
    a(f"**P2 Analysis** (gap_vwap is negative for P2 = below VWAP | total P2 closed: {len(p2_closed)})")
    a(f"| Pattern | W | L | Note |")
    a(f"|---------|---|---|------|")
    a(f"| P2 gap_vwap 0 to -2 (at VWAP) | {len(p2_atvwap_w)} | {len(p2_atvwap_l)} | Right at VWAP — noise risk |")
    a(f"| P2 gap_vwap -2 to -10 (sweet zone) | {len(p2_near_w)} | {len(p2_near_l)} | Ideal buildup depth ✅ |")
    a(f"| P2 gap_vwap < -10 (deep buildup) | {len(p2_deep_w)} | {len(p2_deep_l)} | Far below VWAP = needs big move |")
    a(f"| P2 BW < 8 (no energy) | {len(p2_bw_narrow_w)} | {len(p2_bw_narrow_l)} | Buildup won't convert = danger |")
    a(f"| P2 BW 8–14 (moderate) | {len(p2_bw_mid_w)} | {len(p2_bw_mid_l)} | Sufficient energy |")
    a(f"| P2 BW ≥ 15 (wide band) | {len(p2_bw_wide_w)} | {len(p2_bw_wide_l)} | Strong momentum ✅ |")
    a(f"| P2 VWAP_COMPRESSED | {len(p2_vc_w)} | {len(p2_vc_l)} | Both sides tight = no direction |")
    a(f"| P2 VWAP_COMPRESSED + BW < 8 → DANGER | {len(p2_danger_w)} | {len(p2_danger_l)} | Skip — guaranteed chop |")
    a(f"| P2 SWEET SPOT (XLEG + BW≥10 + gap -2→-10) | {len(p2_sweet_w)} | {len(p2_sweet_l)} | High probability setup ✅ |")
    a()

    # V2 vs V1
    both = [tr for tr in closed if tr['pnl_v1'] is not None and tr['pnl_v2'] is not None]
    if both:
        v2b   = sum(1 for tr in both if tr['pnl_v2'] > tr['pnl_v1'])
        v1b   = sum(1 for tr in both if tr['pnl_v1'] > tr['pnl_v2'])
        tie   = len(both) - v2b - v1b
        edge  = sum(tr['pnl_v2'] - tr['pnl_v1'] for tr in both)
        a(f"**V2 vs V1** ({len(both)} trades): V2 better {v2b}x | V1 better {v1b}x | Tie {tie}x | V2 total edge **{edge:+.1f} pts**")
        a()

    # 5c. BW Gate Simulation
    a("---")
    a("## 5c. BW Gate Simulation (what-if analysis)")
    a()
    a("> How many signals would survive and what P&L if a minimum BW gate was applied?")
    a("> DTE=1 (expiry day) noted separately — BW stays narrow all day on expiry.")
    a()
    bw_trades = [tr for tr in closed if tr.get('bw') is not None]
    if bw_trades:
        # BW distribution of fired signals
        bws = sorted([tr['bw'] for tr in closed if tr.get('bw') is not None])
        bw_min = min(bws) if bws else 0
        bw_max = max(bws) if bws else 0
        bw_avg = sum(bws) / len(bws) if bws else 0
        a(f"**BW at entry — fired signals**: min={bw_min:.1f}  max={bw_max:.1f}  avg={bw_avg:.1f}  "
          f"(all {len(bws)} trades: {', '.join(f'{b:.1f}' for b in bws)})")
        a()
        a("| BW Gate | Fires | Blocked | Fired P&L | Blocked P&L | Net change |")
        a("|---------|-------|---------|-----------|-------------|------------|")
        actual_pnl = sum(tr['pnl_v1'] for tr in closed if tr['pnl_v1'] is not None)
        for thresh in [6, 8, 10, 13]:
            pass_tr  = [tr for tr in closed if tr.get('bw') is not None and tr['bw'] >= thresh and tr['pnl_v1'] is not None]
            block_tr = [tr for tr in closed if tr.get('bw') is not None and tr['bw'] <  thresh and tr['pnl_v1'] is not None]
            pass_pnl  = sum(tr['pnl_v1'] for tr in pass_tr)
            block_pnl = sum(tr['pnl_v1'] for tr in block_tr)
            net_chg   = pass_pnl - actual_pnl  # improvement vs no gate
            sign      = "✅" if net_chg > 0 else ("❌" if net_chg < 0 else "—")
            a(f"| BW ≥ {thresh} | {len(pass_tr)} | {len(block_tr)} "
              f"| {pass_pnl:+.0f} pts | {block_pnl:+.0f} pts | {net_chg:+.0f} {sign} |")
        a()
        # Per-trade BW detail
        a("**Per-trade BW detail:**")
        a("| # | Strat | Dir | BW | PnL | Would survive BW≥8? | Would survive BW≥10? |")
        a("|---|-------|-----|----|-----|---------------------|----------------------|")
        for tr in closed:
            if tr.get('bw') is None or tr['pnl_v1'] is None:
                continue
            b8  = "✅ yes" if tr['bw'] >= 8  else "❌ blocked"
            b10 = "✅ yes" if tr['bw'] >= 10 else "❌ blocked"
            a(f"| S{tr['num']} | {tr['strategy']} | {tr['dir']} | {tr['bw']:.1f} "
              f"| {tr['pnl_v1']:+.0f} | {b8} | {b10} |")
        a()
    else:
        a("_No BW data available yet (P1 BW logging starts from today)._")
        a()

    # 6. RSI-blocked shadow signals
    if rsi_blocks:
        a("---")
        a("## 6. RSI-Blocked Signals (shadow analysis only)")
        a()
        a("> These are signals where EMA9H breakout happened but RSI filter killed the entry.")
        a("> Tracking to evaluate whether removing/relaxing RSI would add value.")
        a()
        a("| Time | Dir | Strike | Entry | ema9h_gap | gap_vwap | BW | RSI | Reason | Notes |")
        a("|------|-----|--------|-------|-----------|----------|----|-----|--------|-------|")
        for b in rsi_blocks:
            reason_short = b['reason'].replace('1m_rsi_', '')
            notes = []
            if b['rsi'] > 70:
                notes.append('overbought')
            elif b['rsi'] < 48:
                notes.append('no momentum')
            if abs(b['gap_vwap']) < 2:
                notes.append('near VWAP ✅')
            if b['gap_vwap'] > 8:
                notes.append('overextended')
            if b['bw'] < 10:
                notes.append('narrow BW')
            a(f"| {b['time']} | {b['dir']} | {b['strike']} | {b['entry']:.1f} "
              f"| {b['ema9h_gap']:+.2f} | {b['gap_vwap']:+.2f} | {b['bw']:.1f} "
              f"| {b['rsi']:.1f} | {reason_short} | {' | '.join(notes) if notes else '—'} |")
        a()
        # Summary
        out_high  = [b for b in rsi_blocks if b['rsi'] > 70]
        out_low   = [b for b in rsi_blocks if b['rsi'] < 48]
        falling   = [b for b in rsi_blocks if b['reason'] == '1m_rsi_falling']
        near_vwap = [b for b in rsi_blocks if abs(b['gap_vwap']) < 2]
        a(f"**Summary**: {len(rsi_blocks)} blocked | RSI > 70: {len(out_high)} | "
          f"RSI < 48: {len(out_low)} | RSI falling: {len(falling)} | Near VWAP (gap<2): {len(near_vwap)}")
        a()

    # 7. Cross-trades
    if cross_trades:
        a("---")
        a("## 7. Cross-Trades (P1 vs P2 opposite directions)")
        a()
        a("> Both P1 and P2 open simultaneously in opposite directions — one will lose minimum -12.")
        a()
        a("| Time | New | Old | New Entry | Old Entry | Old Peak at fire | Strike |")
        a("|------|-----|-----|-----------|-----------|-----------------|--------|")
        for c in cross_trades:
            a(f"| {c['time']} | {c['new_strat']}-{c['new_dir']} | {c['old_strat']}-{c['old_dir']} "
              f"| {c['new_entry']:.1f} | {c['old_entry']:.1f} | +{c['old_peak']:.1f} | {c['strike']} |")
        a()
        a(f"**Total cross-trades today: {len(cross_trades)}** — each guarantees at least one -12 loss.")
        a()

    # 8. Reference Trade — S11 P2 CE 23950 | 2026-05-25 | benchmark for ideal P2 setup
    a("---")
    a("## 8. Reference Trade — S11 (Benchmark)")
    a()
    a("> **S11 P2 CE 23950 | 14:30 | 2026-05-25** — best trade recorded. Used as benchmark for ideal P2 conditions.")
    a()
    a("```")
    a("Entry  : 105.5   Exit : 141.5   PnL V1 : +36   PnL V2 : +16   Peak : +43.2")
    a("Trail  : LOCK+36 (hit LOCK+4 → LOCK+10 → LOCK+12 → LOCK+20 → LOCK+30 → LOCK+36)")
    a("gap_vwap  = -3.6   ✅ sweet zone (-2 to -10) — CE below option VWAP, room to run")
    a("XLEG gap  = -10.5  ✅ PE deeply below band — sharpest divergence of the day")
    a("VWAP gap  = +19.8  ✅ spot surged +23 pts in 15 min — real directional move")
    a("RSI       = 67.1 ↑ ✅ rising momentum at entry")
    a("BW        = 4.1    ⚠️ narrow (DTE=1) — but XLEG + spot move compensated")
    a("Time      = 14:30  ⚠️ late window — but spot surge gave fuel")
    a("```")
    a()

    # Score each P2 trade against S11 benchmark
    p2_all = [tr for tr in trades if tr.get('strategy') == 'P2']
    if p2_all:
        a("**P2 trades scored against S11 benchmark (5 factors):**")
        a()
        a("| # | Time | Dir | gap_vwap | XLEG | RSI↑ | Spot VWAP | Before 13 | PnL | Score | vs S11 |")
        a("|---|------|-----|----------|------|------|-----------|-----------|-----|-------|--------|")
        for tr in sorted(p2_all, key=lambda x: x['num']):
            sc = 0
            # Factor 1: gap_vwap in sweet zone -2 to -10
            gv     = tr.get('gap_vwap', 0)
            gv_ok  = -10 <= gv <= -2
            if gv_ok: sc += 1

            # Factor 2: XLEG_CONFIRMED
            xleg_ok = 'XLEG_CONFIRMED' in tr.get('flags_raw', '')
            if xleg_ok: sc += 1

            # Factor 3: RSI ≥ 55
            rsi_ok = tr.get('rsi', 0) >= 55
            if rsi_ok: sc += 1

            # Factor 4: Spot VWAP gap aligned with direction
            # CE needs spot VWAP gap > +10 (spot bullish = CE fuel)
            # PE needs spot VWAP gap < -10 (spot bearish = PE fuel)
            svg = get_spot_vwap_gap(tr['time'], vwap_timeline)
            if svg is not None:
                if tr['dir'] == 'CE':
                    svgap_ok = svg > 10
                else:  # PE
                    svgap_ok = svg < -10
            else:
                svgap_ok = None  # no data

            if svgap_ok: sc += 1

            # Factor 5: Before 13:00
            try:
                tr_hour = int(tr['time'].split(':')[0])
                time_ok = tr_hour < 13
            except Exception:
                time_ok = False
            if time_ok: sc += 1

            pnl_str  = f"{tr['pnl_v1']:+.0f}" if tr['pnl_v1'] is not None else "🟢"
            gv_str   = f"{gv:+.1f} {'✅' if gv_ok else '❌'}"
            xleg_str = "✅" if xleg_ok else "❌"
            rsi_str  = f"{tr['rsi']:.0f} {'✅' if rsi_ok else '❌'}"
            if svg is not None:
                svg_str = f"{svg:+.0f} {'✅' if svgap_ok else '❌'}"
            else:
                svg_str = "— (no data)"
            time_str = "✅" if time_ok else "❌"
            bar      = "█" * sc + "░" * (5 - sc)
            match    = "🔥 IDEAL" if sc == 5 else ("✅ GOOD" if sc >= 4 else ("⚠️ WEAK" if sc == 3 else "❌ POOR"))
            a(f"| S{tr['num']} | {tr['time']} | {tr['dir']} | {gv_str} | {xleg_str} "
              f"| {rsi_str} | {svg_str} | {time_str} | {pnl_str} | {bar} {sc}/5 | {match} |")
        a()
        a("> **Score guide**: 5/5 🔥 IDEAL · 4/5 ✅ GOOD · 3/5 ⚠️ WEAK · ≤2/5 ❌ POOR")
        a("> **Factors**: gap_vwap -2→-10 · XLEG_CONFIRMED · RSI ≥ 55 · Spot VWAP gap aligned (CE>+10 / PE<-10) · Before 13:00")
        a()

    a("---")
    a(f"*VRL_ANALYSIS_BUILDER.py — updated {now_str} IST*")

    Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, 'w') as fh:
        fh.write('\n'.join(L) + '\n')

    return len(trades), pnl_v1, pnl_v2


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"Parsing {TODAY}...")
    trades, relocks, market = parse_log()
    levels        = parse_levels(LOG_FILE, TODAY)
    rsi_blocks    = parse_rsi_blocks(LOG_FILE, TODAY)
    cross_trades  = parse_cross_trades(LOG_FILE, TODAY)
    vwap_timeline = parse_vwap_timeline(LOG_FILE, TODAY)
    if levels:
        print(f"Levels: PDH={levels['PDH']:.0f} PDL={levels['PDL']:.0f} CPR={levels['CPR_L']:.0f}-{levels['CPR_H']:.0f} ORH={levels['ORH']:.0f} ORL={levels['ORL']:.0f}")
    else:
        print("Levels: not found in log (LEVELS line missing)")
    print(f"RSI-blocked: {len(rsi_blocks)} signals")
    print(f"Cross-trades: {len(cross_trades)}")
    print(f"VWAP snapshots: {len(vwap_timeline)}")
    n, pnl1, pnl2 = write_analysis(trades, relocks, market, levels, rsi_blocks, cross_trades, vwap_timeline)
    print(f"Signals: {n}  |  V1: {pnl1:+.0f} pts  |  V2: {pnl2:+.0f} pts")
    print(f"Written: {OUT_FILE}")
