---
spec_id: "logistics-control-contour"
title: "Logistics Control Contour"
doc_type: spec
domain: "logistics"
status: draft
owner: "operations"
source_of_truth: true
related_code:
  - app/api/logistics.py
  - app/api/logistics_web.py
  - app/models/logistics.py
  - app/schemas/logistics.py
  - app/services/logistics.py
  - app/services/logistics_onec.py
  - tasks/apply_logistics_warehouse_alias_overrides.py
  - tasks/report_logistics_rtu_manual_review.py
  - tasks/sync_logistics_warehouse_aliases_from_onec.py
  - tasks/sync_logistics_rtu_from_onec.py
  - app/telegram/logistics_bot.py
related_tests:
  - tests/test_logistics_api.py
  - tests/test_logistics_onec.py
  - tests/test_logistics_bot.py
  - tests/test_logistics_bot_webhook_api.py
  - tests/test_order_fulfillment_api.py
contracts:
  - docs/TechDesign.LogisticsTelegramMVP.md
  - docs/IntegrationContract.Logistics1C.md
  - docs/IntegrationContract.LogisticsSiteOrders1C.md
  - docs/specs/site-order-fulfillment-control-contour.md
depends_on: []
supersedes: []
rollout_required: true
updated_at: "2026-05-22"
---

# Назначение

Расширить текущий Logistics Telegram MVP до управляемого контура логистики внутри
`pricing-service`: видеть не только факт "в пути", но источник документа, QR,
рейс, ожидаемую точку сдачи, передачу внешнему перевозчику, ручной разбор и связь
с исполнением интернет-заказа.

Контур не заменяет учет в `1С`. Он хранит логистическое состояние, event log,
аудит и операционные исключения, чтобы `Bitrix24` и Telegram/Web были рабочими
слоями, а не единственным местом хранения правды.

# Scope / Out of Scope

Входит:

- совместимость старого flow `sync/transfers -> handoff -> receipt`;
- трактовка `logistics_transfer` как `logistics_unit` без переименования таблицы;
- источники `transfer` и `rtu`;
- QR/lookup code, legacy `barcode` и backfill `lookup_code = barcode`;
- рейсы `logistics_route_run` и состав рейса `logistics_route_run_item`;
- очередь `logistics_manual_review`;
- external carrier state `with_external_carrier`;
- web fallback `/logistics/fallback` через signed cookie;
- bridge в `site_order_execution_event` для РТУ, принятой на финальной точке.

Не входит:

- запись обратно в `1С`;
- API СДЭК/Почты и расчет маршрутов;
- закрытие сделки в `WON` по факту приемки РТУ на точке;
- пакетизация `1 документ = много мест`; v1 сохраняет правило `1 документ = 1 единица`.

# Source of Truth

| Система | Роль |
| --- | --- |
| `1С УТ 10.3` | учет документов, складов, заказов, РТУ, перемещений и удалений |
| `pricing-service` | логистическое состояние, event log, рейсы, manual review, bridge событий |
| `Bitrix24` | задачи, SLA, карточки разбора и операционный контроль |
| Telegram/Web fallback | UX сканирования и подтверждения, без хранения состояния |

Инвариант: read-only sync из `1С` не перетирает активное логистическое состояние.
Если документ изменен/удален в `1С`, а по нему уже есть handoff/receipt/external
carrier state, сервис создает `manual_review` и audit event
`onec_reconciliation_conflict`.

# Data Flow

Базовый поток:

1. `1С` выгружает склады, водителей, пользователей и единицы через
   `/api/logistics/sync/units`.
2. Сервис upsert-ит `logistics_transfer` с `source_document_type`.
3. QR/штрихкод ищется через `/api/logistics/units/lookup` по `lookup_code`, затем
   по legacy `barcode`.
4. Передача водителю создает событие `handed_to_driver`, state `in_transit` и
   привязку к рейсу, если указан `route_run_id`.
