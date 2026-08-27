#!/usr/bin/env python3
"""
PolyBot Dashboard — веб-панель для bot.py.

Читает state.json / trades.csv / bot.log / .env из той же папки и показывает
всё в браузере. Может запускать и останавливать bot.py кнопкой.

Запуск:   python dashboard.py        → открой http://127.0.0.1:8080
Ничего дополнительно ставить не нужно — только стандартная библиотека.
"""

import csv
import json
import os
import signal
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if os.path.exists(".env"):
    for _l in open(".env", encoding="utf-8", errors="ignore"):
        _l = _l.strip()
        if _l.startswith("DASH_PASSWORD=") and not os.getenv("DASH_PASSWORD"):
            os.environ["DASH_PASSWORD"] = _l.split("=", 1)[1].split("#")[0].strip()
        if _l.startswith("DASH_PORT=") and not os.getenv("DASH_PORT"):
            os.environ["DASH_PORT"] = _l.split("=", 1)[1].split("#")[0].strip()

PORT = int(os.getenv("DASH_PORT", "8080"))
PASSWORD = os.getenv("DASH_PASSWORD", "")          # если задан — панель доступна снаружи с паролем
HOST = "0.0.0.0" if PASSWORD else "127.0.0.1"      # без пароля — только с этого компьютера
STATE_FILE, TRADES_FILE, LOG_FILE, ENV_FILE = "state.json", "trades.csv", "bot.log", ".env"

# ───────────────────────── bot process control ─────────────────────────

_proc = None
_proc_lock = threading.Lock()


def bot_running():
    return _proc is not None and _proc.poll() is None


def start_bot():
    global _proc
    with _proc_lock:
        if bot_running():
            return False
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        _proc = subprocess.Popen([sys.executable, "bot.py"], cwd=HERE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        return True


def stop_bot():
    global _proc
    with _proc_lock:
        if not bot_running():
            return False
        try:
            if os.name == "nt":
                _proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                _proc.send_signal(signal.SIGINT)
            _proc.wait(timeout=8)
        except Exception:
            _proc.kill()
        return True


# ───────────────────────── data readers ─────────────────────────

def read_env():
    cfg = {}
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.split("#", 1)[0].strip()
            cfg[k.strip()] = v
    for k in ("MODE", "ASSETS", "BANKROLL", "MAX_ENTRY", "MIN_ELAPSED", "MIN_MOVE", "TIER_ENTRY", "MIN_MOVE_HIGH", "MIN_CONF",
              "KELLY_FRAC", "MAX_POSITIONS", "MAX_EXPOSURE", "DAILY_LOSS_LIMIT", "CONSEC_LOSS_LIMIT", "RATE_LIMIT", "WINDOWS", "PRICE_SOURCE"):
        if k not in cfg and os.getenv(k):
            cfg[k] = os.getenv(k)
    # секреты наружу не отдаём
    for k in ("POLY_PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "DASH_PASSWORD"):
        if k in cfg:
            cfg[k] = "set" if cfg[k] else ""
    return cfg


def cfg_num(cfg, key, default):
    try:
        return float(cfg.get(key) or os.getenv(key) or default)
    except ValueError:
        return default


def read_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return None


def read_trades():
    rows = []
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, encoding="utf-8", errors="ignore") as f:
            for r in csv.reader(f):
                if len(r) < 7:
                    continue
                try:
                    rows.append({"opened": r[0], "asset": r[1], "side": r[2],
                                 "entry": float(r[3]), "cost": float(r[4]),
                                 "won": r[5].strip().lower() == "true", "pnl": float(r[6])})
                except ValueError:
                    continue
    return rows


def tail_log(n=60):
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, encoding="utf-8", errors="ignore") as f:
        return list(deque(f, maxlen=n))


def log_active_recently():
    """Бот жив, если лог менялся в последние 30 сек (для запуска не из панели)."""
    try:
        return (datetime.now().timestamp() - os.path.getmtime(LOG_FILE)) < 30
    except OSError:
        return False


