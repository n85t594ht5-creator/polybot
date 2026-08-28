#!/usr/bin/env python3
"""Каждые 30 сек выкладывает снимок состояния бота в GitHub Gist — для живого дашборда на Pages."""
import json, os, sys, time, requests
import dashboard
import bot

def watch():
    """Рынки, которые бот сейчас сканирует, и почему (не) входит."""
    out, prices = [], {}
    for a in bot.ASSETS:
        try: prices[a] = bot.binance_price(a)
        except Exception: pass
    for m in bot.find_updown_markets()[:12]:
        t = bot.now(); w = {"asset": m["asset"], "question": m["question"], "end": m["end"].isoformat(),
                            "start": m["start"].isoformat(), "minutes": m["minutes"]}
        w["elapsed"] = round((t - m["start"]).total_seconds() / (m["minutes"] * 60), 3)
        if w["elapsed"] < 0:
            w["reason"] = "окно ещё не началось"; out.append(w); continue
        if w["elapsed"] < 0:
            w["reason"] = "окно ещё не началось"; out.append(w); continue
        try:
            w["ref"] = bot.binance_open_at(m["asset"], m["start"]); w["cur"] = prices.get(m["asset"])
            w["move"] = round((w["cur"] - w["ref"]) / w["ref"], 5) if w["ref"] and w["cur"] else None
            w["up_ask"] = bot.clob_ask(m["up_token"]); w["down_ask"] = bot.clob_ask(m["down_token"])
            if w["elapsed"] > 0.4 and w.get("move") is not None:
                tok = m["up_token"] if w["move"] > 0 else m["down_token"]
                bk = bot.clob_book(tok)[:6]
                w["book"] = [[p, s] for p, s in bk]
                cap = min(bot.MAX_ENTRY, (w["up_ask"] if w["move"] > 0 else w["down_ask"]) + bot.MAX_SLIP)
                sp, sh, av = bot.fillable(bk, 1e9, cap)
                w["depth_usd"] = round(sp, 2)
        except Exception as e:
            w["error"] = str(e)[:80]
        try:
            cand, reason = bot.evaluate(m, dashboard_state)
            w["reason"] = "ВХОД" if cand else reason
        except Exception as e:
            w["reason"] = "ошибка: " + str(e)[:60]
        # потенциальная сделка: все условия кроме времени уже выполнены
        try:
            if not cand and w.get("move") is not None and w["elapsed"] < bot.MIN_ELAPSED:
                side = "UP" if w["move"] > 0 else "DOWN"
                ask = w["up_ask"] if side == "UP" else w["down_ask"]
                need = bot.MIN_MOVE_HIGH if ask > bot.TIER_ENTRY else bot.MIN_MOVE
                if abs(w["move"]) >= need and max(0.01, bot.MIN_ENTRY) <= ask <= bot.MAX_ENTRY:
                    secs = int((bot.MIN_ELAPSED - w["elapsed"]) * m["minutes"] * 60)
                    w["potential"] = {"side": side, "ask": ask, "in_sec": secs}
        except Exception:
            pass
        out.append(w)
    return out, prices

class _S:  # минимальный state для evaluate()
    bankroll = bot.BANKROLL
    def exposure(self): return 0.0
dashboard_state = _S()

GIST_ID, TOKEN = os.getenv("GIST_ID", ""), os.getenv("GIST_TOKEN", "")
if not (GIST_ID and TOKEN):
    print("GIST_ID / GIST_TOKEN не заданы — синхронизация выключена"); sys.exit(0)
INTERVAL = int(os.getenv("GIST_INTERVAL", "30"))
COMMIT_EVERY = int(os.getenv("STATE_COMMIT_SEC", "600"))
last = None; last_commit = time.time()

def commit_state():
    """Сохраняем состояние в репозиторий, чтобы прерванный запуск ничего не терял."""
    import subprocess
    cmd = ("git add state.json trades.csv missed.csv signals.csv bot.log && git -c user.name=polybot -c user.email=polybot@users.noreply.github.com commit -qm 'state autosave' ; "
           "git fetch -q origin main && git rebase -q -X theirs origin/main ; git push -q origin HEAD:main")
    try:
        r = subprocess.run(cmd, shell=True, timeout=90, capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run("git rebase --abort", shell=True, capture_output=True); print("autosave failed:", r.stderr[-300:])
    except Exception as e:
        print("autosave:", e)
while True:
    try:
        d = dashboard.snapshot(); d["log"] = d["log"][-40:]; d["bot"]["running"] = True
        diag = {}
        try: diag["price_source"] = f"{bot.PRICE_SOURCE} ok {bot.binance_price('BTC')}"
        except Exception as e: diag["price_source"] = f"{bot.PRICE_SOURCE} ERR {str(e)[:80]}"
        try: diag["markets_found"] = len(bot.find_updown_markets())
        except Exception as e: diag["markets_found"] = "ERR " + str(e)[:80]
        d["diag"] = diag
        # живая стоимость открытых позиций + журнал упущенных
        try:
            for p in d.get("positions", []):
                tok = p.get("token")
                if tok:
                    cur = bot.clob_ask(tok)
                    p["cur_price"] = cur
                    p["cur_value"] = round(p.get("shares", 0) * cur, 2)
                    p["unreal"] = round(p["cur_value"] - p.get("cost", 0), 2)
        except Exception as e:
            print("live pos:", e)
        d["missed"] = dashboard.read_missed()
        d["signals"] = dashboard.read_signals(1200)
        try:
            st = dashboard.read_state() or {}
            dashboard_state.bankroll = st.get("bankroll", bot.BANKROLL)
            d["watch"], d["prices"] = watch()
        except Exception as e:
            d["watch"], d["prices"] = [], {}; print("watch:", e)
        body = json.dumps(d, default=str, ensure_ascii=False)
        if body != last:
            r = requests.patch(f"https://api.github.com/gists/{GIST_ID}",
                               headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
                               json={"files": {"state.json": {"content": body}}}, timeout=15)
            r.raise_for_status(); last = body
    except Exception as e:
        print("gist sync:", e)
    if os.getenv("STATE_AUTOSAVE") == "1" and time.time() - last_commit > COMMIT_EVERY:
        commit_state(); last_commit = time.time()
    time.sleep(INTERVAL)
