#!/usr/bin/env python3
"""
Сбор истории для бэктеста PolyBot.
Для каждого закрытого окна X-updown-{5m,15m,1h}: результат, минутная история цены Up (CLOB),
плюс минутные свечи Coinbase. Результат: data/windows.json, data/candles.json
"""
import json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import requests

GAMMA, CLOB, COINBASE = "https://gamma-api.polymarket.com", "https://clob.polymarket.com", "https://api.exchange.coinbase.com"
ASSETS = [a.strip().upper() for a in os.getenv("ASSETS", "BTC,ETH,SOL").split(",")]
DAYS = {5: float(os.getenv("DAYS_5M", "3")), 15: float(os.getenv("DAYS_15M", "14")), 60: float(os.getenv("DAYS_1H", "14"))}
WINDOWS = [int(w) for w in os.getenv("WINDOWS", "5,15,60").split(",")]
CB = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}
S = requests.Session(); S.headers["User-Agent"] = "polybot-backtest/1.0"
os.makedirs("data", exist_ok=True)

def get(url, tries=3, **params):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 404: return None
            if r.status_code == 429: time.sleep(2 + i * 2); continue
            r.raise_for_status(); return r.json()
        except Exception as e:
            if i == tries - 1: print("  fail", url, params, e); return None
            time.sleep(1 + i)

def slug(asset, minutes, start_ts):
    w = "1h" if minutes == 60 else f"{minutes}m"
    return f"{asset.lower()}-updown-{w}-{start_ts}"

def fetch_window(asset, minutes, start_ts):
    m = get(f"{GAMMA}/markets", slug=slug(asset, minutes, start_ts))
    if not m: return None
    m = m[0] if isinstance(m, list) else m
    if not m.get("closed"): return None
    try:
        tokens = json.loads(m.get("clobTokenIds") or "[]"); outcomes = json.loads(m.get("outcomes") or '["Up","Down"]')
        prices = json.loads(m.get("outcomePrices") or "[]")
    except Exception: return None
    if len(tokens) != 2 or len(prices) != 2: return None
    up_idx = 0 if outcomes[0].lower().startswith("up") else 1
    up_won = float(prices[up_idx]) > 0.5
    end_ts = start_ts + minutes * 60
    hist = get(f"{CLOB}/prices-history", market=tokens[up_idx], startTs=start_ts - 60, endTs=end_ts + 60, fidelity=1)
    pts = [(int(h["t"]), float(h["p"])) for h in (hist or {}).get("history", [])]
    return {"asset": asset, "minutes": minutes, "start": start_ts, "end": end_ts, "up_won": up_won,
            "up_hist": pts, "slug": slug(asset, minutes, start_ts)}

def collect_windows():
    now = int(time.time()); jobs = []
    for minutes in WINDOWS:
        step = minutes * 60; since = now - int(DAYS[minutes] * 86400)
        first = (since // step) * step
        for a in ASSETS:
            for ts in range(first, now - step, step):
                jobs.append((a, minutes, ts))
    print(f"windows to fetch: {len(jobs)}")
    out = []
    with ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(fetch_window, *j): j for j in jobs}
        for i, f in enumerate(as_completed(futs)):
            r = f.result()
            if r: out.append(r)
            if i % 500 == 0: print(f"  {i}/{len(jobs)} found={len(out)}")
    out.sort(key=lambda w: w["start"])
    json.dump(out, open("data/windows.json", "w"))
    print("windows saved:", len(out), "with price history:", sum(1 for w in out if len(w["up_hist"]) > 3))
    return out

def collect_candles(windows):
    """Минутные свечи Coinbase: {asset: {minute_ts: [open, close]}}"""
    out = {}
    for a in ASSETS:
        ws = [w for w in windows if w["asset"] == a]
        if not ws: continue
        lo, hi = min(w["start"] for w in ws) - 120, max(w["end"] for w in ws) + 120
        c = {}; t = lo
        while t < hi:
            t2 = min(t + 300 * 60, hi)
            k = get(f"{COINBASE}/products/{CB[a]}/candles", granularity=60,
                    start=datetime.fromtimestamp(t, timezone.utc).isoformat(), end=datetime.fromtimestamp(t2, timezone.utc).isoformat())
            for row in k or []: c[int(row[0])] = [float(row[3]), float(row[4])]
            t = t2; time.sleep(0.25)
        out[a] = c; print(f"candles {a}: {len(c)}")
    json.dump(out, open("data/candles.json", "w"))

if __name__ == "__main__":
    ws = collect_windows()
    collect_candles(ws)
