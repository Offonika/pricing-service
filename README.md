# Умное ценообразование Master Mobile

Сервис для расчёта рекомендованных цен на основе данных 1С, парсера конкурентов и правил из PRD. Проектный контекст:
- Бизнес-требования и сценарии: `docs/PRD.md`
- High-level архитектура: `docs/architecture.md`
- OpenAPI-контракт: `openapi.yaml`
- План задач: `docs/plan.md`
- Стратегии ценообразования (MVP): `docs/price-strategies.md`
- Release Notes (MVP): `docs/RELEASE_NOTES.md`

Разработка ведётся по правилам `docs/constitution.md`, задачи детализируются в `tasks/`.

## Быстрый старт для разработчика

1) Python 3.12+. Создайте окружение:
```bash
python -m venv .venv
source .venv/bin/activate
```
2) Установите зависимости (включая dev):  
```bash
./.venv/bin/pip install -e .[dev]
```
3) Линтеры и тесты:
```bash
./.venv/bin/python -m ruff check .
./.venv/bin/python -m black --check .
./.venv/bin/python scripts/export_openapi.py --check
./.venv/bin/python -m pytest
```
4) Запуск приложения в dev-режиме (после реализации каркаса в S1.1):  
```bash
./.venv/bin/python -m uvicorn app.main:app --reload
```
Конфигурация берётся из переменных окружения (см. `.env.example`), Pydantic Settings.

Важно: для всех Python-команд в проекте используйте локальное окружение `.venv`, а не системный `python`/`pip`.

## UI сопоставления (MVP каркас)
- Код: `ui/` (Vite + React + TS + AG Grid + React Query + Zustand).
- Запуск:  
  ```bash
  cd ui
  npm install
  npm run dev
  ```
- Env для фронта: создайте `ui/.env.local` (см. пример `ui/.env.example`) с `VITE_API_BASE_URL=http://localhost:18080/api` и одним из вариантов авторизации:
  - Basic (рекомендуется, если включено на backend):  
    `VITE_API_BASIC_USER=<user>`  
    `VITE_API_BASIC_PASSWORD=<password>`
  - или Bearer: `VITE_API_TOKEN=<token>` (если backend настроен на Bearer).
- CORS для backend: задайте `CORS_ALLOW_ORIGINS=http://localhost:5173` в корневом `.env`.

## Docker Compose (dev)
```bash
cp .env.example .env
cd infra && docker-compose up --build
```
Сервисы: FastAPI (`http://localhost:${APP_PORT:-18080}`), PostgreSQL (`localhost:${POSTGRES_PORT:-55432}`), Redis (`localhost:${REDIS_PORT:-16379}`). При конфликте портов можно поменять значения в `.env`. После запуска API healthcheck: `curl http://localhost:${APP_PORT:-18080}/health`.

### Переменные окружения
- App: `APP_PORT`, `ENVIRONMENT`, `LOG_LEVEL`
- DB: `POSTGRES_*`, `DATABASE_URL`
- Redis: `REDIS_URL`
- Competitors: `COMPETITOR_SOURCE_MODE` (zenno/internal), `COMPETITOR_PARSE_LIMIT`, `PROXY_API_URL`, `PROXY_API_TOKEN`, `PROXY_TIMEOUT_SECONDS`, `PROXY_MAX_RETRIES`, `PROXY_RPS_LIMIT`, `COMPETITOR_FTP_IMPORT_ENABLED`, `COMPETITOR_FTP_HOST`, `COMPETITOR_FTP_PORT`, `COMPETITOR_FTP_USER`, `COMPETITOR_FTP_PASSWORD`, `COMPETITOR_FTP_TLS`, `COMPETITOR_FTP_TIMEOUT_SEC`, `COMPETITOR_FTP_SOURCES`, `COMPETITOR_FTP_MAX_FILES_PER_SOURCE`
- Scraper headers: `COMPETITOR_USER_AGENT`, `COMPETITOR_ACCEPT_LANGUAGE`, `COMPETITOR_COOKIES`
- LLM/матчинг: `OPENAI_API_KEY` (и при необходимости `OPENAI_API_BASE`, `OPENAI_MODEL`), локальная LLM: `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_CHAT_MODEL`
- Embeddings/матчинг: `EMBEDDINGS_MODEL`, `EMBEDDINGS_BATCH_SIZE`, `EMBEDDINGS_DIR`, `MATCHING_TOP_K`, `MATCHING_TOP_K_LLM`, `MATCHING_MIN_LLM_CONFIDENCE`, `MATCHING_MIN_EMBED_SCORE`, `MATCHING_MIN_GAP`
- Мониторинг новинок смартфонов: `SMARTPHONE_RELEASES_ENABLED`, `SMARTPHONE_NEWS_API_BASE_URL`, `SMARTPHONE_NEWS_API_KEY`, `SMARTPHONE_NEWS_LANGUAGE`, `SMARTPHONE_NEWS_QUERY`, `SMARTPHONE_NEWS_DAYS_BACK`, `SMARTPHONE_NEWS_PAGE_SIZE`, `SMARTPHONE_RELEASE_REQUEST_DELAY_SECONDS`, `SMARTPHONE_RELEASE_LLM_MODEL`
- Еженедельный digest для закупок: `WEEKLY_BUYER_DIGEST_ENABLED`, `WEEKLY_BUYER_DIGEST_MODEL`, `WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_TOKEN`, `WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_CHAT_ID`
- Еженедельный Excel-отчет по личным продажам менеджеров: `WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_TOKEN`, `WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_CHAT_ID` (можно перечислить несколько `chat_id` через запятую)
- Яндекс.Директ (спрос по ключам): `FEATURE_YANDEX_DEMAND_ENABLED`, `YANDEX_DIRECT_API_TOKEN`, `YANDEX_DIRECT_API_BASE_URL`, `YANDEX_DEFAULT_REGION`, `YANDEX_DIRECT_TIMEOUT`, `YANDEX_DIRECT_BATCH_SIZE`, `YANDEX_DEMAND_DAYS_WINDOW`
- Обновление спроса: `YANDEX_DEMAND_UPDATE_LIMIT`, `YANDEX_DEMAND_STALENESS_DAYS`, `YANDEX_DIRECT_RPS_LIMIT`
- Wordstat API: `YANDEX_WORDSTAT_ENABLED`, `YANDEX_WORDSTAT_BASE_URL`, `YANDEX_WORDSTAT_DEVICES` (по умолчанию `all`)
- Если используете keywordsresearch API, добавьте `YANDEX_DIRECT_CLIENT_LOGIN=<логин рекламодателя>` — он попадёт в заголовок `Client-Login`.
- Telegram: `TELEGRAM_BOT_TOKEN` (при использовании бота), `TELEGRAM_WEBHOOK_URL` (если нужен webhook)
- Логистика: `LOGISTICS_INTERNAL_API_TOKEN` для всех внутренних `/api/logistics/*`, `LOGISTICS_BOT_TOKEN`, `LOGISTICS_BOT_WEBHOOK_URL`, `LOGISTICS_BOT_WEBHOOK_SECRET`

