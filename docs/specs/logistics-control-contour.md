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
  - tasks/cleanup_logistics_rtu_manual_reviews.py
  - tasks/report_logistics_rtu_manual_review.py
  - tasks/sync_logistics_warehouse_aliases_from_onec.py
  - tasks/sync_logistics_rtu_from_onec.py
  - app/telegram/logistics_bot.py
related_tests:
  - tests/test_logistics_api.py
  - tests/test_logistics_onec.py
  - tests/test_logistics_bot.py
  - tests/test_logistics_bot_webhook_api.py
  - tests/test_bitrix_logistics.py
  - tests/test_order_fulfillment_api.py
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
- в выборку входят только РТУ с положительным признаком интернет-заказа:
  заполнен `site_order_number` или `site_delivery_method`; одна лишь связь РТУ
  с обычным заказом покупателя не считается признаком интернет-заказа;
- РТУ, которая еще не проведена, не распечатана или не собрана, остается в
  ожидаемом состоянии readiness и не создает `manual_review`;
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

Пользовательский Bitrix BFF, не раскрывающий internal token браузеру:

- `POST /api/bitrix/logistics/session`, `GET /session/resume`, `GET /bootstrap`;
  scoped BFF-сессия дублируется в `HttpOnly` cookie только для безопасного
  восстановления Bearer-токена после reload Bitrix WebView, а bootstrap возвращает
  собственный открытый draft для продолжения операции;
- `/handoffs/draft/*`, `/receipts/draft/*`;
- `GET /expected-deliveries`, `/monitor`, `/errors`;
- `GET /transfers/{id}/history`;
- `POST /fallback-link`, `/fallback-session`.

Web fallback читает собственный открытый draft через
`GET /api/logistics/web/draft/open`; чужой склад для `sender/receiver`
запрещён и при операциях, и в monitor. Роль и назначенный склад повторно
проверяются при каждом scan/confirm, включая восстановленный draft.

Draft-операции v1 строго разделены для складских ролей: `sender` создаёт и изменяет
только передачу со своего склада, `receiver` — только приёмку на своём складе.
`logist` имеет мониторинг, историю и очередь ошибок, но не создаёт, не сканирует и не
подтверждает draft.

ОТМЕНЕНО (2026-08-28): прежнее правило приравнивало `admin` к read-only роли `logist`.
`admin` получает `handoff`, `receipt`, `expected`, `monitor`, `history` и `errors`, выбирает любой
активный склад для операции. Административная роль не обходит проверки state machine,
доступности единицы, идемпотентности и аудита; ручного обхода этих проверок нет.

Повторный scan уже добавленного документа возвращает неизменённый draft как
идемпотентный no-op. Мутации одного draft сериализуются блокировкой строки;
длинные коды, комментарии, фото и ключи отклоняются с `422`, а составной ключ
идемпотентности всегда укладывается в ограничение PostgreSQL.

Подтверждение дополнительно блокирует все затронутые `transfer` в стабильном
порядке: два разных draft не могут одновременно создать два события передачи
или приёмки одной единицы. В открытом draft разрешено удалить ошибочно
отсканированную позицию либо отменить весь черновик; отмена сохраняется с
сотрудником, временем и причиной и не создаёт логистического события.

DB-outbox выбирает обычные логистические и `execution_*` строки в общем FIFO по
идентификатору. Только `execution_historical_*` остаются фоновым приоритетом.
Worker сначала поднимает строки без незавершённого предшественника того же заказа,
поэтому более позднее событие не вытесняет логистическую цепочку даже при batch из
одной строки. Worker не блокирует строки за пределами фактически обрабатываемого
batch. Сканирование меняет только draft: позиция рейса создаётся или обновляется
атомарно при подтверждении, поэтому удаление скана и отмена draft не оставляют
ложное `planned`-перемещение. Закрытие камеры обязано остановить и уже работающий,
и поздно полученный media stream.

Internal sync/admin endpoints `/api/logistics/*` сохраняют отдельный internal
token. Все интерфейсы подтверждения используют общие drafts и state machine;
в audit фиксируется точный канал `api`, `bitrix`, `telegram` или `web_fallback`.

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
- `python -m tasks.cleanup_logistics_rtu_manual_reviews` показывает dry-run
  устаревших readiness-записей и розничных РТУ без положительного интернет-признака;
- только явный `--apply` закрывает безопасно распознанный шум, добавляет audit markers
  в payload и оставляет повреждённые payload открытыми для ручного разбора;
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
- РТУ без `site_order_number`, только если есть независимый положительный
  признак интернет-заказа, например заполненный `site_delivery_method`;
