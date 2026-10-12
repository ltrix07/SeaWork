# Карта: Recommendation Engine + тонкий Telegram-бот (опорный код)
Снято с ветки geo/normalise-hiring-office (HEAD 1e33aa2). Только факты; код не менялся.

## Главное расхождение с постановкой
- **БД-слой синхронный, не async**: `sqlalchemy.orm.Session`, драйвер `psycopg` (config.py:9, repository.py:15, profile_repository.py:183). Async есть только в ingestion-источниках (httpx) и `asyncio.run` в cli.py:177. Бот на async-фреймворке будет вызывать sync-репозитории (блокирующе или через thread).
- **Нет кода, превращающего `OpportunityRecord` в `EnrichedOpportunity`** (grep по src: `EnrichedOpportunity(` строится только в enrich/rules.py:210). Это первый нужный кусок.

## Точки входа
- cli.py:app (typer): `ingest run|refetch|reprocess --source`, `report coverage --source`, `eval enrichment`, `db upgrade` (cli.py:197-310). Entry point `seawork = seawork.cli:app` (pyproject.toml:416).
- Профиль и Match Score из CLI/кода продукта НЕ вызываются нигде: только из тестов.
- Бота, планировщика, HTTP-слоя нет.

## 1. Профиль (domain/profile.py, storage/profile_repository.py)
Идентификатор: `user_id: UUID` (profile.py:83; PK `user_profiles.user_id`, UUID(as_uuid)). Генерится `uuid.uuid4()` в `get_or_create_profile` (profile_repository.py:189-191), либо передаётся вызывающим. **Внешнего id (telegram id) нет, колонки под него нет** (user_profiles: user_id, facts, created_at, updated_at; 0004:17-27).
`ProfileRepository(session: Session, vocabulary: ProfileVocabulary)` (:183). Словарь: `ProfileVocabulary.from_directory(Path("data/reference"))` (profile_vocabulary.py:110), читать один раз.
- `get_or_create_profile(user_id: UUID | None = None) -> UserProfile` :189 (INSERT ON CONFLICT DO NOTHING + commit; создание пустого профиля = «регистрация»)
- `get_profile(user_id) -> UserProfile | None` :202
- `set_fact(user_id, field: str, fact: Fact[Any]) -> UserProfile` :206; raises `StatedFactProtectedError`, `UnknownReferenceKeyError`, `ValueError` (неизвестное поле), `LookupError` (нет профиля)
- `clear_fact(user_id, field, source: ProfileFactSource) -> UserProfile` :234
- `record_event(event: BehaviourEvent) -> None` :258 (append, commit; FK на профиль -> IntegrityError без профиля)
- `list_events(user_id) -> list[BehaviourEvent]` :271 (order occurred_at, id)
- `interest_counts(user_id) -> InterestCounts` :288 (LEFT JOIN opportunities по `enriched->direction->value` и `->profession->value`; сырые счётчики, не скор)
- `delete_user(user_id) -> bool` :322 (явно удаляет события + профиль; плюс ON DELETE CASCADE)
- `export_user(user_id) -> UserDataExport | None` :337
Модели: `UserProfile` (profile.py:80; поля-факты `Fact[...] | None`: career_stage, career_goal, professions, directions, certificates, languages, residence_country, citizenship, work_authorization, preferred_countries, hiring_country), `Fact[T]{value, source: STATED|OBSERVED|IMPORTED, confidence 0..1, stated_at}` (:61; STATED => confidence==1.0), `CareerStage{undecided,entry,working,experienced}`, `CareerGoal{profession,direction,country}` (все опц., минимум одно), `FACT_FIELDS` (:101).
Журнал: `BehaviourAction = viewed|saved|applied|dismissed|reopened` (:29). **`shown` нет.** `dismissed` есть. `dwell_seconds: int | None, ge=0` есть и хранится (models.py:388), но нигде не читается. `opportunity_id: int` без FK на opportunities (models.py:386, намеренно). Дублирующиеся события разрешены (нет уникальности). Отмены dismissed нет (журнал не редактируется; `reopened` — отдельное действие без заданной семантики).
Хранение: все факты одним JSON-документом `facts` (models.py:363), ключ отсутствует = «неизвестно», `[]` = «известно, пусто».