### Логистический Telegram-бот
- Внутренние logistics endpoint'ы (`/api/logistics/*` и operational routes `/api/logistics/bot/*`, кроме `health` и Telegram webhook) требуют `Authorization: Bearer <LOGISTICS_INTERNAL_API_TOKEN>`.
- При активном webhook (`LOGISTICS_BOT_WEBHOOK_URL`) отдельный polling-сервис `pricing-logistics-bot.service` не обязателен; webhook остаётся основным production-режимом.

Для FTP-прайсов конкурентов (poiskzip-moba, poiskzip-liberti) задайте хост/доступ и список источников:  
`COMPETITOR_FTP_SOURCES=moba:poiskzip-moba:moba-{date}.xlsx,liberti:poiskzip-liberti:liberti-1-{date}.xlsx`. Job `./.venv/bin/python -m tasks.import_competitor_ftp` подключается к FTP (опционально TLS), ищет датированные файлы, валидирует колонки (`group, sku, name, price_opt, price_roz, link, time`, плюс `amount`/`stock`), пишет сырые строки и нормализованные записи в БД, дедуплицируя по `(source, file_date)`. Цепочка ZenLogs отключена.

Матчинг цен конкурентов к товарам: `./.venv/bin/python -m tasks.match_competitor_ftp` — сопоставляет `competitor_ftp_record.sku` с `product.article` (нормализует артикул), пишет цены в `competitor_price` и связи в `product_match`, логируя unmatched/ambiguous.

### Ночной контур SKU и Предмета для УТ 10.3
- Ручной запуск: `./.venv/bin/python -m tasks.generate_product_skus --write --export-existing --write-ready --mode apply --approved-by pricing-service-nightly --message-id sku-nightly-$(date +%Y%m%d%H%M%S) --changed-at $(date +%F)`.
- Cron wrapper: `infra/cron/sku_generation_ut103.sh`; расписание: `infra/cron/sku_generation_ut103.cron` (`02:30` Europe/Moscow).
- Контур берёт только активные товары, не помеченные на удаление. Новым товарам рассчитывает `planned_sku`, а уже рассчитанные товары со статусом `missing_in_1c` отправляет в файловый обмен УТ 10.3.
- Для записи в 1С используется универсальный пакет `nomenclature_property_updates.v1`: `TargetKind=requisite`, `PropertyName=SKU`, `ValueType=string`.
- Следом этот же cron запускает `tasks.build_missing_onec_subject_updates`: ищет в живой 1С строки с пустым свойством `Предмет`, берёт `subject_generated` или классифицирует по названию и отправляет `PropertyName=Предмет`, `ValueType=property_value`.
- Ответы 1С по SKU подтягивает `tasks.apply_ut103_sku_results`: успешные `applied/already_actual` обновляют `fact_sku` и `sku_sync_status`, ошибки сохраняются в `sku_sync_error`.
- Cron wrapper для обратной синхронизации: `infra/cron/sku_result_sync_ut103.sh`; расписание: `infra/cron/sku_result_sync_ut103.cron` (каждый час в `:45` Europe/Moscow).
- Настройки: `UT103_EXCHANGE_ROOT` или `SKU_GENERATION_UT103_EXCHANGE_ROOT`, `SKU_GENERATION_UT103_MODE` (`apply` по умолчанию), `SKU_GENERATION_UT103_APPROVED_BY`, `SKU_GENERATION_UT103_PROPERTY_NAME`, `SKU_GENERATION_UT103_SUBJECT_ENABLED`, `SKU_GENERATION_UT103_SUBJECT_LIMIT`.

