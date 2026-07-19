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

## Проверки

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pyright
.venv/bin/coverage run -m pytest
.venv/bin/coverage report
docker build .
```

