"""Точные границы окна входа: сканируем elapsed по секундам."""
import os, sys, tempfile
from datetime import timedelta
os.environ.update(dict(MODE="paper", ASSETS="BTC,ETH,SOL,XRP", WINDOWS="5,15", BANKROLL="100",
    MIN_ELAPSED="0.75", MIN_ENTRY="0.50", MAX_ENTRY="0.62", TIER_ENTRY="0.55", MIN_MOVE="0.0010",
    MIN_MOVE_HIGH="0.0012", MIN_CONF="0.70", KELLY_FRAC="0.10", MAX_STAKE="0.05", MAX_EXPOSURE="0.15",
    MAX_POSITIONS="3", MAX_PER_WINDOW="1", MAX_SAME_DIR="2", DAILY_LOSS_LIMIT="0.30",
    CONSEC_LOSS_LIMIT="5", COOLDOWN_MIN="30", USE_BOOK="1", MAX_SLIP="0.01", REF_MODE="twap",
    PRE_ENTRY_SEC="60", ORDER_WAIT_SEC="20"))
os.chdir(tempfile.mkdtemp()); sys.path.insert(0, "/home/claude/repo")
import bot
REF = 80000.0
bot.binance_open_at = lambda a, dt: REF
bot.binance_price = lambda a: REF * 1.0015          # 0.15% — движение всегда достаточное
bot.clob_ask = lambda tok: 0.55                      # цена всегда в зоне
bot.clob_book = lambda tok: [(0.55, 100000.0)]
bot.notify = lambda *a, **k: None
st = bot.State(); st.bankroll = 100.0; st.day_start_bankroll = 100.0

print(f"MIN_ELAPSED={bot.MIN_ELAPSED}  PRE_ENTRY_SEC={bot.PRE_ENTRY_SEC}  "
      f"ORDER_WAIT_SEC={bot.ORDER_WAIT_SEC}  MAX_SLIP={bot.MAX_SLIP}  LOOP_SEC={bot.LOOP_SEC}\n")

for minutes in (5, 15):
    total = minutes * 60
    allowed, prewarm, reasons = [], [], {}
    for sec in range(0, total + 1):
        n = bot.now(); start = n - timedelta(seconds=sec)
        m = {"id": f"x{minutes}-{sec}", "asset": "BTC", "question": "q", "start": start,
             "end": start + timedelta(minutes=minutes), "minutes": minutes,
             "up_token": "u", "down_token": "d"}
        cand, why = bot.evaluate(m, st)
        if cand: allowed.append(sec)
        else:
            reasons.setdefault(why.split(":")[0], []).append(sec)
            if m.get("_ref"): prewarm.append(sec)
    lo, hi = (allowed[0], allowed[-1]) if allowed else (None, None)
    print(f"── окно {minutes} мин ({total} сек) ──")
    print(f"  вход разрешён:      {lo}–{hi} сек от старта  ({hi-lo+1} сек длиной)")
    print(f"  в долях окна:       {lo/total:.3f} – {hi/total:.3f}")
    print(f"  до конца окна:      с {total-lo} до {total-hi} сек")
    print(f"  прогрев ref-цены:   с {prewarm[0]}  сек (elapsed {prewarm[0]/total:.3f}) — только сеть, входа нет" if prewarm else "  прогрев: нет")
    for r, secs in reasons.items():
        print(f"    отказ «{r}»: {secs[0]}–{secs[-1]} сек")
    print()
