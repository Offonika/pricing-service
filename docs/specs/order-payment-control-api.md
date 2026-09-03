---
spec_id: "order-payment-control-api"
title: "Order Payment Control API"
doc_type: spec
domain: "order_flow"
status: implemented
owner: "engineering"
source_of_truth: true
related_code:
  - app/api/order_payment_control.py
  - app/schemas/order_payment_control.py
  - app/services/order_payment_control.py
related_tests:
  - tests/test_order_payment_control.py
contracts:
  - openapi.yaml
depends_on:
  - docs/specs/pricing-service-architecture-hardening.md
  - docs/specs/site-order-fulfillment-control-contour.md
  - docs/IntegrationContract.OrderAssemblyQueue1C.md
supersedes: []
rollout_required: true
updated_at: "2026-09-01"
---

# Назначение

`POST /api/order-payment-control/check` — единственный серверный guard оплаты
интернет-заказа. Он читает УТ 10.3 только read-only и разрешает оплату, когда
одновременно совпадают три суммы, заказ проведён и не отменён, склад соответствует
ожидаемому UUID, все строки размещены на этом складе и резерв полностью совпадает
с составом заказа.

Контур создан по задаче №1414 и расширен строгой проверкой резерва для задачи
№3520 — `[Сайт][Наличие] Проверять весь заказ по выбранной точке и показывать
подтверждённый срок готовности`. Создание и проведение резерва принадлежат задаче
№3484 и в этот сервис не входят.

# Scope / Out of Scope

Входит:

- bearer-защищённый `POST /api/order-payment-control/check`;
- свежая read-only проверка единственного активного `ЗаказПокупателя`;
- сверка сумм `1С = сайт = платёж`;
- проверка проведения, закрытия, склада шапки и размещения строк;
- сверка полного резерва по складу, товару, характеристике и серии;
- возврат фактического UUID склада, состояния резерва и времени проверки;
- nullable `confirmed_ready_at` из свежего единственного CRM-снимка после `FULL`.

Не входит:

- запись в 1С, создание/снятие резерва или проведение заказа;
- изменение заказа/платежа на сайте;
- второй API наличия или оплаты;
- передача bearer token браузеру;
- production release и ротация credential.

# Change Summary / Spec Delta

- Ранее `allowed=true` означал только совпадение сумм и допускал непроведённый заказ.
- Теперь `allowed=true` возможен только с причиной
  `amount_and_full_reservation_match` и резервом `FULL`.
- `region_xml_id` и `source_warehouse_xml_id` обязательны; числовой Bitrix Store ID
  в интеграционном контракте не используется.
- Любой непроведённый, закрытый, неоднозначный или складски несогласованный заказ,
  неполный/лишний/чужой резерв и недоступность 1С блокируют оплату.

# API / Data Contracts

Запрос:

```json
{
  "site_order_number": "243784",
  "site_amount": "5461.95",
  "payment_amount": "5461.95",
  "stage": "cloudpayments_check",
  "payment_id": "98765",
  "region_xml_id": "0000512213",
  "source_warehouse_xml_id": "11111111-2222-4333-8444-555555555555",
  "availability_snapshot_id": "sha256-semantic-snapshot"
}
```

`stage` принимает `checkout`, `cloudpayments_check` или `cloudpayments_pay`.
`availability_snapshot_id` опционален и используется для корреляции, но не заменяет
свежую проверку 1С.

Ответ содержит прежние поля суммы/документа и дополнительно:

- `reservation_state`: `FULL`, `NONE`, `PARTIAL` или `MISMATCH`;
- `reservation_quantity_match`;
- фактический `source_warehouse_xml_id` заказа 1С;
- `reservation_confirmed_at` — время текущей успешной проверки `FULL`;
- nullable `confirmed_ready_at`.

Пример разрешения:

```json
{
  "allowed": true,
  "reason": "amount_and_full_reservation_match",
  "reservation_state": "FULL",
  "reservation_quantity_match": true,
  "source_warehouse_xml_id": "11111111-2222-4333-8444-555555555555",
  "reservation_confirmed_at": "2026-08-31T18:00:00Z",
  "confirmed_ready_at": null
}
```

