# Контракт адаптера: Pinpoint

> Реализует `sources/platforms/pinpoint.py` согласно `docs/modules/ingestion.md`
> Первый подключаемый аккаунт: `hollandamericagroup`
> Данные проверены на живом API 19.07.2026, 93 вакансии

---

## 1. Эндпоинт

```
GET https://{account}.pinpointhq.com/postings.json
```

- авторизация не требуется
- **пагинации нет**: параметры `page`, `per_page`, `limit` игнорируются, ответ всегда полный (~737 КБ на 93 вакансии)
- `Content-Type` в заголовке приходит как `text/html`, тело при этом валидный JSON — **не полагаться на content-type**, парсить всегда как JSON
- форма ответа: `{"data": [ ...postings ]}`

Следствие для реализации: `collect()` делает **один** запрос без цикла пагинации.

Проверено: поддомены `princess.pinpointhq.com` и `seabourn.pinpointhq.com` отдают `404`. Это не отдельные аккаунты — их вакансии лежат внутри `hollandamericagroup`.

---

## 2. Реестр аккаунтов

```yaml
# sources/registry.yaml
- platform: pinpoint
  account: hollandamericagroup
  source_id: pinpoint:hollandamericagroup
  trust: PRIMARY
  direction: cruise          # см. data/reference/directions.yaml
```

Подключение следующего работодателя на Pinpoint = ещё один блок здесь, без нового кода.

---

## 3. Две ловушки — прочитать до написания маппинга

### 3.1 `location` — это офис найма, а не место работы

Во всём наборе только **8 уникальных** значений `location.name`:

```
Global,  India - CSSI,  Indonesia - PT. Ratu Oceania Raya,  Indonesia - PTAMI,
Indonesia - SBI,  Philippines - Singa,  Philippines - UPL,  Thailand - CTI
```

Это рекрутинговые офисы Carnival, а не места работы. Вакансия `Chef De Partie` с `location.city = "Kurla West, Mumbai"` — это работа **на судне**, а не в Мумбаи; Мумбаи лишь офис, через который идёт наём.

**Запрещено** отображать `location.city` / `location.province` в наши `city` / `country`. Пользователь, отфильтровавший вакансии по Индии, получил бы судовые контракты, не имеющие к Индии отношения.

Правильная обработка:

- `location.*` кладётся в `NormalizedOpportunity` как `recruitment_office_raw` — отдельное поле, не имеющее отношения к месту работы;
- `country` и `city` остаются `None`;
- при обогащении проставляется `workplace_type_hint = "vessel"` с `provenance=RULE`.

Это прямое применение §3.2 спеки модуля: нормализация не додумывает. Заманчивое поле, отображённое не туда, хуже отсутствующего — оно порождает уверенно неверный Match Score и ложное объяснение пользователю (SPEC.md §10.7).

### 3.2 Даты публикации не существует

В ответе нет ни `created_at`, ни `published_at`, ни `updated_at`. Единственное поле с датой — `deadline_at`, и оно заполнено у 5 записей из 93.

Следствия:

- `posted_at` = `None`, ожидаемое покрытие 0%;
- сортировка по свежести на стороне источника невозможна;
- инкрементальный обход по дате невозможен — `collect()` всегда забирает всё, новизна определяется на нашей стороне;
- в `opportunities` необходимо поле **`first_seen_at`** — момент, когда объект впервые увиден нами. Это наш факт, а не факт источника, поэтому он не является частью `NormalizedOpportunity`; проставляется слоем storage при первой вставке.

Исчезновение записи из ответа — единственный доступный сигнал закрытия вакансии. Обрабатывается на следующей вехе, здесь только фиксируется.

---

## 4. Маппинг полей

### 4.1 Идентификаторы

| Наше поле | Источник | Примечание |
|---|---|---|
| `external_id` | `id` | целое, привести к строке |
| `url` | `url` | абсолютный, использовать как есть |
| `content_hash` | sha256 канонизированного JSON записи | ключ отслеживания изменений |

### 4.2 `NormalizedOpportunity` — только факты источника

| Наше поле | Источник | Покрытие | Примечание |
|---|---|---|---|
| `title` | `title` | 100% | пример: `SBN - Chef De Partie - CSSI` |
| `description` | склейка HTML-блоков (см. ниже) | 100% | HTML вычистить в текст |
| `employer` | `job.structure_custom_group_one.name` | 100% | `Holland America Line` (69), `Seabourn` (24) |
| `recruitment_office_raw` | `location.name` | 100% | **не место работы**, см. §3.1 |
| `posted_at` | — | 0% | поля нет, см. §3.2 |
| `salary_raw` | `compensation` | 0% | все `null`; схема поля есть, компания не публикует |
| `declared_type` | `employment_type` | 100% | `fixed_term_contract` (90), `contract` (3) |
| `language` | детектор по `description` | — | не угадывать по домену |

Порядок склейки `description` (пропускать пустые, сохранять заголовки как подзаголовки):

```
description
key_responsibilities        (заголовок из key_responsibilities_header)
skills_knowledge_expertise  (заголовок из skills_knowledge_expertise_header)
benefits                    (заголовок из benefits_header)
```

Исходные HTML-блоки сохранять и по отдельности — `skills_knowledge_expertise` это раздел квалификаций, из него извлекаются сертификаты, и точечный разбор надёжнее разбора склейки.

