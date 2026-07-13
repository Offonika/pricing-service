# Pricing Service — правила для AI-агентов

Этот файл задаёт короткие обязательные правила для репозитория `pricing-service`.
Подробные ролевые workflow вынесены в skill
`$pricing-service-workflows` и читаются только при совпадении задачи.

## 1. Быстрый старт

1. Из `/opt/MM` запусти маршрутизатор:

   ```bash
   python scripts/mm_context.py \
     --query "<текст задачи>" \
     --project auto \
     --limit 8 \
     --max-bytes 12288 \
     --format text
   ```

2. Если `requires_manual_routing=true`, сверь кандидатов с `/opt/MM/AGENTS.md` и
   повтори команду с `--project pricing-service`.
3. Прочитай только документы, код и тесты, возвращённые маршрутизатором.
   Полный `docs/manifest.yml` открывай только при изменении documentation governance
   или самого маршрутизатора.
4. Используй `$pricing-service-workflows`, выбери одну основную роль и открой её
   reference. Для междоменной задачи разрешена одна дополнительная роль.
5. Если указан `TASK_ID`, найди соответствующий `tasks/TASK_ID-*.md`. Если задача
   выбирается из плана, возьми только одну незавершённую задачу из `docs/plan.md`.
6. Перед изменением прочитай фактический исходник, после изменения запусти адресные
   тесты и обязательные проверки.

## 2. Выбор workflow

| Задача | Основная роль | Reference |
| --- | --- | --- |
| Архитектура, границы модулей, схема БД, интеграционный контракт | `pricing-architect` | `.agents/skills/pricing-service-workflows/references/pricing-architect.md` |
| FastAPI, сервисы, схемы, бизнес-логика | `pricing-backend` | `.agents/skills/pricing-service-workflows/references/pricing-backend.md` |
| Импорт, очистка, нормализация, внешние данные | `pricing-data` | `.agents/skills/pricing-service-workflows/references/pricing-data.md` |
| Формулы и правила ценообразования | `pricing-strategies` | `.agents/skills/pricing-service-workflows/references/pricing-strategies.md` |
| Telegram UX и интеграция бота с API | `pricing-telegram` | `.agents/skills/pricing-service-workflows/references/pricing-telegram.md` |
| CI/CD, контейнеры, cron, окружения | `pricing-devops` | `.agents/skills/pricing-service-workflows/references/pricing-devops.md` |
| PRD, specs, plan, manifest, runbooks | `pricing-docs` | `.agents/skills/pricing-service-workflows/references/pricing-docs.md` |
| Небольшая локальная правка без явного домена | `pricing-universal` | `.agents/skills/pricing-service-workflows/references/pricing-universal.md` |
| Исследование рынка, модели устройств, спрос | `pricing-market-research` | `.agents/skills/pricing-service-workflows/references/pricing-market-research.md` |

## 3. Инварианты и безопасность

- По умолчанию отвечай пользователю и пиши внутреннюю документацию на русском.
- За один запуск выполняй одну задачу; не расширяй scope без явной необходимости.
- Используй виртуальное окружение проекта: `./.venv/bin/python` и
  `./.venv/bin/pip`. Не устанавливай зависимости системным `pip`.
- Не выводи и не коммить секреты, `.env`, токены, webhook URL, пароли и connection
  strings. Для примеров используй безопасные placeholders в `.env.example`.
- Интеграции с 1С по умолчанию read-only. Не меняй данные production, Bitrix24,
  Telegram, внешние API, cron или инфраструктуру без явного разрешения пользователя.
- Любое изменение схемы БД оформляй Alembic-миграцией. Не редактируй существующую
  применённую миграцию вместо новой.
- При изменении API обновляй FastAPI schemas, тесты и `openapi.yaml`; при изменении
  внешнего формата — соответствующий integration contract или spec.
- Не переноси бизнес-логику в scripts/infra и не создавай скрытый второй source of
  truth. Канонические требования находятся в PRD/specs/contracts, поведение — в коде
  и тестах.
- `pricing-service` — отдельный вложенный Git-репозиторий. Git-операции выполняй
  внутри него и не включай изменения соседних проектов в один коммит.

## 4. Контекст

- Сначала используй адресные `rg`, имена функций, эндпоинтов и тестов. Не загружай
  целиком логи, выгрузки, большие CSV/JSON/XML, локальные БД и generated artifacts.
- Перед правкой обязательно открой исходник; после правки проверь поведение тестами.

## 5. Проверки

Минимальный набор зависит от изменённой области; сначала запускай адресные тесты.
Перед готовностью значимой Python-правки выполни:

```bash
./.venv/bin/python -m ruff check .
BLACK_NUM_WORKERS=1 ./.venv/bin/python -m black --check .
./.venv/bin/python -m pytest
./.venv/bin/python scripts/export_openapi.py --check
```

На этом сервере не запускай Black с несколькими worker-ами: это уже приводило к
исчерпанию RAM/swap. Для docs/governance изменений дополнительно из `/opt/MM` выполни:

```bash
python scripts/validate_project_docs.py pricing-service
```

Красная обязательная проверка означает, что работа не готова. Если проверка не
запущена или блокируется существующей проблемой, явно укажи это в итоговом отчёте.

## 6. Завершение и передача

- Обновляй `docs/plan.md` и `tasks/` только когда задача действительно требует этого.
- В итоговом отчёте укажи изменённые файлы, выполненные проверки, известные риски и
  оставшиеся шаги.
- При блокере зафиксируй конкретную причину и нужное решение; не маскируй незавершённую
  работу статусом «готово».
