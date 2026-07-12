---
spec_id: "executive-management-dashboard-bitrix"
title: "Executive Management Dashboard In Bitrix"
doc_type: spec
domain: management
status: draft
owner: finance
source_of_truth: true
related_code:
  - app/api/bitrix_executive_dashboard.py
  - app/api/management.py
  - app/models/executive_dashboard.py
  - app/schemas/executive_dashboard.py
  - app/services/bitrix_executive_dashboard_auth.py
  - app/services/executive_dashboard.py
  - ui/src/api/executiveDashboard.ts
  - ui/src/components/ExecutiveDashboard.tsx
related_tests:
  - tests/test_executive_dashboard.py
contracts:
  - openapi.yaml
depends_on: []
supersedes: []
rollout_required: true
updated_at: "2026-06-29"
---

# Executive Management Dashboard In Bitrix

## Status

Draft / v1 implementation foundation.

## Context

`Bitrix24 Box` является рабочим экраном руководителя, но не базой истины для
финансовых расчетов. Единая управленческая витрина должна утром ответить на
короткий управленческий вопрос: где деньги, кто должен, кому платить, что не
сошлось и какие 5-10 решений нужно принять сегодня.

Расчеты и первичные источники остаются в профильных контурах:

- `1C` — торговый, бухгалтерский и платежный факт.
- `mm-compensation` — финансовые snapshots, ДДС, сверки, кредиторка.
- `pricing-service` — агрегатор, management API, Bitrix embedded workplace.
- `Bitrix24` — рабочий экран, задачи, smart-процессы, уведомления и ссылки на
  действие.

## Scope v1

Делаем read-only Bitrix-first cockpit:

- local app / iframe-раздел `/bitrix/executive-dashboard/`;
- API `/api/management/executive-dashboard`;
- API `/api/management/executive-dashboard/actions`;
- API `/api/management/executive-dashboard/profit-loss-period`;
- короткая Bitrix-сессия через `POST /api/bitrix/executive-dashboard/session`;
- десять обязательных блоков: деньги, отчет о прибылях и убытках, дебиторка
  покупателей, контроль дебиторки, кредиторка, закупки, склад, сверки, задачи,
  фокус дня;
- явные статусы `ready`, `partial`, `stale`, `source_missing`, `source_error`;
- `executive_action_item` как кросс-доменный список решений без автопубликации
  задач в v1.

Не делаем в v1:

- автоплатежи поставщикам;
- запись в `1C`;
- замену бухгалтерии;
- универсальный BI вместо рабочих задач.

## Data Ownership

| Блок | System of record | Runtime reader |
| --- | --- | --- |
| Деньги / ДДС | `mm-compensation` / `finance.fact_cash_position_daily`, `finance.fact_cashflow_daily`, `finance.fact_cashflow_movement` | `pricing-service` читает compact JSON |
| Отчет о прибылях и убытках | `1C` факт продаж, v1 таблица `onec_sales_daily_kpi`; расходы v1 по оплатам ДДС из `mm-compensation` | `pricing-service` |
| Дебиторка покупателей | buyers-сегмент текущих receivables cases в `pricing-service` | `pricing-service` |
| Контроль дебиторки | рабочее место дебиторки, work items, кеш `Контроль папок` | `pricing-service` |
| Кредиторская задолженность | `1C: Задолженность поставщикам товаров / Поставщики; Взаиморасчеты с контрагентами / СОТРУДНИКИ` | `mm-compensation` готовит compact JSON |
| Закупки / импорт | procurement decision contour | `pricing-service` + compact finance snapshot |
| Склад / сборка | `mm-compensation` / `piecework.fact_transfer_header`, `piecework.fact_transfer_lines`, `piecework.fact_transfer_corrections` | `pricing-service` читает compact JSON |
| Сверки | `mm-compensation` jobs Sber / CloudPayments / acquiring / ДДС | `pricing-service` читает compact JSON |
| Задачи | Bitrix cached/read-only task-health, `executive_action_item` | `pricing-service` |
| Фокус дня | `executive_action_item` | `pricing-service` |

## Creditors / Payables Rule

Кредиторская задолженность является обязательным блоком v1. Ее нельзя подменять
закупочным прогнозом или платежным календарем. Канонические источники:

```text
1C: Задолженность поставщикам товаров / Поставщики
1C: Взаиморасчеты с контрагентами / СОТРУДНИКИ
```

