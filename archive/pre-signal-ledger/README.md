# Backup PolyBot перед внедрением Signal Ledger

**Сохранённая версия:** 1.5.0 (LOCKED BASELINE стратегии)
**Дата:** 2026-08-28 (UTC)
**Commit:** 0921ab3a20f88d28ffc3fdfeabf3d91446c5f7a1
**Tag:** backup/pre-signal-ledger-2026-08-28

Предыдущий backup НЕ удалён: `backup/pre-final-strategy-2026-08-28` (v1.4.3).
Доступны обе точки восстановления.

## Что сохранено
Полный снимок исходников v1.5.0: bot.py, dashboard.py, gist_sync.py, report.py,
daily_report.py, backtest/*, workflows/*, конфиг и документация.
Секретов нет — только имена переменных и плейсхолдеры.

## Восстановление

Через tag:
```
git reset --hard backup/pre-signal-ledger-2026-08-28
git push --force
```

Покопийно:
```
cp archive/pre-signal-ledger/bot.py .
cp archive/pre-signal-ledger/dashboard.py .
cp archive/pre-signal-ledger/backtest/*.py backtest/
cp archive/pre-signal-ledger/workflows/*.yml .github/workflows/
```

## Baseline стратегии на момент backup
WINDOWS=5,15 · MIN_ELAPSED=0.75 · MIN_ENTRY=0.50 · MAX_ENTRY=0.62 · TIER_ENTRY=0.55
MIN_MOVE=0.0010 · MIN_MOVE_HIGH=0.0012 · MIN_CONF=0.70 · KELLY_FRAC=0.10
MAX_STAKE=0.05 · MAX_EXPOSURE=0.15 · MAX_POSITIONS=3 · MAX_PER_WINDOW=1 · MAX_SAME_DIR=2
DAILY_LOSS_LIMIT=0.30 · CONSEC_LOSS_LIMIT=5 · COOLDOWN_MIN=30 · USE_BOOK=1 · MAX_SLIP=0.01

Эти параметры зафиксированы как неизменяемый экспериментальный baseline.
