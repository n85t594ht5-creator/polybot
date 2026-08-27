#!/usr/bin/env python3
"""
Симуляция стратегии PolyBot по собранным окнам + перебор настроек.
Выход: results/grid.csv, results/report.md, docs/backtest.html
"""
import csv, itertools, json, math, os, sys
from collections import defaultdict

W = json.load(open("data/windows.json")); C = json.load(open("data/candles.json"))
C = {a: {int(k): v for k, v in d.items()} for a, d in C.items()}
BANKROLL = float(os.getenv("BANKROLL", "500")); SPREAD = float(os.getenv("SPREAD", "0.04"))  # ask ≈ last + spread (пессимистично)
FLAT_STAKE = float(os.getenv("FLAT_STAKE", "0.05"))   # фиксированная ставка: доля стартового банкролла, без реинвеста
MIN_TRADES = int(os.getenv("MIN_TRADES", "30"))
os.makedirs("results", exist_ok=True); os.makedirs("docs", exist_ok=True)

def price_at(asset, ts):
    """Цена, известная в момент ts: открытие текущей минуты (без заглядывания вперёд)."""
    c = C.get(asset, {}); m = (ts // 60) * 60
    if m in c: return c[m][0]
    for d in (60, 120, 180):
        if m - d in c: return c[m - d][1]
    return None

def open_at(asset, ts):
    c = C.get(asset, {}); m = (ts // 60) * 60
    return c[m][0] if m in c else None

# Предрасчёт "траектории" каждого окна: список (elapsed, move, up_ask, down_ask)
TRAJ = []
for w in W:
    if len(w["up_hist"]) < 3: continue
    ref = open_at(w["asset"], w["start"])
    if not ref: continue
    steps = []
    for t, p in w["up_hist"]:
        if t < w["start"] or t > w["end"]: continue
        cur = price_at(w["asset"], t)
        if cur is None: continue
        el = (t - w["start"]) / (w["minutes"] * 60)
        steps.append((el, (cur - ref) / ref, min(0.99, p + SPREAD), min(0.99, (1 - p) + SPREAD), w["end"] - t))
    if steps: TRAJ.append({**w, "steps": steps})
print(f"windows usable: {len(TRAJ)} of {len(W)}")

def run(P, assets=None, windows=None):
    """Одна конфигурация → список сделок (в хронологии) и метрики."""
    trades = []
    for w in TRAJ:
        if assets and w["asset"] not in assets: continue
        if windows and w["minutes"] not in windows: continue
        for el, move, ua, da, left in w["steps"]:
            if el < P["MIN_ELAPSED"] or left < 30 or abs(move) < P["MIN_MOVE"]: continue
            side = "UP" if move > 0 else "DOWN"; ask = ua if side == "UP" else da
            if ask > P["MAX_ENTRY"] or ask <= 0.01 or ask < P.get("MIN_ENTRY", 0): continue
            if ask > P["TIER_ENTRY"] and abs(move) < P["MIN_MOVE_HIGH"]: continue
            conf = min(0.95, 0.5 + el * 0.3 + min(abs(move) / 0.005, 1) * 0.15)
            if conf < P["MIN_CONF"]: continue
            won = (side == "UP") == w["up_won"]
            trades.append({"t": w["start"], "asset": w["asset"], "min": w["minutes"], "side": side, "entry": ask, "conf": conf, "won": won})
            break
    trades.sort(key=lambda x: x["t"])
    bank, peak, dd, wins, gp, gl = BANKROLL, BANKROLL, 0.0, 0, 0.0, 0.0
    size = BANKROLL * FLAT_STAKE
    for tr in trades:
        b = (1 - tr["entry"]) / tr["entry"]
        pnl = size * b if tr["won"] else -size
        tr["size"], tr["pnl"] = round(size, 2), round(pnl, 2)
        bank += pnl; peak = max(peak, bank); dd = max(dd, (peak - bank) / max(peak, 1))
        if tr["won"]: wins += 1; gp += pnl
        else: gl += -pnl
    n = len(trades)
    return trades, {"trades": n, "winrate": wins / n if n else 0, "pf": (gp / gl) if gl else (99 if gp else 0),
                    "pnl": bank - BANKROLL, "final": bank, "max_dd": dd, "avg_entry": sum(t["entry"] for t in trades) / n if n else 0,
                    "exp_per_trade": (bank - BANKROLL) / n / size if n else 0}

GRID = {
    "MIN_ELAPSED": [0.65, 0.75, 0.85],
    "MIN_ENTRY": [0.0, 0.45, 0.5, 0.55],
    "MAX_ENTRY": [0.62, 0.7, 0.8, 0.9, 0.99],
    "MIN_MOVE": [0.0006, 0.001, 0.0015],
    "TIER_ENTRY": [0.45],
    "MIN_MOVE_HIGH": [0.0008, 0.0012],
    "MIN_CONF": [0.6],
    "KELLY_FRAC": [0.15],
}
keys = list(GRID); rows = []
for vals in itertools.product(*GRID.values()):
    P = dict(zip(keys, vals)); _, m = run(P); rows.append({**P, **m})
rows.sort(key=lambda r: (r["trades"] >= MIN_TRADES, r["pf"] if r["trades"] >= MIN_TRADES else 0, r["pnl"]), reverse=True)
with open("results/grid.csv", "w", newline="") as f:
    wr = csv.DictWriter(f, fieldnames=list(rows[0].keys())); wr.writeheader(); wr.writerows(rows)

# Текущие настройки бота и лучшая
CURRENT = {"MIN_ELAPSED": 0.65, "MIN_ENTRY": 0.0, "MAX_ENTRY": 0.62, "MIN_MOVE": 0.0006, "TIER_ENTRY": 0.45, "MIN_MOVE_HIGH": 0.0012, "MIN_CONF": 0.65, "KELLY_FRAC": 0.15}
cur_tr, cur = run(CURRENT)
good = [r for r in rows if r["trades"] >= MIN_TRADES]
best = good[0] if good else rows[0]
BP = {k: best[k] for k in keys}; best_tr, bm = run(BP)

def breakdown(P):
    out = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    tr, _ = run(P)
    for t in tr:
        lo = int(t["entry"] * 20) / 20; eb = f"вход {lo:.2f}–{lo+0.05:.2f}"
        for key in (t["asset"], f"{t['min']}m", f"{t['asset']}-{t['min']}m", eb):
            o = out[key]; o["n"] += 1; o["w"] += t["won"]; o["pnl"] += t.get("pnl", 0)
    return dict(out)

def fmtm(m): return f"сделок {m['trades']} · winrate {m['winrate']:.0%} · PF {m['pf']:.2f} · P&L {m['pnl']:+.0f} $ · макс. просадка {m['max_dd']:.0%} · ожидание {m['exp_per_trade']:+.1%} на ставку"
def brow(k, v): return f"| {k} | {v['n']} | {v['w']/v['n']:.0%} | {v['pnl']:+.0f} |"

nw = len(TRAJ); by = defaultdict(int)
for w in TRAJ: by[f"{w['asset']} {w['minutes']}m"] += 1
base = sum(1 for w in TRAJ if w["up_won"]) / nw if nw else 0

md = [f"# Бэктест PolyBot", "", f"Окон с данными: **{nw}** (" + ", ".join(f"{k}: {v}" for k, v in sorted(by.items())) + f"). Доля Up-исходов: {base:.0%}.",
      f"Банкролл {BANKROLL:.0f} $, ставка фиксированная {FLAT_STAKE:.0%} без реинвеста, спред {SPREAD} (пессимистично), минимум сделок для рейтинга {MIN_TRADES}.", "",
      "## Текущие настройки бота", fmtm(cur), "",
      "## Лучшая конфигурация", ", ".join(f"`{k}={v}`" for k, v in BP.items()), "", fmtm(bm), "",
      "### Разбивка лучшей конфигурации", "| Срез | Сделок | Winrate | P&L |", "|---|---|---|---|"]
md += [brow(k, v) for k, v in sorted(breakdown(BP).items())]
WIDE = {**BP, "MIN_ENTRY": 0.5, "MAX_ENTRY": 0.99}
_, wm = run(WIDE)
md += ["", "### Вход от 0.50 без верхней границы (остальное как в лучшей)", fmtm(wm), "", "| Срез | Сделок | Winrate | P&L |", "|---|---|---|---|"]
md += [brow(k, v) for k, v in sorted(breakdown(WIDE).items()) if k.startswith("вход")]
SHOW = ["MIN_ELAPSED", "MIN_ENTRY", "MAX_ENTRY", "MIN_MOVE", "MIN_MOVE_HIGH"]
md += ["", "## Топ-20 конфигураций", "| " + " | ".join(SHOW) + " | сделок | winrate | PF | P&L | просадка |", "|" + "---|" * 10]
for r in rows[:20]:
    md.append("| " + " | ".join(str(r[k]) for k in SHOW) + f" | {r['trades']} | {r['winrate']:.0%} | {r['pf']:.2f} | {r['pnl']:+.0f} | {r['max_dd']:.0%} |")
md += ["", "## Как читать", "- PF > 1.3 и просадка < 20% при ≥ 30 сделках — есть о чём говорить. PF около 1 — монетка.",
       "- Бэктест не учитывает проскальзывание и то, что по нужной цене могло не быть объёма. Реальный результат хуже.",
       "- Если лучшие конфигурации дают мало сделок — преимущество узкое и хрупкое."]
open("results/report.md", "w").write("\n".join(md))

print("\n".join(md[:12]))


# ── Кривые капитала: фиксированная ставка vs дробный Келли ──
def equity(trades, mode, frac=0.15, cap=0.10, bank0=BANKROLL):
    bank, curve, peak, dd = bank0, [bank0], bank0, 0.0
    for tr in trades:
        b = (1 - tr["entry"]) / tr["entry"]
        if mode == "flat":
            size = bank0 * FLAT_STAKE
        else:
            k = max(0.0, (tr["conf"] * b - (1 - tr["conf"])) / b)
            size = min(k * frac * bank, bank * cap)
        if size < 1 or size > bank: size = min(size, bank)
        bank += size * b if tr["won"] else -size
        peak = max(peak, bank); dd = max(dd, (peak - bank) / peak); curve.append(round(bank, 2))
    return curve, dd

# базовый набор сделок: текущие настройки бота (честнее, чем лучшая по сетке)
base_trades, _ = run(CURRENT)
curves = {}
for name, mode, frac in [("flat", "flat", 0), ("kelly_0.10", "kelly", 0.10), ("kelly_0.15", "kelly", 0.15), ("kelly_0.25", "kelly", 0.25)]:
    c, dd = equity(base_trades, mode, frac); curves[name] = {"curve": c, "max_dd": round(dd, 3), "final": c[-1]}
json.dump({"trades": [{k: t[k] for k in ("t", "asset", "min", "side", "entry", "conf", "won")} for t in base_trades], "curves": curves},
          open("results/equity.json", "w"))
print("equity:", {k: (v["final"], v["max_dd"]) for k, v in curves.items()})


# ── Страница бэктеста ──
from datetime import datetime, timezone
def pack(label, P):
    tr, m = run(P); wins = sum(1 for t in tr if t["won"])
    br = breakdown(P); buckets = {k: v for k, v in br.items() if k.startswith("вход")}; other = {k: v for k, v in br.items() if not k.startswith("вход")}
    cv = {}
    for name, mode, frac in [("flat", "flat", 0), ("kelly_0.10", "kelly", 0.10), ("kelly_0.15", "kelly", 0.15), ("kelly_0.25", "kelly", 0.25)]:
        c, dd = equity(tr, mode, frac); cv[name] = {"curve": c, "max_dd": round(dd, 3), "final": c[-1]}
    return {"label": label, "params": {k: v for k, v in P.items() if k != "KELLY_FRAC"}, "metrics": {**m, "wins": wins},
            "buckets": buckets, "breakdown": other, "curves": cv,
            "trades": [{k: t[k] for k in ("t", "asset", "min", "side", "entry", "won")} for t in tr]}
SHOW_COLS = [k for k in keys if len(GRID[k]) > 1]
page = {"generated": datetime.now(timezone.utc).isoformat(), "windows": nw, "days": round((max(w["end"] for w in TRAJ) - min(w["start"] for w in TRAJ)) / 86400, 1) if TRAJ else 0,
        "spread": SPREAD, "stake": round(BANKROLL * FLAT_STAKE), "bankroll": BANKROLL, "grid_cols": SHOW_COLS,
        "grid": [{**{k: r[k] for k in SHOW_COLS}, **{k: r[k] for k in ("trades", "winrate", "pf", "pnl", "max_dd")}} for r in rows[:40]],
        "configs": {"current": pack("Текущие настройки бота", CURRENT), "best": pack("Лучшая по сетке", BP), "wide": pack("Вход ≥0.50, без верха", WIDE)}}
tpl = open("template.html", encoding="utf-8").read()
open("docs/backtest.html", "w", encoding="utf-8").write(tpl.replace("__DATA__", json.dumps(page, ensure_ascii=False, default=str)))
print("docs/backtest.html written")
