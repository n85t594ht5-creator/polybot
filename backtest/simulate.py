#!/usr/bin/env python3
"""
Симуляция стратегии PolyBot по собранным окнам + перебор настроек.
Выход: results/grid.csv, results/report.md, docs/backtest.html
"""
import csv, itertools, json, math, os, sys
from collections import defaultdict

W = json.load(open("data/windows.json")); C = json.load(open("data/candles.json"))
C = {a: {int(k): v for k, v in d.items()} for a, d in C.items()}
BANKROLL = float(os.getenv("BANKROLL", "500")); SPREAD = float(os.getenv("SPREAD", "0.01"))  # ask ≈ mid + spread
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
            if ask > P["MAX_ENTRY"] or ask <= 0.01: continue
            if ask > P["TIER_ENTRY"] and abs(move) < P["MIN_MOVE_HIGH"]: continue
            conf = min(0.95, 0.5 + el * 0.3 + min(abs(move) / 0.005, 1) * 0.15)
            if conf < P["MIN_CONF"]: continue
            won = (side == "UP") == w["up_won"]
            trades.append({"t": w["start"], "asset": w["asset"], "min": w["minutes"], "side": side, "entry": ask, "conf": conf, "won": won})
            break
    trades.sort(key=lambda x: x["t"])
    bank, peak, dd, wins, gp, gl = BANKROLL, BANKROLL, 0.0, 0, 0.0, 0.0
    for tr in trades:
        b = (1 - tr["entry"]) / tr["entry"]; k = max(0.0, (tr["conf"] * b - (1 - tr["conf"])) / b)
        size = min(k * P["KELLY_FRAC"] * bank, bank * 0.25)
        if size < 1: continue
        pnl = size * b if tr["won"] else -size
        tr["size"], tr["pnl"] = round(size, 2), round(pnl, 2)
        bank += pnl; peak = max(peak, bank); dd = max(dd, (peak - bank) / peak)
        if tr["won"]: wins += 1; gp += pnl
        else: gl += -pnl
    n = len(trades)
    return trades, {"trades": n, "winrate": wins / n if n else 0, "pf": (gp / gl) if gl else (99 if gp else 0),
                    "pnl": bank - BANKROLL, "final": bank, "max_dd": dd, "avg_entry": sum(t["entry"] for t in trades) / n if n else 0}

GRID = {
    "MIN_ELAPSED": [0.5, 0.65, 0.75, 0.85],
    "MAX_ENTRY": [0.35, 0.5, 0.62, 0.75],
    "MIN_MOVE": [0.0004, 0.0006, 0.001],
    "TIER_ENTRY": [0.45],
    "MIN_MOVE_HIGH": [0.0008, 0.0012, 0.002],
    "MIN_CONF": [0.6, 0.65, 0.72],
    "KELLY_FRAC": [0.15],
}
keys = list(GRID); rows = []
for vals in itertools.product(*GRID.values()):
    P = dict(zip(keys, vals)); _, m = run(P); rows.append({**P, **m})
rows.sort(key=lambda r: (r["trades"] >= MIN_TRADES, r["pf"] if r["trades"] >= MIN_TRADES else 0, r["pnl"]), reverse=True)
with open("results/grid.csv", "w", newline="") as f:
    wr = csv.DictWriter(f, fieldnames=list(rows[0].keys())); wr.writeheader(); wr.writerows(rows)

# Текущие настройки бота и лучшая
CURRENT = {"MIN_ELAPSED": 0.65, "MAX_ENTRY": 0.62, "MIN_MOVE": 0.0006, "TIER_ENTRY": 0.45, "MIN_MOVE_HIGH": 0.0012, "MIN_CONF": 0.65, "KELLY_FRAC": 0.15}
cur_tr, cur = run(CURRENT)
good = [r for r in rows if r["trades"] >= MIN_TRADES]
best = good[0] if good else rows[0]
BP = {k: best[k] for k in keys}; best_tr, bm = run(BP)

