<!-- File: docs/RELEASE_NOTES.md -->

# Release Notes — MVP v0.1.0

## Что готово
- FastAPI каркас с логированием, `/health` и автоконфигом через Pydantic Settings.
- База: модели Product/Competitor/CompetitorPrice/ProductMatch/Stock и первичная миграция Alembic.
- Импорт: TopControl CSV/DBF (`app/services/importers/topcontrol.py`), цены конкурентов из CSV (`app/services/importers/competitors.py`), SKU-матчинг по парам (`app/services/matching.py`).
- Ценообразование: базовая стратегия «не ниже закупки + минимальная маржа», расчёт рекомендаций и фоновой перебор (`app/services/pricing*.py`, `app/workers/tasks.py`).
- История стратегий/цен: версии стратегий и лог расчётов (`PricingStrategyVersion`, `PriceRecommendation`, helper в `app/services/pricing.py`).
- API: рекомендации `/api/products/{sku}/recommendation`, отчёты `/api/reports/summary` и `/api/reports/price-changes`, BI-выгрузки `/api/bi/*` (products/recommendations/competitor-prices), расширенный Telegram-интерфейс `/api/telegram/*` (фильтры по бренду/категории/поиск и алерты по марже).
- Выгрузка: CSV для 1С (`app/services/exporters/one_c.py`).
- Тесты: pytest-сьют на импорты, расчёт, отчёты, экспорт, health/logging (см. `tests/`).

## Как запускать
- Локально: `python -m venv .venv && source .venv/bin/activate && pip install -e .[dev]`, затем `uvicorn app.main:app --reload`.
- Docker: `cp .env.example .env && cd infra && docker-compose up --build`.
- Тесты и линтеры: `pytest`, `ruff .`, `black --check .`.
- Импорт примеры: вызвать `import_products(Path(...), session)` для TopControl и `import_competitor_prices_from_csv(Path(...), session)` для файлов парсера.

## Ограничения и известные пробелы
- Telegram-бот — заглушки, нет команд, авторизации и уведомлений.
- Ручное утверждение цен и workflow согласования не реализованы (S3.4 в коде отсутствует).
- Очередь задач — минимальная заглушка без Celery/RQ, ретраев и мониторинга.
- Стратегии ценообразования — только базовая формула; нет многослойных правил по группам/брендам и учёта конкурентов в расчёте отчётов.
- Матчинг — только по SKU; нет правил по названиям/брендам и LLM-поддержки.
- Формат выгрузки для 1С — только CSV; финальный формат под конкретную конфигурацию 1С ещё не согласован.

## Следующие шаги
- Закрыть улучшения из блока S4 (`docs/plan.md`): аналитика ABC/XYZ, история стратегий, интеграция с BI, расширенный Telegram.
- Довести workflow ручного согласования цен и полноценный бот.
- Выбрать и внедрить Celery/RQ с мониторингом и ретраями.
- Уточнить и расширить стратегии (эксклюзивы, дефицит, валютные корректировки) с параметрами из бизнеса.
