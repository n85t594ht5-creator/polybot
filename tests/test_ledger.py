"""Тесты signal ledger (17 пунктов ТЗ). Сеть замокана."""
import os, sys, csv, json, tempfile
from datetime import timedelta
os.environ.update(dict(MODE="paper", ASSETS="BTC,ETH,SOL,XRP", WINDOWS="5,15", BANKROLL="100",
    MIN_ELAPSED="0.75", MIN_ENTRY="0.50", MAX_ENTRY="0.62", TIER_ENTRY="0.55", MIN_MOVE="0.0010",
    MIN_MOVE_HIGH="0.0012", MIN_CONF="0.70", KELLY_FRAC="0.10", MAX_STAKE="0.05", MAX_EXPOSURE="0.15",
    MAX_POSITIONS="3", MAX_PER_WINDOW="1", MAX_SAME_DIR="2", DAILY_LOSS_LIMIT="0.30",
    CONSEC_LOSS_LIMIT="5", COOLDOWN_MIN="30", USE_BOOK="1", MAX_SLIP="0.01", REF_MODE="twap"))
work = tempfile.mkdtemp(); os.chdir(work); sys.path.insert(0, "/home/claude/repo")
import bot
REF = {"BTC": 80000.0, "ETH": 2500.0, "SOL": 110.0, "XRP": 2.0}
CUR = {k: v*1.0015 for k, v in REF.items()}
ASK = {}
DEPTH = 100000.0
bot.binance_open_at = lambda a, dt: REF[a]
bot.binance_price = lambda a: CUR[a]
bot.clob_ask = lambda tok: ASK.get(tok, 0.53)
bot.clob_book = lambda tok: [(ASK.get(tok, 0.53), DEPTH)]
bot.notify = lambda *a, **k: None
R=[]
def ck(n, c, i=""):
    R.append((n, bool(c))); print(("  OK  " if c else " FAIL ")+n+("  "+i if i else ""))
def mkt(a="BTC", m=15, el=0.80, mid=None, start=None):
    nn=bot.now(); st=start or (nn - timedelta(seconds=m*60*el))
    return {"id": mid or f"{a}{m}{el}", "asset": a, "question":"q", "start": st,
            "end": st+timedelta(minutes=m), "minutes": m, "up_token": f"{a}-up", "down_token": f"{a}-down"}
def fresh(b=100.0):
    for f in ("state.json","trades.csv","missed.csv","signals.csv"):
        if os.path.exists(f): os.remove(f)
    s=bot.State(); s.bankroll=b; s.day_start_bankroll=b; return s
def rows():
    return list(csv.DictReader(open("signals.csv", encoding="utf-8"))) if os.path.exists("signals.csv") else []
def close(st, sig_win=True, asset="BTC"):
    """Двигаем окна в прошлое и резолвим."""
    for p in st.positions.values(): p["end"]=(bot.now()-timedelta(seconds=120)).isoformat()
    for s in st.pending_signals: s["end"]=(bot.now()-timedelta(seconds=120)).isoformat(); s["row"]["end"]=s["end"]
    bot.binance_open_at=lambda a,dt: REF[a]*(1.002 if sig_win else 0.998)
    bot.resolve_positions(st); bot.resolve_signals(st)
    bot.binance_open_at=lambda a,dt: REF[a]

print("\n1) qualifying signal сохраняется")
st=fresh(); c,w=bot.evaluate(mkt("BTC",15,0.80,mid="s1"),st); bot.open_position(c,st)
ck("сигнал в очереди", len(st.pending_signals)==1, w)
close(st, True)
r=rows(); ck("строка записана", len(r)==1)
ck("статус EXECUTED", r and r[0]["signal_status"]=="EXECUTED", r[0]["signal_status"] if r else "")
ck("resolution заполнен", r and r[0]["resolution"]=="WIN", r[0]["resolution"] if r else "")
ck("realized_pnl есть", r and r[0]["realized_pnl"] not in ("",None), r[0]["realized_pnl"] if r else "")
ck("все поля схемы", r and list(r[0].keys())==bot.SIGNAL_FIELDS)

print("\n2) blocked signal сохраняется")
st=fresh(1000.0)
start=bot.now()-timedelta(seconds=15*60*0.8)
c1,_=bot.evaluate(mkt("BTC",15,mid="b1",start=start),st); bot.open_position(c1,st)
c2,_=bot.evaluate(mkt("ETH",15,mid="b2",start=start),st)
ck("сигнал не выброшен", c2 is not None)
ck("помечен MAX_PER_WINDOW", c2 and c2["risk_gate"]=="MAX_PER_WINDOW", c2["risk_gate"] if c2 else "")
bot.register_signal(c2,st,"BLOCKED_BY_RISK")
close(st, True)
r=rows(); blocked=[x for x in r if x["signal_status"]=="BLOCKED_BY_RISK"]
ck("blocked записан", len(blocked)==1)
ck("risk_gate в строке", blocked and blocked[0]["risk_gate"]=="MAX_PER_WINDOW")