def breakdown(P):
    out = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    tr, _ = run(P)
    for t in tr:
        for key in (t["asset"], f"{t['min']}m", f"{t['asset']}-{t['min']}m"):
            o = out[key]; o["n"] += 1; o["w"] += t["won"]; o["pnl"] += t.get("pnl", 0)
    return dict(out)

def fmtm(m): return f"сделок {m['trades']} · winrate {m['winrate']:.0%} · PF {m['pf']:.2f} · P&L {m['pnl']:+.0f} $ · макс. просадка {m['max_dd']:.0%}"
def brow(k, v): return f"| {k} | {v['n']} | {v['w']/v['n']:.0%} | {v['pnl']:+.0f} |"

nw = len(TRAJ); by = defaultdict(int)
for w in TRAJ: by[f"{w['asset']} {w['minutes']}m"] += 1
base = sum(1 for w in TRAJ if w["up_won"]) / nw if nw else 0

md = [f"# Бэктест PolyBot", "", f"Окон с данными: **{nw}** (" + ", ".join(f"{k}: {v}" for k, v in sorted(by.items())) + f"). Доля Up-исходов: {base:.0%}.",
      f"Банкролл {BANKROLL:.0f} $, спред {SPREAD}, минимум сделок для рейтинга {MIN_TRADES}.", "",
      "## Текущие настройки бота", fmtm(cur), "",
      "## Лучшая конфигурация", ", ".join(f"`{k}={v}`" for k, v in BP.items()), "", fmtm(bm), "",
      "### Разбивка лучшей конфигурации", "| Срез | Сделок | Winrate | P&L |", "|---|---|---|---|"]
md += [brow(k, v) for k, v in sorted(breakdown(BP).items())]
md += ["", "## Топ-15 конфигураций", "| " + " | ".join(keys[:6]) + " | сделок | winrate | PF | P&L | просадка |", "|" + "---|" * 11]
for r in rows[:15]:
    md.append("| " + " | ".join(str(r[k]) for k in keys[:6]) + f" | {r['trades']} | {r['winrate']:.0%} | {r['pf']:.2f} | {r['pnl']:+.0f} | {r['max_dd']:.0%} |")
md += ["", "## Как читать", "- PF > 1.3 и просадка < 20% при ≥ 30 сделках — есть о чём говорить. PF около 1 — монетка.",
       "- Бэктест не учитывает проскальзывание и то, что по нужной цене могло не быть объёма. Реальный результат хуже.",
       "- Если лучшие конфигурации дают мало сделок — преимущество узкое и хрупкое."]
open("results/report.md", "w").write("\n".join(md))

html = "<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>PolyBot backtest</title><style>body{background:#0d1420;color:#e4ebf5;font:14px/1.5 system-ui;max-width:1100px;margin:0 auto;padding:16px}table{border-collapse:collapse;font:12px monospace;width:100%;overflow:auto;display:block}td,th{padding:5px 8px;border-bottom:1px solid #243248;white-space:nowrap;text-align:left}th{color:#7f8da6}code{color:#8ab4ff}h1,h2,h3{font-weight:600}a{color:#8ab4ff}</style>"
import re
body = "\n".join(md)
body = re.sub(r"^# (.*)$", r"<h1>\1</h1>", body, flags=re.M); body = re.sub(r"^## (.*)$", r"<h2>\1</h2>", body, flags=re.M); body = re.sub(r"^### (.*)$", r"<h3>\1</h3>", body, flags=re.M)
body = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", body); body = re.sub(r"`(.*?)`", r"<code>\1</code>", body)
lines, out, intable = body.split("\n"), [], False
for l in lines:
    if l.startswith("|"):
        cells = [c.strip() for c in l.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "): continue
        if not intable: out.append("<table>"); intable = True; out.append("<tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>"); continue
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    else:
        if intable: out.append("</table>"); intable = False
        out.append(f"<p>{l[2:]}</p>" if l.startswith("- ") else (l if l.startswith("<") else f"<p>{l}</p>" if l.strip() else ""))
if intable: out.append("</table>")
open("docs/backtest.html", "w").write(html + "<p><a href='./'>← дашборд</a></p>" + "\n".join(out))
print("\n".join(md[:12]))
