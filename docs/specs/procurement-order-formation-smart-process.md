---
spec_id: "procurement-order-formation-smart-process"
title: "Procurement Order Formation Bitrix App"
doc_type: spec
domain: "procurement"
status: "implemented"
owner: "operations"
source_of_truth: true
related_code:
  - app/models/procurement_order_formation.py
  - app/api/procurement_order_formation.py
  - app/services/procurement_order_formation.py
  - app/services/procurement_order_formation_workspace.py
  - app/services/bitrix_procurement_order_formation_auth.py
  - app/services/bitrix_order_formation.py
  - app/services/master_mobile_catalog.py
  - app/services/procurement_order_metrics.py
  - app/services/procurement_order_metrics_backfill.py
  - app/services/procurement_order_product_media.py
  - app/services/procurement_supplier_profiles.py
  - tasks/backfill_procurement_order_metrics.py
  - tasks/backfill_procurement_order_product_media.py
  - tasks/build_procurement_order_formation_dry_run.py
  - tasks/sync_procurement_order_formation_results.py
  - ui/src/components/ProcurementOrderFormationApp.tsx
  - ui/src/components/ProcurementOrderFormationWorkspace.tsx
related_tests:
  - tests/test_procurement_order_formation.py
  - tests/test_procurement_order_formation_workspace.py
  - tests/test_procurement_order_formation_api.py
  - tests/test_procurement_order_formation_dry_run.py
  - tests/test_master_mobile_catalog.py
  - tests/test_procurement_order_metrics.py
  - tests/test_procurement_order_metrics_backfill.py
  - tests/test_procurement_order_product_media.py
  - tests/test_procurement_supplier_profiles.py
contracts:
  - openapi.yaml
depends_on:
  - docs/specs/assortment-status-contour-plan.md
supersedes: []
rollout_required: true
updated_at: "2026-08-02"
---

# Приложение Bitrix24 «Формирование заказа»

Статус: production-интерфейс активен, пилотная сборка внутри сервиса выполнена,
запись заказов и свойств в 1С выключена.

Это главный технический источник по рабочей поверхности, API, правам и обмену
приложения. Канонический порядок дальнейших работ находится в
`docs/specs/assortment-status-contour-plan.md`.

Файл сохраняет старое имя только для совместимости ссылок. Рабочий интерфейс больше не использует смарт-процесс.

## Production rollout 2026-08-02

- Активный release-controller релиз:
  `procurement-order-assistant-onec-send-guard-20260802-a661db0`, source commit
  `a661db017c5c800947a9ad130a36d2a66dcd024d`.
- Production readback после публикации: `46` активных строк, `8` открытых
  проектов, `5` полностью готовых проектов и `2` строки без исходного фото.
- Пилотный проект `#14` собран внутри `pricing-service`: одна строка,
  `10` штук по `61` RUB, сумма `610` RUB. После отдельного подтверждения пакет
  `proc-order-14-v3` создал в production 1С один непроведённый и непомеченный на
  удаление черновик. Точные ссылки поставщика, договора, склада и товара
  подтверждены SQL-readback; статусы проекта и 1С — `transmitted`.
- Пакет поставщику для проекта `#14` проверен: CSV содержит поставщика, артикул,
  название, количество, цену, валюту, карточку товара и прямой URL оригинального
  фото. Внутренний класс, рентабельность и статистика брака в пакет не входят.
- Обе production-настройки записи остаются выключены:
  `PROCUREMENT_ORDER_FORMATION_PROPERTY_APPLY_ENABLED=false` и
  `PROCUREMENT_ORDER_FORMATION_ONEC_APPLY_ENABLED=false`.
- Повторная передача проекта `#14` запрещена идемпотентностью. Result-файл 1С
  вернул пустой номер документа; сам документ подтверждён по ссылке и остаётся
  непроведённым. Следующий ручной гейт — открыть черновик в 1С, проверить его и
  отдельно решить, сохранять ли текущий механизм записи без номера.

## Итоговая архитектура

