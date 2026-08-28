# Резервная копия PolyBot перед финальной доработкой стратегии

**Сохранённая версия:** 1.4.3
**Дата backup:** 2026-08-28 (UTC)
**Git commit:** a4f426c975a405d4852005c90fbf200f60712226
**Git tag:** backup/pre-final-strategy-2026-08-28

## Что здесь лежит

Полный снимок рабочих исходников на момент backup:

| Файл | Назначение |
|---|---|
| bot.py | торговый цикл, стратегия, риск-гейты, исполнение |
| dashboard.py | веб-панель (HTML+JS внутри) и локальный сервер |
| gist_sync.py | публикация состояния в Gist для живой панели |
| report.py | сборка docs/index.html |
| daily_report.py | ежедневные отчёты reports/*.json / *.xlsx |
| backtest/collect.py | сбор истории окон с Polymarket |
| backtest/simulate.py | симулятор + перебор конфигураций |
| backtest/template.html | страница тестировщика |
| workflows/polybot.yml | запуск бота в GitHub Actions |
| workflows/backtest.yml | запуск бэктеста |
| .env.example, requirements.txt, README.md, DEPLOY.md, VERSION | конфиг и документация |

Секретов здесь нет: только имена переменных и плейсхолдеры.
Реальные значения лежат в GitHub Secrets/Variables и в них не копировались.

## Как восстановить

**Вариант 1 — через git tag (полное состояние репозитория):**

```
git checkout backup/pre-final-strategy-2026-08-28
# посмотреть; чтобы вернуть рабочую ветку целиком:
git checkout main
git reset --hard backup/pre-final-strategy-2026-08-28
git push --force
```

**Вариант 2 — из этой папки (точечно, без перезаписи истории):**

```
cp archive/pre-final-strategy/bot.py .
cp archive/pre-final-strategy/dashboard.py .
cp archive/pre-final-strategy/backtest/*.py backtest/
cp archive/pre-final-strategy/backtest/template.html backtest/
cp archive/pre-final-strategy/workflows/*.yml .github/workflows/
git commit -am "restore pre-final-strategy"
```

После восстановления перезапустить бота: панель → Питание → Перезапустить.

## Важно

Папка archive/ изолирована: она не импортируется ботом, не участвует
в workflow и не влияет на runtime. Проверка — в отчёте к этой доработке.
Состояние торговли (state.json, trades.csv, missed.csv, reports/) сюда
НЕ копировалось намеренно: это живые данные, они продолжают накапливаться
в рабочей версии.