Полный формат является сгенерированным контрактом `openapi.yaml`.

# Source of Truth

- УТ 10.3 — сумма, состояние, склад, размещение строк и резерв заказа.
- Сайт Bitrix — локальная сумма заказа и выбранные XML_ID региона/точки.
- CloudPayments callback — сумма конкретной попытки платежа.
- CRM assembly queue — nullable подтверждённый срок; она не ослабляет складской
  guard при отсутствии или ошибке.
- `pricing-service` принимает решение read-only, но не изменяет торговый факт.

# Правила проверки 1С

1. Найти ровно один активный заказ по номеру сайта в `_Document132`.
2. Проверить `_Posted=1`, отсутствие блокирующего закрытия и совпадение суммы.
3. Сопоставить склад шапки `_Fld2413_RRRef` с ожидаемым UUID.
4. Прочитать строки `_Document132_VT2427` и убедиться, что размещение каждой строки
   равно складу шапки.
5. Нормализовать количество формулой задачи №3484:
   `quantity * line_coefficient / storage_coefficient`.
6. Сопоставить резерв `_AccumRgT7662` по заказу, складу, товару, характеристике и
   серии. Допуск по количеству — не более `0.001`.
7. Любой дефицит даёт `PARTIAL`, отсутствие положительного резерва — `NONE`,
   лишний ключ/избыток/чужой склад/ошибка данных — `MISMATCH`.

Подтверждённые физические поля:

- склад шапки: `_Document132._Fld2413_RRRef`;
- строки: `_Document132_VT2427`;
- единица строки: `_Fld2429RRef`;
- характеристика: `_Fld2430RRef`;
- количество: `_Fld2431`;
- коэффициент: `_Fld2433`;
- товар: `_Fld2434RRef`;
- размещение: `_Fld2437_RRRef`;
- серия: `_Fld2447RRef`;
- единица хранения товара: `_Reference62._Fld843RRef`;
- коэффициент единицы: `_Reference41._Fld550`;
- резерв: `_AccumRgT7662`, тип регистратора заказа `0x00000084`.

# Подтверждённый срок

`confirmed_ready_at` читается из `order_assembly_queue_item.assembly_due_at`,
который синхронизирует CRM-поле `UF_CRM_MM_ASSEMBLY_DUE_AT`. Значение возвращается
только когда:

- основной заказ 1С единственный и прошёл все проверки;
- резерв имеет состояние `FULL`;
- найдена ровно одна строка очереди со стадией `EXECUTING`;
- CRM-снимок и состояние синхронизации свежее 10 минут.

Ошибка дополнительного CRM-источника оставляет `confirmed_ready_at=null`, но не
ослабляет проверку полного резерва.

# Invariants

- SQL выполняется через canonical `get_onec_engine()` и ничего не записывает в 1С.
- Шапка, закрытие, строки и резерв одного решения читаются одним соединением в
  одной транзакции `SERIALIZABLE`; CRM-срок запрашивается только после завершения
  транзакции 1С.
- Каждый `checkout`, `check` и `pay` выполняет свежую проверку; положительный кеш
  не используется.
- `allowed=true` допустим только при `FULL`, `reservation_quantity_match=true` и
  причине `amount_and_full_reservation_match`.
- UUID конвертируется в бинарную ссылку 1С и обратно без числовых Bitrix ID.
- Недоступность или некорректная бинарная ссылка 1С возвращает HTTP `503`;
  потребитель обязан fail-closed блокировать оплату.
- В логи не попадают токены, connection strings и полные платёжные payload.

# Errors / Edge Cases

- `site_payment_mismatch`;
- `onec_order_not_found`, `onec_order_deleted`, `onec_order_ambiguous`;
- `onec_order_unposted`, `onec_order_closed`;
- `onec_amount_invalid`, `onec_amount_mismatch`;
- `onec_warehouse_missing`, `onec_warehouse_mismatch`;
- `onec_lines_missing`, `onec_line_placement_mismatch`;
- `onec_reservation_none`, `onec_reservation_partial`,
  `onec_reservation_mismatch`;
