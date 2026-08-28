"""
Доказательство инварианта: qualifying signal, заблокированный риск-гейтом,
попадает в ledger, но НИКОГДА не создаёт исполнение.

Для каждого типа риск-гейта проверяем пять условий:
  1. записан в ledger со статусом BLOCKED_BY_RISK
  2. не создаёт позицию
  3. не увеличивает exposure
  4. не увеличивает число открытых позиций
  5. не считается executed/filled сделкой
"""
import os, sys, csv, tempfile
from datetime import timedelta

os.environ.update(dict(MODE="paper", ASSETS="BTC,ETH,SOL,XRP", WINDOWS="5,15", BANKROLL="100",
    MIN_ELAPSED="0.75", MIN_ENTRY="0.50", MAX_ENTRY="0.62", TIER_ENTRY="0.55", MIN_MOVE="0.0010",
    MIN_MOVE_HIGH="0.0012", MIN_CONF="0.70", KELLY_FRAC="0.10", MAX_STAKE="0.05", MAX_EXPOSURE="0.15",
    MAX_POSITIONS="3", MAX_PER_WINDOW="1", MAX_SAME_DIR="2", DAILY_LOSS_LIMIT="0.30",
    CONSEC_LOSS_LIMIT="5", COOLDOWN_MIN="30", USE_BOOK="1", MAX_SLIP="0.01", REF_MODE="twap"))