## 2. Match Score
**`compute_match_score(profile: UserProfile, opportunity: EnrichedOpportunity, *, factors: Sequence[Factor] = FACTORS, now: datetime | None = None) -> MatchScore`** (scoring/engine.py:100; экспорт scoring/__init__.py).
- Чистая функция: без БД, без сети, без I/O. Стоимость: 8 факторов (factors.py:374) + 2 `model_dump_json`+sha256 (engine.py:75-82) на пару. Для 887 вакансий — дёшево, но sha256 считается на каждый вызов (отпечаток профиля пересчитывается 887 раз).
- Вход: `UserProfile` (domain/profile.py) и `EnrichedOpportunity` (domain/models.py:56: `normalized: NormalizedOpportunity`, `type: OpportunityType`, `type_confidence`, `country/city/embarkation_port/operating_region`, `hiring_scope: Inferred[HiringScope]`, `profession`, `direction`, `required_certificates`, `required_languages`, `preferred_languages`, `experience_level`, `salary`, `workplace_type_hint` (все `Inferred[...] | None`), `quality_score`, `quality_flags`, `quality_notes`).
- Выход `MatchScore` (domain/match.py:41, frozen): `value: float | None` (None = нечего сравнивать, НЕ 0), `comparable_factors: int`, `verdicts: list[FactorVerdict]`, `computed_at`, `profile_fingerprint`, `opportunity_fingerprint`. Поля **explanation нет**; объяснение = `verdicts` (FactorVerdict :22: factor, outcome MATCH|MISMATCH|NOT_COMPARABLE, weight, opportunity_evidence, profile_evidence, *_confidence, `caveats: list[str]`).
- Caveats-ключи: `training_offer_signal_unavailable`, `hiring_office_not_normalised` (factors.py:36-37).
- `value = sum(weight MATCH)/sum(weight comparable)`; weight = 1.0 * conf_opp * conf_profile (weights.py:134-154, ВСЁ placeholder до E2.1). Порог 85% не реализован.
- Интересы в скоре не участвуют (factors.py:30, сознательно). dismissed/история скору не видны.
- Факторы: profession, direction, country (vs preferred_countries), hiring_country, languages (только required), certificates (internship -> NOT_COMPARABLE), experience, goal.

## 3. Вакансии в хранилище
`OpportunityRecord` (models.py:62, таблица `opportunities`): id (int PK), raw_item_id FK, source_id, source_trust, external_id, url, title, description, employer, posted_at (nullable), type, type_confidence, `enriched` JSON, quality_score, quality_flags, quality_notes, `status` ('active'|'rejected', enums.py:43), first_seen_at, created_at, updated_at. Unique (source_id, external_id).
- `enriched` = `EnrichedOpportunity.model_dump(mode="json")` БЕЗ normalized/type/type_confidence/quality_* (repository.py:443-451). Восстановить: `EnrichedOpportunity(normalized=..., type=OpportunityType(r.type), type_confidence=..., quality_*=..., **r.enriched)`.
- **Потеря при реконструкции**: в записи нет `location_raw`, `recruitment_office_raw`, `salary_raw`, `declared_type`, `language`, `source_fields` (поля `normalized`, не хранятся). `normalized.recruitment_office_raw` читает hiring_country_factor только как текст-evidence (factors.py:161,176-190); числа не меняет. Полный `normalized` — только повторной нормализацией raw_items.payload (`pipeline.raw_from_record`, pipeline.py:94).
- Один запрос «все активные»: готового метода нет в `Repository`; нужно `select(OpportunityRecord).where(status == 'active')`. Единственный такой фильтр в коде — reporting/coverage.py:53,62 (в Python). Метода чтения вакансий в `Repository` вообще нет (только save_raw, upsert_opportunity, iter_latest_raw, has_duplicate_content).
- «Закрыта/устарела»: **понятия нет**. status только active/rejected (по quality flags, pipeline.py:90). Вакансия, исчезнувшая из источника, остаётся active навсегда (reprocess/ingest ничего не снимает). Дата публикации `posted_at` есть у ~26% (Pinpoint не отдаёт); замена — `first_seen_at` (наш факт). Срока жизни/TTL нет.
- Дедупликация: между постингами только флаг `duplicate_content` (quality.py:50, по content_hash у другого external_id) => такие REJECTED. Схожие вакансии не группируются (SPEC 9.11 не реализован).
- quality_flags (причины отказа): empty_title, invalid_url, description_too_short, duplicate_content => status=rejected, исключены уже статусом. quality_notes (сохраняются, не отклоняют): сейчас только `source_trashed` у PADI (padi.py:130-138; 17 шт.) — **исключать ли такие из выдачи, не решено**, код не фильтрует.
- Объём: 887 по docs/roadmap.md (605 Pinpoint+Crewplanet + 282 PADI). Живую БД не опрашивал; число из доков, implementation-status.md ещё говорит 605. Ограничения на тип: в корпусе job, internship (и др. OpportunityType); Match Score не фильтрует по type.
- Заполняемость: country/city 0% для судовых; у PADI country ~97.5%; profession 98.8%; certificates ~33%; experience ~39% (docs/implementation-status.md:47-57, устарело).

