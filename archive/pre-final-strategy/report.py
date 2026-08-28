#!/usr/bin/env python3
"""Собирает docs/index.html. Если задан GIST_RAW_URL — страница будет живой (тянет данные из Gist),
иначе — статичный снимок из state.json."""
import json, os
import dashboard

gist = os.getenv("GIST_RAW_URL", "").strip()
if gist:
    inject = ("<script>window.__GIST__=" + json.dumps(gist) + ";window.__REPO__=" + json.dumps(os.getenv("GITHUB_REPOSITORY", ""))
              + ";window.__VER__=" + json.dumps(dashboard.VERSION) + ";</script>\n<script>")
else:
    data = dashboard.snapshot(); data["log"] = data["log"][-40:]
    inject = ("<script>window.__DATA__=" + json.dumps(data, default=str, ensure_ascii=False)
              + ";window.__VER__=" + json.dumps(dashboard.VERSION) + ";</script>\n<script>")
html = dashboard.HTML.replace("<script>", inject, 1)
os.makedirs("docs", exist_ok=True)
open("docs/index.html", "w", encoding="utf-8").write(html)
print("docs/index.html:", "live via gist" if gist else "static snapshot")