### Ночной контур Статуса ассортимента для УТ 10.3
- Ручной запуск: `./.venv/bin/python -m tasks.refresh_assortment_lifecycle_classification --write-ready --allow-empty --export-mode apply --approved-by pricing-service-nightly --message-id assortment-lifecycle-nightly-$(date +%Y%m%d%H%M%S) --json`.
- Cron wrapper: `infra/cron/assortment_lifecycle_ut103_export.sh`; расписание: `infra/cron/assortment_lifecycle_ut103_export.cron` (`03:20` Europe/Moscow).
- Контур заново рассчитывает жизненный статус ассортимента, обновляет Postgres-снимок и отправляет в 1С готовые свойства через `nomenclature_property_updates.v1`.
- Пилотный лимит live-выборки по умолчанию: `ASSORTMENT_LIFECYCLE_LIMIT=600`; поднять можно через env или `--limit` после проверки времени 1С-запросов.
- В пакет попадают `Статус ассортимента`, причина, дата, источник, а также связанные свойства вроде `Профиль закупочного поведения`, `Коммерческие признаки` и реквизиты эксклюзивности, если для них есть проверенные данные.
- Витрина `procurement_feature_snapshot.v1` обогащается из живой карточки 1С, регистра свойств номенклатуры и `product` / `productcompatibility`; для дисплеев без заполненного свойства из имени восстанавливаются только предмет, бренд и модель, качество остается обязательным контролируемым заполнением.
- Качество витрины признаков закупки после refresh: `./.venv/bin/python -m tasks.report_procurement_feature_snapshot_quality --folder дисплеи --only-missing --json`. CSV по умолчанию пишется в `reports/assortment_lifecycle/<date>/procurement-feature-snapshot-quality.csv`.
- Кандидаты на заполнение пустого свойства `Качество`: `./.venv/bin/python -m tasks.build_missing_display_quality_updates --folder дисплеи --allow-empty --json`. Задача пишет review CSV и JSON update-строк; качество из полей карточки 1С берется только при явном маркере и возрасте карточки не больше 183 дней. Если по карточке сначала нужно решить закупочный статус, код исключается через `config/assortment/display-quality-status-review-exclusions.json`; решения по фактам жизни товара фиксируются в `config/assortment/display-fact-status-decisions.json`. Для аудита исключенных строк используйте `--include-status-review-required`.
- Формат ручного маппинга качества: `{"items":[{"nomenclature_code":"РБ000022719","quality_raw":"Medium","reason":"Проверено ответственным за папку","approved_by":"Омар"}]}`. После проверки можно получить dry-run XML через `--quality-map-json <path> --print-xml`.
- Настройки: `UT103_EXCHANGE_ROOT` или `ASSORTMENT_LIFECYCLE_UT103_EXCHANGE_ROOT`, `ASSORTMENT_LIFECYCLE_UT103_MODE` (`apply` по умолчанию), `ASSORTMENT_LIFECYCLE_UT103_APPROVED_BY`, `ASSORTMENT_LIFECYCLE_UT103_SOURCE`, `ASSORTMENT_LIFECYCLE_UT103_OVERWRITE`.

### Черновики заказов поставщику для УТ 10.3
- Ручной запуск: `./.venv/bin/python -m tasks.export_ut103_procurement_supplier_orders --mode apply --approved-by "Омар" --input-json supplier-order.json --json`.
- Используется тот же файловый корень `UT103_EXCHANGE_ROOT`, что и для свойств номенклатуры; файл пишется как `to_1c/new/procurement_supplier_orders_<message_id>.ready.xml`.
- Контракт: `procurement_onec_file_exchange.v1`. Сервис передает поставщика, договор, валюту, склад, дату заказа, контур закупки, строки товаров, ссылку на Bitrix-карточку, ID подтверждения и ID расчета.
- Безопасность: `apply` требует `ApprovedBy`, заказ всегда идет с `DraftOnly=true`; 1С-обработка должна создать только непроведенный черновик `ЗаказПоставщику`.
- Ответы 1С читаются через `./.venv/bin/python -m tasks.export_ut103_procurement_supplier_orders --exchange-root <path> --list-results`.