- РТУ с неразрешенным или неоднозначным target warehouse;
- РТУ для внешней доставки/перевозчика без выбранного внутреннего flow;
- РТУ с неуникальным generated lookup;
- ОТМЕНЕНО (2026-08-26): РТУ, которая не прошла readiness gate
  `проведена + распечатана + собрана`, больше не создает `manual_review`, потому
  что это ожидаемое промежуточное состояние, а не ошибка оператора;
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
- разные draft не могут параллельно подтвердить одну логистическую единицу;
- ошибочный scan удаляется, а отменённый draft не блокирует новый черновик;
- очередь из нескольких цепочек handoff/receipt не вытесняет предшествующие события;
- рейс показывает список единиц, точки и фактические приемки;
- external carrier не считается финальной доставкой до приемки обратно;
- РТУ после приемки создает `pickup_stored_at_point`, но не `WON`;
- RTU sync использует `ONEC_DATABASE_URL`, генерирует `MMLOG1|rtu|...` и не
  пишет обратно в `1С`;
- web fallback работает без Telegram и без internal token в браузере;
- Bitrix OAuth-сессия проверяет allowlist портала и привязку пользователя;
- Bitrix WebView восстанавливает BFF-сессию после потери `sessionStorage`, а
  зависший callback `BX24.init` завершается явной ошибкой по таймауту;
- `sender/receiver` не могут работать с чужим складом или операцией другой роли;
- открытый draft с уже отсканированными документами восстанавливается после
  перезагрузки Bitrix UI и web fallback;
- fallback launch token истекает и обменивается только один раз;
- OpenAPI drift и spec manifest validation.

Для пакетного аудита дефекты исправляются короткими адресными проверками. Полный
`pytest` не запускается после каждой правки и выполняется один раз после завершения
всего пакета перед итоговой готовностью.

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
8. 2026-08-28 разрешены merge PR №87 и production release проверенного коммита.
   Текущие production-значения feature flags сохраняются; включение приложения,
   автоматических стадий и SMS требует отдельного управляемого шага.
9. Production-профили Арсения Кештова и Андрея Платонова назначены ролью
   `admin`; склад не закреплён, доступ — ко всем активным складам.

# Changelog

- 2026-08-28 — Арсений Кештов и Андей Платонов назначены администраторами
  логистического приложения без закреплённого склада.
- 2026-08-28 — `admin` получил полный операционный доступ логистического
  приложения и выбор активного склада без обхода бизнес-проверок.
- 2026-08-28 — Bitrix WebView получил восстановление scoped BFF-сессии через
  защищённую cookie и ограниченный таймаут `BX24.init`, чтобы reload не оставлял
  приложение на бесконечной загрузке или белой странице.
- 2026-08-28 — после зелёного CI разрешены merge PR №87 и production release;
  значения логистических feature flags оставлены без изменения.
- 2026-08-28 — draft-операции ограничены ролями `sender/receiver`, internal API
  получил отдельный source-channel `api`, а обычные `execution_*` и логистические
  строки outbox переведены в общий FIFO; historical execution остаётся фоновым.
- 2026-08-28 — закрыты crash-window Telegram после подтверждения draft, пагинация
  удаления более 20 сканов и поздний результат распознавания фото после закрытия камеры.
- 2026-08-27 — устранены starvation outbox между логистическими и `execution_*`
  строками одного заказа и побочный `planned` route item от удалённого либо
  отменённого скана; запись рейса перенесена на подтверждение draft.
- 2026-08-27 — приняты исправления повторного аудита: блокировка transfer между
  разными draft, порядок DB-outbox от предшественника к последователю, безопасное
  закрытие камеры, отмена draft и удаление ошибочного скана во всех интерфейсах.
- 2026-08-27 — повторный scan переведён в идемпотентный no-op, мутации draft
  сериализованы; закрыты продолжение draft после смены роли/склада, смешивание
  handoff/receipt endpoints и PostgreSQL `500` на граничной длине входных данных.
- 2026-08-27 — по аудиту восстановление открытого draft добавлено в Bitrix UI и
  web fallback; fallback ограничен собственной ролью/складом и получил камеру,
  одноразовая ссылка больше не открывает два конкурирующих окна, а BFF-сессия
  получила однократное автоматическое обновление после `401`.
- 2026-08-27 — принят порядок пакетного аудита: короткие проверки по каждому
  дефекту, полный регрессионный прогон один раз в конце.
- 2026-08-26 — исключен розничный шум: для RTU sync обязателен положительный
  признак интернет-заказа, а readiness gate переведен из ошибок в ожидаемое состояние.
- 2026-08-26 — добавлен контракт встроенного приложения Bitrix24, BFF-сессий,
  складских ролей и одноразового web fallback; основной UX перенесен в Bitrix24,
  Telegram и web остаются резервом общей state machine.
- 2026-07-01 — зафиксирована граница задачи `1530`: сначала read-only
  backend-витрина и очереди, 1С-доработку отдельных статусов товара отложить.
- 2026-05-22 — draft created, контур зафиксирован для первой волны реализации.
