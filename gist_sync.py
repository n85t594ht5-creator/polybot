#!/usr/bin/env python3
"""Каждые 30 сек выкладывает снимок состояния бота в GitHub Gist — для живого дашборда на Pages."""
import json, os, sys, time, requests
import dashboard

GIST_ID, TOKEN = os.getenv("GIST_ID", ""), os.getenv("GIST_TOKEN", "")
if not (GIST_ID and TOKEN):
    print("GIST_ID / GIST_TOKEN не заданы — синхронизация выключена"); sys.exit(0)
INTERVAL = int(os.getenv("GIST_INTERVAL", "30"))
last = None
while True:
    try:
        d = dashboard.snapshot(); d["log"] = d["log"][-40:]; d["bot"]["running"] = True
        body = json.dumps(d, default=str, ensure_ascii=False)
        if body != last:
            r = requests.patch(f"https://api.github.com/gists/{GIST_ID}",
                               headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
                               json={"files": {"state.json": {"content": body}}}, timeout=15)
            r.raise_for_status(); last = body
    except Exception as e:
        print("gist sync:", e)
    time.sleep(INTERVAL)