print("\n3) hypothetical рассчитан, 4) не смешан с realized")
ck("hypothetical_pnl есть", blocked and blocked[0]["hypothetical_pnl"] not in ("",None), blocked[0]["hypothetical_pnl"] if blocked else "")
ck("realized_pnl пуст у blocked", blocked and blocked[0]["realized_pnl"]=="", repr(blocked[0]["realized_pnl"]) if blocked else "")
ex=[x for x in r if x["signal_status"]=="EXECUTED"]
ck("у executed есть оба", ex and ex[0]["realized_pnl"] not in ("",None) and ex[0]["hypothetical_pnl"] not in ("",None))
ck("hyp положителен при WIN", blocked and float(blocked[0]["hypothetical_pnl"])>0)

print("\n5) 5m и 15m не смешиваются")
st=fresh()
c,_=bot.evaluate(mkt("SOL",5,0.80,mid="w5"),st); bot.register_signal(c,st,"BLOCKED_BY_RISK")
c,_=bot.evaluate(mkt("SOL",15,0.80,mid="w15"),st); bot.register_signal(c,st,"BLOCKED_BY_RISK")
close(st,True); r=rows()
ck("window=5m/15m различимы", {x["window"] for x in r}=={"5m","15m"}, str({x["window"] for x in r}))

print("\n6) asset breakdown, 12) направление")
st=fresh()
for a in ("BTC","ETH","SOL","XRP"):
    CUR[a]=REF[a]*1.0015
    c,_=bot.evaluate(mkt(a,15,0.80,mid="a"+a),st); bot.register_signal(c,st,"BLOCKED_BY_RISK")
CUR["BTC"]=REF["BTC"]*0.9985
c,_=bot.evaluate(mkt("BTC",15,0.80,mid="adn"),st); bot.register_signal(c,st,"BLOCKED_BY_RISK")
close(st,True); r=rows()
ck("4 актива в журнале", {x["asset"] for x in r}=={"BTC","ETH","SOL","XRP"})
ck("оба направления", {x["direction"] for x in r}=={"UP","DOWN"})
CUR["BTC"]=REF["BTC"]*1.0015

print("\n7) entry buckets, 8) move buckets, 9) elapsed buckets")
st=fresh()
cases=[("BTC",0.52,1.0011,"0.50–0.55"),("ETH",0.55,1.0011,"0.50–0.55"),("SOL",0.58,1.0013,"0.55–0.60"),("XRP",0.61,1.0025,"0.60–0.62")]
for a,ask,mv,exp in cases:
    ASK[f"{a}-up"]=ask; CUR[a]=REF[a]*mv
    c,w=bot.evaluate(mkt(a,15,0.80,mid=f"e{a}{ask}"),st)
    ck(f"бакет {ask} = {exp}", c and c["entry_bucket"]==exp, (c["entry_bucket"] if c else w))
    if c:
        req=0.0012 if ask>0.55 else 0.0010
        ck(f"  required_move {ask}", abs(c["required_move"]-req)<1e-9, str(c["required_move"]))
        bot.register_signal(c,st,"BLOCKED_BY_RISK")
for a in ("BTC","ETH","SOL","XRP"): ASK[f"{a}-up"]=0.53; CUR[a]=REF[a]*1.0015
for el,exp in ((0.76,"75–80%"),(0.82,"80–85%"),(0.87,"85–90%")):
    c,_=bot.evaluate(mkt("BTC",15,el,mid=f"el{el}"),st)
    ck(f"elapsed {el} = {exp}", c and c["elapsed_bucket"]==exp, c["elapsed_bucket"] if c else "")
CUR["BTC"]=REF["BTC"]*1.0011
c,_=bot.evaluate(mkt("BTC",15,0.80,mid="mb1"),st); ck("move bucket 0.11% ", c and c["move_bucket"]=="0.10–0.12%", c["move_bucket"] if c else "")
CUR["BTC"]=REF["BTC"]*1.0025
c,_=bot.evaluate(mkt("BTC",15,0.80,mid="mb2"),st); ck("move bucket 0.25%", c and c["move_bucket"]=="0.20%+", c["move_bucket"] if c else "")
CUR["BTC"]=REF["BTC"]*1.0013
c,_=bot.evaluate(mkt("BTC",15,0.80,mid="mb3"),st); ck("move bucket 0.13%", c and c["move_bucket"]=="0.12–0.15%", c["move_bucket"] if c else "")
CUR["BTC"]=REF["BTC"]*1.0017
c,_=bot.evaluate(mkt("BTC",15,0.80,mid="mb4"),st); ck("move bucket 0.17%", c and c["move_bucket"]=="0.15–0.20%", c["move_bucket"] if c else "")
CUR["BTC"]=REF["BTC"]*1.0015

