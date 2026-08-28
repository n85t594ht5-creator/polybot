"""Сценарные тесты A–K для bot.py. Сеть не используется: подменяем рыночные функции."""
import os, sys, json, tempfile, importlib
from datetime import datetime, timezone, timedelta

os.environ.update(dict(MODE="paper", ASSETS="BTC,ETH,SOL,XRP", WINDOWS="5,15", BANKROLL="100",
    MIN_ELAPSED="0.75", MIN_ENTRY="0.50", MAX_ENTRY="0.62", TIER_ENTRY="0.55",
    MIN_MOVE="0.0010", MIN_MOVE_HIGH="0.0012", MIN_CONF="0.70", KELLY_FRAC="0.10",
    MAX_STAKE="0.05", MAX_EXPOSURE="0.15", MAX_POSITIONS="3", MAX_PER_WINDOW="1",
    MAX_SAME_DIR="2", DAILY_LOSS_LIMIT="0.30", CONSEC_LOSS_LIMIT="5", COOLDOWN_MIN="30",
    USE_BOOK="1", MAX_SLIP="0.01", REF_MODE="twap"))
tmp = tempfile.mkdtemp(); os.chdir(tmp)
sys.path.insert(0, "/home/claude/repo")
import bot

PRICES = {"BTC": 80000.0, "ETH": 2500.0, "SOL": 110.0, "XRP": 2.0}
REFS   = dict(PRICES)
ASKS   = {}
BOOK_SIZE = 10000.0
def fake_price(a): return PRICES[a]
def fake_open(a, dt): return REFS[a]
def fake_ask(tok): return ASKS.get(tok, 0.55)
def fake_book(tok): return [(ASKS.get(tok, 0.55), BOOK_SIZE)]
bot.binance_price = fake_price; bot.binance_open_at = fake_open
bot.clob_ask = fake_ask; bot.clob_book = fake_book
bot.notify = lambda *a, **k: None

def mkt(asset="BTC", minutes=15, elapsed=0.80, mid=None, start=None):
    n = bot.now(); st = start or (n - timedelta(seconds=minutes*60*elapsed))
    return {"id": mid or f"{asset}-{minutes}-{st.isoformat()}", "asset": asset, "question": "q",
            "start": st, "end": st + timedelta(minutes=minutes), "minutes": minutes,
            "up_token": f"{asset}-up", "down_token": f"{asset}-down"}

def fresh_state(bankroll=100.0):
    for f in ("state.json", "trades.csv", "missed.csv"):
        if os.path.exists(f): os.remove(f)
    s = bot.State(); s.bankroll = bankroll; s.day_start_bankroll = bankroll; return s

R = []
def check(name, cond, info=""):
    R.append((name, bool(cond), info)); print(("  OK  " if cond else " FAIL ") + name + ("  " + info if info else ""))

# ── A: обычная прибыльная сделка ──
print("\nA) прибыльная сделка")
st = fresh_state(); PRICES["BTC"] = 80000*1.0015; REFS["BTC"] = 80000.0; ASKS["BTC-up"] = 0.55
m = mkt("BTC", 15, 0.80)
cand, why = bot.evaluate(m, st)
check("A1 сигнал есть", cand is not None, why)
check("A2 сторона UP", cand and cand["side"] == "UP")
check("A3 ставка <= 5% банкролла", cand and cand["cost"] <= st.bankroll*0.05 + 0.01, f"cost={cand['cost'] if cand else '-'}")
check("A4 бакет входа", cand and cand["entry_bucket"] == "0.50–0.55", cand["entry_bucket"] if cand else "")
ok = bot.open_position(cand, st)
check("A5 позиция открыта", ok and len(st.positions) == 1)
p = st.positions[cand["market_id"]]; p["end"] = (bot.now() - timedelta(seconds=120)).isoformat()
REFS["BTC"] = 80000.0; PRICES["BTC"] = 80200.0
bot.binance_open_at = lambda a, dt: 80200.0
bot.resolve_positions(st)
check("A6 закрыта в плюс", st.closed and st.closed[-1]["won"] and st.bankroll > 100, f"bankroll={st.bankroll:.2f}")
bot.binance_open_at = fake_open

