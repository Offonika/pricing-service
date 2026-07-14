<!-- File: docs/architecture.md -->

# Архитектура проекта «Умное ценообразование Master Mobile»

Документ описывает high-level архитектуру сервиса ценообразования, основные компоненты, потоки данных и схемы интеграций.

Исходные бизнес-требования и сценарии см. в `docs/PRD.md`.  
Правила присвоения коммерческих SKU и структура кодов описаны в `docs/sku_policy.md`.  
Общие правила разработки и работы Codex — в `docs/constitution.md`.  
План задач — в `docs/plan.md`.

---

## 1. Общий обзор

Сервис ценообразования — это отдельное приложение, которое:

1. **Импортирует данные**:
   - наши товары, закупочные цены, остатки из 1С;
   - цены и наличие у конкурентов из парсера (ZennoPoster и др.).
   - сигналы по рынку смартфонов: новые модели из пресс-релизов брендов, ключевые фразы по запчастям и спрос по ним из Яндекс.Директ.

2. **Хранит и обрабатывает данные** в БД (PostgreSQL):
   - товары (`Product`);
   - конкуренты (`Competitor`);
   - цены конкурентов (`CompetitorPrice`);
   - матчинги, стратегии и расчёты цен.

3. **Рассчитывает рекомендуемые цены** по стратегиям:
   - учитывая закупку, конкурентов, маржу, статусы товаров (ABC/XYZ и др.).

4. **Отдаёт результаты во внешние системы**:
   - выгрузка в 1С (файлы для документа «Установка цен номенклатуры»);
   - отчёты и списки проблемных позиций (через API и Telegram-бот).

---

## 2. Компоненты системы

### 2.1. Backend-сервис (FastAPI)

- Ядро системы.
- Отвечает за:
  - REST API для внутренних инструментов и Telegram-бота;
  - бизнес-логику импорта, матчинга, стратегий и расчётов;
  - подготовку данных для выгрузок в 1С.

Основные слои:

- `app/api/` — HTTP-эндпоинты (FastAPI routers).
- `app/schemas/` — Pydantic-схемы запросов/ответов.
- `app/services/` — бизнес-логика (импорт, матчинг, расчёт).
- `app/models/` — SQLAlchemy-модели БД.
- `app/core/` — конфиг (Pydantic Settings), логирование и базовые утилиты.
- `app/workers/` — код фоновых задач (если используется Celery/RQ).
- `tests/` — unit и интеграционные тесты (pytest, httpx).

Инфраструктура приложения:
- Конфигурация через переменные окружения (`.env`, Pydantic Settings).
- Логирование: единый конфиг + middleware запросов/ошибок.
- Миграции БД: Alembic.

### 2.2. База данных (PostgreSQL)

Основные сущности (минимальный набор для MVP):

- `Product`
  - наши товары (SKU, название, бренд, категория, статусы, флаги ABC/XYZ и т.п.).
- `Competitor`
  - конкуренты (название, сайт, активность).
- `CompetitorPrice`
  - цены конкурентов по товарам (product_id, competitor_id, цена, наличие, дата сбора).
- `PhoneModel` (или аналогичная сущность)
  - новые модели смартфонов с брендом/моделью/вариантом/датами анонса и продаж, опциональными параметрами экрана; источник — агент по пресс-релизам.
- `SmartphoneRelease`
  - факты анонсов смартфонов из внешних новостных источников: brand/model/full_name, announcement_date, release_status (rumor/announced/released), источник (name/url/type), summary, raw_payload, is_active, timestamps; используется для аналитики и сигналов стратегиям.
- `Keyword`
  - ключевые фразы под запчасти (дисплей/экран/тач/стекло <brand> <model>), привязка к модели телефона.
- `DemandStat`
  - спрос по фразам из Яндекс.Директ (показы/клики/регион/дата), хранит историю запросов.
- `ProductMatch` (или аналог)
  - результаты матчинга наших товаров с карточками конкурентов.
- `PricingStrategy`
  - стратегии ценообразования (условия, параметры, приоритеты).
- `PricingStrategyVersion`
  - версии стратегий с параметрами, чтобы фиксировать историю изменений правил.
- `PriceRecommendation`
  - история рассчитанных цен: рекомендуемая/нижняя граница, ссылка на версию стратегии, причины, время расчёта.

Схема и связи уточняются и поддерживаются через миграции Alembic.

### 2.3. Фоновые задачи