Для групп `Поставщики` и `СОТРУДНИКИ` долг компании — положительное конечное
сальдо в колонке 1С `Сумма (руб)`. Отрицательное сальдо — аванс или переплата:
оно передаётся отдельно и уменьшает общий долг. В интерфейсе показываются чистый
итог и отдельные чистые суммы поставщикам и сотрудникам; долг компании выводится
со знаком `−`. Валовой долг, авансы и список контрагентов остаются техническими
данными snapshot, но не выводятся на витрине. Блок не создаёт платежи и не меняет 1С.

## API Contract

### `GET /api/management/executive-dashboard?date=YYYY-MM-DD`

Возвращает:

- `blocks[]` — только разрешенные пользователю блоки витрины;
- `source_freshness[]` — свежесть только разрешенных источников;
- `top_actions[]` — 5-10 решений дня;
- `access_level` — `full` или `domain`.
- `roles[]`, `allowed_blocks[]`, `allowed_action_domains[]` — backend-policy,
  по которой был собран ответ.

Security boundary находится на backend. UI не должен получать недоступные блоки
и не считается защитой данных. Для `full` доступа API возвращает все блоки
и все суммы. Для ролевого `domain` доступа API возвращает только разрешенные
блоки и действия; финансовые суммы маскируются во всех денежных блоках, которых
нет в `money_blocks` политики. Внутри разрешенных блоков первый экран может
сжимать `source_missing` / `source_error` карточки до плейсхолдера и скрывать
нулевые или дублирующие диагностические метрики, но не должен скрывать сам факт
отсутствующего источника.

Блок `Дебиторка покупателей` должен считаться из buyers-сегмента
`receivable_case`, а не из общего signed-среза всех взаиморасчетов. В карточке
показываются только показатели первого экрана: долг покупателей, просрочка,
90+ и количество клиентов. Сумма к звонку сегодня и 30+ остаются в `summary`
для диагностики и детализации, но не дублируют просрочку в карточке. У блока
должен быть `drilldown_url` в рабочее место дебиторки:

```text
/bitrix/receivables/?date=YYYY-MM-DD
```

Это основной провал из управленческой витрины в уже рабочий операционный экран
`Дебиторка покупателей`; смарт-процесс и BI остаются источниками/деталями ниже,
но первый клик руководителя ведет в приложение.

Блок `Контроль дебиторки` отделен от суммы долга. Он показывает очередь действий
и качество данных: сколько клиентов нужно прозвонить, где нет телефона, где
применяется расчетный срок `7 дней`, сколько переносов оплаты уже зафиксировано,
и сколько строк во вкладке `Контроль папок` требует ручной проверки. Нулевые
диагностические счетчики не обязаны попадать в `metrics` первого экрана и могут
оставаться в `summary`, чтобы не создавать ложные KPI. Клик ведет в рабочее
место сразу на вкладку контроля:

```text
/bitrix/receivables/?date=YYYY-MM-DD&tab=folders
```

Этот блок не должен запускать запись в `1С` или автоперенос папок. Будущие
команды бота по глубине кредита, лимиту или папке контрагента идут только через
утвержденный файловый обмен `UT103_EXCHANGE_ROOT` в режимах `dry_run/apply`.

Блок `Деньги / ДДС` показывает реальные остатки только из
`money_today.cash_position`, а дневной ДДС только из
`money_today.cashflow_today`. Legacy-поля `bank_balance` и `cash_balance`
остаются в snapshot для совместимости, но UI не должен выводить их как остатки:
это net движения за день. Если `cash_position` отсутствует или не прошел
валидацию, карточка показывает компактный статус `source_missing` / `partial`
по остаткам и продолжает показывать ДДС отдельно, если он свежий.

Для CFO-карточки остатки в KPI всегда выводятся в рублевом эквиваленте 1C:
`total_balance_rub`, `bank_balance_total_rub`, `cashbox_balance_total_rub`.
Натуральная валюта счета/кассы выводится не как KPI, а отдельной компактной
таблицей `breakdown_by_currency`: вид денег, валюта, остаток в валюте и остаток
в рублях. Это защищает экран от смешивания USD/EUR/RMB и рублей в одном числе.

На вкладке `Сегодня` карточка `Фокус дня` не дублируется в общей сетке блоков,
потому что ниже уже есть отдельная секция решений дня. Блок остается в API и в
линии управленческой витрины для совместимости и статуса источника.