# ── B: обычный проигрыш ──
print("\nB) убыточная сделка")
st = fresh_state(); PRICES["ETH"] = 2500*1.0015; REFS["ETH"] = 2500.0; ASKS["ETH-up"] = 0.55
cand, why = bot.evaluate(mkt("ETH", 15, 0.80), st); bot.open_position(cand, st)
p = list(st.positions.values())[0]; p["end"] = (bot.now() - timedelta(seconds=120)).isoformat()
bot.binance_open_at = lambda a, dt: 2490.0     # ушло вниз → UP проиграл
bot.resolve_positions(st)
check("B1 закрыта в минус", st.closed and not st.closed[-1]["won"])
check("B2 банкролл уменьшился", st.bankroll < 100, f"{st.bankroll:.2f}")
check("B3 счётчик убытков", st.consec_losses == 1)
bot.binance_open_at = fake_open

# ── C: 5 убытков подряд → cooldown ──
print("\nC) 5 убытков подряд → пауза")
st = fresh_state()
for i in range(5):
    PRICES["SOL"] = 110*1.0015; REFS["SOL"] = 110.0; ASKS["SOL-up"] = 0.55
    c, w = bot.evaluate(mkt("SOL", 15, 0.80, mid=f"c{i}"), st)
    assert c, w
    bot.open_position(c, st)
    pp = st.positions[c["market_id"]]; pp["end"] = (bot.now() - timedelta(seconds=120)).isoformat()
    bot.binance_open_at = lambda a, dt: 109.0
    bot.resolve_positions(st)
bot.binance_open_at = fake_open
check("C1 пауза установлена", st.cooldown_until is not None, str(st.cooldown_until)[11:19])
check("C2 счётчик сброшен", st.consec_losses == 0)
ok, why = st.can_trade()
check("C3 торговля остановлена", (not ok) and why == "cooldown", why)
dt_end = bot.parse_iso(str(st.cooldown_until))
check("C4 пауза ~30 мин", 29 <= (dt_end - bot.now()).total_seconds()/60 <= 30.1,
      f"{(dt_end-bot.now()).total_seconds()/60:.1f} мин")

# ── D: пауза закончилась → торговля возобновляется ──
print("\nD) после паузы торговля возобновляется")
st.cooldown_until = (bot.now() - timedelta(seconds=5)).isoformat()
st.day_pnl = 0.0
ok, why = st.can_trade()
check("D1 торговля разрешена", ok, why)
check("D2 счётчик убытков 0", st.consec_losses == 0)

# ── E: дневной лимит ──
print("\nE) дневной лимит 30%")
st = fresh_state()
check("E1 лимит = 30$ при банкролле 100", abs(st.daily_limit_usd() - 30.0) < 1e-9, f"{st.daily_limit_usd()}")
st.day_pnl = -29.0; check("E2 при -29$ торгуем", st.can_trade()[0])
st.day_pnl = -30.5; ok, why = st.can_trade()
check("E3 при -30.5$ стоп", (not ok) and why == "daily loss limit", why)
st.bankroll = 50.0   # банкролл упал, но лимит считается от старта дня
check("E4 лимит не плывёт", abs(st.daily_limit_usd() - 30.0) < 1e-9, f"{st.daily_limit_usd()}")

# ── F: MAX_EXPOSURE ──
print("\nF) экспозиция 15%")
st = fresh_state(bankroll=1000.0)
for i, a in enumerate(["BTC", "ETH", "SOL"]):
    PRICES[a] = REFS[a]*1.0015; ASKS[f"{a}-up"] = 0.55
    c, w = bot.evaluate(mkt(a, 15, 0.80, mid=f"f{i}"), st)
    if c: bot.open_position(c, st)