5. Приемка на точке создает `accepted_at_point`, закрывает плечо рейса и переводит
   единицу в `at_warehouse`.
6. Для `rtu` приемка на финальном складе дополнительно создает
   `site_order_execution_event` с source `logistics`, confidence `strong` и event
   `pickup_stored_at_point`.

RTU sync из SQL `1С`:

- отдельное подключение не создается, используется существующий `ONEC_DATABASE_URL`;
- engine создается по текущему паттерну проекта:
  `create_engine(settings.onec_database_url, pool_pre_ping=True)`;
- сервис читает `_Document203` / РТУ, связанный заказ `_Document132`, склад
  РТУ `_Reference80` и readiness-события `_InfoRg9448`;
- SQL остается read-only и использует `WITH (NOLOCK)`, как в текущем контуре
  `site_order_fulfillment`;
- готовая РТУ превращается в `source_document_type = rtu` с lookup
  `MMLOG1|rtu|<rtu_external_id>|<site_order_number>`;
- целевой склад выбирается по address aliases из `logistics_warehouse.payload`;
  aliases можно синхронизировать из `_Reference68._Fld9249` через
  `tasks/sync_logistics_warehouse_aliases_from_onec.py`;
- address matching использует безопасное совпадение по токенам улицы/дома/павильона
  и не считает город/время работы достаточным совпадением;
- СДЭК, Почта России и курьерские способы доставки не маппятся на внутренний
  склад автоматически и уходят в `manual_review` как внешний delivery flow;
- если после уточнения правил РТУ успешно синхронизировалась, старые открытые
  `manual_review` по этой РТУ закрываются как `resolved` с отметкой
  `auto_resolved_by = rtu_sync`;
- спорные РТУ не попадают в активную логистику и уходят в `manual_review`.

External carrier:

- `handed_to_external_carrier` переводит единицу из `in_transit` в
  `with_external_carrier` и фиксирует перевозчика/трек/терминал;
- `accepted_from_external_carrier` возвращает единицу в `at_warehouse`;
- состояние у перевозчика не считается финальной доставкой клиенту.
- для РТУ из `1С` с доставкой `СДЭК`/`Почта России`/`Доставка курьером`
  обычный sync по умолчанию оставляет документ в
  `rtu_external_carrier_unmapped`; явный запуск
  `tasks/sync_logistics_rtu_from_onec.py --external-carriers --apply`
  применяет внешний carrier flow: создает logistics unit, пишет событие
  `handed_to_external_carrier` с `source = 1c_sync`, переводит единицу в
  `with_external_carrier` и не создает финальный факт выдачи клиенту.
- для внутреннего `Самовывоз` с пустыми адресными полями target warehouse
  берется из склада РТУ; sync помечает payload флагами
  `business_rule = pickup_empty_address_target_source` и
  `empty_pickup_address_target_source = true`.

# API / Data Contracts

QR v1:

- `MMLOG1|transfer|<external_id>|<document_number>|<optional checksum>`;
- `MMLOG1|rtu|<external_id>|<site_order_number>|<optional checksum>`.

`lookup_code` хранит весь сканируемый код. Для старых строк выполняется backfill:
`lookup_code = barcode`, `source_document_type = transfer`.

Новые поля логистической единицы:

- `source_document_type`: `transfer` или `rtu`;
- `lookup_code`: основной QR/search key;
- `document_target_warehouse_id`: фактическая точка документа;
- `origin_order_external_id`: исходный заказ `1С`/сайта;
- `site_order_number`: номер интернет-заказа для bridge.
- `logistics_warehouse.payload.address_aliases`: список строк для сопоставления
  адреса самовывоза/доставки РТУ с точкой приемки.

Новые endpoints:

