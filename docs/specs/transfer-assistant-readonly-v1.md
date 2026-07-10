---
spec_id: "transfer-assistant-readonly-v1"
title: "Transfer Assistant Readonly V1"
doc_type: spec
domain: logistics
status: implemented
owner: operations
source_of_truth: true
related_code:
  - app/api/logistics.py
  - app/schemas/logistics.py
  - app/services/transfer_assistant.py
  - app/core/config.py
  - tasks/export_transfer_assistant_candidates.py
related_tests:
  - tests/test_transfer_assistant.py
  - tests/test_export_transfer_assistant_candidates_task.py
  - tests/test_logistics_api.py
  - tests/test_logistics_onec.py
  - tests/test_order_fulfillment_api.py
contracts: []
depends_on: []
supersedes: []
rollout_required: false
updated_at: "2026-07-05"
---

# Backend-витрина помощника перемещений v1

Статус: implemented

## Назначение

`GET /api/logistics/transfer-assistant/candidates` показывает read-only очередь
кандидатов для помощника перемещений. Endpoint ничего не пишет в `1С`, Bitrix24
или Telegram. Все статусы в ответе являются операционными сигналами backend, а
не учетными фактами.

# Change Summary / Spec Delta

- Зафиксирован read-only backend endpoint для очереди помощника перемещений.
- Описаны источники 1С, расчетные статусы, ограничения производительности и
  CLI-выгрузка для операторской сверки.
- Документ приведен к project-level spec lifecycle: добавлены frontmatter,
  source-of-truth, API/data contracts, tests и rollout-разделы.

# Acceptance Criteria

- Endpoint и CLI не пишут в `1С`, Bitrix24 или Telegram.
- `available_to_transfer` требует `warehouse_id` и не запускает полный обход
  всех складов без ограничения.
- `pickup_expired` использует расчетный срок хранения, если явный срок не найден
  в 1С.
- `manual_review` покрывает возвраты, перемещения, неполные данные и спорные
  сигналы.
- CSV/JSON выгрузка формируется тем же сервисом расчета, что и API.

# Implementation Checklist

- [x] Описан API `GET /api/logistics/transfer-assistant/candidates`.
- [x] Описаны read-only источники 1С и правила статусов.
- [x] Описана CLI-выгрузка `tasks.export_transfer_assistant_candidates`.
- [x] Зафиксированы минимальные проверки и rollout v1.

## Контракт API

Путь:

- `GET /api/logistics/transfer-assistant/candidates`

Защита:

- общий internal token для `/api/logistics/*`;
- заголовок `Authorization: Bearer <LOGISTICS_INTERNAL_API_TOKEN>`.

Параметры v1:

- `date_from`
- `date_to`
- `warehouse_id`; для `status=available_to_transfer` обязателен в v1
- `status`
- `limit`

Статусы v1:

- `available_to_transfer`
- `reserved_for_order`
- `pickup_waiting`
- `pickup_expired`
- `dismantling_needed`
- `manual_review`

Каждая строка содержит товар, склад, заказ или документ-основание, количество,
расчетный статус, причину, ключи документов 1С, дату факта и источник данных.

## Источники 1С

Все источники читаются через `ONEC_DATABASE_URL`.

| Источник | Физические таблицы | Назначение |
| --- | --- | --- |
| `1c:stock_totals` | `_AccumRgT7745`, `_Reference62`, `_Reference80` | фактический остаток по товару и складу |
| `1c:reserved_stock_totals` | `_AccumRgT7662`, `_Document132`, `_Reference62`, `_Reference80` | товар в резерве на складе |
| `1c:customer_order_placements` | `_AccumRgT7606`, `_Document132`, `_Document133`, `_Reference62`, `_Reference80` | размещение заказа покупателя в заказе поставщику |
| `1c:customer_order_lines` | `_Document132`, `_Document132_VT2427` | строки заказа покупателя |
| `1c:rtu_lines` | `_Document203`, `_Document203_VT4966` | выдача или реализация |
| `1c:return_lines` | `_Document109`, `_Document109_VT1698` | возврат покупателя, всегда требует ручной проверки |
| `1c:transfer_lines` | `_Document178`, `_Document178_VT3822` | перемещение, требует ручной проверки состояния |

Проверенные маппинги:

- остаток: `_AccumRgT7745._Fld7738RRef` товар, `_Fld7742RRef` склад,
  `_Fld7743` количество;
- резерв: `_AccumRgT7662._Fld7655RRef` товар, `_Fld7654RRef` склад,
  `_Fld7657_RRRef` заказ покупателя, `_Fld7659` количество,
  `_Fld7657_RTRef = 0x00000084`;
- размещение: `_AccumRgT7606._Fld7598RRef` товар,
  `_Fld7600_RRRef` заказ покупателя, `_Fld7601_RRRef` заказ поставщику,
  `_Fld7602` количество;
- заказ поставщику: `_Document133._Fld2506RRef` склад,
  `_Document133._IDRRef = _AccumRgT7606._Fld7601_RRRef`;
- заказ покупателя: строка товара `_Document132_VT2427._Fld2434RRef`,
  количество `_Fld2431`, склад строки `_Fld2437_RRRef`, склад шапки
  `_Document132._Fld2413_RRRef`.

## Правила статусов

`available_to_transfer` ставится только для остатка, который не перекрыт
заказом, резервом, размещением, РТУ или перемещением по тому же товару и складу.

`reserved_for_order` ставится для строк с заказом, резервом или размещением,
если нет закрывающего документа и нет более приоритетного сигнала.

`pickup_waiting` ставится для самовывоза, когда есть резерв, размещение, заказ
или РТУ, но расчетный срок хранения еще не прошел.