check("F1 открыто <= MAX_POSITIONS", len(st.positions) <= 3, f"{len(st.positions)}")
check("F2 экспозиция <= 15%", st.exposure() <= st.bankroll*0.15 + 0.01, f"{st.exposure():.2f} / {st.bankroll*0.15:.2f}")
check("F3 гейт от актуального банкролла", st.can_trade()[1] in ("", "max exposure", "max positions"), st.can_trade()[1])
de = st.dir_exposure()
check("F4 directional exposure считается", de["UP"] > 0, str(de))

# ── G: два сигнала в одном окне ──
print("\nG) дубль в одном окне")
st = fresh_state(bankroll=1000.0)
start = bot.now() - timedelta(seconds=15*60*0.8)
PRICES["BTC"] = REFS["BTC"]*1.0015; ASKS["BTC-up"] = 0.55
c1, _ = bot.evaluate(mkt("BTC", 15, mid="g1", start=start), st); bot.open_position(c1, st)
PRICES["ETH"] = REFS["ETH"]*1.0015; ASKS["ETH-up"] = 0.55
c2, why2 = bot.evaluate(mkt("ETH", 15, mid="g2", start=start), st)
# Новый контракт: сигнал не выбрасывается, а помечается risk_gate и идёт в ledger
check("G1 сигнал помечен MAX_PER_WINDOW", c2 is not None and c2.get("risk_gate") == "MAX_PER_WINDOW",
      (c2 or {}).get("risk_gate", why2))
opened = bot.open_position(c2, st)
check("G1b open_position отказал", opened is False and len(st.positions) == 1, f"позиций {len(st.positions)}")

# ── MAX_SAME_DIR ──
print("\nG2) лимит одного направления")
st = fresh_state(bankroll=1000.0)
for i, a in enumerate(["BTC", "ETH", "SOL"]):
    PRICES[a] = REFS[a]*1.0015; ASKS[f"{a}-up"] = 0.55
    c, why = bot.evaluate(mkt(a, 15, 0.80, mid=f"h{i}", start=bot.now()-timedelta(seconds=15*60*0.8+i*60)), st)
    if i == 2:
        check("G2 третий UP помечен MAX_SAME_DIR", c is not None and c.get("risk_gate") == "MAX_SAME_DIR",
              (c or {}).get("risk_gate", why))
    if c: bot.open_position(c, st)          # третий должен быть отклонён самой функцией
check("G3 не больше 2 в одну сторону", sum(1 for p in st.positions.values() if p["side"]=="UP") <= 2)

# ── H: неисполненный GTC ──
print("\nH) ордер не исполнен")
st = fresh_state()
PRICES["BTC"] = REFS["BTC"]*1.0015; ASKS["BTC-up"] = 0.55
c, _ = bot.evaluate(mkt("BTC", 15, 0.80, mid="unf"), st)
orig = bot.place_order
bot.place_order = lambda cand: {"order_id": "x1", "status": "UNFILLED", "filled_shares": 0.0, "filled_cost": 0.0, "avg_fill_price": cand["entry"]}
ok = bot.open_position(c, st)
check("H1 позиция НЕ создана", (not ok) and len(st.positions) == 0)
check("H2 счётчик unfilled", st.execstats["unfilled"] == 1, str(st.execstats["unfilled"]))
check("H3 записано в журнал", os.path.exists("missed.csv") and "не исполнен" in open("missed.csv").read())

# ── I: частичное исполнение ──
print("\nI) частичное исполнение")
st = fresh_state()
c, _ = bot.evaluate(mkt("BTC", 15, 0.80, mid="part"), st)
req_shares, req_cost = c["shares"], c["cost"]
bot.place_order = lambda cand: {"order_id": "x2", "status": "PARTIAL",
                                "filled_shares": round(cand["shares"]*0.4, 2),
                                "filled_cost": round(cand["cost"]*0.4, 2),
                                "avg_fill_price": cand["entry"]+0.005}
