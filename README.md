# SeaWork ingestion

Первый модуль SeaWork: получение, сохранение raw payload, офлайн-нормализация,
quality gate, классификация, детерминированное обогащение и отчёт покрытия.

## Запуск

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
docker compose up -d postgres
.venv/bin/seawork db upgrade
.venv/bin/seawork ingest run --source pinpoint:hollandamericagroup
.venv/bin/seawork report coverage --source pinpoint:hollandamericagroup
```

Повторная обработка сохранённых payload не использует сеть:

```bash
.venv/bin/seawork ingest reprocess --source pinpoint:hollandamericagroup
```

Настройки задаются переменными с префиксом `SEAWORK_`, например
`SEAWORK_DATABASE_URL`. Адаптер отправляет честный User-Agent и проверяет
`robots.txt`. Для рабочего окружения замените контакт в `SEAWORK_USER_AGENT`.

## Развёртывание (VPS, docker compose)

```bash
cp .env.example .env   # задать SEAWORK_TELEGRAM_BOT_TOKEN, SEAWORK_POSTGRES_PASSWORD, при необходимости LLM
docker compose up -d --build
docker compose logs -f bot scheduler
```

Поднимаются четыре сервиса: `postgres`, одноразовый `migrate` (`seawork db upgrade`),
`bot` (`seawork bot run`, long polling) и `scheduler` (`seawork schedule`: раз в сутки
в `SEAWORK_DIGEST_HOUR_UTC` ингест всех источников из реестра, затем рассылка).
`bot` и `scheduler` стартуют только после успешного завершения `migrate` и перезапускаются
автоматически (`restart: unless-stopped`). Настройки берутся из `.env`; адрес базы
для сервисов compose подставляет сам (хост `postgres` внутри сети).

Пароль базы задаётся `SEAWORK_POSTGRES_PASSWORD` (по умолчанию `seawork`, только для
разработки). Он попадает в URL, поэтому без спецсимволов (лучше `openssl rand -hex 16`);
менять пароль после первого старта нужно и в самой базе, том его не перечитывает.
Порт базы публикуется только на `127.0.0.1` (`SEAWORK_POSTGRES_PORT`, по умолчанию 5432).

Обновление: `git pull && docker compose up -d --build`. Миграции применятся сами.
Без токена `bot` выходит с ошибкой и перезапускается по кругу, это ожидаемо до заполнения `.env`.
Если перезапуск контейнера попал на время рассылки, рассылка этого дня пропускается.

## Проверки

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pyright
.venv/bin/coverage run -m pytest
.venv/bin/coverage report
docker build .
```