### 4.3 Игнорируемые поля

`workplace_type` / `workplace_type_text` — у всех 93 записей значение `onsite`, информационная ценность нулевая (и вводит в заблуждение: работа на судне помечена как «на месте»).
`compensation_visible` (везде `false`), `path` (дублирует `url`), `*_header` (нужны только для склейки), `reporting_to`, `job.requisition_id`, `job.id`, `location.id`.

---

## 5. Классификация и обогащение

### Тип объекта

Все записи → `OpportunityType.JOB`, `type_confidence = 1.0`.

Обоснование: `employment_type` принимает только значения `fixed_term_contract` и `contract`, стажировок в наборе нет. Если появится значение вне известного множества — не угадывать, писать в лог и ставить `JOB` с `type_confidence = 0.5`.

### Направление

`direction = cruise`, `provenance = RULE`, `confidence = 1.0` — берётся из конфигурации источника, а не извлекается из данных.

### Профессия

Основной сигнал — `job.department` и `job.division`:

```
divisions:   Hotel (89), Technical (4)
departments: Galley (41), Guest Svc (11), Housekeeping (8), Beverage Svc (8),
             Entertainment (7), Restaurant (5), Human Resources (4), Technical (3)
```

Сопоставление с `data/reference/professions.yaml`. `title` использовать как уточняющий сигнал, но помнить о префиксах бренда и суффиксах офиса (`SBN - Chef De Partie - CSSI`) — их вычищать до сопоставления.

### Сертификаты и уровень опыта

Извлекать правилами из `skills_knowledge_expertise`. Это единственные поля, где для данного источника действительно проверяется качество детерминированного обогащения, — по ним и будет виден ответ на вопрос §1.1 спеки модуля.

**Уровень опыта: требовать контекст.** В тексте квалификаций встречаются периоды, не имеющие отношения к стажу, — например `Basic Food Hygiene course every 2 years` (периодичность переаттестации). Совпадение по шаблону `N years` засчитывается, только если рядом присутствует слово `experience`. Измерено на живых данных: без этого условия 6 из 71 срабатываний ложные.

Брать `max()` по всем найденным числам нельзя: при сочетании «2 года обучения» и «1 год опыта» это завысит уровень. Использовать значение из того совпадения, у которого подтверждён контекст опыта.

**Сертификаты: справочник ведётся по живым данным.** Шаблоны должны совпадать с формулировками источника, а не с каноническим названием сертификата. Проверено: `Marlins` в данных встречается как `Marlins Score 90 or above`, поэтому шаблон `Marlins test` пропускает 12 записей. Аналогично отсутствовали `USPH`, `Food Hygiene` и `Basic Safety` без слова `Training`.

---

## 6. Покрытие

Измерено на снапшоте из 93 вакансий, зафиксировано тестами в `tests/test_snapshot.py`. Расхождение — сигнал регрессии, а не повод подгонять цифры.

| Поле | Факт | Provenance |
|---|---|---|
| `title`, `description`, `url`, `employer` | 100% | SOURCE |
| `type` | 100% | SOURCE |
| `direction` | 100% | RULE (из конфигурации) |
| `profession` | 94.6% (88/93) | RULE (department/division) |
| `experience_level` | 71.0% (66/93) | RULE |
| `required_certificates` | 25.8% (24/93) | RULE |
| `country`, `city` | **0%** | — (см. §3.1) |
| `posted_at` | **0%** | — (см. §3.2) |
| `salary` | **0%** | — |

История правок после первой реализации:

- `required_certificates` 14.0% → 25.8% — в справочник добавлены формулировки, реально встречающиеся в данных (`Marlins` без слова `test`, `USPH`, `Food Hygiene`, `Basic Safety`)
- `experience_level` 76.3% → 71.0% — убраны ложные срабатывания на периодичности переаттестации; снижение здесь означает рост точности, а не потерю данных

Источник даёт образцовую доставку и типизацию, но по трём полям, важным для Match Score (`country`, `posted_at`, `salary`), не даёт ничего. Это ожидаемо и является частью ответа, ради которого пишется веха 1: **одного источника для Match Score не хватит, и Crewplanet с его зарплатами нужен именно поэтому.**

---

## 7. Тесты

- Фикстура: сохранённый полный ответ `postings.json` в `tests/fixtures/pinpoint/hollandamericagroup.json`
- Разбор гоняется офлайн на фикстуре, сеть не используется
- Обязательный тест на §3.1: убедиться, что `country` и `city` остаются `None` при непустом `location`
- Обязательный тест на идемпотентность: два прогона на одной фикстуре дают 93 записи, не 186
- Табличные тесты на извлечение сертификатов из реальных фрагментов `skills_knowledge_expertise`

---

## 8. Definition of Done

- [ ] `seawork ingest run --source pinpoint:hollandamericagroup` создаёт 93 записи
- [ ] повторный запуск не создаёт дубликатов
- [ ] `seawork ingest reprocess` работает при отключённой сети
- [ ] `seawork report coverage` печатает отчёт, цифры сходятся с §6
- [ ] `country` и `city` пусты у всех записей, тест это фиксирует
- [ ] `first_seen_at` проставлен у всех записей
- [ ] `ruff`, `pyright`, `pytest`, CI — зелёные
