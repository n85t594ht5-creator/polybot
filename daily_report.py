#!/usr/bin/env python3
"""
Ежедневный отчёт PolyBot: собирает сделки за день в JSON (для панели) и XLSX (для скачивания).
Кладёт в reports/YYYY-MM-DD.json и reports/YYYY-MM-DD.xlsx. Запускается из workflow раз в сутки.
"""
import json, os, sys, collections
from datetime import datetime, timezone, timedelta

os.makedirs("reports", exist_ok=True)
day = sys.argv[1] if len(sys.argv) > 1 else (datetime.now(timezone.utc) - timedelta(days=0)).strftime("%Y-%m-%d")

state = json.load(open("state.json")) if os.path.exists("state.json") else {}
signals = []
if os.path.exists("signals.csv"):
    import csv as _csv
    with open("signals.csv", encoding="utf-8", errors="ignore") as _f:
        signals = [r for r in _csv.DictReader(_f) if str(r.get("timestamp", ""))[:10] == day]

def _f(v):
    try: return float(v)
    except (TypeError, ValueError): return 0.0

def sig_agg(key):
    out = collections.defaultdict(lambda: {"n": 0, "w": 0, "hyp": 0.0, "real": 0.0})
    for s in signals:
        k = key(s); o = out[k]; o["n"] += 1
        o["w"] += 1 if s.get("resolution") == "WIN" else 0
        o["hyp"] += _f(s.get("hypothetical_pnl")); o["real"] += _f(s.get("realized_pnl"))
    return {k: {**v, "hyp": round(v["hyp"], 2), "real": round(v["real"], 2)} for k, v in out.items()}
closed = state.get("closed") or []
trades = [t for t in closed if str(t.get("opened", ""))[:10] == day]

def agg(key):
    out = collections.defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for t in trades:
        k = key(t); o = out[k]; o["n"] += 1; o["w"] += bool(t.get("won")); o["pnl"] += t.get("pnl", 0)
    return {k: {**v, "pnl": round(v["pnl"], 2)} for k, v in out.items()}

wins = [t for t in trades if t.get("won")]
gp = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in trades if not t.get("won"))
rep = {
    "day": day, "generated": datetime.now(timezone.utc).isoformat(),
    "trades": len(trades), "wins": len(wins), "losses": len(trades) - len(wins),
    "winrate": (len(wins) / len(trades)) if trades else None,
    "pnl": round(sum(t.get("pnl", 0) for t in trades), 2),
    "gross_win": round(gp, 2), "gross_loss": round(-gl, 2),
    "pf": round(gp / gl, 2) if gl else (99.0 if gp else None),
    "bankroll_end": state.get("bankroll"),
    "by_asset": agg(lambda t: t.get("asset", "?")),
    "by_window": agg(lambda t: f"{t.get('minutes', '?')}m"),
    "by_hour": agg(lambda t: str(t.get("opened", ""))[11:13]),
    "by_entry": agg(lambda t: t.get("entry_bucket") or f"{int(float(t.get('entry', 0)) * 20) / 20:.2f}"),
    "by_move": agg(lambda t: t.get("move_bucket") or "—"),
    "by_side": agg(lambda t: t.get("side", "?")),
    "execstats": state.get("execstats") or {},
    "signals": len(signals),
    "signals_executed": sum(1 for s in signals if s.get("signal_status") == "EXECUTED"),
    "signals_blocked": sum(1 for s in signals if s.get("signal_status") == "BLOCKED_BY_RISK"),
    "hypothetical_pnl": round(sum(_f(s.get("hypothetical_pnl")) for s in signals), 2),
    "signals_by_window": sig_agg(lambda s: s.get("window", "?")),
    "signals_by_asset": sig_agg(lambda s: s.get("asset", "?")),
    "signals_by_entry": sig_agg(lambda s: s.get("entry_bucket", "?")),
    "signals_by_gate": sig_agg(lambda s: s.get("risk_gate") or "—"),
    "trade_list": [{k: t.get(k) for k in ("opened", "asset", "side", "minutes", "entry", "cost", "shares", "conf", "move", "won", "pnl", "question")} for t in trades],
}
json.dump(rep, open(f"reports/{day}.json", "w"), ensure_ascii=False, indent=1)

try:
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "Сделки"
    ws.append(["Открыта", "Актив", "Сторона", "Окно", "Вход", "Ставка $", "Акций", "Уверенность", "Движение", "Итог", "P&L $"])
    for t in trades:
        ws.append([t.get("opened"), t.get("asset"), t.get("side"), t.get("minutes"), t.get("entry"), t.get("cost"),
                   t.get("shares"), t.get("conf"), t.get("move"), "WIN" if t.get("won") else "LOSS", t.get("pnl")])
    w2 = wb.create_sheet("Итоги")
    for k in ("day", "trades", "wins", "losses", "winrate", "pnl", "gross_win", "gross_loss", "pf", "bankroll_end"):
        w2.append([k, rep[k]])
    w2.append([]); w2.append(["QUALIFYING SIGNALS (hypothetical — не реальные деньги)"])
    w2.append(["Срез", "сигналов", "побед", "hypothetical P&L", "realized P&L"])
    for nm, key in (("По окнам", "signals_by_window"), ("По активам", "signals_by_asset"),
                    ("По цене входа", "signals_by_entry"), ("По риск-гейтам", "signals_by_gate")):
        w2.append([nm])
        for k, v in sorted(rep[key].items()):
            w2.append([k, v["n"], v["w"], v["hyp"], v["real"]])
    w2.append([]); w2.append(["REALIZED (реальные сделки)"])
    for name, key in (("По активам", "by_asset"), ("По окнам", "by_window"), ("По часам", "by_hour"),
                      ("По цене входа", "by_entry"), ("По силе движения", "by_move"), ("По направлению", "by_side")):
        w2.append([]); w2.append([name, "сделок", "побед", "P&L"])
        for k, v in sorted(rep[key].items()):
            w2.append([k, v["n"], v["w"], v["pnl"]])
    wb.save(f"reports/{day}.xlsx")
except Exception as e:
    print("xlsx skipped:", e)

index = []
for f in sorted(os.listdir("reports")):
    if f.endswith(".json") and f != "index.json":
        try:
            r = json.load(open("reports/" + f))
            index.append({k: r.get(k) for k in ("day", "trades", "wins", "losses", "winrate", "pnl", "pf", "bankroll_end")})
        except Exception:
            pass
json.dump(index, open("reports/index.json", "w"), ensure_ascii=False, indent=1)
print(f"report {day}: {len(trades)} trades, pnl {rep['pnl']}")