def snapshot():
    cfg = read_env()
    state = read_state() or {}
    trades = read_trades()
    closed = state.get("closed") or []
    if len(closed) < len(trades):
        # trades.csv надёжнее, если state.json был сброшен
        closed_src = trades
    else:
        closed_src = [{"opened": t.get("opened"), "asset": t.get("asset"), "side": t.get("side"),
                       "entry": t.get("entry"), "cost": t.get("cost"), "won": t.get("won"),
                       "pnl": t.get("pnl", 0.0), "conf": t.get("conf"), "move": t.get("move"),
                       "question": t.get("question")} for t in closed]

    start_bankroll = cfg_num(cfg, "BANKROLL", 500.0)
    wins = [t["pnl"] for t in closed_src if t["won"]]
    losses = [t["pnl"] for t in closed_src if not t["won"]]
    total_pnl = sum(t["pnl"] for t in closed_src)
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (None if not wins else float("inf"))

    # equity curve
    eq, run = [start_bankroll], start_bankroll
    for t in closed_src:
        run += t["pnl"]
        eq.append(round(run, 2))
    peak, maxdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        maxdd = max(maxdd, peak - v)

    positions = list((state.get("positions") or {}).values())
    exposure = sum(p.get("cost", 0) for p in positions)
    bankroll = state.get("bankroll", start_bankroll)
    max_exposure = cfg_num(cfg, "MAX_EXPOSURE", 0.40)
    max_positions = int(cfg_num(cfg, "MAX_POSITIONS", 10))
    daily_limit = cfg_num(cfg, "DAILY_LOSS_LIMIT", 50.0)
    rate_limit = int(cfg_num(cfg, "RATE_LIMIT", 20))
    consec_limit = int(cfg_num(cfg, "CONSEC_LOSS_LIMIT", 4))

    now_ts = datetime.now(timezone.utc).timestamp()
    trade_times = [t for t in (state.get("trade_times") or []) if t > now_ts - 3600]

    cooldown = state.get("cooldown_until")
    in_cooldown = False
    if cooldown:
        try:
            in_cooldown = datetime.fromisoformat(str(cooldown).replace("Z", "+00:00")) > datetime.now(timezone.utc)
        except ValueError:
            pass

    day_pnl = state.get("day_pnl", 0.0)
    gates = [
        {"name": "Дневной стоп", "value": f"{day_pnl:+.2f} / -{daily_limit:.0f} $",
         "pct": min(1, max(0, -day_pnl / daily_limit)) if daily_limit else 0, "blocked": day_pnl <= -daily_limit},
        {"name": "Открытые позиции", "value": f"{len(positions)} / {max_positions}",
         "pct": len(positions) / max_positions if max_positions else 0, "blocked": len(positions) >= max_positions},
        {"name": "Экспозиция", "value": f"{exposure:.2f} / {start_bankroll * max_exposure:.0f} $",
         "pct": exposure / (start_bankroll * max_exposure) if start_bankroll * max_exposure else 0,
         "blocked": exposure >= start_bankroll * max_exposure},
        {"name": "Сделок за час", "value": f"{len(trade_times)} / {rate_limit}",
         "pct": len(trade_times) / rate_limit if rate_limit else 0, "blocked": len(trade_times) >= rate_limit},
        {"name": "Убытков подряд", "value": f"{state.get('consec_losses', 0)} / {consec_limit}",
         "pct": state.get("consec_losses", 0) / consec_limit if consec_limit else 0,
         "blocked": in_cooldown, "extra": f"пауза до {cooldown}" if in_cooldown else ""},
    ]

    longs = sum(1 for t in closed_src if t["side"] == "UP") + sum(1 for p in positions if p.get("side") == "UP")
    shorts = sum(1 for t in closed_src if t["side"] == "DOWN") + sum(1 for p in positions if p.get("side") == "DOWN")
    per_asset = {}
    for t in closed_src:
        a = per_asset.setdefault(t["asset"], {"n": 0, "wins": 0, "pnl": 0.0})
        a["n"] += 1; a["wins"] += int(bool(t["won"])); a["pnl"] += t["pnl"]

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "mode": (cfg.get("MODE") or "paper").lower(),
        "assets": cfg.get("ASSETS") or "BTC,ETH,SOL",
        "config": cfg,
        "bot": {"running": bot_running() or log_active_recently(), "managed": bot_running()},
        "bankroll": bankroll, "start_bankroll": start_bankroll,
        "day_pnl": day_pnl, "total_pnl": round(total_pnl, 2),
        "trades": len(closed_src), "wins": len(wins), "losses": len(losses),
        "winrate": (len(wins) / len(closed_src)) if closed_src else None,
        "pf": None if pf is None else (pf if pf != float("inf") else 99.0),
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "max_dd": round(maxdd, 2),
        "equity": eq,
        "exposure": round(exposure, 2),
        "positions": positions,
        "closed": closed_src[-40:][::-1],
        "gates": gates,
        "per_asset": per_asset,
        "longs": longs, "shorts": shorts,
        "log": tail_log(),
    }


