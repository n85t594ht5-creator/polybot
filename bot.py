#!/usr/bin/env python3
"""
PolyBot — Crypto Up/Down sniper for Polymarket.

Логика: в 15-минутных / часовых рынках "Bitcoin Up or Down" ждём, пока пройдёт
большая часть окна, смотрим, куда ушла цена относительно старта окна, и
покупаем дешёвый (<= MAX_ENTRY) исход в сторону движения.

MODE=paper  — виртуальные сделки, реальные котировки, ключи не нужны.
MODE=live   — реальные ордера через py-clob-client (нужен .env с ключом).
"""

import json
import os
import re
import sys
import time
import random
import logging
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

# ───────────────────────── config ─────────────────────────

def env(name, default, cast=str):
    v = os.getenv(name)
    return cast(v) if v not in (None, "") else default

MODE              = env("MODE", "paper").lower()
ASSETS            = [a.strip().upper() for a in env("ASSETS", "BTC,ETH,SOL").split(",")]
BANKROLL          = env("BANKROLL", 500.0, float)
MAX_ENTRY         = env("MAX_ENTRY", 0.15, float)
MIN_ENTRY         = env("MIN_ENTRY", 0.0, float)      # не покупать исход дешевле (дешёвые исходы = рынок не согласен)
MIN_ELAPSED       = env("MIN_ELAPSED", 0.50, float)
MIN_MOVE          = env("MIN_MOVE", 0.0008, float)
TIER_ENTRY        = env("TIER_ENTRY", 0.45, float)      # исход дороже этой цены → требуем движение MIN_MOVE_HIGH
MIN_MOVE_HIGH     = env("MIN_MOVE_HIGH", 0.0012, float)
MIN_CONF          = env("MIN_CONF", 0.60, float)
KELLY_FRAC        = env("KELLY_FRAC", 0.25, float)
MAX_POSITIONS     = env("MAX_POSITIONS", 10, int)
MAX_EXPOSURE      = env("MAX_EXPOSURE", 0.40, float)
MAX_STAKE         = env("MAX_STAKE", 0.08, float)     # одна ставка не больше этой доли банкролла
DAILY_LOSS_LIMIT  = env("DAILY_LOSS_LIMIT", 50.0, float)
CONSEC_LOSS_LIMIT = env("CONSEC_LOSS_LIMIT", 4, int)
RATE_LIMIT        = env("RATE_LIMIT", 20, int)
MOVE_MODE         = env("MOVE_MODE", "pct").lower()       # pct — порог в %, sigma — порог в волатильностях
MIN_SIGMA         = env("MIN_SIGMA", 1.5, float)          # для MOVE_MODE=sigma
REF_MODE          = env("REF_MODE", "open").lower()       # open — открытие 1-й минуты, twap — её средняя
SKIP_HOURS        = [int(h) for h in env("SKIP_HOURS", "").split(",") if h.strip() != ""]   # часы UTC без торговли
MAX_PER_WINDOW    = env("MAX_PER_WINDOW", 99, int)        # позиций на одно окно времени
MAX_SAME_DIR      = env("MAX_SAME_DIR", 99, int)          # одновременных позиций в одну сторону
LOOP_SEC          = env("LOOP_SEC", 1.5, float)          # пауза между циклами
MARKETS_TTL       = env("MARKETS_TTL", 30, int)          # как часто обновлять список рынков, сек
PRE_ENTRY_SEC     = env("PRE_ENTRY_SEC", 60, int)        # начинать опрашивать цены за N сек до момента входа
WINDOWS           = [int(w) for w in env("WINDOWS", "15,60").split(",")]   # длины окон в минутах: 5, 15, 60
PRICE_SOURCE      = env("PRICE_SOURCE", "coinbase").lower()               # coinbase | binance

TG_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = env("TELEGRAM_CHAT_ID", "")

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
BINANCE = "https://api.binance.com"
COINBASE = "https://api.exchange.coinbase.com"
SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
CB_PRODUCT = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}
ASSET_WORDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp"}

