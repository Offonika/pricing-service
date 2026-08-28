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
  - app/api/bitrix_logistics.py
  - app/models/logistics.py
  - app/schemas/logistics.py
  - app/services/logistics.py
  - app/services/logistics_onec.py
  - app/services/bitrix_logistics_auth.py
  - tasks/apply_logistics_warehouse_alias_overrides.py
  - tasks/report_logistics_rtu_manual_review.py
  - tasks/sync_logistics_warehouse_aliases_from_onec.py
  - tasks/sync_logistics_rtu_from_onec.py
  - infra/cron/logistics_rtu_sync.sh
  - infra/cron/logistics_rtu_sync.cron
  - app/telegram/logistics_bot.py
related_tests:
  - tests/test_logistics_api.py
  - tests/test_logistics_onec.py
  - tests/test_logistics_bot.py
  - tests/test_logistics_bot_webhook_api.py
  - tests/test_bitrix_logistics.py
  - tests/test_order_fulfillment_api.py
  - tests/test_logistics_rtu_cron.py
contracts:
  - docs/TechDesign.LogisticsTelegramMVP.md
  - docs/IntegrationContract.Logistics1C.md
  - docs/IntegrationContract.LogisticsSiteOrders1C.md
  - docs/specs/site-order-fulfillment-control-contour.md
depends_on: []
supersedes: []
rollout_required: true
updated_at: "2026-08-28"
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
- встроенное приложение Bitrix24 `/bitrix/logistics/` с короткой BFF-сессией;
- одноразовый fallback launch token на 5 минут с однократным обменом на cookie;
- связь `logistics_user.bitrix_user_id`, роли склада и source-channel audit;
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
- до установки отдельного реквизита 1С целевой склад выбирается по address
  aliases из `logistics_warehouse.payload`; aliases можно синхронизировать из
  `_Reference68._Fld9249` через
  `tasks/sync_logistics_warehouse_aliases_from_onec.py`;
