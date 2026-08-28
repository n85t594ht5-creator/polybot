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
ASSETS            = [a.strip().upper() for a in env("ASSETS", "BTC,ETH,SOL,XRP").split(",")]
BANKROLL          = env("BANKROLL", 500.0, float)
MAX_ENTRY         = env("MAX_ENTRY", 0.62, float)
MIN_ENTRY         = env("MIN_ENTRY", 0.50, float)      # не покупать исход дешевле (дешёвые исходы = рынок не согласен)
MIN_ELAPSED       = env("MIN_ELAPSED", 0.75, float)
MIN_MOVE          = env("MIN_MOVE", 0.0010, float)   # доля: 0.0010 = 0.10% от цены старта окна
TIER_ENTRY        = env("TIER_ENTRY", 0.55, float)      # исход дороже этой цены → требуем движение MIN_MOVE_HIGH
MIN_MOVE_HIGH     = env("MIN_MOVE_HIGH", 0.0012, float)
MIN_CONF          = env("MIN_CONF", 0.70, float)     # шкала conf при MIN_ELAPSED=0.75: 0.725...0.935 -> informational
KELLY_FRAC        = env("KELLY_FRAC", 0.10, float)
MAX_POSITIONS     = env("MAX_POSITIONS", 3, int)
MAX_EXPOSURE      = env("MAX_EXPOSURE", 0.15, float)
MAX_STAKE         = env("MAX_STAKE", 0.05, float)     # одна ставка не больше этой доли банкролла
DAILY_LOSS_LIMIT  = env("DAILY_LOSS_LIMIT", 0.30, float)   # <=1 - доля банкролла на начало дня; >1 - доллары
CONSEC_LOSS_LIMIT = env("CONSEC_LOSS_LIMIT", 5, int)
COOLDOWN_MIN      = env("COOLDOWN_MIN", 30, int)      # длительность паузы после серии убытков, минут
RATE_LIMIT        = env("RATE_LIMIT", 20, int)
MOVE_MODE         = env("MOVE_MODE", "pct").lower()       # pct — порог в %, sigma — порог в волатильностях
MIN_SIGMA         = env("MIN_SIGMA", 1.5, float)          # для MOVE_MODE=sigma
REF_MODE          = env("REF_MODE", "twap").lower()       # open — открытие 1-й минуты, twap — её средняя
SKIP_HOURS        = [int(h) for h in env("SKIP_HOURS", "").split(",") if h.strip() != ""]   # часы UTC без торговли
MAX_PER_WINDOW    = env("MAX_PER_WINDOW", 1, int)        # позиций на одно окно времени
MAX_SAME_DIR      = env("MAX_SAME_DIR", 2, int)          # одновременных позиций в одну сторону
USE_BOOK          = env("USE_BOOK", "1") == "1"          # учитывать стакан: входить на доступный объём
MAX_SLIP          = env("MAX_SLIP", 0.01, float)          # допустимое проскальзывание от лучшей цены
ORDER_WAIT_SEC    = env("ORDER_WAIT_SEC", 20, int)        # сколько ждём исполнения ордера в live
LOOP_SEC          = env("LOOP_SEC", 1.5, float)          # пауза между циклами
MARKETS_TTL       = env("MARKETS_TTL", 30, int)          # как часто обновлять список рынков, сек
PRE_ENTRY_SEC     = env("PRE_ENTRY_SEC", 60, int)        # начинать опрашивать цены за N сек до момента входа
WINDOWS           = [int(w) for w in env("WINDOWS", "5,15").split(",")]   # длины окон в минутах: 5, 15, 60
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
MISSED_FILE = "missed.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bot.log")],
)
log = logging.getLogger("polybot")

# ───────────────────────── helpers ─────────────────────────

# Границы включающие сверху — как в tier-логике (entry > TIER_ENTRY → дорогой уровень),
# поэтому 0.55 относится к дешёвому бакету и требует MIN_MOVE, а не MIN_MOVE_HIGH.
ENTRY_BUCKETS = ((0.55, "0.50–0.55"), (0.60, "0.55–0.60"), (0.62, "0.60–0.62"))