Используется для:

- синхронизации с внешними источниками (парсер конкурентов, экспорт в 1С);
- пакетного пересчёта цен по многим товарам;
- формирования выгрузок для 1С по расписанию.

Типовые задачи:

- `import_competitor_prices(file_path)`
- `sync_onec_product_catalog()`
- `recalculate_all_prices(strategy_set_id=...)`
- `generate_price_export_for_1c(date=...)`

Постоянные jobs запускаются через версионируемые CLI entrypoints и cron/systemd.
Cron отвечает только за расписание, lock и технический лог; бизнес-логика живёт в
application services. Redis используется только в контурах, где очередь реализована
явно, и не считается общей очередью всех jobs.

DB access постоянной команды объявляется в `docs/registry/cli-jobs.json`.
Read-only PostgreSQL-команды используют `session_scope(read_only=True)`, который
завершает scope rollback без commit. Команды записи используют явный Unit of Work;
generic `build_engine` остаётся временным compatibility API только до завершения
Release B и не должен появляться в новых постоянных jobs.

### 2.4. Внешние интеграции

#### 1С

- Источник:
  - прямая SQL-база `1С УТ 10.3 / Ekama` с доступом только на чтение
    (`ONEC_DATABASE_URL`).
- Способ интеграции:
  - сервис подключается к MSSQL через pytds/SQLAlchemy;
  - фоновая задача `sync_onec_product_catalog` читает справочник `_Reference62`,
    виды номенклатуры и регистры свойств, затем обновляет `Product` и связанные
    сущности;
  - импорт ограничен активной группой `ОБЩИЙ КАТАЛОГ`, помеченные на удаление и
    дубликаты не становятся активными товарами.
- Практическая карта полей 1С для классификации номенклатуры:
  - карточка товара: `_Reference62`;
  - артикул: `_Reference62._Fld836`;
  - код 1С: `_Reference62._Code`;
  - код инфосистемы: `_Reference62._Fld9175`;
  - родительская группа: `_Reference62._ParentIDRRef -> _Reference62._Description`;
  - `Вид номенклатуры` хранится не в `_InfoRg8928`, а в `_Reference62._Fld857RRef -> _Reference26._Description`;
  - `Предмет` для актуальных карточек хранится не в `_InfoRg8928`, а в typed-регистре `_InfoRg6309`:
    `_InfoRg6309._Fld6310_RRRef -> _Reference62._IDRRef` связывает запись с товаром,
    `_InfoRg6309._Fld6311RRef -> _Chrc401._IDRRef` задаёт имя свойства,
    для свойства с `_Chrc401._Description = 'Предмет'` значение берётся из `_InfoRg6309._Fld6312_RRRef -> _Reference42._Description`,
    а если ссылка пустая, fallback-значение лежит в `_InfoRg6309._Fld6312_S`;
  - `_InfoRg8928` остаётся источником для части доп. свойств (`Категория`, `Качество`, `Емкость`, `Совместим с моделью` и т.д.), но не должен считаться надёжным источником для `Предмет` и `Вида номенклатуры` без проверки.

#### Парсер конкурентов (ZennoPoster и др.)

- Источник:
  - CSV/Excel с ценами и наличием по конкурентам.
- Текущие целевые конкуренты пилота: moba.ru, green-spark.ru, ultra-details.ru, memstech.ru.
- Режимы:
  - `COMPETITOR_SOURCE_MODE=zenno` — ждём файлы от внешнего парсера (ZennoPoster).
  - `COMPETITOR_SOURCE_MODE=internal` — встроенный парсер, запросы идут через Proxy API (`PROXY_API_URL`, `PROXY_API_TOKEN`), объём ограничивает `COMPETITOR_PARSE_LIMIT` (дефолтно 10 для отладки).
