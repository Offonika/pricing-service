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
  - tasks/build_procurement_order_formation_dry_run.py
  - tasks/sync_procurement_order_formation_results.py
  - ui/src/components/ProcurementOrderFormationApp.tsx
  - ui/src/components/ProcurementOrderFormationWorkspace.tsx
related_tests:
  - tests/test_procurement_order_formation.py
  - tests/test_procurement_order_formation_workspace.py
  - tests/test_procurement_order_formation_api.py
  - tests/test_procurement_order_formation_dry_run.py
contracts:
  - openapi.yaml
depends_on:
  - docs/specs/procurement-order-auto-order-unified-contour.md
supersedes: []
rollout_required: true
updated_at: "2026-07-12"
---

# Приложение Bitrix24 «Формирование заказа»

Статус: implemented locally, pilot disabled.

Файл сохраняет старое имя только для совместимости ссылок. Рабочий интерфейс больше не использует смарт-процесс.

## Итоговая архитектура

- Единственная рабочая точка входа — OAuth-приложение Bitrix24 по маршруту `/bitrix/procurement-order-formation`.
- Разделы приложения: `Витрина`, `Заказы`, `Свойства`, `История`.
- Смарт-процесс `1136`, старые стадии и тестовые карточки не участвуют в работе; они сохраняются скрытыми только для отката пилота.
- `pricing-service` хранит заказы, строки, версии, переходы, предложения свойств и журнал событий.
- 1С остаётся источником товарных фактов и получает только непроведённые черновики `ЗаказПоставщику` с `draft_only=true`.
- Товар связывается только по нормализованному `XML_ID = GUID номенклатуры 1С`.
- Розничная цена каталога не читается и не изменяется. Закупочная цена хранится только в строке заказа.

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
- Для `Плод` действует отдельное правило первого закупочного действия: документы `Заказ покупателя` не меняют жизненный статус, но могут открыть ручной кейс или автосоздание черновика первого `ЗаказПоставщику`; детальное правило зафиксировано в `reports/assortment_lifecycle/2026-07-11/display-fruit-order-formation-rule-2026-07-11.md`.
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

- Количество и закупочная цена редактируются в строке, сумма пересчитывается сразу.
- PATCH принимает ожидаемые версии заказа и строки; устаревшая версия возвращает `409`.
- Любое изменение увеличивает версию и снимает прежнее подтверждение.
- Основная команда одна: `Проверил и создать черновик в 1С`.
- Режим dry-run/apply задаётся только серверным флагом; браузер не отправляет `apply`.
- Повторная отправка той же версии возвращает тот же `message_id` и не создаёт дубликат.
- После успешной передачи заказ доступен только для просмотра.

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
- `procurement_lifecycle_transition_proposal` — очередь жизненных переходов и readback.
- `procurement_order_formation_event` — единый журнал действий и обмена.

Миграции: `7a8b9c0d1e23`, затем `8b9c0d1e2f34`.

## Безопасный запуск

По умолчанию оба флага записи выключены:

```env
PROCUREMENT_ORDER_FORMATION_PROPERTY_APPLY_ENABLED=false
PROCUREMENT_ORDER_FORMATION_ONEC_APPLY_ENABLED=false
```

Порядок пилота:

1. Применить миграции и развернуть backend/UI.
2. Настроить отдельные OAuth-параметры приложения и LEFT_MENU placement.
3. Провести `3–5` рабочих дней dry-run без записи свойств и заказов в 1С.
4. Применить одно тестовое свойство и проверить readback по CommerceML.
5. Создать один непроведённый тестовый `ЗаказПоставщику`.
6. Не включать автоматическое проведение документов.

## Проверки

```bash
./.venv/bin/python -m pytest \
  tests/test_procurement_order_formation.py \
  tests/test_procurement_order_formation_workspace.py \
  tests/test_procurement_order_formation_api.py \
  tests/test_procurement_order_formation_dry_run.py \
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

# Rollout

Пилот остаётся выключенным. Перед включением требуется свежий dry-run, проверка
server flags, тестовый XML round-trip с 1С, smoke OAuth-сессии и подтверждённый
rollback на скрытый старый контур. Production side effects не включаются только
фактом наличия этой реализации.
