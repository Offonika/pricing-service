# Умное ценообразование Master Mobile

Сервис для расчёта рекомендованных цен на основе данных TopControl, парсера конкурентов и правил из PRD. Проектный контекст:
- Бизнес-требования и сценарии: `docs/PRD.md`
- High-level архитектура: `docs/architecture.md`
- План задач: `docs/plan.md`
- Стратегии ценообразования (MVP): `docs/price-strategies.md`

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
Сервисы: FastAPI (`http://localhost:8000`), PostgreSQL (`localhost:5432`), Redis (`localhost:6379`). После запуска API healthcheck: `curl http://localhost:8000/health`.

### Переменные окружения
- App: `APP_PORT`, `ENVIRONMENT`, `LOG_LEVEL`
- DB: `POSTGRES_*`, `DATABASE_URL`
- Redis: `REDIS_URL`
- LLM/матчинг: `OPENAI_API_KEY` (и при необходимости `OPENAI_API_BASE`, `OPENAI_MODEL`)
- Telegram: `TELEGRAM_BOT_TOKEN` (при использовании бота), `TELEGRAM_WEBHOOK_URL` (если нужен webhook)

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