- Способ интеграции:
  - скрипт/бот парсинга складывает файлы в подготовленную директорию или отдаёт по API;
  - модуль импорта читает файл и заполняет `Competitor`, `CompetitorPrice`, обновляет `ProductMatch`.
  - Для FTP-выгрузок конкурентов (poiskzip-moba, poiskzip-liberti) используется поток `FTP → XLSX → job import_competitor_ftp`. Источники задаются через `COMPETITOR_FTP_SOURCES` (`name:directory:pattern`, где pattern содержит `{date}`, напр. `moba-{date}.xlsx`). Job подключается к FTP (`COMPETITOR_FTP_HOST`/`PORT`/`USER`/`PASSWORD`, `COMPETITOR_FTP_TLS`, `COMPETITOR_FTP_TIMEOUT_SEC`), ищет датированные файлы, валидирует обязательные колонки (`group, sku, name, price_opt, price_roz, link, time`, опционально `amount`/`stock`), приводит `time` к MSK ISO8601, сверяет дату имени и содержимого, и пишет данные в `competitor_ftp_file` (метаданные), `competitor_ftp_raw_row` (сырьё, ошибки) и `competitor_ftp_record` (нормализованные строки). Дедуп по `(source, file_date)`, при повторной заливке файл перезаписывается. Цепочка ZenLogs (HTTP) и каталог `competitor_item`/`competitor_item_snapshot` удалены, используем только FTP-поток.
  - Матчинг цен: job `./.venv/bin/python -m tasks.match_competitor_ftp` берёт `competitor_ftp_record`, нормализует SKU и сопоставляет с `product.article`, создаёт `CompetitorPrice` (price = `price_roz` или `price_opt`, in_stock из файла, collected_at = `observed_at`) и `ProductMatch` (confidence=1.0). unmatched/ambiguous логируются, много-матч по SKU не записывается.

#### Агент по рынку смартфонов (пресс-релизы/новости)

- Назначение:
  - автоматический ресёрч официальных анонсов производителей (Apple, Samsung, Xiaomi и др.).
- Способ интеграции:
  - агент (ChatGPT/Codex) регулярно обходит конфигурируемый список URL;
  - извлекает бренд/модель/вариант, даты анонса/старта продаж, параметры экрана (если есть);
  - вызывает backend-эндпоинт вида `POST /devices/models` и пишет нормализованные данные в `PhoneModel` (без собственного хранения).
- Запуск:
  - планировщик/cron; список брендов и источников задаётся в конфиге проекта.

#### Генерация ключевых фраз и Яндекс.Директ

- Генерация фраз:
  - на вход — новые модели из `PhoneModel`;
  - backend-процедура или отдельный агент формирует фразы по шаблонам «дисплей/экран/тач/стекло <brand> <model> купить/замена/...», сохраняет в `Keyword`.
- Интеграция с Яндекс.Директ:
  - используется официальный API Директа (не прямой Wordstat) через сервис `yandex_integration_service`;
  - сервис хранит токен/креды, принимает список фраз и регион, возвращает агрегаты (прогноз показов/кликов, при наличии — ставки/конкуренцию);
  - результаты сохраняются в `DemandStat` с привязкой к фразе, дате, региону;
  - внешний интерфейс — абстрактный метод `get_yandex_stats(phrases[], region) → [{phrase, impressions, clicks, date, ...}]`.

#### 1С

- Назначение:
  - получение от сервиса ценообразования предложений по ценам для документа «Установка цен номенклатуры».
- Способ интеграции:
  - сервис формирует файл (CSV/Excel/другой согласованный формат);
  - 1С забирает файл из указанной директории или по HTTP-эндпоинту;
  - формат и правила считывания фиксируются в отдельной спецификации.

#### Мониторинг схемы возвратов

- Назначение:
  - ежедневный контроль подозрительной последовательности `Реализация (Розница) -> Возврат от покупателя -> Реализация (не Розница)` по одной номенклатуре и одному магазину/складу.
- Способ интеграции:
  - backend запускает отдельный job `./.venv/bin/python -m tasks.detect_return_scheme`;
  - extractor читает прямой SQL по `_Document203/_Document203_VT4966` и `_Document109/_Document109_VT1698` в read-only базе 1С (`ONEC_DATABASE_URL`);
  - сотрудник документа читается из реквизита `Ответственный`: `_Document203._Fld4942RRef -> _Reference54` для реализации и `_Document109._Fld1682RRef -> _Reference54` для возврата;
  - строки приводятся к типу `OperationEvent` с полями документа, номенклатуры, магазина, сотрудника, типа цены, количества и суммы;
  - детектор применяет FIFO-матчинг по количеству в окне `RETURN_SCHEME_WINDOW_DAYS` (по умолчанию 7 дней);
  - новые/неуведомлённые инциденты пишутся в таблицу `return_scheme_incident`, группируются в outbox-пакет `return_scheme_alert_batch` и выгружаются в `XLSX`;
  - сервер `A` публикует internal API:
    - `GET /api/internal/alerts/return-scheme/pending`
    - `GET /api/internal/alerts/return-scheme/{batch_id}/report`
    - `POST /api/internal/alerts/return-scheme/{batch_id}/ack`
  - сервер `B` (`Openclaw`) забирает pending batch'и по service token, отправляет отчёт в отдельный Telegram-чат по возвратам и создаёт задачи в `Bitrix24` для повторных/критичных кейсов;
  - описания Bitrix24-задач должны следовать workspace-правилу `docs/runbooks/bitrix-task-writing-rules.md`: объяснять, что это сигнал на ручную проверку, а не доказанное нарушение; для `RETURN_SCHEME_ESC` счетчик всегда расшифровывается как количество строк товаров, потому что несколько строк одного возврата являются одним операционным эпизодом;
  - прямой скрипт `infra/cron/return_scheme_alert.py` на сервере `A` остаётся только как временный fallback и управляется `RETURN_SCHEME_DIRECT_TELEGRAM_ENABLED`.