# ───────────────────────── HTML ─────────────────────────

HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PolyBot</title>
<link rel="manifest" href="/manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="PolyBot">
<meta name="theme-color" content="#0d1420">
<link rel="apple-touch-icon" href="/icon.svg">
<style>
:root{--bg:#0d1420;--panel:#131d2e;--panel2:#182437;--line:#243248;--txt:#e4ebf5;--mut:#7f8da6;
--up:#35d99b;--down:#ff6b7a;--warn:#f5b64a;--acc:#8ab4ff;
--mono:"JetBrains Mono","SF Mono",Consolas,"Cascadia Code",monospace;
--sans:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.45 var(--sans)}
a{color:var(--acc)}
.wrap{max-width:1360px;margin:0 auto;padding:18px;padding-top:calc(18px + env(safe-area-inset-top))}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:16px}
header h1{font:600 20px var(--mono);letter-spacing:.04em;margin:0}
.pill{font:600 11px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;padding:6px 10px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}
.pill.live{border-color:var(--down);color:var(--down)}
.pill.paper{border-color:var(--acc);color:var(--acc)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--mut);margin-right:6px;vertical-align:1px}
.dot.on{background:var(--up);box-shadow:0 0 0 4px rgba(53,217,155,.18);animation:pulse 1.6s infinite}
@keyframes pulse{50%{box-shadow:0 0 0 7px rgba(53,217,155,.05)}}
@media(prefers-reduced-motion:reduce){.dot.on{animation:none}}
.spacer{flex:1}
button{font:600 13px var(--sans);padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);color:var(--txt);cursor:pointer}
button:hover{border-color:var(--acc)}button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
button.primary{background:var(--up);color:#062015;border-color:transparent}
button.danger{background:transparent;color:var(--down);border-color:var(--down)}
button:disabled{opacity:.45;cursor:default}
.grid{display:grid;gap:12px;grid-template-columns:repeat(12,1fr)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.card h2{margin:0 0 10px;font:600 11px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--mut)}
.c3{grid-column:span 3}.c4{grid-column:span 4}.c6{grid-column:span 6}.c8{grid-column:span 8}.c12{grid-column:span 12}
@media(max-width:1000px){.c3{grid-column:span 6}.c4{grid-column:span 6}.c8{grid-column:span 12}.c6{grid-column:span 12}}
@media(max-width:600px){.c3,.c4{grid-column:span 12}.wrap{padding:10px;padding-top:calc(10px + env(safe-area-inset-top))}header h1{font-size:17px}.big{font-size:26px}button{padding:10px 12px}.tw{max-height:300px}}
.big{font:600 30px/1.1 var(--mono);letter-spacing:-.01em}
.sub{color:var(--mut);font-size:12px;margin-top:4px;font-family:var(--mono)}
.m{font-family:var(--mono)}.mini{display:inline-block;width:54px;height:5px;background:#0a101a;border-radius:99px;vertical-align:middle;margin-right:6px}.mini i{display:block;height:100%;background:var(--acc);border-radius:99px}
.go{color:var(--up);font-weight:700}
tr.pot td{background:rgba(245,182,74,.07)}.pot .reason{color:var(--warn)!important;font-weight:600}
.potbox{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.potbox div{border:1px solid var(--warn);border-radius:10px;padding:10px 12px;font-family:var(--mono);font-size:12px;background:rgba(245,182,74,.06)}
.potbox b{font-size:15px}
.tk{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}
.tk div{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 14px;font-family:var(--mono)}
.tk .a{font-size:11px;color:var(--mut);letter-spacing:.1em}.tk .p{font-size:20px;font-weight:600;transition:color .3s}.tk .s{font-size:11px;color:var(--mut)}
.tk .up{color:var(--up)}.tk .dn{color:var(--down)}
.pos{color:var(--up)}.neg{color:var(--down)}.warn{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
th{text-align:left;color:var(--mut);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid rgba(36,50,72,.6);white-space:nowrap}
tr:last-child td{border-bottom:0}
.tw{overflow:auto;max-height:420px}
.gate{display:grid;grid-template-columns:150px 1fr auto;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid rgba(36,50,72,.6)}
.gate:last-child{border-bottom:0}
.gate .n{font-size:12px}
.gate .v{font:12px var(--mono);color:var(--mut);text-align:right}
.bar{height:6px;background:#0a101a;border-radius:99px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc);border-radius:99px;transition:width .4s}
.bar i.hot{background:var(--warn)}.bar i.blocked{background:var(--down)}
.win{margin:10px 0;padding:10px 12px;background:var(--panel2);border-radius:10px;border:1px solid var(--line)}
.win .top{display:flex;justify-content:space-between;gap:10px;font-family:var(--mono);font-size:12px}
.win .track{position:relative;height:10px;background:#0a101a;border-radius:99px;margin:8px 0 6px}
.win .track .e{position:absolute;inset:0 auto 0 0;background:linear-gradient(90deg,var(--line),var(--acc));border-radius:99px}
.win .track .m{position:absolute;top:-4px;width:2px;height:18px;background:var(--txt);border-radius:1px}
.win .foot{display:flex;justify-content:space-between;color:var(--mut);font:11px var(--mono)}
.side{font-weight:700}.side.UP{color:var(--up)}.side.DOWN{color:var(--down)}
pre{margin:0;font:11.5px/1.5 var(--mono);color:#b9c4d8;white-space:pre-wrap;word-break:break-word;max-height:360px;overflow:auto}
pre .l-warn{color:var(--warn)}pre .l-err{color:var(--down)}pre .l-buy{color:var(--acc)}pre .l-win{color:var(--up)}pre .l-loss{color:var(--down)}
.empty{color:var(--mut);font-size:13px;padding:10px 0}
svg{display:block;width:100%;height:170px}
.kv{display:grid;grid-template-columns:1fr auto;gap:4px 12px;font:12px var(--mono)}
.kv span:nth-child(odd){color:var(--mut)}
.toast{position:fixed;right:16px;bottom:16px;background:var(--panel2);border:1px solid var(--line);padding:10px 14px;border-radius:10px;font-size:13px;opacity:0;transition:opacity .3s}
.toast.show{opacity:1}
</style></head><body><div class="wrap">
<header>
  <h1>POLYBOT</h1>
  <span id="modePill" class="pill">—</span>
  <span class="pill"><span id="dot" class="dot"></span><span id="botTxt">не запущен</span></span>
  <span class="pill" id="assets">—</span>

  <span class="spacer"></span>
  <span class="sub" id="updated"></span>
  <button id="btnStart" class="primary">Запустить бота</button>
  <button id="btnStop" class="danger">Остановить</button>
</header>

<div id="ticker" class="tk"></div>
<div class="grid">
  <div class="card c3"><h2>Банкролл</h2><div class="big" id="bankroll">—</div><div class="sub" id="bankrollSub"></div></div>
  <div class="card c3"><h2>P&amp;L сегодня</h2><div class="big" id="dayPnl">—</div><div class="sub" id="totalPnl"></div></div>
  <div class="card c3"><h2>Winrate</h2><div class="big" id="winrate">—</div><div class="sub" id="wl"></div><div class="sub" id="ls"></div></div>
  <div class="card c3"><h2>Profit factor</h2><div class="big" id="pf">—</div><div class="sub" id="pfSub"></div></div>

  <div class="card c8"><h2>Кривая капитала</h2><svg id="eq" viewBox="0 0 800 170" preserveAspectRatio="none"></svg><div class="sub" id="eqSub"></div></div>
  <div class="card c4"><h2>Риск-гейты</h2><div id="gates"></div></div>

  <div class="card c12" id="potCard" style="display:none"><h2>Потенциальные сделки</h2><div class="potbox" id="pot"></div><div class="sub">Движение и цена уже подходят — бот войдёт, когда пройдёт нужная часть окна, если ничего не изменится.</div></div>

  <div class="card c12"><h2>Что видит бот <span id="watchN"></span></h2><div class="tw"><table><thead><tr><th>Актив</th><th>Окно</th><th>Прошло</th><th>Старт</th><th>Сейчас</th><th>Движение</th><th>Up / Down</th><th>Решение</th></tr></thead><tbody id="watch"></tbody></table></div><div class="sub">Вход, если прошло ≥ MIN_ELAPSED окна, движение ≥ MIN_MOVE и исход в сторону движения стоит ≤ MAX_ENTRY.</div></div>

  <div class="card c6"><h2>Открытые позиции <span id="posN"></span></h2><div id="positions"></div></div>
  <div class="card c6"><h2>Закрытые сделки</h2><div class="tw"><table><thead><tr><th>Время</th><th>Актив</th><th>Сторона</th><th>Вход</th><th>Ставка</th><th>Итог</th><th>P&amp;L</th></tr></thead><tbody id="closed"></tbody></table></div></div>

  <div class="card c4"><h2>По активам</h2><div id="perAsset"></div></div>
  <div class="card c8"><h2>Лог</h2><pre id="log"></pre></div>

  <div class="card c12"><h2>Конфиг (.env)</h2><div class="kv" id="cfg"></div></div>
</div>
</div>
<div id="toast" class="toast"></div>
<script>
const $=id=>document.getElementById(id);
const fmt=(n,d=2)=>(n==null||isNaN(n))?'—':Number(n).toFixed(d);
const sgn=n=>(n>0?'+':'')+fmt(n);
const cls=n=>n>0?'pos':n<0?'neg':'';
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let last=null, positions=[];

function toast(t){const e=$('toast');e.textContent=t;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),2500)}

function drawEquity(eq,start){
  const s=$('eq'); if(!eq||eq.length<2){s.innerHTML='';$('eqSub').textContent='Пока нет закрытых сделок — кривая появится после первой.';return}
  const W=800,H=170,p=8, mn=Math.min(...eq,start), mx=Math.max(...eq,start), r=(mx-mn)||1;
  const x=i=>p+i*(W-2*p)/(eq.length-1), y=v=>H-p-(v-mn)*(H-2*p)/r;
  const pts=eq.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const lastV=eq[eq.length-1], col=lastV>=start?'var(--up)':'var(--down)';
  s.innerHTML=`<line x1="${p}" x2="${W-p}" y1="${y(start)}" y2="${y(start)}" stroke="var(--line)" stroke-dasharray="4 4"/>
  <polygon points="${p},${y(mn)} ${pts} ${x(eq.length-1)},${y(mn)}" fill="${col}" opacity=".08"/>
  <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
  $('eqSub').textContent=`старт ${fmt(start,0)} $ → ${fmt(lastV)} $ · макс. просадка ${fmt(last.max_dd)} $`;
}

function renderPositions(){
  const box=$('positions'); $('posN').textContent=positions.length?`(${positions.length})`:'';
  if(!positions.length){box.innerHTML='<div class="empty">Нет открытых позиций. Бот ждёт рынок, где прошло ≥ MIN_ELAPSED окна и цена ушла ≥ MIN_MOVE.</div>';return}
  const now=Date.now();
  box.innerHTML=positions.map(p=>{
    const end=Date.parse(p.end), open=Date.parse(p.opened), minutes=p.minutes||(p.question&&/15/.test(p.question)?15:60);
    const start=end-minutes*60000, total=end-start, el=Math.min(1,Math.max(0,(now-start)/total));
    const left=Math.max(0,end-now), mm=Math.floor(left/60000), ss=Math.floor(left%60000/1000);
    const status=left>0?`до конца ${mm}:${String(ss).padStart(2,'0')}`:'ждём резолва…';
    return `<div class="win">
      <div class="top"><span><b>${esc(p.asset)}</b> <span class="side ${esc(p.side)}">${esc(p.side)}</span> @ ${fmt(p.entry)}</span>
      <span>${fmt(p.cost)} $ · ${fmt(p.shares,1)} шт · выплата ${fmt(p.shares)} $</span></div>
      <div class="track"><div class="e" style="width:${(el*100).toFixed(1)}%"></div><div class="m" style="left:${(((open-start)/total)*100).toFixed(1)}%" title="вход"></div></div>
      <div class="foot"><span>conf ${fmt(p.conf)} · move ${sgn(p.move*100)}% · ref ${fmt(p.ref,2)}</span><span>${status}</span></div>
    </div>`}).join('');
}

function render(d){
  last=d; positions=d.positions||[];
  $('modePill').textContent=d.mode; $('modePill').className='pill '+d.mode;
  $('assets').textContent=d.assets;
  $('dot').className='dot'+(d.bot.running?' on':''); $('botTxt').textContent=d.bot.running?'работает':'не запущен';
  $('btnStart').disabled=d.bot.running; $('btnStop').disabled=!d.bot.managed;
  $('updated').textContent='обновлено '+new Date().toLocaleTimeString();
  const diff=d.bankroll-d.start_bankroll;
  $('bankroll').textContent=fmt(d.bankroll)+' $';
  $('bankrollSub').innerHTML=`<span class="${cls(diff)}">${sgn(diff)} $</span> от старта · в позициях ${fmt(d.exposure)} $`;
  $('dayPnl').textContent=sgn(d.day_pnl)+' $'; $('dayPnl').className='big '+cls(d.day_pnl);
  $('totalPnl').innerHTML=`всего <span class="${cls(d.total_pnl)}">${sgn(d.total_pnl)} $</span> за ${d.trades} сделок`;
  $('winrate').textContent=d.winrate==null?'—':(d.winrate*100).toFixed(0)+'%';
  $('wl').textContent=`${d.wins} побед / ${d.losses} убытков · ср. +${fmt(d.avg_win)} / ${fmt(d.avg_loss)}`;
  $('ls').innerHTML=`<span class="pos">UP ${d.longs??0}</span> · <span class="neg">DOWN ${d.shorts??0}</span> всего`;
  const pf=d.pf; $('pf').textContent=pf==null?'—':(pf>=99?'∞':fmt(pf)); $('pf').className='big '+(pf==null?'':pf>=1.3?'pos':pf>=1?'warn':'neg');
  $('pfSub').textContent=pf==null?'нужны закрытые сделки':pf>=1.3?'выше порога 1.3 — можно думать о live':'ниже 1.3 — на live рано';
  drawEquity(d.equity,d.start_bankroll);
  $('gates').innerHTML=d.gates.map(g=>`<div class="gate"><span class="n">${esc(g.name)}${g.blocked?' <span class="neg">· стоп</span>':''}</span>
    <div class="bar"><i class="${g.blocked?'blocked':g.pct>=.75?'hot':''}" style="width:${Math.min(100,g.pct*100).toFixed(0)}%"></i></div>
    <span class="v">${esc(g.value)}${g.extra?'<br>'+esc(g.extra):''}</span></div>`).join('');
  renderPositions();
  const w=d.watch||[]; $('watchN').textContent=w.length?`(${w.length} рынков)`:'';
  const pots=w.filter(x=>x.potential);$('potCard').style.display=pots.length?'':'none';
  $('pot').innerHTML=pots.map(x=>{const t=Math.max(0,x.potential.in_sec-Math.round((Date.now()-Date.parse(d.now))/1000));
    return `<div><b>${esc(x.asset)} <span class="side ${x.potential.side}">${x.potential.side}</span></b> @ ${fmt(x.potential.ask)}<br>окно ${x.minutes}м · move ${sgn(x.move*100)}%<br>вход через ~${Math.floor(t/60)}:${String(t%60).padStart(2,'0')}</div>`}).join('');
  $('watch').innerHTML=w.length?w.map(x=>{const end=Date.parse(x.end),left=Math.max(0,end-Date.now()),mm=Math.floor(left/60000),ss=Math.floor(left%60000/1000);
    const pot=x.potential?` class="pot"`:'';const rs=x.potential?`⏳ ${x.potential.side} @ ${fmt(x.potential.ask)} через ~${Math.floor(x.potential.in_sec/60)}:${String(x.potential.in_sec%60).padStart(2,'0')}`:(x.error||x.reason||'');
    return `<tr${pot}><td><b>${esc(x.asset)}</b></td><td>${x.minutes}м · до ${new Date(end).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</td>
    <td><span class="mini"><i style="width:${Math.min(100,x.elapsed*100).toFixed(0)}%"></i></span>${(x.elapsed*100).toFixed(0)}% · ${mm}:${String(ss).padStart(2,'0')}</td>
    <td>${fmt(x.ref,2)}</td><td>${fmt(x.cur,2)}</td><td class="${cls(x.move)}">${x.move==null?'—':sgn(x.move*100)+'%'}</td>
    <td><span class="pos">${fmt(x.up_ask)}</span> / <span class="neg">${fmt(x.down_ask)}</span></td>
    <td class="reason ${x.reason==='ВХОД'?'go':''}" style="color:var(--mut)">${esc(rs)}</td></tr>`}).join('')
    :'<tr><td colspan="8" class="empty">Бот ещё не отсканировал рынки — подожди полминуты.</td></tr>';
  if(!window.__tick)tick();
  $('closed').innerHTML=d.closed.length?d.closed.map(t=>`<tr><td>${esc((t.opened||'').slice(5,16).replace('T',' '))}</td><td>${esc(t.asset)}</td>
    <td class="side ${esc(t.side)}">${esc(t.side)}</td><td>${fmt(t.entry)}</td><td>${fmt(t.cost)}</td>
    <td class="${t.won?'pos':'neg'}">${t.won?'WIN':'LOSS'}</td><td class="${cls(t.pnl)}">${sgn(t.pnl)}</td></tr>`).join('')
    :'<tr><td colspan="7" class="empty">Закрытых сделок ещё нет.</td></tr>';
  const pa=Object.entries(d.per_asset);
  $('perAsset').innerHTML=pa.length?`<table><thead><tr><th>Актив</th><th>Сделок</th><th>Winrate</th><th>P&amp;L</th></tr></thead><tbody>`+
    pa.map(([a,s])=>`<tr><td><b>${esc(a)}</b></td><td>${s.n}</td><td>${(s.wins/s.n*100).toFixed(0)}%</td><td class="${cls(s.pnl)}">${sgn(s.pnl)}</td></tr>`).join('')+'</tbody></table>'
    :'<div class="empty">Появится после первых сделок.</div>';
  $('log').innerHTML=d.log.length?d.log.map(l=>{const c=/ERROR/.test(l)?'l-err':/WARN/.test(l)?'l-warn':/ BUY /.test(l)?'l-buy':/ WIN /.test(l)?'l-win':/ LOSS /.test(l)?'l-loss':'';return `<span class="${c}">${esc(l.trimEnd())}</span>`}).join('\n'):'bot.log пуст — запусти бота.';
  $('log').scrollTop=$('log').scrollHeight;
  const hide=new Set(['POLY_FUNDER']);
  $('cfg').innerHTML=Object.entries(d.config).filter(([k])=>!hide.has(k)).map(([k,v])=>`<span>${esc(k)}</span><span>${esc(v||'—')}</span>`).join('')||'<span>.env не найден</span><span>скопируй .env.example → .env</span>';
}

async function refresh(){try{const u=window.__GIST__?window.__GIST__+'?t='+Date.now():'/api/state';const r=await fetch(u,{cache:'no-store'});const d=await r.json();render(d);
 if(window.__GIST__){const age=(Date.now()-Date.parse(d.now))/1000;$('updated').textContent='данные '+(age<90?Math.round(age)+' сек назад':Math.round(age/60)+' мин назад');if(age>180){$('dot').className='dot';$('botTxt').textContent='бот не отвечает'}}}
 catch(e){$('updated').textContent=window.__GIST__?'gist недоступен':'нет связи с dashboard.py'}}
async function act(a){try{const r=await fetch('/api/'+a,{method:'POST'});const j=await r.json();toast(j.msg);setTimeout(refresh,800)}catch(e){toast('Ошибка: '+e)}}
$('btnStart').onclick=()=>act('start'); $('btnStop').onclick=()=>act('stop');
const SYM={BTC:'BTCUSDT',ETH:'ETHUSDT',SOL:'SOLUSDT',XRP:'XRPUSDT'},CB={BTC:'BTC-USD',ETH:'ETH-USD',SOL:'SOL-USD',XRP:'XRP-USD'};
let prev={},src='';
function assets(){return (last?last.assets:'BTC,ETH,SOL').split(',').map(s=>s.trim()).filter(a=>SYM[a])}
async function viaBinance(as){const r=await fetch('https://api.binance.com/api/v3/ticker/price?symbols='+encodeURIComponent(JSON.stringify(as.map(a=>SYM[a]))));const j=await r.json();const o={};j.forEach(x=>{const a=Object.keys(SYM).find(k=>SYM[k]===x.symbol);if(a)o[a]=+x.price});return o}
async function viaCoinbase(as){const o={};await Promise.all(as.map(async a=>{const r=await fetch('https://api.coinbase.com/v2/prices/'+CB[a]+'/spot');o[a]=+(await r.json()).data.amount}));return o}
function showPrices(p,label){$('ticker').innerHTML=Object.entries(p).map(([a,v])=>{const c=prev[a]==null?'':v>prev[a]?'up':v<prev[a]?'dn':'';prev[a]=v;
  return `<div><div class="a">${a}/USD</div><div class="p ${c}">${fmt(v,a==='SOL'||a==='XRP'?2:0)}</div><div class="s">${label}</div></div>`}).join('');
  if(last&&last.watch)last.watch.forEach(w=>{if(p[w.asset]&&w.ref){w.cur=p[w.asset];w.move=(w.cur-w.ref)/w.ref}})}
async function tick(){const as=assets();if(!as.length)return;
  try{showPrices(await viaBinance(as),'Binance · live');window.__tick=true;return}catch(e){}
  try{showPrices(await viaCoinbase(as),'Coinbase · live');window.__tick=true;return}catch(e){}
  if(last&&last.prices&&Object.keys(last.prices).length)showPrices(last.prices,'из снимка бота')}
setInterval(tick,3000);tick();
if(window.__GIST__){$('btnStart').style.display=$('btnStop').style.display='none';refresh();setInterval(refresh,15000);setInterval(renderPositions,1000)}
else if(window.__DATA__){render(window.__DATA__);$('btnStart').style.display=$('btnStop').style.display='none';$('updated').textContent='снимок '+new Date(window.__DATA__.now).toLocaleString();setInterval(renderPositions,1000)}
else{refresh(); setInterval(refresh,5000); setInterval(renderPositions,1000)}
</script></body></html>"""


# ───────────────────────── HTTP ─────────────────────────

ICON = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 180 180'><rect width='180' height='180' rx='36' fill='#0d1420'/><polyline points='30,120 65,95 95,110 150,55' fill='none' stroke='#35d99b' stroke-width='12' stroke-linecap='round' stroke-linejoin='round'/></svg>"""
MANIFEST = json.dumps({"name": "PolyBot", "short_name": "PolyBot", "start_url": "/", "display": "standalone",
                       "background_color": "#0d1420", "theme_color": "#0d1420",
                       "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # тихий сервер
        pass

    def _authed(self):
        if not PASSWORD:
            return True
        import base64
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                if pw == PASSWORD:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="PolyBot"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/manifest.json":
            return self._send(200, MANIFEST, "application/manifest+json")
        if path == "/icon.svg":
            return self._send(200, ICON, "image/svg+xml")
        if not self._authed():
            return
        if path == "/api/state":
            try:
                self._send(200, json.dumps(snapshot(), default=str), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}), "application/json")
        elif path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._authed():
            return
        path = urlparse(self.path).path
        if path == "/api/start":
            if not os.path.exists("bot.py"):
                self._send(200, json.dumps({"ok": False, "msg": "bot.py не найден рядом с dashboard.py"}), "application/json")
                return
            ok = start_bot()
            self._send(200, json.dumps({"ok": ok, "msg": "Бот запущен" if ok else "Бот уже работает"}), "application/json")
        elif path == "/api/stop":
            ok = stop_bot()
            self._send(200, json.dumps({"ok": ok, "msg": "Бот остановлен" if ok else "Бот запущен не из панели — останови его в своём терминале (Ctrl+C)"}), "application/json")
        else:
            self._send(404, "not found", "text/plain")


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    if PASSWORD:
        print(f"PolyBot Dashboard: порт {PORT}, доступ снаружи с паролем   (Ctrl+C — выход)")
    else:
        print(f"PolyBot Dashboard: {url}   (Ctrl+C — выход)")
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    if os.getenv("DASH_AUTOSTART", "").lower() in ("1", "true", "yes") and os.path.exists("bot.py"):
        start_bot(); print("bot.py запущен автоматически")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_bot()
        srv.server_close()


if __name__ == "__main__":
    main()