- отдельный реквизит `ЗаказПокупателя.осиТочкаСамовывоза` и прием
  `PICKUP_POINT = XML_ID` склада реализованы production-based candidate в
  связанной задаче Codex
  [«Уточнить поле точки самовывоза»](codex://threads/01a0499c-af7c-78d0-8bea-64a994831eb5)
  и спецификации
  [UT 10.3 Pickup Point Field Candidate](../../../1C_Dev_Workflow/docs/specs/ut103-task-107-pickup-point-field-candidate-2026-08-28.md);
  candidate еще не установлен в рабочую 1С, поэтому текущий sync сохраняет
  адресное сопоставление до отдельного production-развертывания и readback;
- address matching использует безопасное совпадение по токенам улицы/дома/павильона
  и не считает город/время работы достаточным совпадением;
- РТУ СДЭК, Почты России и курьерской доставки после readiness gate создаются
  на исходном складе в `at_warehouse`; способ доставки определяет внешнего
  перевозчика, а точка сдачи берется из настроенного external terminal;
- если после уточнения правил РТУ успешно синхронизировалась, старые открытые
  `manual_review` по этой РТУ закрываются как `resolved` с отметкой
  `auto_resolved_by = rtu_sync`;
- локальная РТУ (пустой адрес внутреннего самовывоза либо совпавшие исходный и
  целевой склады) не создает logistics unit, остается в ТЗП своего подразделения
  и учитывается счетчиком `local_pickup_skipped`;
- спорные РТУ не попадают в активную логистику и уходят в `manual_review`.

External carrier:

- `handed_to_external_carrier` переводит единицу из `in_transit` в
  `with_external_carrier` и фиксирует перевозчика/трек/терминал;
- `accepted_from_external_carrier` возвращает единицу в `at_warehouse`;
- состояние у перевозчика не считается финальной доставкой клиенту.
- ОТМЕНЕНО (2026-08-28): обычный sync больше не должен массово оставлять РТУ
  `СДЭК`/`Почта России`/`Доставка курьером` в
  `rtu_external_carrier_unmapped`, а режим `--external-carriers` не должен
  создавать `handed_to_external_carrier` с `source = 1c_sync` и сразу
  переводить единицу в `with_external_carrier`.
- ОТМЕНЕНО (2026-08-28): сотрудник точки сдачи или водитель вручную подтверждает
  `handed_to_external_carrier`. Причина: подтвержденный факт приемки должен
  приходить от внешней службы доставки по API.
- ОТМЕНЕНО (2026-08-28): `pricing-service` напрямую принимает API-события
  СДЭК/Почты, создает `handed_to_external_carrier` с `source = carrier_api` и
  сам переводит CRM в `IN_DELIVERY`. Причина: адаптеры перевозчиков уже
  подключены к Bitrix24 и действующие роботы CRM двигают сделку по их событиям.
- Целевое правило: sync создает такую РТУ в `at_warehouse`, а сотрудник склада
  сканированием подтверждает только внутреннее событие `handed_to_driver`. Пока
  наш водитель везет заказ до СДЭК/Почты, единица находится в `in_transit`, а
  CRM-сделка остается на стадии `FINAL_INVOICE` / `Готов к отгрузке`.
  Существующая интеграция перевозчика с Bitrix24 получает событие приемки и сама
  ведет `IN_DELIVERY`, `PICKUP_STORAGE`, `WON` и иные внешние стадии по своему
  действующему контракту. `pricing-service` не подключает второй adapter и не
  записывает повторный stage transition. Статусы СДЭК/Почты не копируются в
  логистический монитор: пользователь смотрит их только в стадиях сделки
  Bitrix24. Возможный технический readback завершения внутреннего плеча не
  должен создавать внешний пользовательский статус или обратную запись в CRM.
  Read-only sync 1С не подтверждает физические события. Если external terminal
  еще не настроен, блокируется внутренняя отправка, но QR и сама логистическая
  единица остаются доступными.
- для внутреннего `Самовывоз` с пустыми адресными полями отдельная логистическая
  единица не создается: РТУ остается в локальной ТЗП.

# API / Data Contracts

Пользовательский Bitrix BFF, не раскрывающий internal token браузеру:

- `POST /api/bitrix/logistics/session`, `GET /bootstrap`;
- `/handoffs/draft/*`, `/receipts/draft/*`;
- `GET /expected-deliveries`, `/monitor`, `/errors`;
- `GET /transfers/{id}/history`;
- `POST /fallback-link`, `/fallback-session`.

Internal sync/admin endpoints `/api/logistics/*` сохраняют отдельный internal
token. Все интерфейсы подтверждения используют общие drafts и state machine.

QR v1:

- `MMLOG1|transfer|<external_id>|<document_number>|<optional checksum>`;
- `MMLOG1|rtu|<external_id>|<site_order_number>|<optional checksum>`.

В текущем пользовательском процессе сотрудник сканирует напечатанный на
документе QR/штрихкод. `lookup_code` является только внутренним техническим
полем backend: в нем хранится нормализованное значение скана для однозначного
и идемпотентного поиска. Внедрение отдельного корпоративного lookup-кода, его
печати и рабочего регламента переносится на следующий этап. Существующие
документы перепечатывать не требуется.

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
- `GET /api/logistics/rtu/ready-for-pickup?warehouse_code=...&format=json|xml`;
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
- `infra/cron/logistics_rtu_sync.sh` запускает тот же CLI под `flock`; по
  умолчанию dry-run, запись включается только `LOGISTICS_RTU_SYNC_APPLY=true`.

# Invariants

- Старые endpoints `/sync/transfers`, `/handoffs/*`, `/receipts/*` остаются
  совместимыми.
- Browser UI не получает `LOGISTICS_INTERNAL_API_TOKEN`; web session создается
  backend-ом и живет в HttpOnly signed cookie.
- ОТМЕНЕНО (2026-08-28): общий manual override для `logist/admin` в штатном
  пользовательском режиме. На период тестирования только `admin` получает все
  экраны и операции `sender`/`receiver` на любом пилотном складе, мониторинг,
  историю, ошибки и ручной разбор логистических конфликтов. Каждое действие
  требует причины и пишется в аудит; финальные стадии и последовательность
  подтвержденных событий остаются защищенными. После пилота расширенный режим
  администратора должен быть отключен.
- Только внутренняя РТУ самовывоза, принятая на однозначно ожидаемом конечном
  складе, переводит заказ в `pickup_stored_at_point`, но не закрывает сделку в
  `WON`. Внешний carrier flow не использует эту pickup-цепочку.
- Локальная РТУ не создаёт logistics unit; межточечная возвращается в ТЗП
  получателя только после `accepted_at_point` на конечном складе.
- Повторный confirm черновика идемпотентен на уровне закрытого draft.

# Решение 2026-07-01: следующий шаг без доработки 1С

По задаче Bitrix24 `1530` доработка `1С` под отдельные статусы товара отложена.
Backend должен идти первым как read-only слой контроля на фактах `1С`:

- читать фактический остаток, резервы, размещения, заказы покупателей,
  перемещения, РТУ, возвраты и закрытия из `1С`;
- не записывать обратно в `1С` и не считать расчетный backend-статус учетным
  фактом;
- строить операционные очереди: `свободно к перемещению`,
  `зарезервировано под заказ`, `ждет самовывоза`, `просрочен самовывоз`,
  `нужно снять резерв/расформировать`, `ручной разбор`;
- считать `просрочен самовывоз` только сигналом к действию, а не автоматическим
  освобождением остатка;
- передавать в `1С` только согласованный список кандидатов/диагностику для
  ручного решения, пока не утвержден отдельный write-back контракт.

Минимальный следующий deliverable: read-only отчет/endpoint по кандидатам для
помощника перемещений с объяснением, из каких документов и регистров `1С`
получен каждый статус.

# Errors / Edge Cases

Manual review создается для:

- неизвестного или неоднозначного QR;
- РТУ без `site_order_number`;
- РТУ с неразрешенным или неоднозначным target warehouse;
- РТУ для внешней доставки без настроенного external terminal или однозначного
  внутреннего плеча; QR такой РТУ при этом остается распознаваемым;
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
- local-skip не создаёт logistics unit и идемпотентно закрывает старый review;
- read-only endpoint отдает только принятую межточечную РТУ целевого склада в
  JSON/XML;
- web fallback работает без Telegram и без internal token в браузере;
- Bitrix OAuth-сессия проверяет allowlist портала и привязку пользователя;
- `sender/receiver` не могут работать с чужим складом или операцией другой роли;
- fallback launch token истекает и обменивается только один раз;
- OpenAPI drift и spec manifest validation.

# Rollout

1. Применить миграцию: nullable-поля, backfill, уникальность
   `(source_document_type, external_id)`, индекс `lookup_code`.
2. Обновить `.env`: `LOGISTICS_WEB_SESSION_SECRET`.
3. Выпустить backend с новыми endpoints, не отключая старый Telegram flow.
4. Подключить read-only sync РТУ из `1С` через
   `tasks/sync_logistics_rtu_from_onec.py` сначала в dry-run, затем с `--apply`.
5. С выключенным `LOGISTICS_BITRIX_APP_ENABLED` настроить OAuth placement и связи
   пользователей, затем включить приложение только для пилотных складов.
6. Пилотировать центральный склад -> Тёплый Стан на 3–5 заказах; web и Telegram
   оставить резервом.
7. Перед production-пилотом ротировать старый рискованный лог с Telegram token.

# Changelog

- 2026-08-28 — отображение внешних статусов в логистическом мониторе отменено: СДЭК/Почта видны только по стадиям сделки Bitrix24; отдельная копия состояния в `pricing-service` не создается.
- 2026-08-28 — ОТМЕНЕНО позднейшим решением того же дня: возможное read-only зеркало внешних статусов рассматривалось как технический вопрос; принято не показывать эти статусы в логистическом мониторе.
- 2026-08-28 — приемка СДЭК/Почтой назначена API-событием перевозчика: внутреннее плечо сохраняет сделку в `Готов к отгрузке`, подтвержденная приемка переводит ее в `Передан в доставку`; для тестового `admin` временно открыты все логистические операции с аудитом и без обхода финальных стадий.
- 2026-08-28 — связана задача Codex по отдельному реквизиту `осиТочкаСамовывоза`; до его production-развертывания сохраняется текущее адресное сопоставление.
- 2026-08-28 — корпоративный процесс отдельного lookup-кода перенесен на следующий этап; текущий UX использует напечатанный QR/штрихкод документа, а `lookup_code` сохранен только как внутреннее техническое поле backend.
- 2026-08-28 — РТУ СДЭК, Почты России и курьерской доставки включены во внутреннее водительское плечо; sync больше не подтверждает передачу внешнему перевозчику.
- 2026-08-28 — локальные РТУ исключены из logistics units; добавлены cron-кандидат sync и read-only endpoint возврата принятой межточечной РТУ в ТЗП получателя.
- 2026-08-26 — добавлен контракт встроенного приложения Bitrix24, BFF-сессий,
  складских ролей и одноразового web fallback; основной UX перенесен в Bitrix24,
  Telegram и web остаются резервом общей state machine.
- 2026-07-01 — зафиксирована граница задачи `1530`: сначала read-only
  backend-витрина и очереди, 1С-доработку отдельных статусов товара отложить.
- 2026-05-22 — draft created, контур зафиксирован для первой волны реализации.