### MVP пайплайна LLM + embeddings (catalog competitor_item)
1) LLM-разбор атрибутов:
   ```bash
   LOCAL_LLM_BASE_URL=http://10.20.2.4:1234 \
   LOCAL_LLM_CHAT_MODEL=qwen2-7b-instruct \
   ./.venv/bin/python -m tasks.extract_competitor_attrs --only-null
   ```
2) Эмбеддинги:
   ```bash
   ./.venv/bin/python -m tasks.compute_embeddings --target products
   ./.venv/bin/python -m tasks.compute_embeddings --target competitors
   ```
3) Матчинг (suggested/needs_review/ambiguous):
   ```bash
   ./.venv/bin/python -m tasks.match_competitor_items_embeddings --only-null
   ```
   С LLM-арбитром:
   ```bash
   LOCAL_LLM_BASE_URL=http://10.20.2.4:1234 \
   LOCAL_LLM_CHAT_MODEL=qwen2-7b-instruct \
   ./.venv/bin/python -m tasks.match_competitor_items_embeddings --use-llm-arbiter
   ```
Флаги перезапуска: `--overwrite`, `--force`, `--dry-run`, `--limit`, `--top-k`, `--top-k-llm`, `--min-embed-score`, `--min-gap`, `--min-llm-confidence`, `--only-bad`, `--only-open`, `--samples-file`, `--report-file`, `--report-csv` (CSV содержит нормализованные/parsed поля и best-product метаданные).

### BI / аналитика
- Витрины спроса по моделям телефонов описаны в `docs/BI.ModelDemand.md` (представления для Power BI/Metabase и REST-эндпоинты `/api/analytics/*`).

### Тестовый запуск агента (локально)
1) Заполните `.env` (или экспортируйте переменные):
   - `FEATURE_YANDEX_DEMAND_ENABLED=true` (если хотите сразу тянуть спрос);
   - `YANDEX_DIRECT_API_TOKEN=<токен>` и опционально `YANDEX_DEFAULT_REGION`, `YANDEX_DIRECT_TIMEOUT`, `YANDEX_DIRECT_BATCH_SIZE`, `YANDEX_DIRECT_RPS_LIMIT`;
   - базовые `APP_PORT`, `DATABASE_URL`.
2) Примените миграции: `./.venv/bin/python -m alembic upgrade head`.
3) Запустите API: `./.venv/bin/python -m uvicorn app.main:app --reload`.
4) Отправьте модель телефона:  
   ```bash
   curl -X POST http://localhost:${APP_PORT:-8000}/api/agents/devices/models \
     -H "Content-Type: application/json" \
     -d '{"brand":"Samsung","model_name":"Galaxy S26","variant":"Ultra","announce_date":"2026-02-10","release_date":"2026-03-01","screen":{"size_inch":6.8,"technology":"AMOLED","refresh_rate_hz":120}}'
   ```
5) Сгенерируйте ключи внутри бекенда:  
   ```bash
   curl -X POST "http://localhost:${APP_PORT:-8000}/api/agents/keywords/generate?phone_model_id=<id>"
   ```
   или сохраните готовые фразы:  
   ```bash
   curl -X POST http://localhost:${APP_PORT:-8000}/api/agents/keywords/bulk \
     -H "Content-Type: application/json" \
     -d '{"phone_model_id":<id>,"phrases":["дисплей galaxy s26 ultra","экран galaxy s26 ultra купить"],"language":"ru","category":"display"}'
   ```
6) Обновите спрос (если есть токен):  
   ```bash
  ./.venv/bin/python -c "from app.workers.demand import update_stale_keyword_demand; print(update_stale_keyword_demand())"
   ```
Примеры payload см. в `samples/agent_requests.http`.

### Агент новинок (LLM)
- Конфиг источников: `config/agents/sources.yaml` (бренд → список URL, критерии анонса).
- Запуск агента (требуется `OPENAI_API_KEY`):  
  ```bash
  ./.venv/bin/python agents/discovery.py --config config/agents/sources.yaml --api-base http://localhost:19000
  ```
- Агент читает URL, извлекает brand/model/variant/даты/экран через OpenAI, отправляет в `/api/agents/devices/models`. Опционально генерация ключей делается на backend через `/api/agents/keywords/generate`.

### Мониторинг новинок смартфонов (News API + RSS)
- Настройте `.env`: `SMARTPHONE_RELEASES_ENABLED=true`, `SMARTPHONE_NEWS_API_BASE_URL`, `SMARTPHONE_NEWS_API_KEY`, `SMARTPHONE_NEWS_LANGUAGE`, `SMARTPHONE_NEWS_QUERY`, `SMARTPHONE_NEWS_DAYS_BACK`, `SMARTPHONE_NEWS_PAGE_SIZE`, `SMARTPHONE_NEWS_MAX_PAGES`, `SMARTPHONE_NEWS_MAX_ITEMS`, `OPENAI_API_KEY`.
- Для RSS-источника GSMArena включите `SMARTPHONE_GSMARENA_ENABLED=true` и при необходимости переопределите `SMARTPHONE_GSMARENA_RSS_URL`, `SMARTPHONE_GSMARENA_MAX_ITEMS`.
- Примените миграции: `./.venv/bin/python -m alembic upgrade head`.
- Запуск фоновой задачи (под cron или вручную):  
  ```bash
  ./.venv/bin/python -m tasks.update_smartphone_releases
  ```
  Либо подключите готовый cron-файл `infra/cron/smartphone_releases.cron` (он вызывает скрипт `infra/cron/update_smartphone_releases.sh`, который сам подхватывает `.env` и пишет лог в `/var/log/pricing/smartphone_releases.log`).
