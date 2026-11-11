# Карта: цитаты для полей, выведенных правилами (остаток E2.3, docs/roadmap.md:232)
## Точки входа
- cli.py:_pipeline (~58): собирает RulesEnricher -> LLMEnricher(rules, configured_client, cache=SqlLLMCache) для run и reprocess.
- ingestion/pipeline.py:_process (~108): normalize -> enricher.enrich -> repository.upsert_opportunity.
- pipeline.py:reprocess (90): гоняет сохранённые raw заново, `keep_closed=True`; CLI `seawork ingest reprocess --source X` (cli.py:127).
- ingestion/enrich/rules.py:RulesEnricher.enrich (134): единственный источник RULE-полей.
## Поток
cli -> LLMEnricher.enrich (llm.py:~235) -> RulesEnricher.enrich -> [LLM только если experience_level или required_certificates None, есть client и >=20 слов] -> _merge (model_copy) -> Repository.upsert_opportunity (model_dump json в JSON-колонку enriched, repository.py:88) -> scoring/factors.py:_cite -> app/cards.py:_reasons -> bot/render.py:_reason.
## Контракты
- domain/inferred.py: `Inferred[T](value, provenance, confidence, quotes: dict[str,str] | None = None)`, frozen. Валидатор только SOURCE=>conf 1.0. Ключ quotes: скаляр = str(value) ("mid"), список = элемент списка ("stcw"). Для SalaryRange/HiringScope (не str, не list) ключа нет: `_cite` их не вызывает.
- LLM: llm.py:_inferred (264) ставит quotes; опыт `{level.value: quote}` (llm.py:~296), сертификаты `{cert: quote}` (329). Дословность: `normalise_whitespace(quote) in normalise_whitespace(source_text)` (llm.py:~287, ~318) + для сертификатов `quote_names_certificate` (паттерны справочника). Невалидная цитата -> поле отбрасывается целиком, не сохраняется без цитаты. Сохраняется исходная цитата модели (не срез из текста).
- _cite (factors.py:55): `"{item}: {quote} [prov]"` если item есть в quotes, иначе `"{item} [prov]"`. Ключ не найден = тихий откат к значению.
## Места создания Inferred в rules.py (строки `Inferred(`)
| стр. | поле | откуда значение | match/span | честная цитата |
|---|---|---|---|---|
| 115 `_parse_symbol_salary` | salary | `salary_raw` источника, fullmatch PADI | весь raw | НЕТ: структурное поле, цитаты не нужно |
| 158 `parse_salary` | salary | `salary_raw`, fullmatch | весь raw | НЕТ: то же |
| 222 | direction | конфиг источника (`direction`) | нет текста | НЕТ |
| 233 | workplace_type_hint | конфиг источника | нет текста | НЕТ |
| 265 `_profession_from_source` fallback | profession (conf 0.75) | подстрока title, `_profession(title)` | span в `_starts_word` теряется (bool) | да: подстрока-алиас в title. Но title не qualification-текст |
| 293 `_profession` | profession (0.9) | alias из professions.yaml в department/division/sector ИЛИ title | `_starts_word` -> re.search, match выбрасывается; победитель = самый длинный алиас (str) | вариант 1: сам алиас-подстрока из folded-текста. Если вход structured (department) — это структурное поле, цитата сомнительна |
| 325 `_language_field` | required_/preferred_languages (2 поля) | `_languages_from(title, qualification_text)` | match и `_sentence_around` есть в цикле (303-304), наружу уходят только ключи; по title — `X-speaking` match | предложение (`window`, уже вычислено) — естественная цитата, ключ = язык. Из title: подстрока совпадения. Нужна смена возврата `list[str]` -> ключ+цитата |
| 391 `_required_certificates` | required_certificates | `_certificates_from(qualification_text)`, `re.search` по patterns | match есть, выбрасывается (`any(...)`) | подстрока совпадения паттерна (как проверяет `quote_names_certificate` для LLM) или предложение/окно вокруг; ключ = cert key |
| 434 | experience_level (ENTRY) | `_NO_EXPERIENCE` в qualification_text | `re.search`, match выбрасывается | подстрока ("no experience") или предложение |
| 463 | experience_level | max по `re.finditer` "N years" + `_is_tenure` | в генераторе есть match, max теряет, какой победил | предложение/окно вокруг победившего match; нужно запомнить match с max (сейчас список float) |
| 493/500/508 `_hiring_scope` | hiring_scope | `recruitment_office_raw` (структурное, голова до тире) | нет | НЕТ: структурное; тип HiringScope не str/list |
| 541 `_country` | country (и страна для hiring_scope 508) | alias из countries.yaml в `location_raw` (структурное) | `_starts_word` bool | НЕТ: структурное поле |
- Итого: цитата осмысленна для languages, certificates, experience (текстовые, qualification_text) и, с оговоркой, profession-по-title. Структурные (salary, direction, workplace, hiring_scope, country, profession-из-department) цитаты не получают.
- Окно: у языков есть `_sentence_around` (115) по `[.;\n]`; у опыта окно символов 60 (`_EXPERIENCE_WINDOW`) для проверки слова, не для цитаты. Лимит бота 100 симв. (render.py MAX_EVIDENCE).
## Слияние правил и LLM
- llm.py:_merge: LLM заполняет только поля, где правило дало None (experience_level, required_certificates). Правило побеждает всегда (test_llm_never_overwrites_rules). Потеря quotes при слиянии невозможна: `model_copy(update=)` переносит поле целиком. Если правило получит quotes, поле от правила ходит как есть. Если правило дало cert list, LLM его не дополняет (нет объединения).
- LLM не вызывается при обоих заполненных правилами полях: цитата от правил — единственный путь для них.
## Что видит пользователь
- Карточка: render.py:109 `"{метка}: {evidence[:100]}"`, evidence = verdict.opportunity_evidence (cards.py:_reasons). Показываются все MATCH и 1 MISMATCH.
- Правило: "Подходит профессия: chef [rule]", "Нужные сертификаты у вас есть: stcw, haccp [rule]", "Подходит уровень опыта: mid [rule]", "Языки совпадают: en [rule]".
- LLM: "Нужные сертификаты у вас есть: stcw: valid STCW documents [llm]", "Подходит уровень опыта: mid: Minimum 3 years of experience [llm]".
- Не цитируются в любом случае: salary, country/direction вне `_membership` (они идут через `_cite` как "diving [rule]"), hiring_country (сырой office без провенанса/цитаты, factors.py:191-217).
- Страны/направление через `_cite` с quotes=None дают ключ. Если у профессии quotes станет не None, она получит "chef: <подстрока> [rule]" автоматически; изменений scoring/bot не нужно. Но цитата-как-подстрока ("chef: Head Chef") может выглядеть шумно.
## Хранение и корпус
- quotes сохраняются: `model_dump(mode="json")` целиком в `enriched` JSON(B) (repository.py:88); round-trip покрыт tests/test_storage.py:261,263. Миграция не нужна (JSON, поле необязательное, старые записи читаются с None).
- Старый корпус получит цитаты только через `ingest reprocess` по каждому источнику (по сохранённому raw, без сети к источнику).
- Reprocess платит за LLM, если настроен client (`configured_client`: provider+model+url+key) и поле опыта/сертификатов у правил пусто. Кэш SqlLLMCache по (sha256(text), PROMPT_VERSION, model_id) — повторный reprocess по неизменному тексту бесплатен; изменение правил на кэш не влияет. Смена PROMPT_VERSION/модели = полный платный прогон. Reprocess без LLM-настроек нулевой по стоимости.
## Тесты
- покрыто: LLM quotes (tests/test_llm_enricher.py: выдуманная цитата отбрасывается, правило не перезаписывается, кэш); `_cite` с цитатой и без (tests/test_match_score.py:440-475, точное "stcw [llm]" на :205); storage round-trip quotes (test_storage.py:228-263); снапшоты правил по источникам: счётчики полей (test_snapshot.py:42-54, test_padi.py:151-158, test_crewplanet.py:135-153, test_pinpoint.py) — не проверяют quotes, но ловят смену value.
- не покрыто: любые quotes у RULE-полей; голые юнит-тесты RulesEnricher по языкам/опыту/сертификатам на уровне функций отдельным файлом (test_rules.py нет, правила тестируются через снапшоты источников); дословность/подстрочность цитаты правил; render с реальным quotes-текстом >100 симв.
- Eval (reporting/enrichment_eval.py, `seawork eval enrichment`): оценивает только ответы LLM против эталона tests/fixtures/eval/enrichment_gold.yaml; RulesEnricher не затрагивает. Цитаты правил на eval не влияют.
## Риски / нельзя трогать
- Значения и confidence правил не менять: снапшоты (test_snapshot, test_padi, test_crewplanet) закрепляют счётчики; задача только добавляет quotes.
- `_languages_from` порядок проверок (title -> enumeration -> preferred -> required) критичен (docstring); не перестраивать при извлечении цитаты.
- `qualification_text` намеренно исключает описание обязанностей; цитата должна браться из того же текста (offset-ы относительно `qualification_text`, не description).
- Ключ quotes обязан равняться str(value)/элементу списка, иначе `_cite` тихо молчит.
- Цитата из `_languages_from` — окно по `[.;\n]` может быть длинным (>100): режется render.
- Структурные поля цитат не получают (инвариант Inferred.quotes docstring).
## Защитный шум
- rules.py:~211,217,220,223,293-298,327-337,422-436: `isinstance`/`.get(..., [])` над yaml-справочником на каждой итерации (cast + isinstance), справочник проверен загрузкой.
- llm.py:~253: `except Exception` вокруг вызова клиента + `_LOG.warning`, fail-open — граница внешнего API, допустимо.
- llm.py:~275-330: `isinstance(item, dict)` по ответу модели, JSON-схема уже задана `response_schema()`; частично оправдано (внешний ответ).
- factors.py:70-78: `quotes is not None and item in quotes` — тихий откат к значению (маскирует несовпадение ключа).