- `POST /api/logistics/sync/units`;
- `GET /api/logistics/units/lookup?code=...`;
- `POST /api/logistics/route-runs`, `GET /api/logistics/route-runs`;
- `GET /api/logistics/manual-review`;
- `POST /api/logistics/transfers/{id}/external-carrier/handoff`;
- `POST /api/logistics/transfers/{id}/external-carrier/accept`;
- `POST /api/logistics/manual-ready-overrides`;
- web-safe `/api/logistics/web/*` с signed cookie.

RTU sync запускается не публичным API, а CLI:

- `python tasks/sync_logistics_warehouse_aliases_from_onec.py`;
- без `--apply` выполняется dry-run address aliases складов;
- с `--apply` обновляет `logistics_warehouse.payload.address_aliases`;
- `python tasks/sync_logistics_rtu_from_onec.py --date-from YYYY-MM-DD --limit 500`;
- без `--apply` выполняется dry-run;
- с `--apply` пишет logistics units и `manual_review`.
- `python tasks/report_logistics_rtu_manual_review.py --review-type rtu_target_warehouse_unresolved`
  группирует открытый ручной разбор по причине, способу доставки и адресу.
- `python tasks/apply_logistics_warehouse_alias_overrides.py aliases.json` делает
  dry-run подтвержденных aliases, а `--apply` добавляет их в
  `logistics_warehouse.payload.address_aliases` с историей подтверждения.

# Invariants

- Старые endpoints `/sync/transfers`, `/handoffs/*`, `/receipts/*` остаются
  совместимыми.
- Browser UI не получает `LOGISTICS_INTERNAL_API_TOKEN`; web session создается
  backend-ом и живет в HttpOnly signed cookie.
- Manual override доступен только ролям `logist/admin`, требует причину и пишет
  событие `manual_ready_override`.
- РТУ на точке переводит заказ в `pickup_stored_at_point`, но не закрывает
  сделку в `WON`.
- Повторный confirm черновика идемпотентен на уровне закрытого draft.

# Errors / Edge Cases

Manual review создается для:

- неизвестного или неоднозначного QR;
- РТУ без `site_order_number`;
- РТУ с неразрешенным или неоднозначным target warehouse;
- РТУ для внешней доставки/перевозчика без выбранного внутреннего flow;
- РТУ с неуникальным generated lookup;
- РТУ, которая не прошла readiness gate `проведена + распечатана + собрана`;
- изменения/удаления документа `1С` при активном logistics state;
- ручного override готовности.

Операционный монитор должен уметь фильтровать:

- по рейсу;
- по источнику `transfer`/`rtu`;
- по состоянию `with_external_carrier`;
- по зависшим единицам;
- по открытым `manual_review`;
- по расхождениям с `1С`.

# Tests

Минимальный набор:

- старый logistics API flow не ломается;
- lookup работает по `lookup_code`, legacy `barcode` остается совместимым;
- неверная точка приемки, повторный scan и повторный confirm не создают неверных событий;
- рейс показывает список единиц, точки и фактические приемки;
- external carrier не считается финальной доставкой до приемки обратно;
- РТУ после приемки создает `pickup_stored_at_point`, но не `WON`;
- RTU sync использует `ONEC_DATABASE_URL`, генерирует `MMLOG1|rtu|...` и не
  пишет обратно в `1С`;
- web fallback работает без Telegram и без internal token в браузере;
- OpenAPI drift и spec manifest validation.

# Rollout

1. Применить миграцию: nullable-поля, backfill, уникальность
   `(source_document_type, external_id)`, индекс `lookup_code`.
2. Обновить `.env`: `LOGISTICS_WEB_SESSION_SECRET`.
3. Выпустить backend с новыми endpoints, не отключая старый Telegram flow.
4. Подключить read-only sync РТУ из `1С` через
   `tasks/sync_logistics_rtu_from_onec.py` сначала в dry-run, затем с `--apply`.
5. Пилотировать web fallback на ограниченной группе `logist/admin`.
6. Перед production-пилотом ротировать старый рискованный лог с Telegram token.

# Changelog

- 2026-05-22 — draft created, контур зафиксирован для первой волны реализации.