- Job опрашивает News API и RSS, прогоняет новости через LLM и пишет dedup-результат в таблицу `smartphone_releases`. Проверить успешность можно по выводу `./.venv/bin/python -m tasks.update_smartphone_releases` или по логу cron (`tail -f /var/log/pricing/smartphone_releases.log`).
- Если в статье перечислено несколько моделей, нормализатор вернёт массив `models`, и сервис создаст по одной записи на каждую модель (источник будет иметь суффикс `#1`, `#2`, чтобы сохранить уникальность URL).
- Чтобы получать уведомления в Telegram, задайте `SMARTPHONE_RELEASES_ALERT_TELEGRAM_TOKEN=<bot_token>` и `SMARTPHONE_RELEASES_ALERT_TELEGRAM_CHAT_ID=<chat_id>` — после каждого прогона cron вызовет `infra/cron/telegram_alert.py` и отправит сводку (fetched/processed/errors) в указанный чат или группу.

### Еженедельный обзор новинок для закупщиков
- Фича-флаг и модель LLM: `WEEKLY_BUYER_DIGEST_ENABLED=true`, опционально `WEEKLY_BUYER_DIGEST_MODEL` (по умолчанию `gpt-5.1`). Используется тот же `OPENAI_API_KEY`/`OPENAI_API_BASE`.
- Запуск вручную: `./.venv/bin/python -m tasks.generate_weekly_buyer_digest` — берёт записи из `smartphone_releases` за последние 7 дней (announcement_date/market_release_date, статусы announced/released), генерирует Markdown-обзор и сохраняет/обновляет уникальную запись в `weekly_smartphone_digest`. Дополнительно сохраняет артефакты в `data/digests/weekly/<week_end>.md` и `...png` (требуется `pillow`).
- Если релизов нет, создаётся короткое сообщение «значимых анонсов нет», без вызова LLM.
- Проверка результата: `SELECT week_start, week_end, content FROM weekly_smartphone_digest ORDER BY week_end DESC LIMIT 1;`
- Cron: `infra/cron/weekly_buyer_digest.sh` (шаблон расписания `infra/cron/weekly_buyer_digest.cron`).
- Telegram-уведомление (отдельно от дневного алерта): задайте `WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_TOKEN` и `WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_CHAT_ID` (можно переиспользовать токен/чат от ежедневного алерта). Скрипт `infra/cron/weekly_buyer_digest_alert.py` отправит: `sendPhoto` с PNG-анонсом, `sendDocument` с файлом `.md`, и короткий тизер (<=4096 символов). Хвост логов шлётся только при ошибке.

### Еженедельный Excel-отчет по личным продажам менеджеров
- Запуск вручную: `./.venv/bin/python -m tasks.send_weekly_manager_sales_report` — по умолчанию берёт последнюю полностью закрытую неделю из `onec_sales_daily_kpi`, сравнивает её с предыдущей неделей и формирует Excel в `reports/sales/weekly/<week_end>/Личные продажи менеджеров <week_start>-<week_end>.xlsx`.
- Если нужен конкретный период, можно передать любую дату внутри нужной недели: `./.venv/bin/python -m tasks.send_weekly_manager_sales_report --date 2026-04-01`.
- В файле есть листы `Сводка`, `Дашборд`, `Личные продажи`, `Зона внимания`, `Продажи по магазинам`, `РКО излишек-недостача`; все листы отформатированы с `freeze panes`, автофильтрами, числовыми форматами и подсветкой дельт/сигналов.
- Во вложении `Долги сотрудников ...xlsx` теперь есть отдельная вкладка `Реализации и списания`: она показывает по текущим employee-контрагентам документы роста долга из нормализованного `receivable_ledger_event` с типом `Реализация на контрагента` или `Списание / корректировка`.
- Прямой Telegram-режим на сервере `A` оставлен как ручной fallback: задайте `WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_TOKEN` и `WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_CHAT_ID`, затем запускайте `./.venv/bin/python -m tasks.send_weekly_manager_sales_report --send-telegram`. Скрипт отправит weekly sales report и вложение `Долги сотрудников ...xlsx`; в `...CHAT_ID` можно перечислить несколько получателей через запятую.
- Legacy cron на `A`: `infra/cron/weekly_manager_sales_report.sh` (шаблон расписания `infra/cron/weekly_manager_sales_report.cron`).
- Внутренний API для сервера `B`:
  - `GET /api/management/weekly-manager-sales-report/health?week_end=YYYY-MM-DD`
  - `GET /api/management/weekly-manager-sales-report?week_end=...`
  - `GET /api/management/weekly-manager-sales-report/sales?week_end=...`
  - `GET /api/management/weekly-manager-sales-report/employee?week_end=...`