- Операционный контур:
  - shell-обвязка `infra/cron/return_scheme_monitoring.sh`;
  - сервер `B` может использовать pull-скрипт `infra/cron/return_scheme_pull_from_a.py`;
  - конфиг через `RETURN_SCHEME_ENABLED`, `RETURN_SCHEME_RETAIL_PRICE_TYPES`, `RETURN_SCHEME_OUTPUT_DIR`, `RETURN_SCHEME_INTERNAL_API_TOKEN`, `RETURN_SCHEME_ALERT_TELEGRAM_*`.

#### Foundation дебиторки

- Назначение:
  - нормализованный ledger взаиморасчётов на сервере `A` как фундамент для витрин и кейсов дебиторки.
- Способ интеграции:
  - backend запускает sync `./.venv/bin/python -m tasks.sync_receivable_ledger --sql-file ...`;
  - extractor читает read-only SQL из 1С через `ONEC_DATABASE_URL`, но ожидает уже нормализованную проекцию ledger-событий;
  - финансовые события пишутся в `receivable_ledger_event`;
  - история ответственных менеджеров восстанавливается в `counterparty_manager_assignment`;
  - ежедневный срез открытой дебиторки пишется в `receivable_balance_snapshot`.
  - поверх snapshot'ов собираются кейсы `receivable_case` c сегментами `new_daily`, `inactive`, `employee`, `fired_manager`, `adjustment_candidates`;
  - chain документов для кейса хранится в JSON и восстанавливается из ledger от origin-документа долга.

#### Foundation staffing

- Назначение:
  - нормализованный staffing-контур на сервере `A` для daily snapshots и period summary по укомплектованности.
- Способ интеграции:
  - backend запускает sync `./.venv/bin/python -m tasks.sync_staffing --staff-file ... --plan-file ... --fact-file ...`;
  - входом служат нормализованные JSON-проекции сотрудников, планов смен и фактов выхода из `Bitrix24`/HR;
  - сотрудники пишутся в `staff_member`, план смен в `store_shift_plan`, факт выхода в `store_shift_fact`;
  - management API `GET /api/management/task-payloads?date=YYYY-MM-DD` публикует индивидуальные payload'ы задач для `Bitrix24/Openclaw`;
  - сервер `B` использует pull-скрипт `infra/cron/management_tasks_from_a.py`, который:
    - читает payload'ы по service token;
    - мапит `owner_code`/`watcher_codes` в `Bitrix24 user id` через `team.yaml` и env overrides;
    - идемпотентно создаёт или обновляет задачи в `Bitrix24` по `dedupe_key`;
    - хранит локальный state в `.data/management-tasks/state.json`;
    - может запускаться в `dry-run` для безопасной диагностики до включения по cron.
  - daily snapshots пишутся в `staffing_snapshot` с полями `planned / assigned / confirmed / no_show / deficit / criticality`;
  - period summary и forecast на `3/7/14` дней считаются из исторических `staffing_snapshot`, а не из live-данных.

#### Management API

- Назначение:
  - приватные read-only endpoint'ы для сервера `B` поверх готовых витрин дебиторки и staffing.
- Текущие endpoint'ы:
  - `GET /api/receivables/new-daily?date=YYYY-MM-DD`
  - `GET /api/receivables/cases?date=YYYY-MM-DD&segment=...`
  - `GET /api/receivables/employee-cases?date=YYYY-MM-DD`
  - `GET /api/receivables/manager-summary?date=YYYY-MM-DD`
  - `GET /api/staffing/daily?date=YYYY-MM-DD`
  - `GET /api/staffing/period-summary?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`