STATE_FILE = "state.json"
TRADES_FILE = "trades.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bot.log")],
)
log = logging.getLogger("polybot")

# ───────────────────────── helpers ─────────────────────────

def notify(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text}, timeout=5)
    except Exception:
        pass

def get(url, **params):
    r = requests.get(url, params=params, timeout=10, headers={"User-Agent": "polybot/1.0"})
    r.raise_for_status()
    return r.json()

def now():
    return datetime.now(timezone.utc)

def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

# ───────────────────────── state ─────────────────────────

class State:
    def __init__(self):
        self.bankroll = BANKROLL
        self.positions = {}        # market_id -> position dict
        self.closed = []           # resolved positions
        self.day = now().date().isoformat()
        self.day_pnl = 0.0
        self.consec_losses = 0
        self.cooldown_until = None
        self.trade_times = []
        self.load()

    def load(self):
        if os.path.exists(STATE_FILE):
            try:
                d = json.load(open(STATE_FILE))
                self.__dict__.update(d)
                log.info("State loaded: bankroll=%.2f, open=%d", self.bankroll, len(self.positions))
            except Exception as e:
                log.warning("Could not load state: %s", e)

    def save(self):
        json.dump(self.__dict__, open(STATE_FILE, "w"), indent=1, default=str)

    def roll_day(self):
        d = now().date().isoformat()
        if d != self.day:
            self.day, self.day_pnl = d, 0.0

    def exposure(self):
        return sum(p["cost"] for p in self.positions.values())

    def can_trade(self):
        self.roll_day()
        if self.cooldown_until and now() < parse_iso(str(self.cooldown_until)):
            return False, "cooldown"
        if self.day_pnl <= -DAILY_LOSS_LIMIT:
            return False, "daily loss limit"
        if len(self.positions) >= MAX_POSITIONS:
            return False, "max positions"
        if self.exposure() >= BANKROLL * MAX_EXPOSURE:
            return False, "max exposure"
        cutoff = time.time() - 3600
        self.trade_times = [t for t in self.trade_times if t > cutoff]
        if len(self.trade_times) >= RATE_LIMIT:
            return False, "rate limit"
        return True, ""

# ───────────────────────── market data ─────────────────────────

_price_cache = {}   # asset -> (ts, price)
_sigma_cache = {}   # asset -> (ts, sigma)

