# Умное ценообразование Master Mobile

Сервис для расчёта рекомендованных цен на основе данных TopControl, парсера конкурентов и правил из PRD. Проектный контекст:
- Бизнес-требования и сценарии: `docs/PRD.md`
- High-level архитектура: `docs/architecture.md`
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
pip install -e .[dev]
```
3) Линтеры и тесты:
```bash
ruff .
black --check .
pytest
```
4) Запуск приложения в dev-режиме (после реализации каркаса в S1.1):  
```bash
uvicorn app.main:app --reload
```
Конфигурация берётся из переменных окружения (см. `.env.example`), Pydantic Settings.

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
- LLM/матчинг: `OPENAI_API_KEY` (и при необходимости `OPENAI_API_BASE`, `OPENAI_MODEL`)
- Мониторинг новинок смартфонов: `SMARTPHONE_RELEASES_ENABLED`, `SMARTPHONE_NEWS_API_BASE_URL`, `SMARTPHONE_NEWS_API_KEY`, `SMARTPHONE_NEWS_LANGUAGE`, `SMARTPHONE_NEWS_QUERY`, `SMARTPHONE_NEWS_DAYS_BACK`, `SMARTPHONE_NEWS_PAGE_SIZE`, `SMARTPHONE_RELEASE_REQUEST_DELAY_SECONDS`, `SMARTPHONE_RELEASE_LLM_MODEL`
- Яндекс.Директ (спрос по ключам): `FEATURE_YANDEX_DEMAND_ENABLED`, `YANDEX_DIRECT_API_TOKEN`, `YANDEX_DIRECT_API_BASE_URL`, `YANDEX_DEFAULT_REGION`, `YANDEX_DIRECT_TIMEOUT`, `YANDEX_DIRECT_BATCH_SIZE`, `YANDEX_DEMAND_DAYS_WINDOW`
- Обновление спроса: `YANDEX_DEMAND_UPDATE_LIMIT`, `YANDEX_DEMAND_STALENESS_DAYS`, `YANDEX_DIRECT_RPS_LIMIT`
- Wordstat API: `YANDEX_WORDSTAT_ENABLED`, `YANDEX_WORDSTAT_BASE_URL`, `YANDEX_WORDSTAT_DEVICES` (по умолчанию `all`)
- Если используете keywordsresearch API, добавьте `YANDEX_DIRECT_CLIENT_LOGIN=<логин рекламодателя>` — он попадёт в заголовок `Client-Login`.
- Telegram: `TELEGRAM_BOT_TOKEN` (при использовании бота), `TELEGRAM_WEBHOOK_URL` (если нужен webhook)

Для FTP-прайсов конкурентов (poiskzip-moba, poiskzip-liberti) задайте хост/доступ и список источников:  
`COMPETITOR_FTP_SOURCES=moba:poiskzip-moba:moba-{date}.xlsx,liberti:poiskzip-liberti:liberti-1-{date}.xlsx`. Job `python -m tasks.import_competitor_ftp` подключается к FTP (опционально TLS), ищет датированные файлы, валидирует колонки (`group, sku, name, price_opt, price_roz, link, time`, плюс `amount`/`stock`), пишет сырые строки и нормализованные записи в БД, дедуплицируя по `(source, file_date)`. Цепочка ZenLogs отключена.

Матчинг цен конкурентов к товарам: `python -m tasks.match_competitor_ftp` — сопоставляет `competitor_ftp_record.sku` с `product.sku` (нормализует артикул), пишет цены в `competitor_price` и связи в `product_match`, логируя unmatched/ambiguous.

### BI / аналитика
- Витрины спроса по моделям телефонов описаны в `docs/BI.ModelDemand.md` (представления для Power BI/Metabase и REST-эндпоинты `/api/analytics/*`).

### Тестовый запуск агента (локально)
1) Заполните `.env` (или экспортируйте переменные):
   - `FEATURE_YANDEX_DEMAND_ENABLED=true` (если хотите сразу тянуть спрос);
   - `YANDEX_DIRECT_API_TOKEN=<токен>` и опционально `YANDEX_DEFAULT_REGION`, `YANDEX_DIRECT_TIMEOUT`, `YANDEX_DIRECT_BATCH_SIZE`, `YANDEX_DIRECT_RPS_LIMIT`;
   - базовые `APP_PORT`, `DATABASE_URL`.
2) Примените миграции: `alembic upgrade head`.
3) Запустите API: `uvicorn app.main:app --reload`.
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
   python -c "from app.workers.demand import update_stale_keyword_demand; print(update_stale_keyword_demand())"
   ```
Примеры payload см. в `samples/agent_requests.http`.

### Агент новинок (LLM)
- Конфиг источников: `config/agents/sources.yaml` (бренд → список URL, критерии анонса).
- Запуск агента (требуется `OPENAI_API_KEY`):  
  ```bash
  python agents/discovery.py --config config/agents/sources.yaml --api-base http://localhost:19000
  ```
- Агент читает URL, извлекает brand/model/variant/даты/экран через OpenAI, отправляет в `/api/agents/devices/models`. Опционально генерация ключей делается на backend через `/api/agents/keywords/generate`.

### Мониторинг новинок смартфонов (News API + RSS)
- Настройте `.env`: `SMARTPHONE_RELEASES_ENABLED=true`, `SMARTPHONE_NEWS_API_BASE_URL`, `SMARTPHONE_NEWS_API_KEY`, `SMARTPHONE_NEWS_LANGUAGE`, `SMARTPHONE_NEWS_QUERY`, `SMARTPHONE_NEWS_DAYS_BACK`, `SMARTPHONE_NEWS_PAGE_SIZE`, `SMARTPHONE_NEWS_MAX_PAGES`, `SMARTPHONE_NEWS_MAX_ITEMS`, `OPENAI_API_KEY`.
- Для RSS-источника GSMArena включите `SMARTPHONE_GSMARENA_ENABLED=true` и при необходимости переопределите `SMARTPHONE_GSMARENA_RSS_URL`, `SMARTPHONE_GSMARENA_MAX_ITEMS`.
- Примените миграции: `alembic upgrade head`.
- Запуск фоновой задачи (под cron или вручную):  
  ```bash
  python -m tasks.update_smartphone_releases
  ```
  Либо подключите готовый cron-файл `infra/cron/smartphone_releases.cron` (он вызывает скрипт `infra/cron/update_smartphone_releases.sh`, который сам подхватывает `.env` и пишет лог в `/var/log/pricing/smartphone_releases.log`).
- Job опрашивает News API и RSS, прогоняет новости через LLM и пишет dedup-результат в таблицу `smartphone_releases`. Проверить успешность можно по выводу `python -m tasks.update_smartphone_releases` или по логу cron (`tail -f /var/log/pricing/smartphone_releases.log`).
- Если в статье перечислено несколько моделей, нормализатор вернёт массив `models`, и сервис создаст по одной записи на каждую модель (источник будет иметь суффикс `#1`, `#2`, чтобы сохранить уникальность URL).
- Чтобы получать уведомления в Telegram, задайте `SMARTPHONE_RELEASES_ALERT_TELEGRAM_TOKEN=<bot_token>` и `SMARTPHONE_RELEASES_ALERT_TELEGRAM_CHAT_ID=<chat_id>` — после каждого прогона cron вызовет `infra/cron/telegram_alert.py` и отправит сводку (fetched/processed/errors) в указанный чат или группу.

## CI
GitHub Actions (`.github/workflows/ci.yml`) гоняет ruff, black --check и pytest на push/PR в main.

## Миграции (Alembic)
- Генерация новой миграции:  
  `DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/pricing alembic revision --autogenerate -m "message"`
- Применение миграций:  
  `DATABASE_URL=... alembic upgrade head`

## Тесты
- Запуск всех тестов: `pytest`
- Базовые интеграционные проверки: `/health` и операции с БД на SQLite (см. `tests/`).
- Импорт TopControl: поддерживаются CSV и DBF выгрузки (см. `app/services/importers/topcontrol.py` и тесты).