- Доступ:
  - bearer token через `MANAGEMENT_INTERNAL_API_TOKEN` с fallback на `RETURN_SCHEME_INTERNAL_API_TOKEN`;
  - ответы унифицированы по полям `as_of`, `freshness_status`, `source_status`, `payload`.

#### Management Rules Engine

- Назначение:
  - вычислять task payload'ы для `Bitrix24/Openclaw` на сервере `A` из готовых витрин, без повторного пересчёта на сервере `B`.
- Текущие правила:
  - `receivable_overdue`
  - `receivable_fired_manager`
  - `receivable_adjustment_candidate`
  - `staffing_shift_deficit`
- Контракт:
  - `GET /api/management/task-payloads?date=YYYY-MM-DD`
  - каждый payload содержит `rule_code`, `severity`, `owner_code`, `watcher_codes`, `reaction_deadline_at`, `due_at`, `dedupe_key`, `metrics`, `references`;
  - сервер `B` отвечает за маппинг `owner_code/watcher_codes` в реальные `Bitrix24` user ids и за дедупликацию side effects.

#### Management Health / Freshness

- Назначение:
  - контроль SLA свежести и полноты management-витрин до того, как их начнёт забирать сервер `B`.
- Контракт:
  - `GET /api/management/health?date=YYYY-MM-DD`
  - ответ содержит общий `status`, а также покомпонентную диагностику по `receivables`, `staffing`, `task_payloads`.
- Что проверяется:
  - latest snapshot date;
  - lag в днях относительно запрошенной даты;
  - source status (`ready` / `partial` / `empty`);
  - freshness status (`fresh` / `stale` / `missing`);
  - базовые counts по snapshot'ам и payload'ам.
- SLA-пороги:
  - `MANAGEMENT_RECEIVABLES_MAX_LAG_DAYS`
  - `MANAGEMENT_STAFFING_MAX_LAG_DAYS`
  - `MANAGEMENT_TASK_PAYLOADS_MAX_LAG_DAYS`

#### Openclaw Management Adapter (сервер B)

- Назначение:
  - read-only adapter на сервере `B`, который подтягивает management snapshots с `A` и подмешивает их в prompt утренних отчётов `Openclaw`.
- Реализация:
  - скрипт `infra/cron/management_digest_from_a.py`;
  - поддерживает endpoint'ы:
    - `GET /api/management/health`
    - `GET /api/receivables/new-daily`
    - `GET /api/receivables/cases?segment=inactive|fired_manager|adjustment_candidates`
    - `GET /api/receivables/employee-cases`
    - `GET /api/receivables/manager-summary`
    - `GET /api/staffing/daily`
    - `GET /api/management/task-payloads`
  - при ошибке `A` не падает целиком: возвращает деградированный digest с явным списком недоступных компонентов;
  - повторный запуск не создаёт side effects, потому что adapter только читает API и печатает summary/json.
- Конфиг на `B`:
  - `MANAGEMENT_SOURCE_URL` с fallback на `RETURN_SCHEME_SOURCE_URL`;
  - `MANAGEMENT_SOURCE_TOKEN` с fallback на `RETURN_SCHEME_SOURCE_TOKEN`;
  - `MANAGEMENT_ADAPTER_TIMEOUT_SECONDS`;
  - `MANAGEMENT_ADAPTER_RETRIES`;
  - `MANAGEMENT_ADAPTER_RETRY_DELAY_SECONDS`.
- Интеграция в prompt builder:
  - `Openclaw`-конфиг утренних отчётов должен передавать `management_script` для ролей, которым нужен управленческий блок;
  - builder обязан явно просить модель отразить статус свежести/degradation, дебиторку, staffing и, при наличии отдельного источника, AI action items по встречам;
  - B-side digest `infra/cron/meeting_action_digest.py` читает `calls/transcripts/call_analysis`, формирует новые follow-up кейсы за вчера и зависшие open-кейсы (`pending_review/callback/support`) старше SLA, после чего подключается в `morning_call_reports.yaml` через `meeting_action_script`.

#### Telegram-бот

- Использует backend-API для:
  - получения агрегированных отчётов и списков товаров;
  - (опционально) подтверждения/отклонения цен.