- Сервер `B` / доставка через Openclaw:
  ```bash
  python infra/cron/weekly_manager_sales_reports_from_a.py --week-end 2026-04-05
  python infra/cron/weekly_manager_sales_reports_from_a.py --week-end 2026-04-05 --dry-run --json
  ```
- Основные env для доставки на `B`: `WEEKLY_MANAGER_SALES_B_TELEGRAM_TOKEN`, `WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID`, `WEEKLY_MANAGER_SALES_STATE_PATH`, `WEEKLY_MANAGER_SALES_REPORT_DIR`.
- Для раздельных получателей по артефактам можно задать scoped env: `WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID_SALES`, `WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID_EMPLOYEE`. Если scoped env не задан, используется общий список `WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID`.
- Bridge тянет единый weekly bundle, проверяет checksum обоих XLSX, дедуплицирует по `report_key + revision` и при изменении checksum доставляет корректирующую версию.

### Утренняя новая дебиторка
- Источник на сервере `A`: `GET /api/receivables/new-daily?date=YYYY-MM-DD`.
- Для ролей `ceo`, `cco`, `coo`, `cfo`, `development_director`, `retail_director`, `retail_network_head` утренний management digest дополнительно:
  - показывает блок `Розница, открытый месяц YYYY-MM` по текущему MTD-диапазону;
  - тянет monthly KPI закрытого месяца из `GET /api/management/retail-director-monthly-kpi?month=YYYY-MM`.
- Для всех ролей утренний management digest тянет месячную эффективность выполнения задач сотрудников из
  `GET /api/management/task-efficiency?month=YYYY-MM`. Источник истины - витрина
  `mm-compensation.reconciliation.bitrix_fact_employee_task_kpi_monthly` с метрикой
  `personal_tasks_on_time_share`, а при наличии raw-задач используется Bitrix-like расчет как в разделе
  `Задачи и Проекты -> Эффективность`: `100 - (замечания / всего в работе) * 100`.
  В текст попадает сводка `% / в работе / завершено / замечаний` и короткий список по сотрудникам,
  полный payload доступен ассистенту в JSON.
- Env для этого bridge: `MANAGEMENT_TASK_EFFICIENCY_DATABASE_URL` (если не задан, используется
  `TELEPHONY_MDM_DATABASE_URL`), `MANAGEMENT_TASK_EFFICIENCY_SCHEMA`,
  `MANAGEMENT_TASK_EFFICIENCY_SOURCE_SCOPE`, `MANAGEMENT_TASK_EFFICIENCY_LOW_THRESHOLD_PCT`.
- Контур `new_daily` больше не создаёт Bitrix24-задачи через `/api/management/task-payloads`; вместо этого сервер `B` формирует утренний XLSX и отправляет его в Telegram как вложение.
- Сервер `B` / доставка через Openclaw:
  ```bash
  python infra/cron/new_daily_receivables_from_a.py --date 2026-03-20
  python infra/cron/new_daily_receivables_from_a.py --date 2026-03-20 --dry-run --json
  ```
- Основные env для доставки на `B`: `MANAGEMENT_NEW_DAILY_TELEGRAM_TOKEN`, `MANAGEMENT_NEW_DAILY_TELEGRAM_CHAT_ID`, `MANAGEMENT_NEW_DAILY_STATE_PATH`, `MANAGEMENT_NEW_DAILY_REPORT_DIR`.
- Допустимые fallback для токена/чата: `WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_TOKEN`, `WEEKLY_MANAGER_SALES_B_TELEGRAM_TOKEN`, `TELEGRAM_TOKEN_MM` и соответствующие chat id.
- Bridge дедуплицирует отправку по `date + revision`; если состав XLSX на ту же дату изменился, доставляет корректирующую версию.

### Замороженные weekly KPI-отчеты
- Миграция публикационного контура: `./.venv/bin/python -m alembic upgrade head`.
- Сервер `A`: `mm-compensation` пишет `draft` snapshots в таблицы `weekly_kpi_report_snapshot` и `weekly_kpi_report_metric_snapshot`; `pricing-service` публикует их и обслуживает read-only API `/api/management/weekly-kpi-reports*`.
- Ручная публикация `draft -> published`:  
  ```bash
  ./.venv/bin/python -m tasks.publish_weekly_kpi_reports --week-end 2026-04-05
  ```
- Сборка frozen XLSX-артефактов после публикации:
  ```bash
  ./.venv/bin/python -m tasks.build_weekly_kpi_artifacts --week-end 2026-04-05
  ```
- Внутренний API для сервера `B`:
  - `GET /api/management/weekly-kpi-reports/health?week_end=YYYY-MM-DD`
  - `GET /api/management/weekly-kpi-reports?week_end=...`

