---
name: pricing-service-workflows
description: Маршрутизирует изменения pricing-service к минимальному ролевому workflow и проверкам. Использовать, когда нужно спроектировать или внести правку в архитектуру, FastAPI/backend, импорт данных, ценообразование, Telegram, DevOps/CI, документацию или market research. Не использовать для read-only запросов «где находится», «объясни», «покажи код или тесты».
---

# Pricing Service Workflows

Выбирай минимальный ролевой контекст только для задачи, разрешающей изменения.

## Workflow

1. Убедись, что запрос явно разрешает изменение; иначе вернись к navigation workflow
   из `AGENTS.md` и не читай references.
2. Используй полный router output из `AGENTS.md`; не запускай router повторно, если
   результат уже получен для текущей задачи.
3. Выбери одну основную роль ниже и прочитай только её reference.
4. Для задачи, реально пересекающей два домена, прочитай один дополнительный reference и
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

- Не подменяй root-router или project `AGENTS.md`: skill только выбирает workflow
  реализации и не нужен для read-only навигации.
- Не считай результаты manifest или code graph доказательством поведения.
- Не раскрывай секреты и не выполняй внешние side effects без явного разрешения.
- Не используй universal workflow для крупных архитектурных или межсистемных решений.