- API для бота:
  - `/api/telegram/today` с фильтрами (бренд/категория/поиск);
  - `/api/telegram/alerts` — алерты по марже и отсутствующей закупке.

#### BI (Power BI / Metabase)

- Читает данные из read-only API `/api/bi/*`:
  - `/api/bi/products` — справочник товаров с ABC/XYZ и закупкой;
  - `/api/bi/recommendations` — последние рекомендации с версией стратегии и причинами;
  - `/api/bi/competitor-prices` — последние цены конкурентов.
  - `/api/bi/receivables-current?date=YYYY-MM-DD` — плоская таблица текущих остатков по контрагентам на дату;
  - `/api/bi/receivable-cases?date=YYYY-MM-DD&segment=...` — сегменты дебиторки (`new_daily`, `employee`, `fired_manager`, `inactive`, `adjustment_candidates`);
  - `/api/bi/receivables-manager-summary?date=YYYY-MM-DD` — агрегат по текущим менеджерам: портфель, новые долги, кейсы на корректировку и т.д.
- Доступ предполагается через авторизованный backend (без прямого доступа к БД).
- Готовые Power Query M-запросы для дебиторки — в `docs/BI.Receivables.md`.
- Если BI подключается напрямую к БД (Power BI / Metabase), используем read-only витрины (views) с понятными русскими полями:
  - `vw_competitor_item_catalog_ru` — каталог конкурентов: **«Бренд товара»** (аксессуар/запчасть) vs **«Бренд устройства (parsed)»** (бренд телефона).
  - `vw_competitor_item_compatibility_ru` — совместимости: **«Бренд устройства (совм.)»** / модель / вариант (для какого устройства подходит).
  - `vw_competitor_display_ru` — витрина по дисплеям с русскими атрибутами (тип/качество/рамка/тач/цвет).
- Дополнительные витрины по спросу для моделей телефонов:
  - представления `vw_bi_model_demand_daily` и `vw_bi_model_demand_30d` (см. миграцию `1a4fb0e69e78...`);
  - HTTP-эндпоинты `/api/analytics/model-demand/top` и `/api/analytics/model-demand/{id}/timeseries` для простых дашбордов/админок;
  - подробнее — `docs/BI.ModelDemand.md`.

---

### 2.5. Подсистема Market Research / Demand

**Назначение:** получать новые модели смартфонов, генерировать поисковые фразы по запчастям и собирать спрос через Яндекс.Директ как отдельный источник данных для pricing-engine.

**Состав и границы:**
- `DeviceModelService` — приём и хранение моделей телефонов от агента пресс-релизов.
- `KeywordGenerationService` — генерация фраз по шаблонам для дисплеев/экранов/тач/стекло.
- `DemandService` — фасад для вызова `YandexDirectClient`, сохранение агрегатов спроса.
- `YandexDirectClient` — тонкий клиент поверх официального API (токен/лимиты/ретраи внутри).
- API-слой `/api/agents/*` — HTTP-инструменты, через которые агенты отдают модели и фразы.

**Модель данных (основные поля):**
- `PhoneModel`:
  - `brand`, `model_name`, `variant?`, `announce_date?`, `release_date?`;
  - параметры экрана (опционально): `screen_size_inch?`, `screen_technology?`, `screen_refresh_rate_hz?`;
  - служебные: `created_at`, `updated_at`, `is_active`.
- `Keyword`:
  - `phrase`, `language?`, `category` (например, `display`);
  - `phone_model_id` (FK), `source` (agent/backend), `is_active`, `created_at`, `updated_at`.
- `DemandStat`:
  - `keyword_id` (FK), `date`, `region`;
  - метрики: `impressions`, `clicks?`, `ctr?`, `bid_metrics?` (расширяемый набор под ответ Директа);
  - служебные: `source="yandex_direct"`, `received_at`.

**Поток:** агент → `/api/agents/devices/models` → `PhoneModel` → генерация фраз → `/api/agents/keywords/bulk` (если фразы сгенерированы агентом) или `KeywordGenerationService` → `Keyword` → `DemandService` → Яндекс.Директ → `DemandStat` → pricing-engine.

---

### 2.6. Мониторинг новинок смартфонов (News API + LLM)

