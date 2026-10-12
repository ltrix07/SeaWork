# Запуск бота: от Match Score до первой подборки в Telegram
Цель: человек открывает бота, отвечает на один вопрос и получает подборку вакансий, которая не повторяется, не содержит снятых вакансий и обновляется сама.
Карта: ./map.md
Спеки: docs/modules/recommendation-engine.md; docs/modules/telegram-bot.md (пишется на шаге 5)
Вне рамок: тарифы и оплата (E4, все на FREE), калибровка весов (E2.1), E2.2, статьи и не-вакансии, Mini App, вывод интересов из поведения.

## Шаги
- [x] 1. Общая pg-фикстура в conftest [mech]
  Файлы: tests/conftest.py, tests/test_storage.py, tests/test_profile.py
  Сделать: вынести дублированную фикстуру `session` в conftest, параметризовав схему; поведение тестов не меняется.
  Готово когда: pytest зелёный, число тестов то же, фикстура объявлена в одном месте.

- [ ] 2. Чтение кандидатов из базы [logic]
  Файлы: src/seawork/domain/recommendation.py (только `Candidate`), src/seawork/storage/repository.py, tests/test_storage.py
  Сделать: конвертер `OpportunityRecord → EnrichedOpportunity`, `Repository.list_feed_candidates()` по §3.2 и §4.1 спеки движка (пока без условия «снята» — его нет). Решить по `raw_items.payload`, восстанавливается ли `recruitment_office_raw`.
  Готово когда: тест round-trip запись → кандидат → `compute_match_score` даёт тот же результат, что на исходном `EnrichedOpportunity`; `source_trashed` исключён.

- [ ] 3. Снятые вакансии [logic]
  Файлы: определит разведка (pipeline.py, repository.py, enums.py, models.py, миграция 0005, тесты ingestion)
  Сделать: сначала scout — какие источники отдают полную выдачу за прогон, а какие частичную. Затем: вакансия, отсутствующая в полной выдаче источника, получает статус снятой (с датой); вернувшаяся — оживает. Частичные прогоны ничего не снимают. `list_feed_candidates` исключает снятые.
  Готово когда: тест «прогон без вакансии X снимает X, прогон с X возвращает»; прогон с ошибкой источника не снимает ничего.

- [ ] 4. Recommendation Engine и `deliveries` [logic]
  Файлы: src/seawork/domain/recommendation.py, src/seawork/recommendation/__init__.py, src/seawork/recommendation/engine.py, src/seawork/storage/models.py, src/seawork/storage/profile_repository.py (или feed_repository.py), alembic/versions/0006_deliveries.py, tests/test_recommendation.py
  Сделать: по docs/modules/recommendation-engine.md целиком.
  Готово когда: DoD спеки движка выполнен, включая замер на живом корпусе в журнале.

- [ ] 5. Спека бота и доставки [docs]
  Файлы: docs/modules/telegram-bot.md
  Сделать: архитектор пишет спеку: сервисный слой (use cases) между ботом и доменом, привязка аккаунта канала к `user_id`, онбординг, карточка, кнопки, команды, ежедневная подборка, удаление и выгрузка, тексты. Нужны ответы владельца (см. журнал).
  Готово когда: владелец согласовал спеку.

- [ ] 6. Аккаунты каналов и сервисный слой [logic]
  Файлы: по спеке бота (src/seawork/app/…, storage, миграция 0007, тесты)
  Сделать: таблица `channel_accounts`, use cases без Telegram: старт, ответ онбординга, подборка, реакция, пауза, удаление, выгрузка.
  Готово когда: use cases покрыты тестами на базе без единого импорта бот-фреймворка.

- [ ] 7. Telegram-бот [logic]
  Файлы: pyproject.toml, src/seawork/config.py, src/seawork/bot/…, tests/test_bot_*.py
  Сделать: тонкие хендлеры поверх сервисного слоя, клавиатуры, тексты, токен в Settings, команда `seawork bot run`.
  Готово когда: хендлеры не содержат решений (только вызов use case и рендер); ручной прогон в тестовом боте: /start → вопрос → подборка → «Не то» → подборка без неё.

- [ ] 8. Ежедневная рассылка [logic]
  Файлы: src/seawork/cli.py, src/seawork/bot/delivery.py (по спеке), тесты
  Сделать: команда `seawork deliver digest`: для каждого активного аккаунта — подборка и отправка; заблокировавший бота аккаунт помечается неактивным; повторный запуск в тот же день не шлёт второй раз.
  Готово когда: тесты на идемпотентность и на блокировку; ручной прогон на тестовом боте.

- [ ] 9. Развёртывание [mech]
  Файлы: docker-compose.yml, Dockerfile, .env.example, README.md
  Сделать: под VPS — сервисы `bot` (long polling) и `scheduler` (ingest всех источников + `deliver digest` по расписанию), миграции при старте.
  Готово когда: `docker compose up` на чистой машине поднимает базу, бота и расписание.

- [ ] 10. Документы [mech]
  Файлы: docs/roadmap.md, docs/implementation-status.md
  Сделать: отметить E3, частично E4; обновить устаревший implementation-status (605 → 887, Match Score реализован).
  Готово когда: документы совпадают с кодом.

## Журнал
- 12.10.2026: план и спека движка написаны.
- 12.10.2026, решения владельца: привязка аккаунта — таблица `channel_accounts` (channel, external_id) → user_id, не поле профиля; бот на русском, все тексты в одном модуле; хостинг — VPS + docker compose (Postgres, бот на long polling, планировщик). Цепочка PR #17–#20 влита в main через #19, ветка `rec/feed` от eb9b099.
- 12.10.2026: план утверждён; `source_trashed` вне выдачи — подтверждено. Открыто: время ежедневной подборки (шаг 5).
- 1: `session` в conftest, схему задаёт фикстура `test_schema`, которую модуль переопределяет (атрибут модуля не проходит pyright strict). 171 passed на живой БД. Ловушка окружения: порт 5432 на машине разработчика занят чужим PostgreSQL, и тесты с БД молча пропускаются; локально — `SEAWORK_POSTGRES_PORT=5452 docker compose up -d postgres` и `SEAWORK_TEST_DATABASE_URL=postgresql+psycopg://seawork:seawork@localhost:5452/seawork`.