os.chdir(tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

REF = {"BTC": 80000.0, "ETH": 2500.0, "SOL": 110.0, "XRP": 2.0}
CUR = {k: v * 1.0015 for k, v in REF.items()}
bot.binance_open_at = lambda a, dt: REF[a]
bot.binance_price = lambda a: CUR[a]
bot.clob_ask = lambda tok: 0.53
bot.clob_book = lambda tok: [(0.53, 100000.0)]
bot.notify = lambda *a, **k: None

R = []
def ck(name, cond, info=""):
    R.append((name, bool(cond))); print(("  OK  " if cond else " FAIL ") + name + ("  " + info if info else ""))

def mkt(a="BTC", m=15, el=0.80, mid=None, start=None):
    n = bot.now(); st = start or (n - timedelta(seconds=m * 60 * el))
    return {"id": mid or f"{a}{m}{el}", "asset": a, "question": "q", "start": st,
            "end": st + timedelta(minutes=m), "minutes": m,
            "up_token": f"{a}-up", "down_token": f"{a}-down"}

def fresh(b=100.0):
    for f in ("state.json", "trades.csv", "missed.csv", "signals.csv"):
        if os.path.exists(f): os.remove(f)
    s = bot.State(); s.bankroll = b; s.day_start_bankroll = b; return s

def ledger():
    if not os.path.exists("signals.csv"): return []
    return list(csv.DictReader(open("signals.csv", encoding="utf-8")))

def prove(label, st, cand, expect_gate):
    """Пять инвариантов для заблокированного сигнала."""
    pos_before = len(st.positions); exp_before = st.exposure()
    es_before = dict(st.execstats)
    ck(f"{label}: помечен {expect_gate}", cand is not None and cand.get("risk_gate") == expect_gate,
       (cand or {}).get("risk_gate", "сигнала нет"))
    if cand is None:
        for extra in ("open_position вернул False", "позиция не создана", "exposure не вырос",
                      "submitted не вырос", "filled не вырос", "в ledger как BLOCKED_BY_RISK",
                      "не помечен executed"):
            ck(f"{label}: {extra}", False, "сигнал не сгенерирован")
        return
    opened = bot.open_position(cand, st)          # прямой вызов в обход развилки цикла
    ck(f"{label}: open_position вернул False", opened is False, str(opened))
    ck(f"{label}: позиция не создана", len(st.positions) == pos_before,
       f"{pos_before} → {len(st.positions)}")
    ck(f"{label}: exposure не вырос", abs(st.exposure() - exp_before) < 1e-9,
       f"{exp_before:.2f} → {st.exposure():.2f}")
    ck(f"{label}: submitted не вырос", st.execstats.get("submitted", 0) == es_before.get("submitted", 0))
    ck(f"{label}: filled не вырос", st.execstats.get("filled", 0) == es_before.get("filled", 0))
    rows = [r for r in st.pending_signals if r["row"]["risk_gate"] == expect_gate]
    ck(f"{label}: в ledger как BLOCKED_BY_RISK",
       any(r["row"]["signal_status"] == "BLOCKED_BY_RISK" for r in rows), f"{len(rows)} записей")
    ck(f"{label}: не помечен executed", all(not r.get("executed") for r in rows))

print("\n══ MAX_PER_WINDOW ══")
st = fresh(1000.0)
start = bot.now() - timedelta(seconds=15 * 60 * 0.8)
c1, _ = bot.evaluate(mkt("BTC", 15, mid="w1", start=start), st)
bot.open_position(c1, st)
c2, _ = bot.evaluate(mkt("ETH", 15, mid="w2", start=start), st)
prove("MAX_PER_WINDOW", st, c2, "MAX_PER_WINDOW")

print("\n══ MAX_SAME_DIR ══")
st = fresh(1000.0)
for i, a in enumerate(["BTC", "ETH"]):
    c, _ = bot.evaluate(mkt(a, 15, 0.80, mid=f"d{i}",
                           start=bot.now() - timedelta(seconds=15 * 60 * 0.8 + i * 60)), st)
    bot.open_position(c, st)
c3, _ = bot.evaluate(mkt("SOL", 15, 0.80, mid="d2",
                         start=bot.now() - timedelta(seconds=15 * 60 * 0.8 + 120)), st)
prove("MAX_SAME_DIR", st, c3, "MAX_SAME_DIR")

print("\n══ MAX_POSITIONS ══")
st = fresh(100000.0)   # денег много, упрёмся именно в число позиций
# 2 UP + 1 DOWN, иначе раньше сработает MAX_SAME_DIR
CUR["SOL"] = REF["SOL"] * 0.9985
# смещения по 30 сек: окна разные (иначе MAX_PER_WINDOW), но все внутри зоны входа
for i, a in enumerate(["BTC", "ETH", "SOL"]):
    c, _ = bot.evaluate(mkt(a, 15, mid=f"p{i}",
                           start=bot.now() - timedelta(seconds=15 * 60 * 0.78 + i * 30)), st)
    if c and not c.get("risk_gate"): bot.open_position(c, st)
CUR["SOL"] = REF["SOL"] * 1.0015
ck("MAX_POSITIONS: открыто 3", len(st.positions) == 3, str(len(st.positions)))
c4, _ = bot.evaluate(mkt("XRP", 15, mid="p3",
                         start=bot.now() - timedelta(seconds=15 * 60 * 0.78 + 90)), st)
prove("MAX_POSITIONS", st, c4, "MAX_POSITIONS")

print("\n══ COOLDOWN ══")
st = fresh()
st.cooldown_until = (bot.now() + timedelta(minutes=10)).isoformat()
c, _ = bot.evaluate(mkt("BTC", 15, 0.80, mid="cd1"), st)
prove("COOLDOWN", st, c, "COOLDOWN")

print("\n══ DAILY_LOSS_LIMIT ══")
st = fresh()
st.day_pnl = -35.0        # лимит 30% от 100 = 30$
c, _ = bot.evaluate(mkt("BTC", 15, 0.80, mid="dl1"), st)
prove("DAILY_LOSS_LIMIT", st, c, "DAILY_LOSS_LIMIT")

print("\n══ MAX_EXPOSURE ══")
st = fresh(1000.0)
st.positions["fake"] = {"cost": 1000.0 * 0.15, "side": "DOWN", "start": "x", "shares": 1, "asset": "BTC"}
c, _ = bot.evaluate(mkt("BTC", 15, 0.80, mid="ex1"), st)
prove("MAX_EXPOSURE", st, c, "MAX_EXPOSURE")

print("\n══ Инвариант после резолва ══")
st = fresh(1000.0)
start = bot.now() - timedelta(seconds=15 * 60 * 0.8)
c1, _ = bot.evaluate(mkt("BTC", 15, mid="r1", start=start), st); bot.open_position(c1, st)
c2, _ = bot.evaluate(mkt("ETH", 15, mid="r2", start=start), st); bot.open_position(c2, st)
for p in st.positions.values(): p["end"] = (bot.now() - timedelta(seconds=120)).isoformat()
for s in st.pending_signals:
    s["end"] = (bot.now() - timedelta(seconds=120)).isoformat(); s["row"]["end"] = s["end"]
bot.binance_open_at = lambda a, dt: REF[a] * 1.002
bot.resolve_positions(st); bot.resolve_signals(st)
bot.binance_open_at = lambda a, dt: REF[a]
rows = ledger()
blocked = [r for r in rows if r["signal_status"] == "BLOCKED_BY_RISK"]
executed = [r for r in rows if r["signal_status"] == "EXECUTED"]
ck("после резолва: 1 executed + 1 blocked", len(executed) == 1 and len(blocked) == 1,
   f"exec={len(executed)} blocked={len(blocked)}")
ck("у blocked нет realized_pnl", blocked and blocked[0]["realized_pnl"] == "",
   repr(blocked[0]["realized_pnl"]) if blocked else "")
ck("у blocked есть hypothetical_pnl", blocked and blocked[0]["hypothetical_pnl"] not in ("", None),
   blocked[0]["hypothetical_pnl"] if blocked else "")
ck("у executed есть realized_pnl", executed and executed[0]["realized_pnl"] not in ("", None))
ck("банкролл изменился ровно на realized", abs(st.bankroll - (1000.0 + float(executed[0]["realized_pnl"]))) < 0.01,
   f"{st.bankroll:.2f}")
ck("hypothetical не влился в банкролл",
   abs(st.bankroll - (1000.0 + float(executed[0]["realized_pnl"]) + float(blocked[0]["hypothetical_pnl"]))) > 0.01)

bad = [n for n, o in R if not o]
print(f"\n{'=' * 52}\nИТОГО: {len(R) - len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