- **Назначение:** фиксировать анонсы смартфонов в единой таблице `smartphone_releases`, чтобы видеть новые модели раньше и кормить downstream-анализ (спрос/ассортимент/стратегии цен).
- **Компоненты:**
  - `SmartphoneNewsClient` — HTTP-клиент к внешнему новостному API (конфиг `.env`: `SMARTPHONE_NEWS_API_BASE_URL`, `SMARTPHONE_NEWS_API_KEY`, `SMARTPHONE_NEWS_LANGUAGE`, `SMARTPHONE_NEWS_QUERY`, `SMARTPHONE_NEWS_DAYS_BACK`, `SMARTPHONE_NEWS_PAGE_SIZE`).
  - `SmartphoneReleaseNormalizer` — LLM-промпт к OpenAI (использует `OPENAI_API_KEY`), возвращает `is_phone_announcement`, `brand`, `model`, `announcement_date`, `release_status (rumor/announced/released)`.
  - `SmartphoneReleaseService` — фасад, который делает dedup/upsert в `smartphone_releases` по `brand+model+announcement_date` и `source_name+source_url`.
  - фоновая job `./.venv/bin/python -m tasks.update_smartphone_releases` (Cron/Codex), включается фича-флагом `SMARTPHONE_RELEASES_ENABLED`.
- **Поток данных:** внешнее News API → нормализация через OpenAI → upsert в `smartphone_releases` (brand/model/full_name/announcement_date/release_status/source/summary/raw_payload/is_active) → сигнал для аналитики/спроса/стратегий.
- **Ограничения MVP:** один источник, опрос раз в сутки (или реже), отсутствие прямого влияния на pricing-engine (пока только сигнал «модель появилась»).

### 2.7. Еженедельный digest новинок для закупщиков

- **Назначение:** собрать короткий обзор новинок смартфонов за неделю для отдела закупок Master Mobile.
- **Компоненты:**
  - таблица `weekly_smartphone_digest` (уникальная по `week_start+week_end`, хранит Markdown-обзор, модель LLM, ids релизов и мету);
  - сервис `WeeklyBuyerDigestService` (берёт `smartphone_releases` за 7 дней по `announcement_date/market_release_date`, фильтрует `announced/released`, формирует промпт и вызывает LLM или fallback);
  - воркер `run_weekly_buyer_digest_job` + CLI `./.venv/bin/python -m tasks.generate_weekly_buyer_digest`, фича-флаг `WEEKLY_BUYER_DIGEST_ENABLED`, модель `WEEKLY_BUYER_DIGEST_MODEL`.
- **Поток данных:** `smartphone_releases` (нормализованные анонсы) → агрегатор за неделю → LLM-подготовленный Markdown → `weekly_smartphone_digest` → опциональное сообщение в Telegram (`infra/cron/weekly_buyer_digest_alert.py`, отдельное от ежедневного алерта).
- **Расписание:** шаблон cron `infra/cron/weekly_buyer_digest.cron` запускает `infra/cron/weekly_buyer_digest.sh` раз в неделю; при отсутствии релизов создаётся короткий "пустой" обзор без вызова LLM.

---

## 3. Потоки данных (high-level)

1. **Сбор рынка смартфонов и спроса (агентный контур)**
   - Планировщик вызывает агента пресс-релизов, который обходит источники брендов и отправляет структурированные модели в backend (`PhoneModel`).
   - Отдельная фоновая таска мониторинга новостей (News API + LLM) пишет анонсы в `smartphone_releases`, чтобы фиксировать появление моделей, даже если агент по пресс-релизам не нашёл их.
   - Генератор ключевых фраз (процедура или агент) формирует шаблонные запросы по запчастям и сохраняет их в `Keyword`.
   - Сервис `yandex_integration_service` принимает набор фраз и регион, обращается к API Яндекс.Директ и пишет агрегаты спроса в `DemandStat`.
   - Полученные признаки (новые модели, спрос по фразам) используются в стратегиях ценообразования и приоритизации ассортимента.

2. **Синхронизация наших данных (1С SQL)**
   - сервис подключается напрямую к базе 1С (режим read-only).
   - Фоновая задача `sync_onec_product_catalog`:
     - считывает активный каталог и свойства из 1С;
     - обновляет `Product`, совместимость и классификационные признаки;
     - логирует количество обработанных/пропущенных записей.