def entry_bucket(p):
    for hi, name in ENTRY_BUCKETS:
        if p <= hi + 1e-9:
            return name
    return "прочее"

def move_bucket(m):
    a = abs(m)
    if a < 0.0012: return "0.10–0.12%"
    if a < 0.0020: return "0.12–0.20%"
    if a < 0.0035: return "0.20–0.35%"
    return "≥0.35%"

def log_missed(mkt, side, entry, reason, extra=""):
    """Журнал упущенных сигналов: условия сошлись, но войти не удалось."""
    try:
        with open(MISSED_FILE, "a") as f:
            f.write(f"{now().isoformat()},{mkt['asset']},{mkt['minutes']},{side},{entry},{reason},{extra},{mkt['end'].isoformat()}\n")
    except Exception:
        pass

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
        self.day_start_bankroll = BANKROLL
        self.execstats = {"submitted": 0, "filled": 0, "partial": 0, "unfilled": 0, "cancelled": 0,
                          "slip_sum": 0.0, "slip_n": 0, "by_window": {}}
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
            self.day_start_bankroll = self.bankroll

    def daily_limit_usd(self):
        """Дневной стоп в долларах: доля от банкролла на начало дня либо абсолютная сумма."""
        base = getattr(self, "day_start_bankroll", None) or self.bankroll or BANKROLL
        return base * DAILY_LOSS_LIMIT if DAILY_LOSS_LIMIT <= 1 else DAILY_LOSS_LIMIT

    def dir_exposure(self):
        """Экспозиция по направлениям: корреляционный риск UP/DOWN в долларах."""
        out = {"UP": 0.0, "DOWN": 0.0}
        for p in self.positions.values():
            k = p.get("side", "UP")
            out[k] = out.get(k, 0.0) + p.get("cost", 0.0)
        return out

    def exposure(self):
        return sum(p["cost"] for p in self.positions.values())

    def can_trade(self):
        self.roll_day()
        if self.cooldown_until and now() < parse_iso(str(self.cooldown_until)):
            return False, "cooldown"
        if self.day_pnl <= -self.daily_limit_usd():
            return False, "daily loss limit"
        if len(self.positions) >= MAX_POSITIONS:
            return False, "max positions"
        if self.exposure() >= self.bankroll * MAX_EXPOSURE:
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

def clob_book(token_id):
    """Стакан: [(цена, размер в шт.), ...] по возрастанию цены — что можно купить."""
    try:
        b = get(f"{CLOB}/book", token_id=token_id)
    except Exception:
        return []
    asks = [(float(x["price"]), float(x["size"])) for x in (b.get("asks") or [])]
    return sorted(asks, key=lambda x: x[0])

