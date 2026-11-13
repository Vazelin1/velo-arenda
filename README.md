# Аренда электровелосипедов — Telegram mini app

Бот: @rent_volt_voyaj_bot · адрес: https://rent.volt-voyaj.ru
Работает на том же сервере, что и выкуп (`~/velo`), полностью отдельно: своя папка `~/rent`, своя база `~/rent/data`.
Общий только Caddy в `~/rent/proxy`, он раздаёт оба адреса.

## Файлы
- `server.py` — сервер и вся логика (периоды, неустойка, отсрочки, оплаты, QR Точки, отчёты, копии).
- `static/index.html` — интерфейс.
- `proxy/` — общий Caddy для выкупа и аренды.

## Первый запуск (один раз)
```
cd ~ && git clone https://github.com/Vazelin1/velo-arenda rent
cd ~/rent && cp .env.example .env && nano .env        # токен бота, ваш Telegram ID
docker network create web
cd ~/velo && git pull && docker compose up -d --build --remove-orphans   # выкуп без своего Caddy
cd ~/rent/proxy && cp .env.example .env && docker compose up -d           # общий Caddy
cd ~/rent && docker compose up -d --build                                  # аренда
```

## Обновление
GitHub → Add file → Upload files → Commit. На сервере:
```
cd ~/rent && git pull && docker compose up -d --build
```

## Копии
Каждую ночь в 03:00 — `~/rent/data/backups`. Скачать или загрузить файл — Ещё → Копии и перенос.

## Оплата по QR (Точка)
Ещё → Настройки → «Оплата по QR»: ID торговой точки, ID счёта, JWT-токен из интернет-банка.
Оплаты сверяются каждые 2–3 минуты; адрес вебхука показан там же.