def sigma_1m(asset):
    """Ст. отклонение минутных доходностей за последний час (Coinbase). Кэш 60 сек."""
    c = _sigma_cache.get(asset)
    if c and time.time() - c[0] < 60:
        return c[1]
    end = now(); start = end - timedelta(minutes=70)
    k = get(f"{COINBASE}/products/{CB_PRODUCT[asset]}/candles", granularity=60,
            start=start.strftime("%Y-%m-%dT%H:%M:%SZ"), end=end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    k = sorted(k or [], key=lambda c: c[0])
    closes = [float(c[4]) for c in k]
    rets = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes)) if closes[i-1]]
    if len(rets) < 20:
        return None
    m = sum(rets) / len(rets); sg = (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5
    _sigma_cache[asset] = (time.time(), sg)
    return sg

def binance_price(asset):
    """Текущая цена (имя историческое — источник выбирается PRICE_SOURCE). Кэш 1 сек."""
    c = _price_cache.get(asset)
    if c and time.time() - c[0] < 1.0:
        return c[1]
    p = _fetch_price(asset)
    _price_cache[asset] = (time.time(), p)
    return p

def _fetch_price(asset):
    if PRICE_SOURCE == "binance":
        return float(get(f"{BINANCE}/api/v3/ticker/price", symbol=SYMBOL[asset])["price"])
    return float(get(f"{COINBASE}/products/{CB_PRODUCT[asset]}/ticker")["price"])

def binance_open_at(asset, dt):
    """Цена открытия минутной свечи, начинающейся в dt."""
    if PRICE_SOURCE == "binance":
        ms = int(dt.timestamp() * 1000)
        k = get(f"{BINANCE}/api/v3/klines", symbol=SYMBOL[asset], interval="1m", startTime=ms, limit=1)
        return float(k[0][1]) if k else None
    start = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (dt + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    k = get(f"{COINBASE}/products/{CB_PRODUCT[asset]}/candles", granularity=60, start=start, end=end)
    if not k:
        return None
    k.sort(key=lambda c: c[0])           # coinbase отдаёт от новых к старым: [time, low, high, open, close, vol]
    return float(k[0][3]) if REF_MODE == "open" else (float(k[0][3]) + float(k[0][4])) / 2

def clob_ask(token_id):
    return float(get(f"{CLOB}/price", token_id=token_id, side="BUY")["price"])

SLUG_RE = re.compile(r"^([a-z]+)-updown-(\d+)(m|h)-(\d+)$")

def find_updown_markets():
    """Активные рынки 'X Up or Down' по нашим активам (slug вида btc-updown-15m-<unix start>)."""
    out = []
    try:
        markets = get(f"{GAMMA}/markets", closed="false", active="true", limit=200, order="endDate",
                      ascending="true", end_date_min=now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    except Exception as e:
        log.warning("Gamma error: %s", e)
        return out
    for m in markets:
        mt = SLUG_RE.match((m.get("slug") or "").lower())
        if not mt:
            continue
        asset = mt.group(1).upper()
        if asset not in ASSETS or not m.get("endDate"):
            continue
        minutes = int(mt.group(2)) * (60 if mt.group(3) == "h" else 1)
        if minutes not in WINDOWS:
            continue
        start = datetime.fromtimestamp(int(mt.group(4)), tz=timezone.utc)
        end = parse_iso(m["endDate"])
        if end <= now():
            continue
        try:
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            outcomes = json.loads(m.get("outcomes") or '["Up","Down"]')
        except Exception:
            continue
        if len(tokens) != 2:
            continue
        up_idx = 0 if outcomes[0].lower().startswith("up") else 1
        out.append({
            "id": m["id"], "asset": asset, "question": m.get("question") or m["slug"],
            "start": start, "end": end, "minutes": minutes,
            "up_token": tokens[up_idx], "down_token": tokens[1 - up_idx],
        })
    return out

# ───────────────────────── strategy ─────────────────────────

def evaluate(mkt, state):
    """Прогоняет гейты. Возвращает кандидата или (None, причина)."""
    t = now()
    if t.hour in SKIP_HOURS:
        return None, f"час {t.hour:02d} UTC выключен"
    elapsed = (t - mkt["start"]).total_seconds() / (mkt["minutes"] * 60)
    if elapsed < 0:
        return None, "окно ещё не началось"
    if elapsed < MIN_ELAPSED:
        secs_to_entry = (MIN_ELAPSED - elapsed) * mkt["minutes"] * 60
        if secs_to_entry > PRE_ENTRY_SEC:
            return None, f"elapsed {elapsed:.0%} < {MIN_ELAPSED:.0%}"
        # близко к входу: прогреваем кэш цены старта, чтобы в момент входа не ждать
        if not mkt.get("_ref"):
            try: mkt["_ref"] = binance_open_at(mkt["asset"], mkt["start"])
            except Exception: pass
        return None, f"elapsed {elapsed:.0%} < {MIN_ELAPSED:.0%}"
    if (mkt["end"] - t).total_seconds() < 30:
        return None, "too close to end"

    ref = mkt.get("_ref") or binance_open_at(mkt["asset"], mkt["start"])
    mkt["_ref"] = ref
    cur = binance_price(mkt["asset"])
    if not ref:
        return None, "no reference price"
    move = (cur - ref) / ref
    if MOVE_MODE == "sigma":
        sg = sigma_1m(mkt["asset"])
        if not sg:
            return None, "no volatility data"
        z = abs(move) / (sg * max(1.0, elapsed * mkt["minutes"]) ** 0.5)
        if z < MIN_SIGMA:
            return None, f"move {z:.2f}σ < {MIN_SIGMA}σ"
        strength = min(z / 3, 1)
    else:
        if abs(move) < MIN_MOVE:
            return None, f"move {move:+.3%} < {MIN_MOVE:.3%}"
        strength = min(abs(move) / 0.005, 1)

    side = "UP" if move > 0 else "DOWN"
    token = mkt["up_token"] if side == "UP" else mkt["down_token"]
    entry = clob_ask(token)
    if entry > MAX_ENTRY:
        return None, f"{side} ask {entry:.2f} > {MAX_ENTRY}"
    if entry <= 0.01:
        return None, "no liquidity"
    if entry < MIN_ENTRY:
        return None, f"{side} ask {entry:.2f} < MIN_ENTRY {MIN_ENTRY}"
    if entry > TIER_ENTRY:
        if MOVE_MODE == "sigma":
            if z < MIN_SIGMA * 1.5:
                return None, f"ask {entry:.2f} > {TIER_ENTRY}: move {z:.2f}σ < {MIN_SIGMA*1.5:.2f}σ"
        elif abs(move) < MIN_MOVE_HIGH:
            return None, f"ask {entry:.2f} > {TIER_ENTRY}: move {move:+.3%} < {MIN_MOVE_HIGH:.3%}"

    # корреляционные лимиты
    same_win = sum(1 for p in state.positions.values() if p.get("start") == mkt["start"].isoformat())
    if same_win >= MAX_PER_WINDOW:
        return None, f"уже {same_win} позиций в этом окне"
    same_dir = sum(1 for p in state.positions.values() if p.get("side") == side)
    if same_dir >= MAX_SAME_DIR:
        return None, f"уже {same_dir} позиций {side}"

    # Уверенность: чем позже и чем сильнее движение — тем выше.
    # Это эвристика, не модель. Калибруется по логам paper-режима.
    conf = min(0.95, 0.5 + elapsed * 0.3 + strength * 0.15)
    if conf < MIN_CONF:
        return None, f"conf {conf:.2f} < {MIN_CONF}"

    # Kelly: f = (p*b - q) / b, где b = выигрыш на 1$ ставки
    b = (1 - entry) / entry
    kelly = (conf * b - (1 - conf)) / b
    size = max(0.0, kelly) * KELLY_FRAC * state.bankroll
    size = min(size, state.bankroll * MAX_STAKE, state.bankroll * MAX_EXPOSURE - state.exposure())
    if size < 1.0:
        return None, "size too small"

    return {
        "market_id": mkt["id"], "asset": mkt["asset"], "side": side, "token": token,
        "minutes": mkt["minutes"], "start": mkt["start"].isoformat(),
        "entry": entry, "cost": round(size, 2), "shares": round(size / entry, 2),
        "conf": round(conf, 3), "move": round(move, 5), "elapsed": round(elapsed, 2),
        "ref": ref, "end": mkt["end"].isoformat(), "opened": now().isoformat(),
        "question": mkt["question"],
    }, ""

# ───────────────────────── execution ─────────────────────────

_clob_client = None

def live_client():
    global _clob_client
    if _clob_client:
        return _clob_client
    from py_clob_client.client import ClobClient
    key = os.getenv("POLY_PRIVATE_KEY")
    if not key:
        raise RuntimeError("POLY_PRIVATE_KEY not set in .env")
    kwargs = dict(host=CLOB, key=key, chain_id=137,
                  signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "0")))
    if os.getenv("POLY_FUNDER"):
        kwargs["funder"] = os.getenv("POLY_FUNDER")
    c = ClobClient(**kwargs)
    c.set_api_creds(c.create_or_derive_api_creds())
    _clob_client = c
    return c

def place_order(cand):
    if MODE != "live":
        return "PAPER"
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    c = live_client()
    order = c.create_order(OrderArgs(
        token_id=cand["token"], price=cand["entry"], size=cand["shares"], side=BUY))
    resp = c.post_order(order, OrderType.GTC)
    return resp.get("orderID", str(resp))

def open_position(cand, state):
    oid = place_order(cand)
    cand["order_id"] = oid
    state.positions[cand["market_id"]] = cand
    state.trade_times.append(time.time())
    state.save()
    msg = (f"[{MODE.upper()}] BUY {cand['asset']} {cand['side']} @ {cand['entry']:.2f} "
           f"${cand['cost']:.2f} conf={cand['conf']} move={cand['move']:+.3%}")
    log.info(msg)
    notify(msg)

def resolve_positions(state):
    """Закрываем позиции по цене Binance в момент окончания окна."""
    for mid, p in list(state.positions.items()):
        end = parse_iso(p["end"])
        if now() < end + timedelta(seconds=90):
            continue
        try:
            final = binance_open_at(p["asset"], end)
        except Exception as e:
            log.warning("resolve %s: %s", mid, e)
            continue
        if not final:
            continue
        went_up = final > p["ref"]
        won = (p["side"] == "UP") == went_up
        pnl = (p["shares"] * 1.0 - p["cost"]) if won else -p["cost"]
        state.bankroll += pnl
        state.day_pnl += pnl
        state.consec_losses = 0 if won else state.consec_losses + 1
        p.update({"won": won, "pnl": round(pnl, 2), "final": final})
        state.closed.append(p)
        del state.positions[mid]
        with open(TRADES_FILE, "a") as f:
            f.write(f"{p['opened']},{p['asset']},{p['side']},{p['entry']},{p['cost']},{won},{pnl:.2f}\n")
        msg = f"{'WIN ' if won else 'LOSS'} {p['asset']} {p['side']} pnl={pnl:+.2f} bankroll={state.bankroll:.2f}"
        log.info(msg); notify(msg)
        if state.consec_losses >= CONSEC_LOSS_LIMIT:
            state.cooldown_until = (now() + timedelta(hours=24)).isoformat()
            log.warning("Consecutive loss limit hit — cooldown 24h"); notify("⚠ Cooldown 24h")
        state.save()

# ───────────────────────── main loop ─────────────────────────

def stats(state):
    c = state.closed
    if not c:
        return "no closed trades yet"
    wins = [t["pnl"] for t in c if t["won"]]; losses = [t["pnl"] for t in c if not t["won"]]
    pf = (sum(wins) / -sum(losses)) if losses else float("inf")
    return (f"trades={len(c)} winrate={len(wins)/len(c):.0%} "
            f"pnl={sum(t['pnl'] for t in c):+.2f} PF={pf:.2f} bankroll={state.bankroll:.2f}")

def main():
    if MODE == "live":
        log.warning("LIVE MODE — real money. Ctrl+C within 10s to abort.")
        time.sleep(10)
        live_client()  # fail fast if keys are wrong
    state = State()
    log.info("PolyBot started mode=%s assets=%s bankroll=%.0f", MODE, ASSETS, state.bankroll)
    n = 0
    markets, markets_ts = [], 0.0
    while True:
        try:
            if n % 10 == 0:
                resolve_positions(state)
            if time.time() - markets_ts > MARKETS_TTL:
                fresh = find_updown_markets()
                # сохраняем прогретые кэши цены старта
                old = {m["id"]: m.get("_ref") for m in markets}
                for m in fresh:
                    if old.get(m["id"]): m["_ref"] = old[m["id"]]
                markets, markets_ts = fresh, time.time()
            ok, why = state.can_trade()
            if ok:
                for mkt in markets:
                    if mkt["end"] <= now():
                        continue
                    if mkt["id"] in state.positions:
                        continue
                    cand, reason = evaluate(mkt, state)
                    if cand:
                        open_position(cand, state)
                    else:
                        log.debug("%s %s: %s", mkt["asset"], mkt["end"].strftime("%H:%M"), reason)
            elif n % 40 == 0:
                log.info("Trading paused: %s", why)
            n += 1
            if n % 80 == 0:
                log.info("STATS %s", stats(state))
        except KeyboardInterrupt:
            log.info("Stopped. %s", stats(state)); state.save(); break
        except Exception as e:
            log.error("loop error: %s", e)
        time.sleep(LOOP_SEC)

if __name__ == "__main__":
    main()