ok = bot.open_position(c, st)
p = list(st.positions.values())[0]
check("I1 позиция создана", ok and len(st.positions) == 1)
check("I2 объём = исполненному", abs(p["shares"] - round(req_shares*0.4,2)) < 0.01, f"{p['shares']} из {req_shares}")
check("I3 стоимость = исполненной", abs(p["cost"] - round(req_cost*0.4,2)) < 0.01)
check("I4 остаток записан", p["remaining_shares"] > 0, str(p["remaining_shares"]))
check("I5 счётчик partial", st.execstats["partial"] == 1)
check("I6 проскальзывание учтено", st.execstats["slip_n"] == 1 and st.execstats["slip_sum"] > 0, f"{st.execstats['slip_sum']:.4f}")
bot.place_order = orig

# ── J: 5m поздний сигнал ──
print("\nJ) 5m окно")
st = fresh_state()
PRICES["SOL"] = REFS["SOL"]*1.0015; ASKS["SOL-up"] = 0.55
c, why = bot.evaluate(mkt("SOL", 5, 0.60, mid="j1"), st)
check("J1 при 60% окна вход запрещён", c is None and "elapsed" in why, why)
c, why = bot.evaluate(mkt("SOL", 5, 0.80, mid="j2"), st)
check("J2 при 80% окна вход разрешён", c is not None, why)
check("J3 minutes=5 в позиции", c and c["minutes"] == 5)
c3, why3 = bot.evaluate(mkt("SOL", 5, 0.92, mid="j3"), st)
check("J4 за 24 сек до конца — стоп", c3 is None and "close to end" in why3, why3)

# ── K: 15m поздний сигнал + тир-логика ──
print("\nK) 15m и двухуровневый порог движения")
st = fresh_state()
PRICES["XRP"] = REFS["XRP"]*1.0011; ASKS["XRP-up"] = 0.52      # 0.11% движение, вход 0.52 (<=0.55 → нужен 0.10%)
c, why = bot.evaluate(mkt("XRP", 15, 0.80, mid="k1"), st)
check("K1 дешёвый уровень: 0.11% проходит", c is not None, why)
ASKS["XRP-up"] = 0.58                                            # тот же сигнал, но вход 0.58 → нужен 0.12%
c, why = bot.evaluate(mkt("XRP", 15, 0.80, mid="k2"), st)
check("K2 дорогой уровень: 0.11% не проходит", c is None and "0.120%" in why, why)
PRICES["XRP"] = REFS["XRP"]*1.0013                               # 0.13% → проходит и на дорогом
c, why = bot.evaluate(mkt("XRP", 15, 0.80, mid="k3"), st)
check("K3 дорогой уровень: 0.13% проходит", c is not None, why)
check("K4 бакет 0.55–0.60", c and c["entry_bucket"] == "0.55–0.60", c["entry_bucket"] if c else "")

# ── границы цены ──
print("\nL) границы цены входа")
st = fresh_state()
PRICES["BTC"] = REFS["BTC"]*1.0015
ASKS["BTC-up"] = 0.48; c, why = bot.evaluate(mkt("BTC", 15, 0.80, mid="l1"), st)
check("L1 дешевле 0.50 не берём", c is None and "MIN_ENTRY" in why, why)
ASKS["BTC-up"] = 0.66; c, why = bot.evaluate(mkt("BTC", 15, 0.80, mid="l2"), st)
check("L2 дороже 0.62 не берём", c is None and "0.62" in why, why)

# ── сохранение состояния ──
print("\nM) персистентность состояния")
st = fresh_state(); st.day_pnl = -5.0; st.consec_losses = 2
st.cooldown_until = (bot.now()+timedelta(minutes=10)).isoformat(); st.save()
st2 = bot.State()
check("M1 банкролл восстановлен", abs(st2.bankroll - st.bankroll) < 1e-9)
check("M2 дневной P&L восстановлен", abs(st2.day_pnl + 5.0) < 1e-9)
check("M3 пауза восстановлена", not st2.can_trade()[0])
check("M4 execstats восстановлены", isinstance(st2.execstats, dict))

bad = [n for n, ok, _ in R if not ok]
print(f"\n{'='*50}\nИТОГО: {len(R)-len(bad)}/{len(R)} пройдено")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