def fillable(book, budget, max_price):
    """Сколько $ и по какой средней цене реально купить в пределах max_price."""
    spent = shares = 0.0
    for price, size in book:
        if price > max_price or spent >= budget:
            break
        take = min(size, (budget - spent) / price)
        spent += take * price; shares += take
    return round(spent, 2), round(shares, 2), (spent / shares if shares else 0.0)

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
        log_missed(mkt, side, entry, "дорого", f"потолок {MAX_ENTRY}")
        return None, f"{side} ask {entry:.2f} > {MAX_ENTRY}"
    if entry <= 0.01:
        return None, "no liquidity"
    if entry < MIN_ENTRY:
        log_missed(mkt, side, entry, "дёшево", f"минимум {MIN_ENTRY}")
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
        log_missed(mkt, side, entry, "лимит окна", f"{same_win} позиций")
        return None, f"уже {same_win} позиций в этом окне"
    same_dir = sum(1 for p in state.positions.values() if p.get("side") == side)
    if same_dir >= MAX_SAME_DIR:
        log_missed(mkt, side, entry, "лимит стороны", f"{same_dir} позиций {side}")
        return None, f"уже {same_dir} позиций {side}"

    # Уверенность: чем позже и чем сильнее движение — тем выше.
    # Это эвристика, не модель. Калибруется по логам paper-режима.
    conf = min(0.95, 0.5 + elapsed * 0.3 + strength * 0.15)
    if conf < MIN_CONF:
        return None, f"conf {conf:.2f} < {MIN_CONF}"

    # Kelly: f = (p*b - q) / b, где b = выигрыш на 1$ ставки
    b = (1 - entry) / entry
    kelly = (conf * b - (1 - conf)) / b
    if kelly <= 0:                      # нет положительного edge — сделки нет
        log_missed(mkt, side, entry, "нет edge", f"kelly {kelly:.3f}")
        return None, f"kelly {kelly:.3f} <= 0"
    size = kelly * KELLY_FRAC * state.bankroll
    size = min(size, state.bankroll * MAX_STAKE, state.bankroll * MAX_EXPOSURE - state.exposure())
    if size < 1.0:
        log_missed(mkt, side, entry, "мало места", f"размер {size:.2f}$")
        return None, "size too small"

    # Стакан: сколько реально можно купить, не разгоняя цену
    avg, shares, depth_note = entry, round(size / entry, 2), ""
    if USE_BOOK:
        book = clob_book(token)
        if book:
            cap = min(MAX_ENTRY, entry + MAX_SLIP)
            spent, sh, avg_p = fillable(book, size, cap)
            if spent < 1.0:
                log_missed(mkt, side, entry, "нет объёма", f"в стакане до {cap:.2f} меньше 1$")
                return None, f"нет объёма до {cap:.2f}"
            if spent < size * 0.99:
                depth_note = f"стакан дал {spent:.2f}$ из {size:.2f}$"
                log_missed(mkt, side, entry, "частичный объём", depth_note)
            size, shares, avg = spent, sh, round(avg_p, 4)

    return {
        "market_id": mkt["id"], "asset": mkt["asset"], "side": side, "token": token,
        "minutes": mkt["minutes"], "start": mkt["start"].isoformat(),
        "entry": avg, "best_ask": entry, "cost": round(size, 2), "shares": shares, "depth_note": depth_note,
        "conf": round(conf, 3), "move": round(move, 5), "elapsed": round(elapsed, 2),
        "entry_bucket": entry_bucket(avg), "move_bucket": move_bucket(move),
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
    """Отправляет ордер и ДОЖИДАЕТСЯ факта исполнения.

    Возвращает dict: order_id, status (FILLED/PARTIAL/UNFILLED/CANCELLED/PAPER),
    filled_shares, filled_cost, avg_fill_price. Позиция открывается только на
    фактически исполненный объём — submitted != открытая позиция.
    """
    if MODE != "live":
        return {"order_id": "PAPER", "status": "PAPER", "filled_shares": cand["shares"],
                "filled_cost": cand["cost"], "avg_fill_price": cand["entry"]}
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    c = live_client()
    order = c.create_order(OrderArgs(
        token_id=cand["token"], price=cand["entry"], size=cand["shares"], side=BUY))
    resp = c.post_order(order, OrderType.GTC)
    oid = resp.get("orderID") or resp.get("orderId") or str(resp)

    deadline = time.time() + ORDER_WAIT_SEC
    filled = 0.0; cost = 0.0; status = "UNFILLED"
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            o = c.get_order(oid) or {}
        except Exception as e:
            log.warning("get_order %s: %s", oid, e); continue
        st = str(o.get("status", "")).upper()
        try:
            matched = float(o.get("size_matched") or o.get("sizeMatched") or 0)
            px = float(o.get("price") or cand["entry"])
        except (TypeError, ValueError):
            matched, px = 0.0, cand["entry"]
        filled, cost = matched, matched * px
        if matched >= cand["shares"] * 0.999 or st in ("MATCHED", "FILLED"):
            status = "FILLED"; break
        if st in ("CANCELED", "CANCELLED", "EXPIRED"):
            status = "PARTIAL" if matched > 0 else "CANCELLED"; break
    else:
        # не исполнился за отведённое время — снимаем остаток
        try:
            c.cancel(oid); log.info("Ордер %s снят по таймауту", oid)
        except Exception as e:
            log.warning("cancel %s: %s", oid, e)
        status = "PARTIAL" if filled > 0 else "UNFILLED"

    avg = (cost / filled) if filled else cand["entry"]
    return {"order_id": oid, "status": status, "filled_shares": round(filled, 2),
            "filled_cost": round(cost, 2), "avg_fill_price": round(avg, 4)}

def open_position(cand, state):
    """Открывает позицию ТОЛЬКО на фактически исполненный объём."""
    es = state.execstats
    es["submitted"] = es.get("submitted", 0) + 1
    wkey = f"{cand['minutes']}m"
    w = es.setdefault("by_window", {}).setdefault(wkey, {"signal": 0, "submitted": 0, "filled": 0,
                                                         "partial": 0, "unfilled": 0, "cancelled": 0})
    w["submitted"] += 1
    requested_shares, requested_cost = cand["shares"], cand["cost"]

    r = place_order(cand)
    cand["order_id"] = r["order_id"]; cand["order_status"] = r["status"]
    cand["requested_shares"] = requested_shares; cand["requested_cost"] = requested_cost

    if r["status"] in ("UNFILLED", "CANCELLED"):
        es[r["status"].lower()] = es.get(r["status"].lower(), 0) + 1
        w["unfilled" if r["status"] == "UNFILLED" else "cancelled"] += 1
        state.save()
        msg = f"[{MODE.upper()}] {cand['asset']} {cand['side']} ордер не исполнен ({r['status']}) — позиции нет"
        log.warning(msg); notify(msg)
        log_missed({"asset": cand["asset"], "minutes": cand["minutes"], "end": parse_iso(cand["end"])},
                   cand["side"], cand["entry"], "ордер не исполнен", r["status"])
        return False

    # исполнено полностью или частично — позиция равна фактическому объёму
    cand["shares"] = r["filled_shares"]; cand["cost"] = r["filled_cost"]
    cand["entry"] = r["avg_fill_price"]
    cand["remaining_shares"] = round(requested_shares - r["filled_shares"], 2)
    if r["status"] == "PARTIAL":
        es["partial"] = es.get("partial", 0) + 1; w["partial"] += 1
    else:
        es["filled"] = es.get("filled", 0) + 1; w["filled"] += 1
    slip = r["avg_fill_price"] - cand.get("best_ask", r["avg_fill_price"])
    es["slip_sum"] = es.get("slip_sum", 0.0) + slip; es["slip_n"] = es.get("slip_n", 0) + 1

    state.positions[cand["market_id"]] = cand
    state.trade_times.append(time.time())
    state.save()
    msg = (f"[{MODE.upper()}] BUY {cand['asset']} {cand['side']} @ {cand['entry']:.3f} "
           f"${cand['cost']:.2f} ({r['status']}) conf={cand['conf']} move={cand['move']:+.3%}")
    log.info(msg); notify(msg)
    return True

def resolve_positions(state):
    """Закрываем позиции по цене окончания окна.

    ВНИМАНИЕ: это приближение. Polymarket резолвит по TWAP от Chainlink, а мы
    считаем по Coinbase (REF_MODE=twap — среднее открытия и закрытия минуты).
    На пограничных окнах результат paper-режима может отличаться от реального.
    """
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
            state.cooldown_until = (now() + timedelta(minutes=COOLDOWN_MIN)).isoformat()
            state.consec_losses = 0          # после паузы счёт начинается заново
            log.warning("Серия из %d убытков — пауза %d мин", CONSEC_LOSS_LIMIT, COOLDOWN_MIN)
            notify(f"⚠ {CONSEC_LOSS_LIMIT} убытков подряд — пауза {COOLDOWN_MIN} мин")
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
                        wk = f"{mkt['minutes']}m"
                        state.execstats.setdefault("by_window", {}).setdefault(
                            wk, {"signal": 0, "submitted": 0, "filled": 0, "partial": 0, "unfilled": 0, "cancelled": 0})["signal"] += 1
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