## 4. Инфраструктура
- config.py:6 `Settings(BaseSettings)`, env_prefix `SEAWORK_`, `.env`: database_url (default `postgresql+psycopg://seawork:seawork@localhost:5432/seawork`), user_agent, request_*, reference_dir (`data/reference`), source_registry, llm_* (провайдер выключен пока заданы не все 4 поля). **Полей под бот-токен нет.**
- Зависимости (pyproject.toml:11-24): alembic, bs4, httpx, langdetect, psycopg[binary], pydantic, pydantic-settings, pyyaml, sqlalchemy, tenacity, typer. Нет aiogram/PTB, нет asyncpg. dev: pytest, pytest-asyncio (asyncio_mode=auto), respx, ruff, pyright strict, coverage.
- docker-compose.yml: только `postgres` (pgvector/pgvector:pg16, порт `${SEAWORK_POSTGRES_PORT:-5432}`), volume seawork-postgres. Сервиса приложения нет. Dockerfile: python:3.12-slim, `pip install .`, ENTRYPOINT `seawork`; копирует src, data, sources, alembic.
- CI (.github/workflows/ci.yml): ruff check/format, pyright, coverage+pytest (PG service), audit-скрипты, `seawork db upgrade`, `docker build`.
- Миграции: 0001_ingestion, 0002_llm_cache, 0003_quality_notes, 0004_user_profile (head). `seawork db upgrade`. Новые таблицы нужны как 0005+ И в `storage/models.py` (тесты используют `Base.metadata.create_all`, не alembic).
- Тесты с БД: tests/test_storage.py:40-69 и tests/test_profile.py:50-72 — **фикстура `session` дублируется в каждом файле, в conftest.py её нет** (conftest: только reference_dir, pinpoint_posting). Схема `seawork_test` (storage) и `seawork_profile_test` (profile), `-csearch_path`, drop_all/create_all на каждый тест, DROP SCHEMA в конце; skip, если PG недоступен. URL: `SEAWORK_TEST_DATABASE_URL` -> `SEAWORK_DATABASE_URL` -> localhost default. Хелпер `store_opportunity` (test_profile.py:~75) вставляет вакансию через реальный Repository.
- Ingestion: только вручную `seawork ingest run --source <id>` (источники в sources/registry.yaml: pinpoint:hollandamericagroup, pinpoint:princesscruises, crewplanet, padi). **Планировщика нет** (cron/apscheduler не найдены). Ингест синхронный в процессе CLI, сессия на запуск.

## 5. Документы: блок E
- roadmap E1 [~] профиль реализован (59 тестов). Остаток «требует владельца»: **сопоставление telegram id с user_id (нужно к E3)**; конверсия наблюдений в OBSERVED и роль dwell_seconds (после запуска); противоречие сказанного/замеченного; отменяемость dismissed.
- E2 [~] Match Score реализован; остаток: веса и порог 85% (нужен E2.1: 30-50 размеченных пар, «нужен человек»). E2.2 (признак «обучение предоставляется» из текста) открыт; E2.3 цитаты сделано.
- E3 Telegram-бот [ ] «тонкий клиент, после E2, писать последним»; SPEC 1.5: продукт не зависит от Telegram.
- E4 Тарифы и лимиты [ ] = этапы 7-10 SPEC 9.9-9.12: лимиты FREE/VIP (подписка не влияет на скор), история (повтор только если открыта и score > ~85%), группировка похожих (одна карточка «ещё 18 похожих»), формирование рекомендации. Ничего не начато.
- implementation-status.md устарел (22.09: 605 вакансий, «Match Score не начат», «tests/test_storage.py отсутствует» при том что он есть). Верить roadmap.md и коду.
- Open questions владельцу: порог/веса; кого считать «открытой» вакансией; что делать с source_trashed; как показывать новичка с value=None (match-score.md:46 — решение за Recommendation Engine); модель тарифов; граница бесплатного AI (docs/ai-cost-boundary.md).

