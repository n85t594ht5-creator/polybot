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
              "KELLY_FRAC", "MAX_POSITIONS", "MAX_EXPOSURE", "MAX_STAKE", "MIN_ENTRY", "POLY_ADDRESS", "DAILY_LOSS_LIMIT", "CONSEC_LOSS_LIMIT", "RATE_LIMIT", "WINDOWS", "PRICE_SOURCE"):
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
tr.pot td{background:rgba(245,182,74,.07)}.pot .reason .rh{color:var(--warn);font-weight:600}.go .rh{color:var(--up);font-weight:700}
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
.menu{position:relative}.menu .dd{display:none;position:absolute;top:110%;left:0;background:var(--panel2);border:1px solid var(--line);border-radius:10px;min-width:210px;z-index:20;overflow:hidden}
.menu.open .dd{display:block}.menu .dd a{display:block;padding:11px 14px;color:var(--txt);text-decoration:none;font-size:14px;border-bottom:1px solid var(--line)}.menu .dd a:hover,.menu .dd a.on{background:var(--panel);color:var(--acc)}
.view{display:none}.view.on{display:block}
.set{display:grid;grid-template-columns:1fr;gap:10px}.set .row{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.set .row .h{display:flex;justify-content:space-between;align-items:center;gap:10px;font-family:var(--mono);font-size:13px}.set .row .h b{font-size:14px;color:var(--acc)}
.set .row .d{font-size:12px;color:var(--mut);margin:6px 0 8px;line-height:1.5}
.set input[type=range]{width:100%;accent-color:var(--acc)}.set input[type=text],.set input[type=password]{width:100%;font:13px var(--mono);padding:9px 10px;border-radius:8px;border:1px solid var(--line);background:#0a101a;color:var(--txt)}
.set .chips{display:flex;gap:6px;flex-wrap:wrap}.set .chips label{font:600 12px var(--mono);padding:6px 10px;border:1px solid var(--line);border-radius:999px;cursor:pointer;color:var(--mut)}.set .chips input{display:none}.set .chips input:checked+span{color:var(--txt)}.set .chips label:has(input:checked){border-color:var(--acc);color:var(--txt)}
.seg2{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}.seg2 button{border:0;border-radius:0;background:transparent;color:var(--mut)}.seg2 button.on{background:var(--acc);color:#08131f}.seg2 button.live.on{background:var(--down);color:#fff}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.pw{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}.pw button{padding:16px;font-size:14px;text-align:left;border-radius:10px}.pw button small{display:block;font-weight:400;color:var(--mut);margin-top:4px;font-size:12px}
.kbox{font-family:var(--mono);font-size:12px;color:var(--mut);background:#0a101a;border:1px solid var(--line);border-radius:8px;padding:10px;margin-top:6px}.kbox b{color:var(--up)}
.warnbox{border:1px solid var(--warn);background:rgba(245,182,74,.08);border-radius:10px;padding:10px 12px;font-size:13px;margin-bottom:12px}
.ov{position:fixed;inset:0;background:rgba(5,9,16,.7);display:none;align-items:center;justify-content:center;z-index:50;padding:16px}.ov.on{display:flex}
.md{background:var(--panel);border:1px solid var(--line);border-radius:14px;max-width:720px;width:100%;max-height:90vh;overflow:auto;padding:18px 20px}
.md h3{margin:0 0 10px;font:600 16px var(--sans)}.md .body{font-size:14px;line-height:1.55}.md .body .sub{margin-top:8px}.md .foot{display:flex;gap:10px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap}
.md input[type=text]{width:100%;font:13px var(--mono);padding:9px 10px;border-radius:8px;border:1px solid var(--line);background:#0a101a;color:var(--txt)}
.rh{color:var(--txt)}.rr{color:var(--mut);font-size:11px}
.alink{cursor:pointer;color:var(--acc);border-bottom:1px dotted var(--acc)}.tk div{cursor:pointer}.chs{display:flex;gap:6px;margin-bottom:8px}.chs button{padding:5px 10px;font-size:12px}.chs button.on{border-color:var(--acc);color:var(--acc)}
.toast{position:fixed;right:16px;bottom:16px;background:var(--panel2);border:1px solid var(--line);padding:10px 14px;border-radius:10px;font-size:13px;opacity:0;transition:opacity .3s}
.toast.show{opacity:1}
</style></head><body><div class="wrap">
<header>
  <div class="menu" id="menu"><button id="menuBtn">☰ Меню</button><div class="dd">
    <a href="#" data-view="dash" class="on">Дашборд</a><a href="backtest.html">Тестировщик</a><a href="#" data-view="settings">Настройки бота</a><a href="#" data-view="keys">Ключи</a><a href="#" data-view="power">Питание</a><a href="#" data-view="stats">Статистика · архив</a></div></div>
  <h1>POLYBOT</h1>
  <span id="modePill" class="pill">—</span>
  <span class="pill"><span id="dot" class="dot"></span><span id="botTxt">не запущен</span></span>
  <span class="pill" id="assets">—</span>

  <span class="spacer"></span>
  <span class="sub" id="updated"></span>
  <button id="btnStart" class="primary">Запустить бота</button>
  <button id="btnStop" class="danger">Остановить</button>
</header>

<div id="v_dash" class="view on">
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

<div id="v_stats" class="view">
 <div class="card"><h2>Статистика · архив</h2>
  <div class="sub" style="margin-bottom:10px">Снимок сохраняется автоматически при каждом «Сохранить настройки» и «Обнулить». Можно сохранить вручную.</div>
  <div class="actions" style="margin:0 0 14px"><button class="primary" id="arcSave">Сохранить снимок сейчас</button><span class="sub" id="arcMsg"></span></div>
  <div class="tw"><table><thead><tr><th>Дата</th><th>Название</th><th>Режим</th><th>Сделок</th><th>Winrate</th><th>PF</th><th>P&amp;L</th><th>Банкролл</th><th></th></tr></thead><tbody id="arcList"></tbody></table></div>
  <div id="arcDetail" style="margin-top:14px"></div>
 </div>
</div>

<div id="v_settings" class="view">
 <div class="card"><h2>Настройки бота</h2>
  <div id="ghWarn" class="warnbox" style="display:none">Чтобы сохранять настройки, добавь токен GitHub в разделе «Ключи».</div>
  <div class="set" id="setForm"></div>
  <div class="actions"><button class="primary" id="setSave">Сохранить и перезапустить</button><button id="setCancel">Отмена</button><span class="sub" id="setMsg"></span></div>
 </div>
</div>

<div id="v_keys" class="view">
 <div class="card"><h2>Ключи</h2>
  <div class="set">
   <div class="row"><div class="h"><b>Токен GitHub</b><span id="ghState" class="sub"></span></div><div class="d">Нужен самой панели, чтобы менять настройки, ключи и управлять ботом. Хранится только в этом браузере. Создать: github.com/settings/tokens/new → галочки <code>repo</code>, <code>workflow</code>.</div><input type="password" id="ghToken" placeholder="ghp_..."><div class="actions"><button id="ghSave">Сохранить в браузере</button><button id="ghForget">Забыть</button></div></div>
   <div class="row"><div class="h"><b>Кошелёк Polygon — приватный ключ</b></div><div class="d">Ключ кошелька с USDC, с которого бот торгует в live. Уходит в GitHub зашифрованным, панель его не хранит и показать не сможет — только адрес. Заведи отдельный кошелёк только под бота.</div><input type="password" id="k_pk" placeholder="0x..."><div class="kbox" id="k_pk_state">не задан</div></div>
   <div class="row"><div class="h"><b>Адрес прокси-кошелька Polymarket (POLY_FUNDER)</b></div><div class="d">Нужен, если в Polymarket заходил через email или Magic: Profile → Settings → адрес депозита. Для MetaMask оставь пустым.</div><input type="text" id="k_funder" placeholder="0x... (необязательно)"></div>
   <div class="row"><div class="h"><b>Тип подписи (POLY_SIGNATURE_TYPE)</b></div><div class="d">0 — обычный кошелёк (MetaMask), 1 — вход через email/Magic, 2 — Gnosis proxy.</div><div class="chips"><label><input type="radio" name="sig" value="0" checked><span>0 · MetaMask</span></label><label><input type="radio" name="sig" value="1"><span>1 · Email</span></label><label><input type="radio" name="sig" value="2"><span>2 · Gnosis</span></label></div></div>
   <div class="row"><div class="h"><b>Telegram</b></div><div class="d">Токен бота от @BotFather и твой chat id (узнать у @userinfobot). Бот будет присылать каждую сделку.</div><input type="password" id="k_tg" placeholder="токен бота" style="margin-bottom:8px"><input type="text" id="k_chat" placeholder="chat id"><div class="kbox" id="k_tg_state">не задан</div></div>
  </div>
  <div class="actions"><button class="primary" id="keysSave">Сохранить ключи</button><span class="sub" id="keysMsg"></span></div>
 </div>
</div>

<div id="v_power" class="view">
 <div class="card"><h2>Питание</h2>
  <div class="sub" id="pwState" style="margin-bottom:12px">—</div>
  <div class="pw">
   <button class="primary" id="pwStart">▶ Запустить<small>включить расписание и стартовать цикл</small></button>
   <button id="pwRestart">↻ Перезапустить<small>остановить текущий цикл и начать новый — подхватит новые настройки</small></button>
   <button class="danger" id="pwStop">■ Остановить полностью<small>прервать цикл и выключить расписание</small></button>
   <button class="danger" id="pwReset">⟲ Обнулить<small>скачать всю статистику в Excel, затем сбросить банкролл и сделки</small></button>
  </div>
  <div class="sub" id="pwMsg" style="margin-top:12px"></div>
 </div>
</div>
</div>
<div id="toast" class="toast"></div>
<div id="ov" class="ov"><div class="md"><h3 id="mdT"></h3><div class="body" id="mdB"></div><div class="foot" id="mdF"></div></div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/libsodium-wrappers/0.7.13/sodium.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/ethers/6.13.2/ethers.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"></script>
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
  const RU=[[/^ВХОД$/,()=>'Входим'],[/окно ещё не началось/,()=>'Окно ещё не началось'],[/^elapsed (\d+)% < (\d+)%/,m=>`Рано: прошло ${m[1]}% окна, ждём ${m[2]}%`],[/too close to end/,()=>'Поздно: до конца меньше 30 сек'],[/no reference price/,()=>'Нет цены старта окна'],
   [/^move ([+-][\d.]+)% < ([\d.]+)%/,m=>`Слабое движение ${m[1]}%, нужно ≥ ${m[2]}%`],[/^(UP|DOWN) ask ([\d.]+) > ([\d.]+)/,m=>`${m[1]} слишком дорог: ${m[2]}, потолок ${m[3]}`],[/^(UP|DOWN) ask ([\d.]+) < MIN_ENTRY ([\d.]+)/,m=>`${m[1]} слишком дёшев: ${m[2]} — рынок не согласен`],
   [/^ask ([\d.]+) > ([\d.]+): move ([+-][\d.]+)% < ([\d.]+)%/,m=>`Дорогой вход ${m[1]}: движение ${m[3]}% мало, нужно ≥ ${m[4]}%`],[/no liquidity/,()=>'Нет ликвидности'],[/^conf ([\d.]+) < ([\d.]+)/,m=>`Уверенность ${m[1]} ниже порога ${m[2]}`],[/size too small/,()=>'Ставка вышла бы меньше $1']];
  const ru=r=>{for(const[re,f]of RU){const m=re.exec(r||'');if(m)return f(m)}return r||''};
  $('watch').innerHTML=w.length?w.map(x=>{const end=Date.parse(x.end),left=Math.max(0,end-Date.now()),mm=Math.floor(left/60000),ss=Math.floor(left%60000/1000);
    const pot=x.potential?` class="pot"`:'';const raw=x.error||x.reason||'';const rs=x.potential?`<span class="rh">Готовится вход ${x.potential.side} по ${fmt(x.potential.ask)} через ~${Math.floor(x.potential.in_sec/60)}:${String(x.potential.in_sec%60).padStart(2,'0')}</span><br><span class="rr">waiting elapsed ≥ MIN_ELAPSED</span>`:`<span class="rh">${esc(x.error?'Ошибка данных':ru(raw))}</span><br><span class="rr">${esc(raw)}</span>`;
    return `<tr${pot}><td><span class="alink" onclick="openChart('${esc(x.asset)}',${x.start?Date.parse(x.start):0},${x.ref||0})"><b>${esc(x.asset)}</b></span></td><td>${x.minutes}м · до ${new Date(end).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</td>
    <td><span class="mini"><i style="width:${Math.min(100,x.elapsed*100).toFixed(0)}%"></i></span>${(x.elapsed*100).toFixed(0)}% · ${mm}:${String(ss).padStart(2,'0')}</td>
    <td>${fmt(x.ref,2)}</td><td>${fmt(x.cur,2)}</td><td class="${cls(x.move)}">${x.move==null?'—':sgn(x.move*100)+'%'}</td>
    <td><span class="pos">${fmt(x.up_ask)}</span> / <span class="neg">${fmt(x.down_ask)}</span></td>
    <td class="reason ${x.reason==='ВХОД'?'go':''}" style="white-space:normal;min-width:220px">${rs}</td></tr>`}).join('')
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
  return `<div onclick="openChart('${a}',0,0)"><div class="a">${a}/USD</div><div class="p ${c}">${fmt(v,a==='SOL'||a==='XRP'?2:0)}</div><div class="s">${label}</div></div>`}).join('');
  if(last&&last.watch)last.watch.forEach(w=>{if(p[w.asset]&&w.ref){w.cur=p[w.asset];w.move=(w.cur-w.ref)/w.ref}})}
async function tick(){const as=assets();if(!as.length)return;
  try{showPrices(await viaBinance(as),'Binance · live');window.__tick=true;return}catch(e){}
  try{showPrices(await viaCoinbase(as),'Coinbase · live');window.__tick=true;return}catch(e){}
  if(last&&last.prices&&Object.keys(last.prices).length)showPrices(last.prices,'из снимка бота')}
setInterval(tick,3000);tick();
// ───── modal ─────
function modal(title,body,buttons){return new Promise(res=>{$('mdT').textContent=title;$('mdB').innerHTML=body;$('mdF').innerHTML='';
 (buttons||[{t:'Закрыть',v:true}]).forEach(b=>{const el=document.createElement('button');el.textContent=b.t;if(b.cls)el.className=b.cls;el.onclick=()=>{$('ov').classList.remove('on');res(b.v)};$('mdF').appendChild(el)});$('ov').classList.add('on')})}
const info=(t,b)=>modal(t,b);const ask=(t,b,ok='Да',cls='primary')=>modal(t,b,[{t:'Отмена',v:false},{t:ok,v:true,cls}]);
async function askText(t,b,def){const v=await modal(t,b+`<input type="text" id="mdIn" value="${esc(def||'')}" style="margin-top:10px">`,[{t:'Отмена',v:false},{t:'Сохранить',v:true,cls:'primary'}]);return v?document.getElementById('mdIn').value:null}
$('ov').onclick=e=>{if(e.target===$('ov'))$('ov').classList.remove('on')};
// ───── mini chart ─────
let chTimer=null,chState={};
async function candles(a,n){try{const r=await fetch(`https://api.binance.com/api/v3/klines?symbol=${SYM[a]}&interval=1m&limit=${n}`);const j=await r.json();return{src:'Binance',c:j.map(k=>({t:k[0],o:+k[1],h:+k[2],l:+k[3],c:+k[4]}))}}catch(e){}
 const r=await fetch(`https://api.exchange.coinbase.com/products/${CB[a]}/candles?granularity=60`);const j=await r.json();return{src:'Coinbase',c:j.slice(0,n).reverse().map(k=>({t:k[0]*1000,o:k[3],h:k[2],l:k[1],c:k[4]}))}}
function drawCandles(d,winStart,ref){const W=680,H=260,p=10,pr=54;const c=d.c;if(!c.length)return '<div class="empty">нет данных</div>';
 const lo=Math.min(...c.map(x=>x.l),ref||Infinity),hi=Math.max(...c.map(x=>x.h),ref||-Infinity),r=(hi-lo)||1;const bw=(W-p-pr)/c.length;const x=i=>p+i*bw,y=v=>p+(hi-v)*(H-2*p)/r;
 let g='';for(let k=0;k<=4;k++){const v=lo+r*k/4;g+=`<line x1="${p}" x2="${W-pr}" y1="${y(v)}" y2="${y(v)}" stroke="#243248" stroke-dasharray="2 4"/><text x="${W-pr+4}" y="${y(v)+4}" fill="#7f8da6" font-size="10" font-family="monospace">${v.toFixed(v>100?0:v>1?2:4)}</text>`}
 c.forEach((k,i)=>{const up=k.c>=k.o,col=up?'#35d99b':'#ff6b7a';g+=`<line x1="${x(i)+bw/2}" x2="${x(i)+bw/2}" y1="${y(k.h)}" y2="${y(k.l)}" stroke="${col}" stroke-width="1"/><rect x="${x(i)+1}" y="${y(Math.max(k.o,k.c))}" width="${Math.max(1,bw-2)}" height="${Math.max(1,Math.abs(y(k.o)-y(k.c)))}" fill="${col}"/>`});
 if(winStart){const i=c.findIndex(k=>k.t>=winStart);if(i>=0)g+=`<line x1="${x(i)}" x2="${x(i)}" y1="${p}" y2="${H-p}" stroke="#f5b64a" stroke-dasharray="4 3"/><text x="${x(i)+3}" y="${p+10}" fill="#f5b64a" font-size="10" font-family="monospace">старт окна</text>`}
 if(ref){g+=`<line x1="${p}" x2="${W-pr}" y1="${y(ref)}" y2="${y(ref)}" stroke="#8ab4ff" stroke-dasharray="4 3"/><text x="${p+3}" y="${y(ref)-3}" fill="#8ab4ff" font-size="10" font-family="monospace">цена старта ${ref}</text>`}
 const lastC=c[c.length-1];g+=`<line x1="${p}" x2="${W-pr}" y1="${y(lastC.c)}" y2="${y(lastC.c)}" stroke="#e4ebf5" stroke-width=".6" opacity=".5"/><rect x="${W-pr+1}" y="${y(lastC.c)-7}" width="${pr-2}" height="14" fill="#e4ebf5"/><text x="${W-pr+4}" y="${y(lastC.c)+4}" fill="#0d1420" font-size="10" font-family="monospace">${lastC.c.toFixed(lastC.c>100?0:lastC.c>1?2:4)}</text>`;
 return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${g}</svg>`}
async function renderChart(){const{a,n,winStart,ref}=chState;try{const d=await candles(a,n);const first=d.c[0],lastC=d.c[d.c.length-1];const chg=first?(lastC.c-first.o)/first.o*100:0;
 $('chBody').innerHTML=drawCandles(d,winStart,ref)+`<div class="sub" style="margin-top:6px">${d.src} · 1 мин · последние ${n} свечей · за период <span class="${cls(chg)}">${chg>=0?'+':''}${chg.toFixed(2)}%</span>${winStart?' · жёлтая линия — старт окна, синяя — цена, от которой считается движение':''}</div>`}catch(e){$('chBody').innerHTML='<div class="empty">Не удалось загрузить котировки: '+esc(e.message)+'</div>'}}
function openChart(a,winStart,ref){chState={a,n:60,winStart,ref};
 modal(a+'/USD · 1 минута',`<div class="chs">${[30,60,120].map(n=>`<button class="${n===60?'on':''}" onclick="chState.n=${n};document.querySelectorAll('.chs button').forEach(b=>b.classList.toggle('on',+b.textContent.replace(/\D/g,'')===${n}));renderChart()">${n} мин</button>`).join('')}</div><div id="chBody"><div class="empty">загрузка…</div></div>`).then(()=>{clearInterval(chTimer);chTimer=null});
 renderChart();clearInterval(chTimer);chTimer=setInterval(()=>{if($('ov').classList.contains('on'))renderChart();else clearInterval(chTimer)},5000)}
// ───── control panel ─────
const REPO=window.__REPO__||'';const GH='https://api.github.com/repos/'+REPO;
const tok=()=>localStorage.getItem('gh_token')||'';
async function gh(path,opt={}){const r=await fetch(GH+path,{...opt,headers:{'Authorization':'Bearer '+tok(),'Accept':'application/vnd.github+json','Content-Type':'application/json',...(opt.headers||{})}});if(r.status===204)return{};const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.message||('HTTP '+r.status));return j}
// views
document.querySelectorAll('#menu .dd a[data-view]').forEach(a=>a.onclick=e=>{e.preventDefault();showView(a.dataset.view)});
$('menuBtn').onclick=e=>{e.stopPropagation();$('menu').classList.toggle('open')};document.addEventListener('click',()=>$('menu').classList.remove('open'));
function showView(v){document.querySelectorAll('.view').forEach(x=>x.classList.toggle('on',x.id==='v_'+v));document.querySelectorAll('#menu .dd a').forEach(a=>a.classList.toggle('on',a.dataset.view===v));$('menu').classList.remove('open');
 if(v==='settings')loadSettings();if(v==='keys')loadKeys();if(v==='power')loadPower();if(v==='stats')loadArchive()}
// settings schema
const S=[
 {k:'MODE',t:'mode',n:'Режим',d:'paper — виртуальные сделки по реальным котировкам, деньги не нужны. live — реальные ордера на Polymarket с твоего кошелька. Переключай на live только после теста и с ключами в разделе «Ключи».'},
 {k:'ASSETS',t:'chips',opts:['BTC','ETH','SOL','XRP','DOGE'],n:'Активы',d:'Какие монеты бот сканирует. Каждая — отдельные рынки Up/Down на Polymarket. XRP и DOGE более дёрганые: больше сигналов, но и разворотов.'},
 {k:'WINDOWS',t:'chips',opts:['5','15','60'],n:'Окна (минуты)',d:'Длина рынков, которые бот торгует. 5 минут — много сделок, но против нас в основном другие боты; 15 — золотая середина; 60 — редко, но спокойнее.'},
 {k:'BANKROLL',t:'range',min:50,max:5000,step:10,n:'Стартовый банкролл, $',d:'От этой суммы бот считает размер ставок. В paper — виртуальная; в live должна соответствовать USDC на кошельке.'},
 {k:'MIN_ELAPSED',t:'range',min:0.3,max:0.95,step:0.05,pct:1,n:'Входить не раньше, чем прошло N% окна',d:'Чем позже вход, тем меньше шанс, что цена развернётся, но и тем дороже обычно стоит нужный исход. Бэктест показал: 75% — лучший баланс.'},
 {k:'MIN_ENTRY',t:'range',min:0,max:0.9,step:0.01,n:'Минимальная цена исхода',d:'Не покупать исход дешевле. Дешёвый исход значит, что рынок не согласен с направлением — и на истории рынок обычно прав. Бэктест: ниже 0.45 убыточно.'},
 {k:'MAX_ENTRY',t:'range',min:0.1,max:0.99,step:0.01,n:'Максимальная цена исхода',d:'Не покупать исход дороже. Чем дороже, тем меньше выплата: за 0.62 получаешь +61% при выигрыше, за 0.9 — только +11%, а проигрыш всё равно −100%.'},
 {k:'MIN_MOVE',t:'range',min:0.0002,max:0.005,step:0.0001,pct:100,dec:2,n:'Минимальное движение цены, %',d:'Насколько цена монеты должна уйти от старта окна, чтобы это считалось сигналом, а не шумом. 0.06% для BTC по $80 000 — это $48.'},
 {k:'TIER_ENTRY',t:'range',min:0.3,max:0.9,step:0.01,n:'Порог «дорогого» входа',d:'Если исход дороже этой цены — требуется более сильное движение (следующий ползунок). Защита от покупки фаворита на слабом сигнале.'},
 {k:'MIN_MOVE_HIGH',t:'range',min:0.0002,max:0.005,step:0.0001,pct:100,dec:2,n:'Движение для дорогих входов, %',d:'Сколько должна пройти цена, если исход дороже порога выше. Обычно чуть больше обычного минимума.'},
 {k:'MIN_CONF',t:'range',min:0.5,max:0.95,step:0.01,n:'Минимальная уверенность',d:'Внутренняя оценка бота: чем позже в окне и чем сильнее движение, тем выше. Ниже этой планки сделка не открывается. Это эвристика, не предсказание.'},
 {k:'KELLY_FRAC',t:'range',min:0.05,max:0.5,step:0.01,n:'Доля Келли',d:'Формула Келли считает «идеальную» ставку для максимального роста. Мы берём её часть: 0.15 — осторожно, 0.25 — стандарт, 0.5 — агрессивно и с большими просадками.'},
 {k:'MAX_STAKE',t:'range',min:0.01,max:0.25,step:0.01,pct:1,n:'Потолок одной ставки, % банкролла',d:'Что бы ни насчитал Келли, одна сделка не больше этой доли. 8% значит: пять убытков подряд — минус ~34%, а не половина.'},
 {k:'MAX_POSITIONS',t:'range',min:1,max:20,step:1,n:'Максимум открытых сделок',d:'Сколько окон бот может держать одновременно.'},
 {k:'MAX_EXPOSURE',t:'range',min:0.05,max:0.8,step:0.05,pct:1,n:'Максимум в позициях, % банкролла',d:'Суммарно во всех открытых сделках не больше этой доли. Остальное всегда остаётся в резерве.'},
 {k:'DAILY_LOSS_LIMIT',t:'range',min:5,max:500,step:5,n:'Дневной стоп, $',d:'Как только за день потеряно столько — бот перестаёт торговать до следующего дня (UTC).'},
 {k:'CONSEC_LOSS_LIMIT',t:'range',min:1,max:10,step:1,n:'Убытков подряд до паузы',d:'После стольких убытков подряд — пауза 24 часа. Защита от дня, когда рынок «пилит» и стратегия не работает.'},
 {k:'RATE_LIMIT',t:'range',min:1,max:60,step:1,n:'Максимум сделок в час',d:'Ограничитель, чтобы бот не «разогнался» на серии одинаковых сигналов.'},
];
const DEF={MODE:'paper',ASSETS:'BTC,ETH,SOL',WINDOWS:'15,60',BANKROLL:'500',MIN_ELAPSED:'0.5',MIN_ENTRY:'0',MAX_ENTRY:'0.15',MIN_MOVE:'0.0008',TIER_ENTRY:'0.45',MIN_MOVE_HIGH:'0.0012',MIN_CONF:'0.6',KELLY_FRAC:'0.25',MAX_STAKE:'0.08',MAX_POSITIONS:'10',MAX_EXPOSURE:'0.4',DAILY_LOSS_LIMIT:'50',CONSEC_LOSS_LIMIT:'4',RATE_LIMIT:'20'};
let VARS={};
function showVal(x,v){if(x.t==='range'){const f=x.pct?(+v*x.pct).toFixed(x.dec??0)+'%':(+v).toString();return f}return v}
async function loadSettings(){$('ghWarn').style.display=tok()?'none':'';let vars={};
 if(tok()){try{const j=await gh('/actions/variables?per_page=100');j.variables.forEach(v=>vars[v.name]=v.value)}catch(e){$('setMsg').textContent='GitHub: '+e.message}}
 else if(last&&last.config)vars=last.config;
 VARS=vars;
 $('setForm').innerHTML=S.map(x=>{const v=vars[x.k]??DEF[x.k];let ctl='';
  if(x.t==='mode')ctl=`<div class="seg2"><button class="${v!=='live'?'on':''}" data-k="MODE" data-v="paper" onclick="setMode(this)">paper</button><button class="live ${v==='live'?'on':''}" data-k="MODE" data-v="live" onclick="setMode(this)">live</button></div><input type="hidden" id="s_MODE" value="${v}">`;
  else if(x.t==='chips'){const cur=String(v).split(',').map(s=>s.trim());ctl=`<div class="chips">${x.opts.map(o=>`<label><input type="checkbox" name="s_${x.k}" value="${o}" ${cur.includes(o)?'checked':''}><span>${o}${x.k==='WINDOWS'?'м':''}</span></label>`).join('')}</div>`}
  else ctl=`<input type="range" id="s_${x.k}" min="${x.min}" max="${x.max}" step="${x.step}" value="${v}" oninput="document.getElementById('sv_${x.k}').textContent=showVal(S.find(y=>y.k==='${x.k}'),this.value)">`;
  return `<div class="row"><div class="h"><span>${x.n} <span class="sub">${x.k}</span></span><b id="sv_${x.k}">${x.t==='range'?showVal(x,v):''}</b></div><div class="d">${x.d}</div>${ctl}</div>`}).join('')}
async function setMode(b){if(b.dataset.v==='live'&&!await ask('Переключить в LIVE?','Бот начнёт выставлять реальные ордера на Polymarket с твоего кошелька.<div class="sub">Проверь: ключ сохранён в «Ключах», тест в paper пройден, на кошельке отдельная небольшая сумма.</div>','Да, live','danger'))return;document.querySelectorAll('.seg2 button').forEach(x=>x.classList.remove('on'));b.classList.add('on');$('s_MODE').value=b.dataset.v}
function readSettings(){const out={};S.forEach(x=>{if(x.t==='chips')out[x.k]=[...document.querySelectorAll(`input[name=s_${x.k}]:checked`)].map(i=>i.value).join(',');else out[x.k]=$('s_'+x.k).value});return out}
async function setVar(k,v){try{await gh('/actions/variables/'+k,{method:'PATCH',body:JSON.stringify({name:k,value:String(v)})})}catch(e){await gh('/actions/variables',{method:'POST',body:JSON.stringify({name:k,value:String(v)})})}}
$('setSave').onclick=async()=>{if(!tok())return toast('Сначала добавь токен GitHub в «Ключи»');const v=readSettings();if(!v.ASSETS||!v.WINDOWS)return toast('Выбери хотя бы один актив и одно окно');
 if(v.MODE==='live'&&VARS.POLY_ADDRESS==null&&!await ask('Ключ не сохранён','Приватный ключ кошелька не задан — в live бот не сможет выставлять ордера. Всё равно сохранить настройки?','Сохранить'))return;
 $('setMsg').textContent='сохраняю…';try{try{await saveSnapshot('до смены настроек')}catch(e){}for(const[k,val]of Object.entries(v))if(String(VARS[k]??DEF[k])!==String(val))await setVar(k,val);await restartBot();$('setMsg').textContent='';info('Настройки сохранены','Бот перезапускается и подхватит новые значения примерно через минуту. Снимок прежних настроек и статистики лежит в «Статистика · архив».')}catch(e){$('setMsg').textContent='';info('Не удалось сохранить',esc(e.message))}};
$('setCancel').onclick=()=>{loadSettings();showView('dash')};
// keys
async function loadKeys(){$('ghToken').value=tok();$('ghState').textContent=tok()?'сохранён':'не задан';if(!tok())return;
 try{const j=await gh('/actions/variables?per_page=100');const v={};j.variables.forEach(x=>v[x.name]=x.value);
  $('k_pk_state').innerHTML=v.POLY_ADDRESS?`сохранён · адрес <b>${v.POLY_ADDRESS}</b> · ${v.POLY_KEY_SAVED||''}`:'не задан';$('k_funder').value=v.POLY_FUNDER||'';document.querySelectorAll('input[name=sig]').forEach(r=>r.checked=r.value===(v.POLY_SIGNATURE_TYPE||'0'));
  $('k_chat').value=v.TELEGRAM_CHAT_ID||'';$('k_tg_state').innerHTML=v.TG_SAVED?`сохранён · ${v.TG_SAVED}`:'не задан'}catch(e){$('keysMsg').textContent='GitHub: '+e.message}}
$('ghSave').onclick=()=>{localStorage.setItem('gh_token',$('ghToken').value.trim());loadKeys();info('Токен сохранён','Он хранится только в этом браузере. Теперь доступны настройки, ключи, питание и архив.')};$('ghForget').onclick=()=>{localStorage.removeItem('gh_token');$('ghToken').value='';loadKeys()};
async function setSecret(name,value){await window.sodium.ready;const pk=await gh('/actions/secrets/public-key');const bin=window.sodium.from_base64(pk.key,window.sodium.base64_variants.ORIGINAL);const enc=window.sodium.crypto_box_seal(window.sodium.from_string(value),bin);
 await gh('/actions/secrets/'+name,{method:'PUT',body:JSON.stringify({encrypted_value:window.sodium.to_base64(enc,window.sodium.base64_variants.ORIGINAL),key_id:pk.key_id})})}
$('keysSave').onclick=async()=>{if(!tok())return toast('Сначала сохрани токен GitHub');$('keysMsg').textContent='сохраняю…';const now=new Date().toLocaleString();
 try{const pk=$('k_pk').value.trim();if(pk){let addr;try{addr=new window.ethers.Wallet(pk).address}catch(e){throw new Error('приватный ключ не распознан')}await setSecret('POLY_PRIVATE_KEY',pk);await setVar('POLY_ADDRESS',addr);await setVar('POLY_KEY_SAVED',now);$('k_pk').value=''}
  await setVar('POLY_FUNDER',$('k_funder').value.trim());await setVar('POLY_SIGNATURE_TYPE',document.querySelector('input[name=sig]:checked').value);
  const tg=$('k_tg').value.trim();if(tg){await setSecret('TELEGRAM_BOT_TOKEN',tg);await setVar('TG_SAVED',now);$('k_tg').value=''}if($('k_chat').value.trim())await setSecret('TELEGRAM_CHAT_ID',$('k_chat').value.trim());
  $('keysMsg').textContent='';loadKeys();info('Ключи сохранены','Секреты ушли в GitHub в зашифрованном виде. Бот подхватит их при следующем запуске цикла (кнопка «Перезапустить» в «Питании»).')}catch(e){$('keysMsg').textContent='';info('Не удалось сохранить',esc(e.message))}};
// archive
const KEYS=S.map(x=>x.k);
async function ghPublic(path){const r=await fetch(GH+path,{headers:{'Accept':'application/vnd.github+json',...(tok()?{'Authorization':'Bearer '+tok()}:{})}});if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}
function snapshot(label){const d=last||{};const cfg={};KEYS.forEach(k=>cfg[k]=(VARS[k]??(d.config||{})[k]??DEF[k]));
 return {label,ts:new Date().toISOString(),mode:d.mode,settings:cfg,stats:{bankroll:d.bankroll,start_bankroll:d.start_bankroll,trades:d.trades,wins:d.wins,losses:d.losses,winrate:d.winrate,pf:d.pf,total_pnl:d.total_pnl,max_dd:d.max_dd,per_asset:d.per_asset,longs:d.longs,shorts:d.shorts},closed:d.closed||[]}}
async function saveSnapshot(label){if(!tok())throw new Error('нужен токен GitHub');if(!Object.keys(VARS).length){try{const j=await gh('/actions/variables?per_page=100');j.variables.forEach(v=>VARS[v.name]=v.value)}catch(e){}}
 const snap=snapshot(label);const name='archive/'+snap.ts.replace(/[:.]/g,'-').slice(0,19)+'.json';
 await gh('/contents/'+name,{method:'PUT',body:JSON.stringify({message:'archive: '+label,content:btoa(unescape(encodeURIComponent(JSON.stringify(snap,null,1))))})});return name}
async function loadArchive(){$('arcList').innerHTML='<tr><td colspan="9" class="empty">загрузка…</td></tr>';$('arcDetail').innerHTML='';
 let files=[];try{files=await ghPublic('/contents/archive')}catch(e){$('arcList').innerHTML='<tr><td colspan="9" class="empty">Архив пуст — сохрани первый снимок.</td></tr>';return}
 files=files.filter(f=>f.name.endsWith('.json')).sort((a,b)=>b.name.localeCompare(a.name));window.__arc={};
 const rows=await Promise.all(files.map(async f=>{try{const r=await fetch(f.download_url+'?t='+Date.now());const j=await r.json();window.__arc[f.name]=j;const st=j.stats||{};
  return `<tr><td>${j.ts.slice(0,16).replace('T',' ')}</td><td>${esc(j.label)}</td><td>${j.mode||''}</td><td>${st.trades??''}</td><td>${st.winrate==null?'—':(st.winrate*100).toFixed(0)+'%'}</td><td>${st.pf==null?'—':st.pf>=99?'∞':fmt(st.pf)}</td><td class="${cls(st.total_pnl)}">${st.total_pnl==null?'—':sgn(st.total_pnl)}</td><td>${fmt(st.bankroll)}</td><td><button onclick="arcShow('${f.name}')">открыть</button></td></tr>`}catch(e){return ''}}));
 $('arcList').innerHTML=rows.join('')||'<tr><td colspan="9" class="empty">Архив пуст.</td></tr>'}
async function arcShow(name){const j=window.__arc[name];if(!j)return;const cur={};KEYS.forEach(k=>cur[k]=VARS[k]??(last&&last.config||{})[k]??DEF[k]);const st=j.stats||{};
 const body=`<div class="sub" style="margin:0 0 10px">${j.ts.slice(0,16).replace('T',' ')} · ${j.mode||''} · сделок ${st.trades??'—'} · winrate ${st.winrate==null?'—':(st.winrate*100).toFixed(0)+'%'} · PF ${st.pf==null?'—':fmt(st.pf)} · P&amp;L ${st.total_pnl==null?'—':sgn(st.total_pnl)} $</div>
 <table><thead><tr><th>Параметр</th><th>В снимке</th><th>Сейчас</th></tr></thead><tbody>${KEYS.map(k=>`<tr><td>${k}</td><td>${esc(j.settings[k])}</td><td class="${String(j.settings[k])!==String(cur[k])?'warn':''}">${esc(cur[k])}</td></tr>`).join('')}</tbody></table>
 <div class="sub">По активам: ${Object.entries(st.per_asset||{}).map(([a,v])=>`${a} ${v.n} (${(v.wins/v.n*100).toFixed(0)}%)`).join(', ')||'—'}. Жёлтым — отличается от текущих.</div>`;
 const v=await modal(j.label,body,[{t:'Закрыть',v:0},{t:'Скачать Excel',v:2},{t:'Восстановить настройки',v:1,cls:'primary'}]);if(v===2)arcXlsx(name);if(v===1)arcRestore(name)}
async function arcRestore(name){if(!tok())return info('Нужен токен GitHub','Добавь его в разделе «Ключи».');const j=window.__arc[name];if(!await ask('Восстановить настройки?','Применить настройки из снимка «'+esc(j.label)+'» и перезапустить бота?','Восстановить'))return;
 try{for(const[k,v]of Object.entries(j.settings))await setVar(k,v);await restartBot();VARS={...VARS,...j.settings};info('Восстановлено','Настройки из снимка применены, бот перезапускается.')}catch(e){info('Ошибка',esc(e.message))}}
function arcXlsx(name){const j=window.__arc[name];const wb=window.XLSX.utils.book_new();
 window.XLSX.utils.book_append_sheet(wb,window.XLSX.utils.json_to_sheet((j.closed||[]).map(t=>({Открыта:t.opened,Актив:t.asset,Сторона:t.side,Вход:t.entry,Ставка:t.cost,Итог:t.won?'WIN':'LOSS',PnL:t.pnl,Рынок:t.question}))),'Сделки');
 window.XLSX.utils.book_append_sheet(wb,window.XLSX.utils.json_to_sheet([{...j.stats,per_asset:JSON.stringify(j.stats.per_asset||{})},...Object.entries(j.settings).map(([k,v])=>({Параметр:k,Значение:v}))]),'Итоги и настройки');
 window.XLSX.writeFile(wb,'polybot_'+j.ts.slice(0,16).replace(/[:T]/g,'-')+'.xlsx')}
$('arcSave').onclick=async()=>{const label=await askText('Сохранить снимок','Название, чтобы потом найти в списке:','снимок '+new Date().toLocaleString());if(label==null)return;$('arcMsg').textContent='сохраняю…';try{await saveSnapshot(label);$('arcMsg').textContent='';info('Снимок сохранён','Настройки, итоги и все сделки на этот момент — в списке.');loadArchive()}catch(e){$('arcMsg').textContent='';info('Ошибка',esc(e.message))}};
// power
const WF='/actions/workflows/polybot.yml';
async function runningIds(){const j=await gh('/actions/runs?per_page=10');return j.workflow_runs.filter(r=>r.path&&r.path.endsWith('polybot.yml')&&['in_progress','queued','waiting'].includes(r.status)).map(r=>r.id)}
async function cancelRuns(){for(const id of await runningIds())await gh('/actions/runs/'+id+'/cancel',{method:'POST'})}
async function dispatch(){await gh(WF+'/enable',{method:'PUT'});await gh(WF+'/dispatches',{method:'POST',body:JSON.stringify({ref:'main'})})}
async function restartBot(){await cancelRuns();await new Promise(r=>setTimeout(r,8000));await dispatch()}
async function loadPower(){if(!tok()){$('pwState').textContent='Нужен токен GitHub (раздел «Ключи»)';return}
 try{const wf=await gh(WF);const ids=await runningIds();$('pwState').innerHTML=`Расписание: <b>${wf.state==='active'?'включено':'выключено'}</b> · активных циклов: <b>${ids.length}</b> · режим: <b>${(last&&last.mode)||'—'}</b>`}catch(e){$('pwState').textContent='GitHub: '+e.message}}
async function power(fn,msg,done){if(!tok())return info('Нужен токен GitHub','Добавь его в разделе «Ключи».');$('pwMsg').textContent=msg+'…';try{await fn();$('pwMsg').textContent='';info('Готово',done);setTimeout(loadPower,3000)}catch(e){$('pwMsg').textContent='';info('Ошибка',esc(e.message))}}
$('pwStart').onclick=()=>power(dispatch,'запускаю','Расписание включено, цикл стартует. Через минуту в дашборде появится «работает».');
$('pwRestart').onclick=()=>power(restartBot,'перезапускаю','Текущий цикл остановлен, новый запущен. Состояние и сделки сохранены.');
$('pwStop').onclick=async()=>{if(!await ask('Остановить полностью?','Текущий цикл прервётся, расписание выключится. Открытые paper-позиции закроются при следующем запуске.','Остановить','danger'))return;power(async()=>{await cancelRuns();await gh(WF+'/disable',{method:'PUT'})},'останавливаю','Бот остановлен. Запустить снова — кнопка «Запустить».')};
$('pwReset').onclick=async()=>{if(!await ask('Обнулить статистику?','Сначала сохранится снимок в архив и скачается Excel со всеми сделками. Потом банкролл вернётся к стартовому, история сделок очистится, бот перезапустится.','Обнулить','danger'))return;power(async()=>{
 const get=async p=>{try{const j=await gh('/contents/'+p);return{sha:j.sha,text:decodeURIComponent(escape(atob(j.content.replace(/\n/g,''))))}}catch(e){return null}};
 try{await saveSnapshot('перед обнулением')}catch(e){}
 const st=await get('state.json'),tr=await get('trades.csv');const d=last||{};
 const wb=window.XLSX.utils.book_new();const closed=(st&&JSON.parse(st.text).closed)||d.closed||[];
 window.XLSX.utils.book_append_sheet(wb,window.XLSX.utils.json_to_sheet(closed.map(t=>({Открыта:t.opened,Актив:t.asset,Сторона:t.side,Вход:t.entry,Ставка:t.cost,Акций:t.shares,Уверенность:t.conf,Движение:t.move,Окно_закрыто:t.end,Итог:t.won?'WIN':'LOSS',PnL:t.pnl,Рынок:t.question}))),'Сделки');
 window.XLSX.utils.book_append_sheet(wb,window.XLSX.utils.json_to_sheet([{Дата:new Date().toISOString(),Банкролл:d.bankroll,Старт:d.start_bankroll,Сделок:d.trades,Побед:d.wins,Убытков:d.losses,Winrate:d.winrate,PF:d.pf,МаксПросадка:d.max_dd},...Object.entries(d.config||{}).map(([k,v])=>({Параметр:k,Значение:v}))]),'Итоги и настройки');
 if(tr)window.XLSX.utils.book_append_sheet(wb,window.XLSX.utils.aoa_to_sheet(tr.text.trim().split('\n').map(l=>l.split(','))),'trades.csv');
 window.XLSX.writeFile(wb,'polybot_'+new Date().toISOString().slice(0,16).replace(/[:T]/g,'-')+'.xlsx');
 const bank=+(VARS.BANKROLL||(d.config&&d.config.BANKROLL)||500);const fresh={bankroll:bank,positions:{},closed:[],day:new Date().toISOString().slice(0,10),day_pnl:0,consec_losses:0,cooldown_until:null,trade_times:[]};
 await cancelRuns();
 const put=async(p,content,sha)=>gh('/contents/'+p,{method:'PUT',body:JSON.stringify({message:'reset stats',content:btoa(unescape(encodeURIComponent(content))),...(sha?{sha}:{})})});
 await put('state.json',JSON.stringify(fresh,null,1),st&&st.sha);await put('trades.csv','',tr&&tr.sha);
 await new Promise(r=>setTimeout(r,5000));await dispatch()},'обнуляю','Статистика сброшена, Excel скачан, снимок в архиве. Бот стартует заново.')};
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