Верхняя полоса карточек является контекстной. На вкладке `Сегодня` она остается
линией всей управленческой витрины по разрешенным доменам. На доменных вкладках
она показывает только KPI выбранного контура, чтобы финансы, дебиторка, закупки
и задачи не конкурировали за внимание на чужом экране. Для `Деньги / ДДС`
верхняя полоса строится как CFO-срез решений, а не как повтор таблицы остатков:
общий остаток в рублевом эквиваленте, валютная позиция, сверки `Сбер / 1С`,
ошибки ДДС и компактная визуализация дневного ДДС `поступило / списано / net`.
Подробная карточка на этой же вкладке не должна повторять эти крупные KPI:
остатки раскрываются как структура по счетам, кассам, картам/эквайрингу и
прочим деньгам; ДДС раскрывается как детализация движений и контрольных строк.
Историческую динамику остатков можно добавлять только после появления
проверенного ряда дневных snapshot-ов; до этого нельзя рисовать тренд из одного
дня.

Следующие CFO-KPI для развития финансовой вкладки: платежный горизонт на
7/14/30 дней, деньги в пути и внутренние переводы, просроченные исходящие
платежи, план-факт ДДС, остаток доступных лимитов/овердрафтов, концентрация
денег по банкам и валютный риск. Их можно выводить только после появления
подтвержденного источника; неподключенный показатель остается в `summary` или
показывается как честный `source_missing`, но не как нулевой факт.

Витрина показывает отдельную вкладку `Прибыли / убытки`. V1 строится из
`onec_sales_daily_kpi`: выручка, себестоимость продаж, валовая прибыль, валовая
маржа, дневная динамика и разрезы по магазинам и менеджерам. Операционные
расходы v1 берутся из `cashflow_period_cache.profit_loss_expenses`: это
cash-based управленческий срез по исходящим оплатам ДДС, а не бухгалтерское
начисление по `Поступлениям услуг`.

В операционные расходы ОПУ v1 входят только утвержденные ДДС-подгруппы:
аренда, ФОТ, логистика/транспорт, IT/связь, комиссии банка и прочие
операционные статьи. Поставщики, кредиты, собственники, оборудование,
внутренние переводы, возвраты покупателям, налоги и неразмеченные статьи не
задваиваются в расходах; они выводятся как открытые вопросы или отдельные
неоперационные движения. Чистая прибыль остается `source_missing`, пока не
утверждены правила по налогам, прочим доходам/расходам, кредитам и капвложениям.

### `GET /api/management/executive-dashboard/profit-loss-period`

Параметры:

- `date_from`, `date_to`.

Endpoint возвращает данные только пользователям, у которых есть доступ к блоку
`profit_loss` и право видеть денежные суммы. Для ролей без денежного доступа
возвращается `403`; UI не является security boundary. По умолчанию периодом
является месяц к выбранной дате витрины. Ответ содержит `totals`, `lines`,
`daily`, `by_store`, `by_manager`, `expense_breakdown`,
`expense_open_questions`, `expense_source_status` и `filters.source_table`.

Витрина показывает отдельную вкладку `ОДДС CashFlow`. Она использует тот же
доступ к денежному блоку `money_today`, но отделяет финансовую форму от
оперативной карточки `Деньги / ДДС`. Вкладка читает отдельный rolling cache:

```text
../mm-compensation/build/executive_dashboard/cashflow_period_cache.json
```

Кэш строится в `mm-compensation` из `finance.fact_cashflow_movement`,
`finance.fact_cash_position_daily` и `finance.fact_cashflow_quality_issue`.
`pricing-service` не ходит в 1C на каждый клик пользователя: он фильтрует уже
подготовленные строки кэша по периоду, статье/группе ДДС, счету/кассе, валюте и
направлению. Внутренние переводы показываются отдельно и могут быть выключены из
периодного анализа.
Ошибки контроля берутся из компактного `quality_daily`: это сохраняет точные
счетчики и суммы по периоду, а `quality_issues` остается коротким списком
примеров для экрана.

Форма на вкладке собирает остаток на начало, CFO/CFI/CFF, Free Cash Flow,
чистый денежный поток по ОДДС, внутренние перемещения справочно, изменение
остатка по регистру, остаток на конец и контрольную строку.

Первый набор коэффициентов для финансиста:

- `days_cash_on_hand` — сколько дней проживем на текущем остатке при среднем
  дневном внешнем расходе;
