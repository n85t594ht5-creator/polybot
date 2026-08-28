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
    "by_entry": agg(lambda t: f"{int(float(t.get('entry', 0)) * 20) / 20:.2f}"),
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
    for name, key in (("По активам", "by_asset"), ("По окнам", "by_window"), ("По часам", "by_hour"), ("По цене входа", "by_entry")):
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