- Единственная рабочая точка входа — OAuth-приложение Bitrix24 по маршруту `/bitrix/procurement-order-formation`.
- Разделы приложения: `Витрина`, `Помощник`, `Заказы`, `Свойства`, `История`.
- Смарт-процесс `1136`, старые стадии и тестовые карточки не участвуют в работе; они сохраняются скрытыми только для отката пилота.
- `pricing-service` хранит заказы, строки, версии, переходы, предложения свойств и журнал событий.
- 1С остаётся источником товарных фактов и получает только непроведённые черновики `ЗаказПоставщику` с `draft_only=true`.
- Товар связывается только по нормализованному `XML_ID = GUID номенклатуры 1С`.
- Розничная цена каталога не читается и не изменяется. Закупочная цена хранится только в строке заказа.
- `BitrixItemUrl` в пакете 1С необязателен для проектов OAuth-приложения без
  legacy-карточки; контроль подтверждения сохраняют обязательные
  `ConfirmationId`, `CalculationId` и `ApprovedBy`.

## OAuth и права

Приложение использует отдельную подписанную сессию со scope `procurement_order_formation`. Токен приложения этикеток не принимается.

- Просмотр, изменение заказа и отправка: Арсений `115204`, Омар `130757`, Эльдар `4241`.
- Жизненные переходы пилота: Омар или Эльдар.
- `ПРОДАЖА → Рабочий` для папки `дисплеи`: только Омар `130757`.
- Ручные свойства: Омар или Эльдар; самоутверждение запрещено.
- Закупочные цены доступны только пользователям приложения из указанного списка.

## Витрина и жизненные переходы

Порядок карточек фиксирован: `Плод → Новорожденный → Новинка → СП → ПРОДАЖА → Рабочий`.

- Общее количество открывает все товары статуса.
- Бейдж `К переходу N` открывает очередь утверждения.
- Для `Плод` действует отдельное правило первого закупочного действия: документы `Заказ покупателя` не меняют жизненный статус, но могут автоматически открыть кейс первого `ЗаказПоставщику`; детальное правило зафиксировано в `reports/assortment_lifecycle/2026-07-11/display-fruit-order-formation-rule-2026-07-11.md`.
- Для сильного безопасного среза `Плод` система сама считает рекомендацию и
  готовит кейс. Ответственный подтверждает итоговое количество; только после
  этого формируется непроведённый черновик. Любой блокер или неизвестное
  значение до подтверждения переводит строку в `Review`.
- Для `Рабочего` используется `На пересмотр N`, маршрут `Рабочий → Review → решение`.
- `ДН / Добор новорождённого` находится внутри очереди `Новорожденного`.
- При открытии очереди ничего не выбрано.
- `Выбрать готовые на странице` выбирает только актуальные строки без блокеров.
- Пакет ограничен `100` строками; команды `Утвердить всё` нет.
- Каждая строка фиксирует run id, хэш фактов, исходный и целевой статус, пользователя и результат.
- Возможные результаты: `approved`, `stale`, `blocked`, `conflict`, `failed`.

Во время пилота серверный флаг `PROCUREMENT_ORDER_FORMATION_PROPERTY_APPLY_ENABLED=false` формирует только dry-run XML. После результата 1С выполняется CommerceML-readback каталога по GUID.

## Заказы

Одна строка реестра — один будущий заказ поставщику, сгруппированный по поставщику, договору, валюте, складу, маршруту и партии.

Внутренний статус `draft` показывается пользователю как `Заказ на подтверждении`.
Слово `черновик` используется только для непроведённого документа, который уже
создан или передан на создание в 1С.

- Количество и закупочная цена редактируются в строке, сумма пересчитывается сразу.
- PATCH принимает ожидаемые версии заказа и строки; устаревшая версия возвращает `409`.
- Любое изменение увеличивает версию и снимает прежнее подтверждение.
- Основная команда одна: `Проверил и создать черновик в 1С`.
- Серверного запуска создания черновика без действия ответственного нет.
- Режим dry-run/apply задаётся только серверным флагом; браузер не отправляет `apply`.
- Повторная отправка той же версии возвращает тот же `message_id` и не создаёт дубликат.
- После успешной передачи заказ доступен только для просмотра.
- Реестр выгружает все строки, соответствующие текущим фильтрам, в Excel по кнопке
  `Скачать Excel`; пагинация интерфейса выгрузку не ограничивает.
- Первые колонки выгрузки фиксированы: `Предмет`, `Категория`, `Группа`,
  `Номенклатура`, `Артикул`. Классификация и артикул читаются из актуальной
  витрины `assortment_lifecycle_classification` по коду номенклатуры; отсутствие
  классификации не блокирует скачивание и даёт пустые значения.