- `average_daily_external_outflow` — средний дневной внешний расход;
- `inflow_outflow_coverage` — покрытие списаний поступлениями;
- `internal_turnover_share` — доля внутренних переводов в обороте;
- `review_share` — доля строк ДДС на проверку качества;
- `net_cashflow_margin` — внешний net к поступлениям.

Коэффициенты `cash ratio`, `quick ratio`, DPO, платежный горизонт и полный cash
conversion cycle подключаются только после появления надежной кредиторки и
платежного календаря.

### `GET /api/management/executive-dashboard/cashflow-period`

Параметры:

- `date_from`, `date_to`;
- `dds_group`;
- `cash_account_ref`;
- `currency`;
- `direction`;
- `include_internal`, по умолчанию `true`.

Endpoint возвращает только пользователям, у которых есть доступ к блоку
`money_today` и право видеть денежные суммы. Для ролей без денежного доступа
возвращается `403`; UI не является security boundary.

Карточка `Закупки / импорт` не показывает `Валюта` как отдельный KPI, если сумма
полностью совпадает с `Готовность к оплате`. Карточка `Сверки` оставляет главным
сигналом `Не сошлось`, а нулевые диагностические счетчики показывает только при
ненулевом значении. Детальная свежесть источников по умолчанию свернута в строку
`Источники данных` с числом проблемных источников.

Вкладка `Склад` читает отдельный compact snapshot:

```text
../mm-compensation/build/executive_dashboard/warehouse_snapshot.json
```

Snapshot строится в `mm-compensation` из схемы `piecework` после загрузки факта
перемещений из 1С. Первый экран показывает объем сборки, количество сборщиков,
расчетную потребность в людях по логике прежнего Power BI / сдельной оплаты,
практический пик и контроль качества таймингов. `pricing-service` не ходит в 1С
на каждый клик и не записывает данные обратно; отсутствие snapshot показывается
как `source_missing`, а не как нулевой складской факт.

Вкладка `Сверки` должна показывать регулярный финансовый контроль, который уже
формируется и отправляется задачами: счетчик расхождений, абсолютную сумму дельт,
типы ошибок из `issues.csv`, 3-5 примеров строк, статус отправленного Bitrix
отчета (`task_ids`, файл) и отдельный счетчик ошибок качества ДДС из
`finance.fact_cashflow_quality_issue`. Для CFO это не BI-таблица, а список
исключений: что не сошлось, где, на какую дельту и какой следующий шаг.

### `GET /api/management/executive-dashboard/actions`

Параметры:

- `date`;
- `status`, по умолчанию `open`;
- `domain`;
- `limit`.

Если `domain` не входит в `allowed_action_domains` текущей сессии, API должен
вернуть `403`. Для `personal` доступа дополнительно действует фильтр:
возвращаются только действия, где пользователь ответственный, или публичные
действия без ответственного.

Каждое действие имеет:

- `stable_key`;
- `domain`;
- `severity`;
- `amount`;
- `responsible_bitrix_user_id`;
- `deadline_at`;
- `source_system`;
- `source_ref`;
- `dedupe_key`;
- `drilldown_url`.

### `POST /api/bitrix/executive-dashboard/session`

Получает Bitrix launch payload, проверяет `user.current`, выдает короткий bearer
token для iframe.

## Roles

- `full` — собственник, директор, финансовый контроль: видит все 8 блоков, все
  действия и все суммы.
- `procurement` — отдел закупки: `procurement_import`, закупочные действия,
  суммы только в закупочном блоке.
- `receivables` — контроль дебиторки: `debtors`, `receivables_control`,
  действия по дебиторке; суммы покупателей остаются скрытыми, если блок не
  указан в `money_blocks`.
- `finance` — финансовый контур: `money_today`, `debtors`,
  `receivables_control`, `creditors_payables`, `reconciliation`; закупочный
  операционный блок не возвращается, если он не указан в policy.
- `personal` — личный фокус: `tasks`, `daily_focus`; действия только свои или
  публичные; суммы скрыты.

Если пользователь попадает сразу в несколько ролей, разрешения объединяются.
`full` всегда побеждает. Старый список
`EXECUTIVE_DASHBOARD_BITRIX_DOMAIN_ACCESS_USER_IDS` остается совместимым
fallback-режимом: пользователь видит прежнюю структуру с маскировкой сумм и
фильтром своих/публичных действий.

Настройки:

- `EXECUTIVE_DASHBOARD_BITRIX_ENABLED`;
- `EXECUTIVE_DASHBOARD_BITRIX_ALLOWED_DOMAINS`;
- `EXECUTIVE_DASHBOARD_BITRIX_ALLOWED_MEMBER_IDS`;
- `EXECUTIVE_DASHBOARD_BITRIX_FULL_ACCESS_USER_IDS`;
- `EXECUTIVE_DASHBOARD_BITRIX_DOMAIN_ACCESS_USER_IDS`;
- `EXECUTIVE_DASHBOARD_ACCESS_RULES_JSON`;
- `EXECUTIVE_DASHBOARD_BITRIX_SESSION_SECRET`;
- `EXECUTIVE_DASHBOARD_FINANCE_SNAPSHOT_PATH`.

Пример `EXECUTIVE_DASHBOARD_ACCESS_RULES_JSON`:

```json
{
  "roles": [
    {
      "role": "procurement",
      "bitrix_user_ids": ["201"],
      "allowed_blocks": ["procurement_import"],
      "allowed_action_domains": ["procurement_import"],
      "money_blocks": ["procurement_import"]
    },
    {
      "role": "finance",
      "bitrix_user_ids": ["203"]
    }
  ]
}
```

## Refresh SLA

- dashboard API — быстрый cached/read-only ответ;
- finance snapshot — целевой лаг до 1 дня;
- procurement snapshot — полный read-only список открытых `cargo + ved_import`,
  обновление в 10:35; после 11:00 `stale/missing/source_error` считается ошибкой мониторинга;
- payables snapshot — read-only срез кредиторской задолженности 1С в 10:40;
  после 11:00 `stale/missing/source_error` блока `creditors_payables` считается
  ошибкой мониторинга;
- receivables — целевой лаг до 1 дня;
- actions — идемпотентные записи по `stable_key` и `dedupe_key`;
- закупочные actions формируются из `procurement_import.attention_items`: первые 10
  показываются сразу, полный список раскрывается по кнопке;
- клик открывает карточку решения с номером `Заказа поставщику` и полем 1С для
  исправления; витрина не пишет в 1С, а убирает действие после подтверждения в
  следующем snapshot;
- отсутствующий источник показывает `source_missing`, а не нулевой факт.

## Rollout

1. Read-only dashboard на тестовых snapshots.
2. Bitrix iframe для 2-3 руководителей без публикации задач.
3. Drill-down и action list.
4. Deduplicated Bitrix-задачи только после dry-run и сверки `dedupe_key`.

## Deployment Notes 2026-06-27

Текущий public handler для Bitrix Box:

```text
https://bitrix-app.offonika.ru/bitrix/executive-dashboard/
```

Публичные nginx routes должны проксировать в `pricing-service`:

- `/bitrix/executive-dashboard`;
- `/bitrix/executive-dashboard/`;
- `/bitrix/executive-dashboard/*`;
- `/api/bitrix/executive-dashboard/session`;
- `/api/management/executive-dashboard*`.

На 2026-06-27 текущий Box webhook для `BITRIX_BOX_WEBHOOK_BASE` возвращает `403`
на `placement.get`, поэтому автоматическая внешняя проверка/привязка placement
через webhook недоступна. Регистрацию local app нужно делать через Bitrix local
app/admin context или через OAuth-контекст установленного приложения.

Локальная готовность v1:

- Alembic head: `3d4e5f6a7b80`;
- finance snapshot path:
  `../mm-compensation/build/executive_dashboard/finance_snapshot.json`;
- snapshot может честно отдавать `source_missing`, пока реальные finance/payables
  inputs не подключены;
- dashboard API должен возвращать 8 блоков даже при частично отсутствующих
  источниках.

## Test Plan

- API: свежие, старые и отсутствующие источники.
- Access: `domain` доступ не видит финансовые суммы.
- Receivables: сумма `Дебиторка покупателей` совпадает с buyers-сегментом
  `receivable_case`, а не с общим signed-срезом.
- Receivables control: очередь действий и `Контроль папок` выводятся отдельным
  блоком без write-back в `1С`.
- Payables: кредиторская задолженность идет из двух групп взаиморасчетов 1С:
  `Поставщики` и `СОТРУДНИКИ`.
- Idempotency: повторный запуск не создает дубли actions.
- Bitrix iframe smoke: страница, session, загрузка блоков, drill-down links.
- Docs/OpenAPI: manifest и generated contract без drift.