- `onec_unavailable`, `onec_invalid_data` / HTTP `503`.

# Acceptance Criteria

- [x] Request требует UUID региона и склада; snapshot ID опционален.
- [x] Проверяется единственный активный проведённый заказ и отсутствие отмены.
- [x] Склад шапки и размещение всех строк совпадают с ожидаемым UUID.
- [x] Резерв сверяется по полному ключу и нормализованному количеству с допуском
      `0.001`.
- [x] Частичный, отсутствующий, лишний и чужой резерв блокируют оплату.
- [x] `allowed=true` возвращается только как
      `amount_and_full_reservation_match` + `FULL`.
- [x] Nullable подтверждённый срок читается только после полного резерва.
- [x] Схемы, тесты и `openapi.yaml` синхронизированы локально.
- [ ] Строгая версия развернута в production `pricing-service`.
- [ ] Site-owned CloudPayments wrapper активирован на production-сайте.
- [ ] Credential ротирован до пилота.

# Implementation Checklist

- [x] Расширить request/response schemas UUID-контекстом и резервными полями.
- [x] Добавить read-only SQL строк заказа и регистра резервов.
- [x] Реализовать нормализацию единиц, UUID conversion и допуск `0.001`.
- [x] Запретить allow без проведённого заказа, ожидаемого склада и `FULL`.
- [x] Подключить nullable CRM readiness только после полного резерва.
- [x] Читать весь снимок 1С одной `SERIALIZABLE`-транзакцией и безопасно
      преобразовывать повреждённые ссылки 1С в fail-closed HTTP `503`.
- [x] Обновить unit/API regression tests и `openapi.yaml`.
- [x] Обновить канонический API-spec и project manifest.
- [ ] Выполнить production release и smoke по отдельной команде.

# Tests

- совпавшие суммы без резерва;
- полный, частичный и лишний резерв;
- резерв другого склада;
- характеристики, серии и дубли строк;
- непроведённый, закрытый, отсутствующий и неоднозначный заказ;
- расхождение склада и размещения;
- недоступность 1С;
- UUID ↔ binary 1C reference;
- одно соединение и одна `SERIALIZABLE`-транзакция на согласованный снимок 1С;
- повреждённая бинарная ссылка 1С → `onec_invalid_data` / HTTP `503`;
- повторные callbacks;
- свежий, неоднозначный и устаревший CRM-срок.

# Rollout

1. Развернуть новую версию `pricing-service` штатным release controller.
2. Проверить deny без токена, deny без резерва и allow на полном тестовом резерве.
3. Развернуть сайт с выключенными feature flags.
4. После приёмки №3484 включить полный контур только для склада Пятигорска.
5. Выполнить 10 последовательных заказов и наблюдать 48 часов.

## Rollback

Отключить сайт-флаг и платёжную форму, оставить заказ неоплаченным для
ручной обработки; существующие резервы не снимать и старую небезопасную оплату не
возвращать.

# Changelog

- 2026-09-01 — guard переведён на один согласованный `SERIALIZABLE`-снимок 1С;
  повреждённые binary references теперь возвращают безопасный
  `onec_invalid_data` / HTTP `503`, а CRM-срок читается после закрытия транзакции.
- 2026-09-01 — повторно пройдены focused regression-тесты, Ruff/Black и проверка
  OpenAPI после обработки отрицательного резерва как `MISMATCH` и nullable
  CRM-срока при недоступной application DB.
- 2026-08-31 — по задаче №3520 контракт ужесточён до обязательного полного резерва,
  UUID склада, nullable подтверждённого CRM-срока и новой причины allow; реализация
  и тесты готовы локально, production release не выполнялся.
- 2026-08-03 — реализован первоначальный read-only guard суммы по задаче №1414.