print("\n11) restart не теряет сигналы")
st=fresh()
c,_=bot.evaluate(mkt("BTC",15,0.80,mid="rs1"),st); bot.register_signal(c,st,"BLOCKED_BY_RISK")
n_before=len(st.pending_signals); st.save()
st2=bot.State()
ck("pending восстановлены", len(st2.pending_signals)==n_before, f"{len(st2.pending_signals)} из {n_before}")
close(st2,True)
ck("резолв после рестарта", len(rows())==1 and rows()[0]["resolution"]=="WIN")

print("\n12-14) partial / unfilled / cancelled в журнале")
st=fresh(); orig=bot.place_order
c,_=bot.evaluate(mkt("BTC",15,0.80,mid="p1"),st)
bot.place_order=lambda cd:{"order_id":"o","status":"PARTIAL","filled_shares":round(cd["shares"]*0.4,2),"filled_cost":round(cd["cost"]*0.4,2),"avg_fill_price":cd["entry"]+0.004}
bot.open_position(c,st)
c,_=bot.evaluate(mkt("ETH",15,0.80,mid="p2"),st)
bot.place_order=lambda cd:{"order_id":"o","status":"UNFILLED","filled_shares":0.0,"filled_cost":0.0,"avg_fill_price":cd["entry"]}
bot.open_position(c,st)
c,_=bot.evaluate(mkt("SOL",15,0.80,mid="p3"),st)
bot.place_order=lambda cd:{"order_id":"o","status":"CANCELLED","filled_shares":0.0,"filled_cost":0.0,"avg_fill_price":cd["entry"]}
bot.open_position(c,st)
bot.place_order=orig
close(st,True); r=rows()
ck("PARTIAL в журнале", any(x["order_status"]=="PARTIAL" for x in r))
ck("UNFILLED в журнале", any(x["signal_status"]=="ORDER_UNFILLED" for x in r))
ck("CANCELLED в журнале", any(x["signal_status"]=="ORDER_CANCELLED" for x in r))
unf=[x for x in r if x["signal_status"]=="ORDER_UNFILLED"][0]
ck("unfilled без realized", unf["realized_pnl"]=="")
ck("unfilled с hypothetical", unf["hypothetical_pnl"] not in ("",None), unf["hypothetical_pnl"])
ck("slippage у partial", any(x["slippage"] not in ("",None) and float(x["slippage"] or 0)>0 for x in r if x["order_status"]=="PARTIAL"))

print("\n15) risk-blocked не становится сделкой")
st=fresh(1000.0)
start=bot.now()-timedelta(seconds=15*60*0.8)
c1,_=bot.evaluate(mkt("BTC",15,mid="x1",start=start),st); bot.open_position(c1,st)
c2,_=bot.evaluate(mkt("ETH",15,mid="x2",start=start),st); bot.register_signal(c2,st,"BLOCKED_BY_RISK")
ck("позиция одна", len(st.positions)==1)
ck("банкролл не тронут дважды", st.exposure()==c1["cost"])

print("\n16) execution_quality")
st=fresh()
c,_=bot.evaluate(mkt("BTC",15,0.80,mid="q1"),st)
ck("GOOD при запасе времени и объёма", c and c["execution_quality"]=="GOOD", c["execution_quality"] if c else "")
c,_=bot.evaluate(mkt("SOL",5,0.86,mid="q2"),st)
ck("MARGINAL на 5m в конце", c and c["execution_quality"]=="MARGINAL", f"{c['execution_quality']} rem={c['remaining_sec']}" if c else "")
DEPTH=0.5
c,_=bot.evaluate(mkt("ETH",15,0.80,mid="q3"),st)
ck("UNEXECUTABLE при пустом стакане", c and c["execution_quality"]=="UNEXECUTABLE", c["execution_quality"] if c else "")
DEPTH=100000.0

print("\n17) дубликаты не создаются")
st=fresh()
c,_=bot.evaluate(mkt("BTC",15,0.80,mid="d1"),st); bot.register_signal(c,st,"BLOCKED_BY_RISK")
seen={s["row"]["market_id"] for s in st.pending_signals}
ck("market_id в seen", "d1" in seen)
close(st,True)
ck("одна строка на сигнал", len(rows())==1, str(len(rows())))

bad=[n for n,o in R if not o]
print(f"\n{'='*50}\nИТОГО: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