3. **Импорт цен конкурентов**
   - Парсер конкурентов выдаёт CSV/Excel с ценами и наличием.
   - Фоновая задача `import_competitor_prices`:
     - обновляет `Competitor` и `CompetitorPrice`;
     - при необходимости обновляет/создаёт связи в `ProductMatch`.
  - В режиме `COMPETITOR_SOURCE_MODE=zenno` используется job `./.venv/bin/python -m tasks.import_zenlogs_competitors`, которая проходит по всем источникам из `ZENLOGS_SOURCES`, скачивает XLSX ZenLogs (`group`, `sku`, `name`, `price_opt`, `link`, `stock`) и пишет данные в `competitor_item` + `competitor_item_snapshot`, фиксируя историю цен/наличия без привязки к нашим SKU. Для FTP-прайсов есть отдельная job `./.venv/bin/python -m tasks.import_competitor_ftp`, которая выкачивает датированные XLSX по маске, валидирует и сохраняет в `competitor_ftp_*` таблицы с дедупликацией по дате файла.

4. **Матчинг товаров и карточек конкурентов**
   - Запускается по расписанию или после импорта.
   - Использует правила (по SKU, названию, бренду и т.д.).
   - Результат сохраняется в `ProductMatch` с флагами надёжности матча.

5. **Расчёт рекомендованных цен**
   - Фоновая задача `recalculate_all_prices`:
     - выбирает активные товары и связанные данные;
     - применяет стратегии (`PricingStrategy` и бизнес-правила из PRD);
     - сохраняет результат в `PriceRecommendation` с объяснением (поля «почему так»).
   - Возможен выбор стратегий по группам товаров, брендам, статусам и т.д.

6. **Выгрузка и отчёты**
   - Фоновые/ручные задачи для формирования:
     - файлов для 1С (по рекомендуемым ценам);
     - отчётов для коммерческого директора (API/витрины).
   - Telegram-бот или веб-интерфейс показывает:
     - агрегированные показатели;
     - проблемные позиции (сомнительный матч, маржа ниже порога, эксклюзивы и т.п.).

---

## 4. Структура каталогов (поддерживается Codex)

Минимальная целевая структура репозитория:

- `app/`
  - `api/` — роуты FastAPI
  - `core/` — конфиги, логирование, общие утилиты
  - `models/` — SQLAlchemy-модели
  - `schemas/` — Pydantic-схемы
  - `services/`
    - `importers/` — интеграции с парсером конкурентов и прочими внешними источниками (1С синхронизируем напрямую из SQL)
    - `pricing_strategies/` — логика стратегий
    - `market_research/` (новые модули) — модели телефонов, генерация ключей, спрос через Яндекс.Директ
    - другие сервисы
  - `workers/` — фоновые задачи (Celery/RQ)
- `tests/` — unit и интеграционные тесты
- `infra/`
  - `docker-compose.yml`
  - при необходимости `k8s/`
- `docs/`
  - `PRD.md`
  - `constitution.md`
  - `architecture.md`
  - `plan.md`
  - `price-strategies.md`
  - `AGENTS.md`
- `tasks/` — детальные ТЗ по задачам из `plan.md`
- `scripts/` — вспомогательные скрипты (миграции, импорты, dev-utils)

Dev-окружение:
- Docker Compose поднимает app + Postgres + Redis (`infra/docker-compose.yml`).
- Параметры окружения в `.env.example` (APP_PORT, DATABASE_URL, REDIS_URL и др.).

---

## 5. Нефункциональные требования (архитектурный уровень)

- Обработка целевого объёма данных (≈ 1000+ товаров, 4–5 конкурентов) в пределах минут при пересчёте.
- Возможность масштабировать:
  - по количеству товаров и конкурентов;
  - по частоте пересчёта.
- Детальное логирование:
  - импорта,
  - матчинга,
  - расчёта цен,
  - выгрузок.
- Объяснимость:
  - для каждой рекомендованной цены хранить входные параметры и причины.

---

## 6. Открытые вопросы

Эти пункты требуют отдельного уточнения и могут повлиять на архитектуру:

1. Конкретный формат файлов для обмена с 1С (CSV, Excel, DBF, XML).
2. Финальный выбор очереди задач: Celery или RQ (и связанный стек мониторинга).
3. Стратегия хранения истории:
   - глубина хранения старых расчётов цен;
   - объём логов и политика архивирования.
4. Наличие/отсутствие отдельной админ-панели (веб-интерфейса) для управления стратегиями и выгрузками
   (на первых этапах может отсутствовать, управление через файлы/конфиги).

Все решения по этим пунктам должны быть зафиксированы в отдельные задачи в `docs/plan.md` и реализовываться по мере развития проекта.