## 6. Чего нет (понадобится Recommendation Engine / боту)
- Связь telegram id <-> user_id (таблица/колонка, уникальный индекс), поиск пользователя по внешнему id.
- История выдачи: действия `shown`/`delivered`, таблица показанного (user_id, opportunity_id, shown_at, score-снимок), метод «что уже показывали». `BehaviourAction` без `shown`; журнал append-only и не годится для запроса «последний статус» без агрегации (нужен свой запрос по max(occurred_at)).
- Чтение вакансий: `Repository.list_active()` и конвертер `OpportunityRecord -> EnrichedOpportunity`.
- Понятие актуальности вакансии (закрыта/снята/expired), возраст, пересчёт при исчезновении из источника.
- Группировка похожих вакансий; ранжирование/выбор top-N; обработка `value is None` для пустого профиля (fallback-политика).
- Исключение по dismissed (оценка не видит историю): Engine должен фильтровать сам.
- Тариф/подписка/лимиты (сущности и таблиц нет), пользовательский язык/локаль, бот-токен в Settings, зависимость на бот-фреймворк, сервис бота в compose, планировщик ингеста/рассылки.
- Кэш скоров (по отпечаткам, match-score.md 3.6) — не нужен для 887, но в доке допущен.
- Общая pg-фикстура в conftest.py.

## Инварианты
- Пусто vs неизвестно: `None` != `[]` (profile.py:3-6; factors.py:11-13). Не нормализовать `[]` в `None` и наоборот.
- NOT_COMPARABLE выпадает из расчёта, weight=0 по построению; жёстких фильтров нет, кроме явного dismissed (match-score 3.3).
- STATED нельзя перезаписать/снять не-STATED источником (profile_repository.py:224-229, 244-249).
- Ключи справочников профиля = ключи вакансий (professions/directions/certificates/languages.yaml); страны ISO alpha-2, upper-case, проверка по форме (profile_vocabulary.py:82,180).
- `set_fact` пишет под `SELECT ... FOR UPDATE`, откат при любой ошибке (:350-364); работает только на PostgreSQL (ON CONFLICT, JSONB-операторы в interest_counts).
- Журнал только append; `opportunity_id` без FK; удаление пользователя удаляет и журнал.
- `MatchScore` не хранить как истину; fingerprint = sha256 `model_dump_json` (зависит от порядка полей моделей).
- Ингест идемпотентен: upsert по (source_id, external_id), `first_seen_at` переживает reprocess, REJECTED может ожить.
- Домен frozen pydantic; pyright strict + ruff в CI.

## Тесты
- покрыто: профиль (tests/test_profile.py, ~59: факты, источники, журнал, счётчики, удаление, выгрузка, словарь, блокировки); Match Score (tests/test_match_score.py, ~39: все факторы, веса, fingerprints, internship, hiring office); хранилище (tests/test_storage.py: raw/opportunity upsert, first_seen_at, quality_notes); ingestion (test_snapshot/padi/crewplanet/pinpoint/stages/llm_*).
- не покрыто: round-trip `OpportunityRecord -> EnrichedOpportunity -> compute_match_score` (его не существует); `shown`; interest_counts на реальных данных в связке со скором; dwell_seconds; поведение на 887 реальных вакансиях (производительность/распределение value, доля None); конкурентные первые сообщения одного пользователя (есть только последовательная идемпотентность).

## Риски / нельзя трогать
- weights.py, `_STAGE_LEVELS`, `BASE_WEIGHT`: placeholder, не «подкручивать» под красивые примеры (weights.py:1-14). Порог 85% не вшивать (match-score 6.3).
- Не расширять `countries.yaml` до ISO и не использовать его для проверки профиля (roadmap E1).
- Не добавлять `BehaviourAction` без обновления `interest_counts`/тестов покрытия; не делать журнал редактируемым; не заводить default-значения у `ProfileRepository.vocabulary`.
- Не класть telegram id в `facts` JSON: это идентификатор, а не факт профиля; нужна отдельная колонка/таблица (решение владельца E1).
- Менять `EnrichedOpportunity`/`Inferred` = менять fingerprints и JSON в `opportunities.enriched`; `enriched` читается через `->>` в interest_counts (ключи `direction`, `profession`, `["value"]`).
- Бизнес-логику не в бот (SPEC 1.5): бот вызывает Engine.

## Защитный шум (не чистить попутно)
- profile_repository.py:362 `except Exception: rollback; raise` — это не шум (освобождает FOR UPDATE), оставить.
- profile_vocabulary.py:114 `load_yaml(...).get(section, [])` — маскирует отсутствие секции в справочнике пустым набором, после чего любой ключ «неизвестен».
- profile_vocabulary.py:164-171 `isinstance`-ветки в `canonicalize`/`_check_keys`:176-183 при типизированном входе `Fact[Any]`.
- scoring/weights.py:152-153 None -> 1.0 (осознанный, документированный).
- cli.py:245-258 `isinstance` на данные yaml в eval enrichment; cli.py:239 проверка провайдера.
- repository.py:424-431 повторный select после ON CONFLICT (нужен, не шум).
