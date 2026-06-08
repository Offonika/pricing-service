---
spec_id: "counterparty-folder-recommendations"
title: "Counterparty Folder Recommendations"
doc_type: spec
domain: "receivables"
status: implemented
owner: "finance"
source_of_truth: true
related_code:
  - app/api/management.py
  - app/services/counterparty_folder_recommendations.py
  - app/schemas/management.py
  - infra/cron/counterparty_folder_recommendations_from_a.py
  - infra/cron/counterparty_folder_recommendations_from_a.sh
related_tests:
  - tests/test_counterparty_folder_recommendations.py
contracts:
  - openapi.yaml
depends_on:
  - docs/specs/receivables-smart-process-workflow.md
  - docs/specs/ut103-bot-command-file-exchange.md
supersedes: []
rollout_required: true
updated_at: "2026-05-29"
---

# Назначение

Контур выявляет контрагентов с открытой дебиторкой, у которых папка в
`Справочник.Контрагенты.Родитель` не совпадает с папкой подразделения, создавшего
старейший просроченный долг. V1 не меняет 1С: сервис только строит рекомендации и
выгружает dry-run отчет для ручной сверки с финансами и руководителями розницы.

# Scope / Out of Scope

Входит:
- расчет статусов `move_recommended`, `ok`, `no_overdue`, `needs_review`;
- выбор владельца по старейшему непогашенному долгу из `receivable_balance_snapshot`;
- проверка просрочки по текущей глубине кредита;
- чтение текущей папки контрагента и папки подразделения из 1С;
- management API и CSV wrapper для ежедневной выгрузки.

Не входит:
- автоматическое изменение `Контрагент.Родитель` в 1С;
- хранение исходной папки до переноса;
- Bitrix/Telegram доставка без отдельного acceptance и включения root orchestration.
- новый канал записи в 1С: будущие команды бота должны использовать существующий
  файловый обмен `UT103_EXCHANGE_ROOT` из `docs/specs/ut103-bot-command-file-exchange.md`.

# Source of Truth

`1С` остается системой учета для контрагентов, подразделений, папок и документов
долга. `pricing-service` использует локальную витрину дебиторки
`receivable_balance_snapshot`, где `origin_document_ref` уже соответствует старейшему
непогашенному долгу по FIFO-логике.

# Data Flow

1. `pricing-service` берет строки `receivable_balance_snapshot` за дату отчета с
   положительной открытой дебиторкой.
2. Для каждого контрагента читает из 1С текущую папку:
   `_Reference54._ParentIDRRef`.
3. Для документа старейшего долга читает подразделение реализации:
   `_Document203._Fld4937RRef -> _Reference68`.
4. Рекомендуемую папку берет из `Справочник.Подразделения.ОсиГруппа`:
   `_Reference68._Fld8927RRef -> _Reference54`.
5. Сравнивает текущую и рекомендуемую папку и возвращает статус.
6. Root orchestration может забрать отчет через API и доставить его в канал
   управления; dedupe ключ: `date + report_revision`.

# API / Data Contracts

Endpoint:

```http
GET /api/management/counterparty-folder-recommendations?date=YYYY-MM-DD
```

Параметры:
- `date` - дата снапшота дебиторки;
- `status` - необязательный фильтр: `move_recommended`, `ok`, `no_overdue`,
  `needs_review`;
- `limit` - необязательное ограничение количества строк.

Ответ:
- `as_of`, `freshness_status`, `source_status`;
- `report_revision` - короткий hash состава выгрузки;
- `summary` - счетчики строк и суммы;
- `payload[]` - контрагент, текущая папка, рекомендуемая папка, подразделение
  долга, сумма, документ, дата долга, просрочка, статус и причина ручной проверки.

CSV wrapper:

```bash
python infra/cron/counterparty_folder_recommendations_from_a.py \
  --date YYYY-MM-DD --status move_recommended
```

По умолчанию wrapper только экспортирует CSV-артефакт и state. С `--dry-run` он
ничего не пишет.

# Invariants

- V1 не выполняет запись в 1С.
- Если позже появится запись в 1С, она идет только через общий файловый обмен
  `UT103_EXCHANGE_ROOT/to_1c/new -> UT103_EXCHANGE_ROOT/from_1c/new`, без прямого
  SQL и без отдельной новой папки под этот контур.
- Если долг не просрочен, возвращается `no_overdue`, даже если папки отличаются.
- Если просроченных долгов несколько, рекомендация строится по старейшему
  непогашенному долгу из витрины дебиторки.
- Если нет документа, подразделения, папки подразделения или текущей папки
  контрагента, строка уходит в `needs_review`.

# Errors / Edge Cases

- Нет `ONEC_DATABASE_URL` или 1С недоступна: API отвечает `503`.
- Неверный `status`: API отвечает `422` на уровне FastAPI validation.
- Нет снапшота за дату: `source_status=empty`, `freshness_status=missing`.
- Документ долга не найден в `_Document203`: `needs_review`,
  `review_reason=origin_document_not_found`.

# Tests

- Unit/integration тесты расчета статусов на sqlite-имитации 1С.
- API smoke тест с management bearer token.
- Проверка фильтра `status=move_recommended`.
- Rollout acceptance: первые топ-20 рекомендаций вручную сверить с
  Максимом/Тимуром и 2-3 дня запускать dry-run без изменений в 1С.

# Rollout

1. Развернуть API и CSV wrapper в `pricing-service`.
2. Запускать локальный dry-run после сборки витрины дебиторки.
3. Сверить первые отчеты с бизнесом.
4. После acceptance включить root delivery в Bitrix/Telegram отдельным изменением.
5. Автоперенос в 1С рассматривать отдельным этапом после подтверждения качества.
6. Любые будущие команды на изменение глубины кредита, лимита или папки
   контрагента оформлять через `onec_commands.v1` в существующем
   `UT103_EXCHANGE_ROOT`, сначала только в `dry_run`.

# Changelog

- 2026-05-29 - implemented read-only API, CSV wrapper, tests and management job registry draft.
- 2026-05-29 - documented future 1C write path through existing `UT103_EXCHANGE_ROOT`.