### Автоматическое формирование из расчёта дисплеев

Штатный `display_auto_order_sync.sh` после расчёта и адаптивного сравнения
формирует заказы через `tasks.build_procurement_order_formation_dry_run`.
Прямая запись результатов автозаказа в legacy-смарт-процесс не используется.

- Каталог Bitrix проверяется пакетно по нормализованным `XML_ID` номенклатуры 1С.
- Один заказ группирует строки по поставщику, договору, валюте, складу, маршруту
  и партии.
- По умолчанию cron создаёт только JSON/CSV dry-run. Запись в БД приложения
  включается отдельным флагом `DISPLAY_AUTO_ORDER_FORMATION_PERSIST_DB=true`.
- При наличии хотя бы одного блокера пакет не сохраняется в БД.
- Повтор одного расчёта обновляет тот же открытый пакет без дублей и снимает
  прежнее подтверждение при изменении строк.
- Новый расчёт помечает прежние открытые автоматические пакеты статусом
  `superseded`; они доступны по фильтру, но не входят в обычную сводку.
- Заказы со статусом передачи или подтверждённой передачей в 1С не изменяются.
  Для новой потребности создаётся отдельная ревизия заказа.
- События создания, обновления и замены пакета фиксируются в общей истории.

Разовый безопасный запуск состоит из двух фаз: сначала команда без
`--persist-db`, затем после нулевого числа блокеров та же команда с
`--persist-db --supersede-open-batches`. Оба режима не создают документы 1С;
передача непроведённого черновика по-прежнему требует действия ответственного
в интерфейсе.

## Помощник формирования заказов

`Помощник` находится между автоматическим расчётом и реестром заказов. Расчёт
создаёт предварительно сгруппированные проекты в БД `pricing-service`, но не
подтверждает их и не передаёт в 1С. Закупщик проверяет строки в помощнике и
командой `Собрать проекты заказов` переводит полностью выбранные готовые проекты
в статус `approved`. Передача в 1С остаётся отдельным ручным действием в
разделе `Заказы`.

Рабочая таблица содержит исходное фото, потребность, поставщика, закупочную цену
и её изменение, рентабельность, процент брака и объём исторической базы, срок
поставки и решение по строке. Колонки `Что мешает` нет: блокировки показываются
через недоступность сборки и адресные сообщения.

Оригинальное фото обязательно для серверной сборки проекта. Миниатюра используется
только для быстрого просмотра; пакеты `Список + фото` и `Фото отдельно` содержат
URL исходного изображения без повторного сжатия.

Карточка и исходное фото разрешаются read-only через публичный каталог
`https://master-mobile.ru/catalog/?q=<артикул>`. Автоматически принимается только
единственное точное совпадение артикула с повторной проверкой на canonical-карточке;
сопоставление по названию запрещено. В `payload` строки сохраняются
`product_card_url`, `photos[0].thumbnail`, `photos[0].original` и
`photo_source=master_mobile_site`, а API отдельно отдаёт ссылку карточки и источник
фото. Backfill по умолчанию работает как dry-run, затрагивает только открытые строки
помощника и при apply создаёт rollback-manifest, повышает версии и пишет событие,
не меняя количество, цену, поставщика, статус, Bitrix24 или 1С.
Если строка содержит внутренний код 1С вида `РБ...`, публичный артикул определяется
только через точное соответствие `code_1c -> article` локального каталога; несколько
разных артикулов для одного кода считаются неоднозначностью и не применяются.
Повтор apply с тем же committed `--output-json` возвращает сохранённый результат без
обращения к БД и не может перезаписать rollback-manifest.

Правая панель использует версионируемый профиль по точному `supplier_ref` 1С.
Класс `A/B/C`, преимущества и внутренний комментарий вводятся согласующим вручную;
число заказов, сроки, история цен и подтверждённый брак обновляются read-only из
1С. Официальные условия оплаты относятся только к точному договору поставщика.
Если реквизит договора не заполнен, API возвращает `terms_status=missing`, а UI
показывает «Не заполнено в 1С» без использования клиентских кредитных полей.
При обогащении дополнительно подтверждаются точный `contract_ref` и его владелец
`supplier_ref`; реквизиты контроля клиентской задолженности не считаются условиями
поставщика и намеренно не читаются как отсрочка или кредитный лимит.

