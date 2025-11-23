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

## Docker Compose (dev)
```bash
cp .env.example .env
cd infra && docker-compose up --build
```
Сервисы: FastAPI (`http://localhost:8000`), PostgreSQL (`localhost:5432`), Redis (`localhost:6379`). После запуска API healthcheck: `curl http://localhost:8000/health`.
