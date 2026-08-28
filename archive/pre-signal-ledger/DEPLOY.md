# PolyBot — установка на сервер (24/7)

Бот и панель работают на сервере, ты открываешь панель с телефона или компа.
Всё можно сделать с iPhone: SSH-клиент — **Termius** (бесплатно в App Store).

## 1. Сервер

Hetzner (€4/мес) или DigitalOcean ($4–6/мес): Ubuntu 24.04, самый дешёвый тариф.
При создании выбери «SSH key» или запиши пароль root. Тебе нужен IP сервера.

Бесплатные тарифы (Render, Railway, Fly) для бота не подходят — они засыпают.
Oracle Free Tier работает, но регистрация капризная.

## 2. Загрузить файлы

Вариант А — через GitHub: залей папку `polybot` в **приватный** репозиторий
(без `.env`!), затем на сервере:

```
apt update && apt install -y python3-pip git
git clone https://github.com/ТВОЙ_ЛОГИН/polybot.git
cd polybot
```

Вариант Б — Termius умеет загружать файлы через SFTP (вкладка SFTP → перетащить папку).

## 3. Установить и настроить

```
cd polybot
pip3 install -r requirements.txt --break-system-packages
cp .env.example .env
nano .env
```

В `.env` заполни:

```
MODE=paper              ← сначала paper! live — только после недели теста
DASH_PASSWORD=придумай_длинный_пароль
```

Ctrl+O, Enter, Ctrl+X — сохранить и выйти.

## 4. Открыть порт

```
ufw allow 22
ufw allow 8080
ufw --force enable
```

## 5. Автозапуск (переживает перезагрузку сервера)

```
cat > /etc/systemd/system/polybot.service <<EOF
[Unit]
Description=PolyBot dashboard + bot
After=network.target

[Service]
WorkingDirectory=/root/polybot
Environment=DASH_AUTOSTART=1
ExecStart=/usr/bin/python3 /root/polybot/dashboard.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now polybot
systemctl status polybot
```

Должно быть `active (running)`.

## 6. Открыть панель

В Safari: `http://IP_СЕРВЕРА:8080` → логин любой, пароль из `.env`.
Поделиться → «На экран Домой» — будет иконка как у приложения.
С компа тот же адрес.

## 7. Управление

- Кнопки «Запустить / Остановить» в панели управляют ботом.
- Логи: `journalctl -u polybot -f` или блок «Лог» в панели.
- После правки `.env`: `systemctl restart polybot`.

## 8. Переход на live — только после paper-теста

Порядок:
1. Минимум неделя в paper, Profit factor в панели ≥ 1.3.
2. Отдельный кошелёк, на нём $50–100 USDC на Polygon. Не основной.
3. На Polymarket сделай одну ручную сделку с этого кошелька (включает allowance).
4. В `.env`: `MODE=live`, `POLY_PRIVATE_KEY=...`, `BANKROLL=50`, `MAX_POSITIONS=3`.
5. `systemctl restart polybot`, в панели пилюля станет красной `live`.

Файл `.env` с ключом никуда не выкладывай, в GitHub не коммить.

## Безопасность

Панель открыта в интернет по паролю без шифрования (HTTP). Ключ она не показывает,
но пароль делай длинным. Если хочешь закрыть панель от всех, кроме себя —
поставь Tailscale на сервер и телефон, а `ufw allow 8080` замени на
`ufw allow in on tailscale0 to any port 8080`.