### Телефония для unified reports
- Сервер `A`: sync полного mapping `1С user -> computer -> extension` в snapshot-таблицу:
  ```bash
  ./.venv/bin/python -m tasks.sync_telephony_mapping --snapshot-date 2026-04-18 --export-dir reports/telephony
  ```
- Для битрикс-сопоставления sync использует:
  - `ONEC_DATABASE_URL` для `1С` (`_Reference69`, `_Reference94`, `_Reference80`, `_Reference68`, `_Reference9471_VT9487`);
  - `TELEPHONY_MDM_DATABASE_URL` для `mm_comp_piecework.reconciliation.employee_master_map`.
- Дополнительные env для production-проекции:
  - `TELEPHONY_SERVICE_LINE_LABELS` — JSON-словарь service/admin-линий, которых нет в retail-проекции 1С;
  - `TELEPHONY_REVIEW_LINE_IDS` — список линий, которые не надо автоматически деактивировать до ручного разбора.
- Internal API на `A`:
  - `GET /api/management/telephony/health?date=YYYY-MM-DD`
  - `GET /api/management/telephony/employee-line-map?snapshot_date=YYYY-MM-DD`
  - `GET /api/management/telephony/retail-line-map?snapshot_date=YYYY-MM-DD&active_only=true`
- `employee-line-map` отдаёт полный user-level snapshot, а `retail-line-map` строит line-level projection под контракт `Openclaw retail_line_map`.
- Правила projection:
  - один активный Bitrix-пользователь на линии -> `telephony_user_<bitrix_user_id>`;
  - shared-линия -> `telephony_line_<extension>`;
  - service/admin overlay берётся из `TELEPHONY_SERVICE_LINE_LABELS`, но не переопределяет уже найденные retail-линии;
  - review-линии из `TELEPHONY_REVIEW_LINE_IDS` не попадают в auto-deactivate.
- Сервер `B` / bridge для `retail_line_map`:
  ```bash
  ./.venv/bin/python infra/cron/telephony_line_map_from_a.py --snapshot-date 2026-04-18 --json
  ```
- Основные env для bridge на `B`: `TELEPHONY_LINE_MAP_SOURCE_URL`, `TELEPHONY_LINE_MAP_SOURCE_TOKEN`, `TELEPHONY_LINE_MAP_STATE_PATH`, `TELEPHONY_LINE_MAP_ARTIFACT_DIR`, `TELEPHONY_LINE_MAP_DEACTIVATE_MISSING`, `TELEPHONY_LINE_MAP_REVIEW_LINE_IDS`.
- Bridge сохраняет staging CSV и diff JSON по ревизии, обновляет `retail_line_map_stage`, считает diff с текущим production `retail_line_map`, дедуплицирует доставку по `snapshot_date + revision` и апсертит production map без ручной подготовки CSV.
- Для shadow-first rollout рекомендуемый стартовый режим на `B`: `TELEPHONY_LINE_MAP_DEACTIVATE_MISSING=false`. После review-окна и разбора review-линий можно переключать в `true`.
  - `GET /api/management/weekly-kpi-reports/{report_id}`
  - `GET /api/management/weekly-kpi-reports/{report_id}/artifact`
- Сервер `B` / доставка через Openclaw:
  ```bash
  python infra/cron/weekly_kpi_reports_from_a.py --week-end 2026-04-05
  python infra/cron/weekly_kpi_reports_from_a.py --week-end 2026-04-05 --dry-run --json
  ```
