---
name: pricing-service-workflows
description: Маршрутизирует задачи pricing-service к минимальному ролевому workflow и проверкам. Использовать для архитектуры, FastAPI/backend, импорта и нормализации данных, правил ценообразования, Telegram, DevOps/CI, документации, небольших универсальных правок и исследования рынка смартфонов или спроса.
---

# Pricing Service Workflows

Выбирай минимальный ролевой контекст вместо чтения всех инструкций проекта.

## Workflow

1. Из `/opt/MM` запусти `scripts/mm_context.py` с текстом задачи и `--project auto`.
2. Убедись, что выбран `pricing-service`; при неоднозначности повтори с
   `--project pricing-service` после сверки с root `AGENTS.md`.
3. Выбери одну основную роль по таблице ниже и прочитай только её reference.
4. Для задачи, реально пересекающей два домена, прочитай одну дополнительную роль и
   зафиксируй границу ответственности. Не загружай все references заранее.
5. Открой только возвращённые маршрутизатором документы, исходники и тесты. Расширяй
   поиск адресно через `rg`.
6. Перед изменением проверь фактический исходник, после изменения запусти проверки из
   reference и обязательный набор из `AGENTS.md`.

## Маршрутизация ролей

- Архитектура, границы, схема БД, системные контракты:
  [pricing-architect.md](references/pricing-architect.md).
- FastAPI, сервисы, Pydantic schemas, бизнес-логика:
  [pricing-backend.md](references/pricing-backend.md).
- Импорт, преобразование, качество и хранение внешних данных:
  [pricing-data.md](references/pricing-data.md).
- Формулы, ограничения и объяснимость цены:
  [pricing-strategies.md](references/pricing-strategies.md).
- Telegram-команды, сценарии и интеграция с backend:
  [pricing-telegram.md](references/pricing-telegram.md).
- CI/CD, Docker, cron, окружения и эксплуатационные скрипты:
  [pricing-devops.md](references/pricing-devops.md).
- PRD, specs, contracts, plan, manifest и runbooks:
  [pricing-docs.md](references/pricing-docs.md).
- Небольшая локальная правка без профильного владельца:
  [pricing-universal.md](references/pricing-universal.md).
- Модели устройств, официальные анонсы и метрики спроса:
  [pricing-market-research.md](references/pricing-market-research.md).

## Общие ограничения

- Не подменяй root-router или project `AGENTS.md`: этот skill только выбирает workflow.
- Не считай результаты manifest или code graph доказательством поведения.
- Не раскрывай секреты и не выполняй внешние side effects без явного разрешения.
- Не используй universal workflow для крупных архитектурных или межсистемных решений.
