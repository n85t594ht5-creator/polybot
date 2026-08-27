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

DIAG = {}

def collect_windows_paged():
    """Запасной способ: листаем закрытые рынки по endDate и фильтруем по slug."""
    pat = re.compile(r"^([a-z]+)-updown-(\d+)(m|h)-(\d+)$"); out = []; offset = 0
    oldest = time.time() - max(DAYS.values()) * 86400
    while offset < 20000:
        page = get(f"{GAMMA}/markets", closed="true", limit=500, offset=offset, order="endDate", ascending="false")
        if not page: break
        for m in page:
            mt = pat.match((m.get("slug") or "").lower())
            if not mt: continue
            a = mt.group(1).upper(); minutes = int(mt.group(2)) * (60 if mt.group(3) == "h" else 1)
            if a not in ASSETS or minutes not in WINDOWS: continue
            out.append((a, minutes, int(mt.group(4)), m))
        try: last_end = datetime.fromisoformat(page[-1]["endDate"].replace("Z", "+00:00")).timestamp()
        except Exception: last_end = 0
        offset += 500
        if last_end < oldest: break
    DIAG["paged_found"] = len(out); DIAG["paged_pages"] = offset // 500
    return out

def collect_windows():
    now = int(time.time()); jobs = []
    # диагностика: как достать закрытые окна
    ts_live = (now // 900) * 900; ts_old = ts_live - 3 * 900
    for name, url, params in [
        ("markets_slug_live", f"{GAMMA}/markets", dict(slug=slug("BTC", 15, ts_live))),
        ("markets_slug_old", f"{GAMMA}/markets", dict(slug=slug("BTC", 15, ts_old))),
        ("events_slug_live", f"{GAMMA}/events", dict(slug=slug("BTC", 15, ts_live))),
        ("events_slug_old", f"{GAMMA}/events", dict(slug=slug("BTC", 15, ts_old))),
        ("events_closed_list", f"{GAMMA}/events", dict(closed="true", limit=20, order="endDate", ascending="false")),
        ("markets_closed_list", f"{GAMMA}/markets", dict(closed="true", limit=20, order="endDate", ascending="false")),
        ("events_tag_search", f"{GAMMA}/events", dict(limit=20, order="endDate", ascending="false", closed="true", tag_slug="crypto")),
        ("public_search", f"{GAMMA}/public-search", dict(q="btc-updown-15m", limit_per_type=5)),
    ]:
        r = get(url, **params)
        if isinstance(r, list):
            DIAG[name] = {"n": len(r), "slugs": [x.get("slug") for x in r[:20]], "first": str(r[0])[:300] if r else ""}
        else:
            DIAG[name] = str(r)[:600]
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
    DIAG["slug_found"] = len(out)
    if len(out) < 50:
        print("slug lookup weak, trying paged listing")
        pairs = collect_windows_paged(); print("  paged found:", len(pairs))
        have = {w["slug"] for w in out}
        def fw(t):
            a, minutes, ts, m = t
            try:
                tokens = json.loads(m.get("clobTokenIds") or "[]"); outcomes = json.loads(m.get("outcomes") or '["Up","Down"]'); prices = json.loads(m.get("outcomePrices") or "[]")
                if len(tokens) != 2 or len(prices) != 2: return None
                up_idx = 0 if outcomes[0].lower().startswith("up") else 1
                hist = get(f"{CLOB}/prices-history", market=tokens[up_idx], startTs=ts - 60, endTs=ts + minutes * 60 + 60, fidelity=1)
                pts = [(int(h["t"]), float(h["p"])) for h in (hist or {}).get("history", [])]
                return {"asset": a, "minutes": minutes, "start": ts, "end": ts + minutes * 60, "up_won": float(prices[up_idx]) > 0.5, "up_hist": pts, "slug": slug(a, minutes, ts)}
            except Exception as e:
                return None
        with ThreadPoolExecutor(8) as ex:
            for r in ex.map(fw, [p for p in pairs if slug(p[0], p[1], p[2]) not in have]):
                if r: out.append(r)
        if pairs:
            DIAG["hist_probe"] = str(get(f"{CLOB}/prices-history", market=json.loads(pairs[0][3].get("clobTokenIds") or "[\"\"]")[0], startTs=pairs[0][2]-60, endTs=pairs[0][2]+pairs[0][1]*60, fidelity=1))[:300]
    out.sort(key=lambda w: w["start"])
    json.dump(out, open("data/windows.json", "w"))
    os.makedirs("results", exist_ok=True); json.dump(DIAG, open("results/collect_diag.json", "w"), indent=1, default=str)
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