Метрики строки считаются за 180 дней. Рентабельность равна
`(чистая выручка − себестоимость) / себестоимость × 100`; при нулевой
себестоимости значение пустое и сопровождается причиной. Изменение цены берётся
по двум последним проведённым заказам одинаковых `поставщик + SKU + валюта`.
Срок разделён на сборку у поставщика, логистику и общий срок, с уровнем источника
`sku|display_group` и уверенностью; legacy `delivery_days` остаётся в контракте,
но UI его не использует. Если последние закупки есть только в другой валюте,
процент не рассчитывается: API возвращает `price_change_status=currency_mismatch`,
ожидаемую и доступные валюты, а UI показывает эту причину без ложного блокера.

Брак товара считается только по возвратам с причиной брак/качество/дефект.
`supplier_defect_pct` появляется только при доказанной связи с поставкой или
партией конкретного поставщика; иначе UI показывает отдельный брак товара с
пометкой «поставщик не подтверждён». База `<30` — `weak`, `30–99` — `warning`,
`>=100` — `reliable`. Сборку блокируют только изменение цены более 10% по модулю
и подтверждённый брак поставщика более 10% на базе не менее 100 штук.

## Ручные свойства

Доступные статусы: `Рабочий`, `Матричный`, `Под заказ`, `Кандидат на замену`, `Кандидат на неликвид`, `Не закупать`.

- Причина обязательна.
- При ручном минимуме обязательна дата пересмотра.
- `Не закупать`, `Кандидат на замену`, `Кандидат на неликвид` блокируют строку.
- `Под заказ` разрешён только при явной потребности.
- Утверждение создаёт `nomenclature_property_updates.v1` с ожидаемым текущим значением.
- Прямые записи в SQL 1С и прямое изменение каталога Bitrix запрещены.

## API

- `POST /api/procurement-order-formation/session`
- `GET /api/procurement-order-formation/dashboard`
- `GET /api/procurement-order-formation/lifecycle/transitions`
- `POST /api/procurement-order-formation/lifecycle/transitions/approve`
- `GET /api/procurement-order-formation/orders`
- `GET /api/procurement-order-formation/assistant`
- `POST /api/procurement-order-formation/assistant/assemble`
- `GET|PATCH /api/procurement-order-formation/suppliers/{supplier_ref}/profile`
- `POST /api/procurement-order-formation/orders/{id}/lines/{line_id}/classification/{proposal_id}/reject`
- `GET /api/procurement-order-formation/orders/export.xlsx`
- `GET /api/procurement-order-formation/orders/{id}`
- `PATCH /api/procurement-order-formation/orders/{id}`
- `PATCH /api/procurement-order-formation/orders/{id}/lines/{line_id}`
- `GET /api/procurement-order-formation/classification-proposals`
- `POST /api/procurement-order-formation/orders/{id}/send-to-1c`
- `GET /api/procurement-order-formation/events`

Legacy-маршрут `/bitrix/procurement-assortment` и чтение по `bitrix_item_id` скрыты из OpenAPI и сохраняются только на время пилота.

## Данные и миграции

- `procurement_order_formation` — шапка и версия заказа.
- `procurement_order_formation_line` — товарные строки.
- `procurement_classification_proposal` — ручные изменения свойств.
- `procurement_supplier_profile` — ручной класс и read-only факты поставщика.
- `procurement_lifecycle_transition_proposal` — очередь жизненных переходов и readback.
- `procurement_order_formation_event` — единый журнал действий и обмена.

Миграции: `7a8b9c0d1e23`, `8b9c0d1e2f34`, затем
`f3a4b5c6d7e9` для профиля поставщика и аудита отклонения классификации.

## Безопасный запуск

По умолчанию оба флага записи выключены:

```env
PROCUREMENT_ORDER_FORMATION_PROPERTY_APPLY_ENABLED=false
PROCUREMENT_ORDER_FORMATION_ONEC_APPLY_ENABLED=false
```

Требование `transmit_order()` к пользовательской сессии и запись пользователя
как согласовавшего являются обязательным контрольным шлюзом. Backend не должен
создавать черновик без подтверждения количества ответственным. Даже после
подтверждения в 1С передаётся только `draft_only`.

Порядок пилота:

1. Применить миграции и развернуть backend/UI.
2. Настроить отдельные OAuth-параметры приложения и LEFT_MENU placement.
3. Провести `3–5` рабочих дней dry-run без записи свойств и заказов в 1С.
4. Применить одно тестовое свойство и проверить readback по CommerceML.
5. После подтверждения количества ответственным создать один непроведённый
   тестовый `ЗаказПоставщику`.
6. Не включать автоматическое проведение документов.

## Проверки

```bash
./.venv/bin/python -m pytest \
  tests/test_procurement_order_formation.py \
  tests/test_procurement_order_formation_workspace.py \
  tests/test_procurement_order_formation_api.py \
  tests/test_procurement_order_formation_dry_run.py \
  tests/test_procurement_order_metrics.py \
  tests/test_procurement_order_metrics_backfill.py \
  tests/test_procurement_supplier_profiles.py \
  tests/test_ut103_procurement_orders_exporter.py -q
cd ui && npm run lint && npm run build
```

Визуальная спецификация и утверждённые макеты: `reports/assortment_lifecycle/2026-07-10/order-formation-app-final-design-2026-07-10.md`.

# Change Summary / Spec Delta

- Было: рабочая поверхность предполагалась в Bitrix smart-process.
- Стало: единственная рабочая поверхность — OAuth-приложение
  `/bitrix/procurement-order-formation`; старый smart-process сохранён только для
  rollback и не участвует в операционном потоке.
- Не меняется: 1С остаётся источником товарного факта, а создаваемые документы
  остаются непроведёнными черновиками.

# Acceptance Criteria

- [x] Приложение использует отдельную подписанную сессию и серверные права.
- [x] Повторная отправка одной версии не создаёт второй XML/message id.
- [x] Устаревшие версии и изменённые строки блокируются до повторного подтверждения.
- [x] 1С получает только `draft_only` payload; браузер не может включить apply.
- [x] Действующие API, workspace и dry-run сценарии покрыты тестами.
- [x] Расчёт заказов скачивается в Excel с классификацией и текущими фильтрами.
- [x] Метрики содержат период, источник, надёжность и честное разделение брака.
- [x] Профиль поставщика защищён optimistic version и правами согласующего.
- [x] Автор не может принять или отклонить своё предложение; причина отклонения обязательна.
- [x] Backfill метрик поддерживает dry-run, apply, повторный запуск и rollback-manifest.

# Source of Truth

- `pricing-service` хранит заказы, строки, версии, переходы и журнал событий.
- `1С УТ 10.3` хранит товарный факт и создаёт только непроведённые черновики
  `ЗаказПоставщику` после серверной проверки.
- Bitrix24 OAuth-приложение является рабочей поверхностью, но не аналитической БД.

# API / Data Contracts

- Каноничный HTTP-контракт экспортируется в `openapi.yaml`.
- Публичные маршруты находятся под `/api/procurement-order-formation`.
- Запись использует optimistic versions; stale update возвращает `409`.
- Файловый обмен с 1С использует версионированные XML payload/result и
  `draft_only=true`.

# Implementation Checklist

- [x] Модели заказов, строк, событий и lifecycle transitions.
- [x] API, backend auth и Bitrix OAuth application surface.
- [x] Dry-run builder, result sync и idempotent send-to-1C.
- [x] Workspace UI и серверная проверка прав.
- [x] Unit/integration tests и OpenAPI export.
- [ ] Production pilot включается отдельным rollout-решением после live dry-run.

# Tests

- `tests/test_procurement_order_formation.py` — модели и бизнес-правила.
- `tests/test_procurement_order_formation_workspace.py` — workspace read model.
- `tests/test_procurement_order_formation_api.py` — HTTP и права.
- `tests/test_procurement_order_formation_dry_run.py` — dry-run и payload.
- `tests/test_procurement_order_metrics.py` — формулы и атрибуция брака.
- `tests/test_procurement_order_metrics_backfill.py` — безопасный backfill/rollback.
- `tests/test_procurement_supplier_profiles.py` — версии и права профиля.

# Rollout

Пилот остаётся выключенным. Перед включением требуется свежий dry-run, проверка
server flags, тестовый XML round-trip с 1С, smoke OAuth-сессии и подтверждённый
rollback на скрытый старый контур. Production side effects не включаются только
фактом наличия этой реализации.