- Основные env для доставки: `WEEKLY_KPI_B24_WEBHOOK_URL`, `WEEKLY_KPI_B24_DISK_FOLDER_ID`, `WEEKLY_KPI_B24_TARGET_MODE`, `WEEKLY_KPI_STATE_PATH`, `WEEKLY_KPI_REPORT_DIR`. Адаптер тянет только `published + eligible + artifact_ready` manifests, дедуплицирует по `report_key + revision`, скачивает frozen XLSX и строит обзор только из `summary_payload`.
- Для weekly KPI роли `retail_director` и `retail_network_head` Openclaw дополнительно подтягивает блок закрытого месяца из `GET /api/management/retail-director-monthly-kpi?month=YYYY-MM` и дописывает в сопроводительное сообщение breakdown по потерям и премиальной части.
- Для Арсена добавлен ежемесячный Excel по клиентским типам цен: сервер `A` отдаёт `GET /api/management/retail-customer-price-type-recommendations?month=YYYY-MM`, а сервер `B` запускает `infra/cron/retail_price_type_recommendations_from_a.py` и отправляет XLSX через Telegram-ассистента. Отчёт берёт только группу 1С `ПОКУПАТЕЛИ`, выводит код 1С `РБ...`, текущий тип цен читает из реквизита договора, показывает чистые продажи текущего и прошлого месяца, возвраты и изменение к прошлому месяцу, и подсказывает, кому поставить `Серебро`, кому `Золото`, кого понизить до `Серебра` или `Бронзы`.
- Расписание на сервере `B`: `1` число каждого месяца в `10:20`, `12:20` и `15:20` MSK. Скрипт идемпотентный по state, поэтому повторные попытки в тот же день нужны только как страховка, если сервер `A` ещё не отдал закрытый месяц.
- Основные env для этого отчёта: `RETAIL_PRICE_TYPE_B_TELEGRAM_TOKEN`, `RETAIL_PRICE_TYPE_B_TELEGRAM_CHAT_ID`, `RETAIL_PRICE_TYPE_STATE_PATH`, `RETAIL_PRICE_TYPE_REPORT_DIR`. Допустимые fallback для токена/чата: `RETAIL_PRICE_TYPE_TELEGRAM_TOKEN`, `RETAIL_PRICE_TYPE_ASSISTANT_TELEGRAM_TOKEN`, `WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_TOKEN`, `WEEKLY_MANAGER_SALES_B_TELEGRAM_TOKEN`, `TELEGRAM_TOKEN_MM` и соответствующие chat id.
- Операционная проверка сервера `B` делается по SSH в удалённый Openclaw-контур. Не ориентируйтесь на наличие `/home/deploy/.openclaw` в локальной рабочей копии `pricing-service`: локально этот каталог может отсутствовать, а боевой cron/log/state живут на другом сервере.
- Важное правило для канала Telegram/Openclaw: итоговый обзор, сообщение ассистента и сопроводительный текст к weekly KPI-отчету должны формироваться на русском языке. Не смешивать русский и английский без отдельного требования бизнеса.

## CI
GitHub Actions (`.github/workflows/ci.yml`) гоняет `ruff check .`, `black --check .` и `pytest` на push/PR в `main`.

## Миграции (Alembic)
- Генерация новой миграции:  
  `DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/pricing ./.venv/bin/python -m alembic revision --autogenerate -m "message"`
- Применение миграций:  
  `DATABASE_URL=... ./.venv/bin/python -m alembic upgrade head`

## Тесты
- Запуск всех тестов: `./.venv/bin/python -m pytest`
- Базовые интеграционные проверки: `/health` и операции с БД на SQLite (см. `tests/`).
- Каталог и остатки берём напрямую из базы 1С (Ekama SQL-подключение), отдельного файлового импортера нет.

## Конкурентный матчинги нормализация моделей
- При любом изменении в `app/services/competitor_matching.py` обновляйте `docs/competitor_matching.md`.
- Тесты, которые нужно поддерживать в актуальном виде: `tests/test_model_parser.py`, `tests/test_competitor_matching.py` (`./.venv/bin/python -m pytest tests/test_model_parser.py -q && ./.venv/bin/python -m pytest tests/test_competitor_matching.py -q`).
- Очистка мусорных моделей: `./.venv/bin/python -m tasks.cleanup_phone_models --brand <brand> [--no-dry-run] [--rerun-matching]`.

Доступные фильтры в tasks.match_competitor_items (каталог competitor_item):

--source moba — по полю competitor.
--category-contains дисплеи — ILIKE по category.
--name-contains iphone — ILIKE по name.
--parsed-model-contains 5 — ILIKE по parsed_device_model (можно выбрать кривые модели для переобработки).
--missing-parsed — только записи с пустым parsed_device_brand или parsed_device_model.
--limit N — ограничить выборку.
--overwrite — перезаписывать parsed_device_* даже если заполнены.
LLM:
--llm — включить, --force-llm — вызывать даже при высокой уверенности парсера.
--llm-limit — максимум вызовов (0 = без лимита).
--llm-threshold — звать LLM, если confidence ниже порога (при force не важно).
Env: LOCAL_LLM_BASE_URL, LOCAL_LLM_CHAT_MODEL; промт можно править в config/prompts/llm_parse_phone_model.txt или через PROMPT_LLM_PARSE_FILE/PROMPT_LLM_PARSE_TEXT.
Пример использования с несколькими фильтрами и перезаписью:

LOCAL_LLM_BASE_URL=http://10.20.2.4:1234 \
LOCAL_LLM_CHAT_MODEL=qwen2-7b-instruct \
./.venv/bin/python -m tasks.match_competitor_items \
  --source moba \
  --category-contains дисплеи \
  --name-contains iphone \
  --parsed-model-contains 5 \
  --limit 10\
  --llm --force-llm --llm-limit 100 --llm-threshold 1.0 \
  --overwrite
Если нужно “пройтись по всем дисплеям, даже уже заполненным” — уберите --missing-parsed и оставьте --overwrite/--force-llm.

LOCAL_LLM_BASE_URL=http://10.20.2.4:1234 \
LOCAL_LLM_CHAT_MODEL=qwen2-7b-instruct \
./.venv/bin/python -m tasks.match_competitor_items \
  --source moba \
  --category-contains дисплеи \
  --name-contains Дисплей \
  --parsed-model-contains A2221 \
  --llm --force-llm --llm-limit 100 --llm-threshold 1.0 \
  --overwrite