`pickup_expired` ставится только как сигнал: срок хранения прошел, но backend
не освобождает резерв и не закрывает документы.

`manual_review` ставится для конфликтов и неполных данных: возвраты, перемещения,
не найденный заказ, неоднозначный склад, отсутствующий документ или ручная
причина разбора.

`dismantling_needed` поддержан в классификаторе, но в v1 нет подтвержденного
прямого 1С-сигнала для автоматической установки этого статуса.

## Срок хранения самовывоза

В текущей 1С-базе явный `СрокХраненияДо` для заказа покупателя не найден.
Поэтому v1 использует расчетный срок:

- настройка `LOGISTICS_TRANSFER_ASSISTANT_PICKUP_HOLD_DAYS`;
- значение по умолчанию: `7`;
- в ответе `pickup_deadline_source = "derived"`.

Такой срок не считается фактом 1С и нужен только для очереди проверки.

## Производительность

При переданном `status` сервис ограничивает набор читаемых источников:

- `available_to_transfer`: только остатки; в v1 требует `warehouse_id`, чтобы не
  запускать тяжелую проверку свободного остатка по всем складам 1С;
- `reserved_for_order`: заказ, резерв, размещение;
- `pickup_waiting`, `pickup_expired`, `dismantling_needed`: заказ, резерв,
  размещение, РТУ;
- `manual_review`: резерв, размещение, РТУ, возвраты, перемещения.

Это не меняет внешний контракт, но уменьшает лишнее чтение из 1С.

## Выгрузка для сверки

Для первичной сверки без UI добавлена read-only команда:

```bash
./.venv/bin/python -m tasks.export_transfer_assistant_candidates \
  --status pickup_expired \
  --limit 100 \
  --output reports/logistics/transfer-assistant/pickup_expired.csv
```

Команда использует тот же сервис расчета, что и API, поддерживает `date_from`,
`date_to`, `warehouse_id`, `status`, `limit` и пишет JSON или CSV. Для
`available_to_transfer` сохраняется ограничение v1: нужен `warehouse_id`.

Для технической сверки конкретного источника 1С в команде есть CLI-only фильтр
`--source-kind`. Он не добавлен в API v1 и нужен, например, чтобы отдельно
выгрузить размещения:

```bash
./.venv/bin/python -m tasks.export_transfer_assistant_candidates \
  --status reserved_for_order \
  --source-kind placement \
  --limit 100 \
  --output reports/logistics/transfer-assistant/reserved_for_order_placements.csv
```

Первую операторскую приемку нужно делать по текущему рабочему периоду, чтобы не
смешивать живую очередь со старыми зависшими документами. Согласованный стартовый
фильтр:

```bash
--date-from 2026-01-01
```

Рекомендуемый пакет для сверки:

- `pickup_expired_current.csv` - просроченные самовывозы текущего периода;
- `reserved_for_order_current.csv` - текущие резервы и заказные блокировки;
- `manual_review_returns_current.csv` - свежие возвраты для ручного разбора;
- `reserved_for_order_placements_archive.csv` - отдельный архивный техразбор
  размещений, не текущая очередь к работе.

Операторам нужно вернуть по строкам одну из отметок: `OK`, `не тот статус`,
`неактуально`, `непонятно`, `дубль`. На этой приемке запрещено менять 1С,
снимать резервы, закрывать заказы или писать статусы обратно: проверяется только
качество backend-сигнала.

## Проверки

Минимальный набор:

```bash
./.venv/bin/python -m pytest tests/test_transfer_assistant.py tests/test_logistics_api.py tests/test_logistics_onec.py tests/test_order_fulfillment_api.py
./.venv/bin/python -m pytest tests/test_export_transfer_assistant_candidates_task.py
./.venv/bin/python -m ruff check app/services/transfer_assistant.py app/api/logistics.py app/schemas/logistics.py tasks/export_transfer_assistant_candidates.py tests/test_transfer_assistant.py tests/test_export_transfer_assistant_candidates_task.py
BLACK_NUM_WORKERS=1 ./.venv/bin/python -m black --check app/services/transfer_assistant.py app/api/logistics.py app/schemas/logistics.py tasks/export_transfer_assistant_candidates.py tests/test_transfer_assistant.py tests/test_export_transfer_assistant_candidates_task.py
./.venv/bin/python scripts/export_openapi.py --check
```

# Source of Truth

`1С` остается источником факта по остаткам, резервам, заказам, размещениям,
РТУ, возвратам и перемещениям. `pricing-service` строит только read-only
операционные сигналы для проверки оператором и не пишет изменения в `1С`,
Bitrix24 или Telegram.

# API / Data Contracts

Публичный backend-контракт v1 - `GET /api/logistics/transfer-assistant/candidates`.
Командная выгрузка `tasks.export_transfer_assistant_candidates` использует тот
же сервис расчета и пишет JSON/CSV для сверки. `openapi.yaml` должен отражать
API-контракт, а CLI-only фильтр `--source-kind` не добавляется в API v1.

# Tests

Минимальная приемка v1: unit/API/1C tests для transfer assistant, task export,
`ruff`, `black --check` и `scripts/export_openapi.py --check`. Команды
перечислены выше в разделе `Проверки`.

# Rollout

Контур уже реализован как read-only backend-витрина. Перед расширением до UI
или write-back нужно сверить `50-100` строк JSON/CSV с операторами и отдельно
согласовать источник сигнала для `dismantling_needed`.

## Что осталось после v1

- сверить 50-100 строк JSON/CSV с операторами;
- после сверки решить, нужен ли UI;
- для `dismantling_needed` найти или согласовать явный источник сигнала.
