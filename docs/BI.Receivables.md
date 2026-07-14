<!-- File: docs/BI.Receivables.md -->

# BI-витрины по дебиторке для Power BI

Назначение: быстро сверять дебиторку в Power BI до включения/перенастройки задач в `Bitrix24`.

Источники данных:
- `GET /api/bi/receivables-current?date=YYYY-MM-DD`
- `GET /api/bi/receivable-cases?date=YYYY-MM-DD&segment=...`
- `GET /api/bi/receivables-manager-summary?date=YYYY-MM-DD`
- `GET /api/bi/receivables-contract-balances?date=YYYY-MM-DD`

Рекомендуемый стартовый набор таблиц:
- `ДебиторкаТекущая` — текущие signed-остатки по контрагентам;
- `ДебиторкаНачальныеОстатки` — импортированные начальные остатки из 1С для полной сверки по всем контрагентам;
- `ДебиторкаСверка1С` — signed-остатки для буквальной сверки с выгрузками 1С, включая отрицательные строки;
- `ДебиторкаКейсы` — сегменты дебиторки (`new_daily`, `employee`, `fired_manager`, `inactive`, `adjustment_candidates`);
- `ДебиторкаМенеджеры` — агрегат по менеджерам;
- `ДебиторкаДоговоры` — детализация по договорам, виду договора и слою расчёта.
- `ДебиторкаДоговоры1С` — сверка с отчетом 1С `Ведомость по взаиморасчетам с контрагентами`
  в режиме `ПОКУПАТЕЛИ + руб`.

Практически для первой сверки лучше подключаться напрямую к PostgreSQL, чтобы убрать из уравнения API-слой и видеть сырые данные витрин.

---

## 1. Параметры Power BI

В Power BI удобно завести два текстовых параметра:

- `BaseUrl`
  - пример: `https://price.mm.offonika.ru`
- `SnapshotDate`
  - пример: `2026-03-21`

Если позже на BI-эндпоинты будет добавлен bearer token, можно завести ещё параметр:

- `ApiToken`
  - по умолчанию пустая строка

---

## 2. Query: `ReceivablesCurrent`

```powerquery
let
    BaseUrl = Text.Trim(BaseUrl),
    SnapshotDate = Text.Trim(SnapshotDate),
    ApiToken = try Text.Trim(ApiToken) otherwise "",
    Headers =
        if ApiToken = "" then
            [Accept = "application/json"]
        else
            [
                Accept = "application/json",
                Authorization = "Bearer " & ApiToken
            ],
    Raw = Json.Document(
        Web.Contents(
            BaseUrl,
            [
                RelativePath = "api/bi/receivables-current",
                Query = [date = SnapshotDate],
                Headers = Headers
            ]
        )
    ),
    AsTable = Table.FromRecords(Raw),
    Typed = Table.TransformColumnTypes(
        AsTable,
        {
            {"snapshot_date", type date},
            {"counterparty_ref", type text},
            {"counterparty_name", type text},
            {"current_balance", Currency.Type},
            {"aged_bucket", type text},
            {"activity_segment", type text},
            {"is_overdue", type logical},
            {"overdue_days", Int64.Type},
            {"due_date", type datetime},
            {"planned_payment_date", type datetime},
            {"credit_depth_days", Int64.Type},
            {"payment_term_source", type text},
            {"shipment_ban", type logical},
            {"origin_document_ref", type text},
            {"origin_document_number", type text},
            {"origin_document_date", type datetime},
            {"origin_manager_ref", type text},
            {"origin_manager_name", type text},
            {"current_manager_ref", type text},
            {"current_manager_name", type text},
            {"last_sale_at", type datetime},
            {"last_payment_at", type datetime}
        },
        "ru-RU"
    )
in
    Typed
```

Что даёт:
- главный реестр для сверки остатков;
- можно строить top контрагентов, aged buckets, overdue, разрезы по менеджерам.

Важно:
- `ДебиторкаТекущая` теперь хранит signed-остаток;
- если нужен старый режим только по положительной дебиторке, добавляй фильтр `current_balance > 0`.

### Вариант для прямого подключения к PostgreSQL

Если сверяемся и хотим убрать из уравнения API-слой, лучше сначала подключаться напрямую к БД.

Параметры:
- сервер: `mm.offonika.ru:55433`
- база: `pricing`

```powerquery
let
    SnapshotDate = "2026-03-21",
    Source = PostgreSQL.Database(
        "mm.offonika.ru:55433",
        "pricing",
        [
            Query =
"
select
  snapshot_date,
  counterparty_ref,
  counterparty_name,
  current_balance,
  aged_bucket,
  activity_segment,
  is_overdue,
  overdue_days,
  due_date,
  planned_payment_date,
  credit_depth_days,
  payment_term_source,
  shipment_ban,
  origin_document_ref,
  origin_document_number,
  origin_document_date,
  origin_manager_ref,
  origin_manager_name,
  current_manager_ref,
  current_manager_name,
  last_sale_at,
  last_payment_at
from receivable_balance_snapshot
where snapshot_date = date '" & SnapshotDate & "'
order by current_balance desc, counterparty_ref
"
        ]
    ),
    #"Измененный тип" = Table.TransformColumnTypes(
        Source,
        {
            {"snapshot_date", type date},
            {"counterparty_ref", type text},
            {"counterparty_name", type text},
            {"current_balance", Currency.Type},
            {"aged_bucket", type text},
            {"activity_segment", type text},
            {"is_overdue", type logical},
            {"overdue_days", Int64.Type},
            {"due_date", type datetime},
            {"planned_payment_date", type datetime},
            {"credit_depth_days", Int64.Type},
            {"payment_term_source", type text},
            {"shipment_ban", type logical},
            {"origin_document_ref", type text},
            {"origin_document_number", type text},
            {"origin_document_date", type datetime},
            {"origin_manager_ref", type text},
            {"origin_manager_name", type text},
            {"current_manager_ref", type text},
            {"current_manager_name", type text},
            {"last_sale_at", type datetime},
            {"last_payment_at", type datetime}
        }
    )
in
    #"Измененный тип"
```

---

## 3. Query: `ReceivableCases`

```powerquery
let
    BaseUrl = Text.Trim(BaseUrl),
    SnapshotDate = Text.Trim(SnapshotDate),
    Segment = "employee",
    ApiToken = try Text.Trim(ApiToken) otherwise "",
    Headers =
        if ApiToken = "" then
            [Accept = "application/json"]
        else
            [
                Accept = "application/json",
                Authorization = "Bearer " & ApiToken
            ],
    Raw = Json.Document(
        Web.Contents(
            BaseUrl,
            [
                RelativePath = "api/bi/receivable-cases",
                Query = [
                    date = SnapshotDate,
                    segment = Segment
                ],
                Headers = Headers
            ]
        )
    ),
    AsTable = Table.FromRecords(Raw),
    Typed = Table.TransformColumnTypes(
        AsTable,
        {
            {"snapshot_date", type date},
            {"segment", type text},
            {"owner_type", type text},
            {"recommendation", type text},
            {"counterparty_ref", type text},
            {"counterparty_name", type text},
            {"current_balance", Currency.Type},
            {"aged_bucket", type text},
            {"activity_segment", type text},
            {"is_overdue", type logical},
            {"overdue_days", Int64.Type},
            {"due_date", type datetime},
            {"planned_payment_date", type datetime},
            {"credit_depth_days", Int64.Type},
            {"payment_term_source", type text},
            {"shipment_ban", type logical},
            {"origin_document_ref", type text},
            {"origin_document_number", type text},
            {"origin_document_date", type datetime},
            {"origin_manager_ref", type text},
            {"origin_manager_name", type text},
            {"current_manager_ref", type text},
            {"current_manager_name", type text}
        },
        "ru-RU"
    )
in
    Typed
```

---

## 2.1. Query: `ReceivablesContractBalances1C`

Для буквальной сверки с отчетом 1С, где включены фильтры:
- папка контрагентов `ПОКУПАТЕЛИ`;
- взаиморасчеты `руб`.

Используй `buyers_rub_only=true` на контрактной витрине:

```powerquery
let
    BaseUrl = Text.Trim(BaseUrl),
    SnapshotDate = Text.Trim(SnapshotDate),
    ApiToken = try Text.Trim(ApiToken) otherwise "",
    Headers =
        if ApiToken = "" then
            [Accept = "application/json"]
        else
            [
                Accept = "application/json",
                Authorization = "Bearer " & ApiToken
            ],
    Raw = Json.Document(
        Web.Contents(
            BaseUrl,
            [
                RelativePath = "api/bi/receivables-contract-balances",
                Query = [
                    date = SnapshotDate,
                    buyers_rub_only = "true"
                ],
                Headers = Headers
            ]
        )
    ),
    AsTable = Table.FromRecords(Raw),
    Typed = Table.TransformColumnTypes(
        AsTable,
        {
            {"snapshot_date", type date},
            {"counterparty_ref", type text},
            {"counterparty_name", type text},
            {"contract_ref", type text},
            {"contract_name", type text},
            {"contract_kind_ref", type text},
            {"contract_kind_name", type text},
            {"source_layer", type text},
            {"current_balance", Currency.Type},
            {"event_count", Int64.Type},
            {"last_event_at", type datetime}
        },
        "ru-RU"
    )
in
    Typed
```

Это именно сверочный срез. Он уже режет данные до режима:
- контрагентов из группы `ПОКУПАТЕЛИ`;
- рублевого текущего остатка на дату среза.

Важное уточнение:
- это контрагентный сверочный срез, а не буквальная договорная расшифровка;
- `contract_ref` и `contract_name` могут быть пустыми;
- это сделано специально, потому что в движениях оплаты и урегулирования часто приходят без договора, а в отчете 1С уже схлопнуты в конечный рублевый остаток.

Как использовать:
- сделай копии запроса и меняй `Segment`:
  - `"new_daily"`
  - `"employee"`
  - `"fired_manager"`
  - `"inactive"`
  - `"adjustment_candidates"`

Если хочется одной таблицей все сегменты сразу, убери `segment = Segment` из блока `Query`.

### Вариант для прямого подключения к PostgreSQL

```powerquery
let
    SnapshotDate = "2026-03-21",
    Segment = "employee",
    Source = PostgreSQL.Database(
        "mm.offonika.ru:55433",
        "pricing",
        [
            Query =
"
select
  snapshot_date,
  segment,
  owner_type,
  recommendation,
  counterparty_ref,
  counterparty_name,
  current_balance,
  aged_bucket,
  activity_segment,
  is_overdue,
  overdue_days,
  due_date,
  planned_payment_date,
  credit_depth_days,
  payment_term_source,
  shipment_ban,
  origin_document_ref,
  origin_document_number,
  origin_document_date,
  origin_manager_ref,
  origin_manager_name,
  current_manager_ref,
  current_manager_name
from receivable_case
where snapshot_date = date '" & SnapshotDate & "'
  and segment = '" & Segment & "'
order by current_balance desc, counterparty_ref
"
        ]
    ),
    #"Измененный тип" = Table.TransformColumnTypes(
        Source,
        {
            {"snapshot_date", type date},
            {"segment", type text},
            {"owner_type", type text},
            {"recommendation", type text},
            {"counterparty_ref", type text},
            {"counterparty_name", type text},
            {"current_balance", Currency.Type},
            {"aged_bucket", type text},
            {"activity_segment", type text},
            {"is_overdue", type logical},
            {"overdue_days", Int64.Type},
            {"due_date", type datetime},
            {"planned_payment_date", type datetime},
            {"credit_depth_days", Int64.Type},
            {"payment_term_source", type text},
            {"shipment_ban", type logical},
            {"origin_document_ref", type text},
            {"origin_document_number", type text},
            {"origin_document_date", type datetime},
            {"origin_manager_ref", type text},
            {"origin_manager_name", type text},
            {"current_manager_ref", type text},
            {"current_manager_name", type text}
        }
    )
in
    #"Измененный тип"
```

Если нужна одна таблица сразу по всем сегментам, убери строку:

```sql
and segment = 'employee'
```

---

## 4. Query: `ReceivablesManagerSummary`

```powerquery
let
    BaseUrl = Text.Trim(BaseUrl),
    SnapshotDate = Text.Trim(SnapshotDate),
    ApiToken = try Text.Trim(ApiToken) otherwise "",
    Headers =
        if ApiToken = "" then
            [Accept = "application/json"]
        else
            [
                Accept = "application/json",
                Authorization = "Bearer " & ApiToken
            ],
    Raw = Json.Document(
        Web.Contents(
            BaseUrl,
            [
                RelativePath = "api/bi/receivables-manager-summary",
                Query = [date = SnapshotDate],
                Headers = Headers
            ]
        )
    ),
    AsTable = Table.FromRecords(Raw),
    Typed = Table.TransformColumnTypes(
        AsTable,
        {
            {"snapshot_date", type date},
            {"manager_ref", type text},
            {"manager_name", type text},
            {"counterparty_count", Int64.Type},
            {"total_balance", Currency.Type},
            {"new_daily_count", Int64.Type},
            {"inactive_count", Int64.Type},
            {"employee_count", Int64.Type},
            {"fired_manager_count", Int64.Type},
            {"adjustment_candidates_count", Int64.Type}
        },
        "ru-RU"
    )
in
    Typed
```

Что даёт:
- сверка портфеля по текущим менеджерам;
- рейтинг по сумме долга;
- сравнение количества проблемных кейсов по менеджерам.

### Вариант для прямого подключения к PostgreSQL

```powerquery
let
    SnapshotDate = "2026-03-21",
    Source = PostgreSQL.Database(
        "mm.offonika.ru:55433",
        "pricing",
        [
            Query =
"
select
  s.snapshot_date,
  s.current_manager_ref as manager_ref,
  s.current_manager_name as manager_name,
  count(*) as counterparty_count,
  sum(s.current_balance) as total_balance,
  sum(case when c.segment = 'new_daily' then 1 else 0 end) as new_daily_count,
  sum(case when c.segment = 'inactive' then 1 else 0 end) as inactive_count,
  sum(case when c.segment = 'employee' then 1 else 0 end) as employee_count,
  sum(case when c.segment = 'fired_manager' then 1 else 0 end) as fired_manager_count,
  sum(case when c.segment = 'adjustment_candidates' then 1 else 0 end) as adjustment_candidates_count
from receivable_balance_snapshot s
left join receivable_case c
  on c.snapshot_date = s.snapshot_date
 and c.counterparty_ref = s.counterparty_ref
where s.snapshot_date = date '" & SnapshotDate & "'
group by
  s.snapshot_date,
  s.current_manager_ref,
  s.current_manager_name
order by total_balance desc nulls last, manager_name
"
        ]
    ),
    #"Измененный тип" = Table.TransformColumnTypes(
        Source,
        {
            {"snapshot_date", type date},
            {"manager_ref", type text},
            {"manager_name", type text},
            {"counterparty_count", Int64.Type},
            {"total_balance", Currency.Type},
            {"new_daily_count", Int64.Type},
            {"inactive_count", Int64.Type},
            {"employee_count", Int64.Type},
            {"fired_manager_count", Int64.Type},
            {"adjustment_candidates_count", Int64.Type}
        }
    )
in
    #"Измененный тип"
```

---

## 5. Query: `ДебиторкаДоговоры`

Эта таблица нужна для расследования расхождений по сотрудникам и договорам. Она даёт:
- фильтр по `contract_kind_name` (`С покупателем`, `С поставщиком`, `Прочее`);
- фильтр по `source_layer` (`regular_receivables`, `employee_summary`);
- детализацию до `контрагент + договор`.

### Вариант для прямого подключения к PostgreSQL

```powerquery
let
    SnapshotDate = "2026-03-21",
    Source = PostgreSQL.Database(
        "mm.offonika.ru:55433",
        "pricing",
        [
            Query =
"
select
  date '" & SnapshotDate & "' as snapshot_date,
  counterparty_ref,
  counterparty_name,
  contract_ref,
  contract_name,
  contract_kind_ref,
  contract_kind_name,
  source_layer,
  sum(amount_delta) as current_balance,
  count(*) as event_count,
  max(external_document_date) as last_event_at
from receivable_ledger_event
where external_document_date < (date '" & SnapshotDate & "' + interval '1 day')
group by
  counterparty_ref,
  counterparty_name,
  contract_ref,
  contract_name,
  contract_kind_ref,
  contract_kind_name,
  source_layer
having sum(amount_delta) <> 0
order by abs(sum(amount_delta)) desc, counterparty_ref, contract_ref
"
        ]
    ),
    #"Измененный тип" = Table.TransformColumnTypes(
        Source,
        {
            {"snapshot_date", type date},
            {"counterparty_ref", type text},
            {"counterparty_name", type text},
            {"contract_ref", type text},
            {"contract_name", type text},
            {"contract_kind_ref", type text},
            {"contract_kind_name", type text},
            {"source_layer", type text},
            {"current_balance", Currency.Type},
            {"event_count", Int64.Type},
            {"last_event_at", type datetime}
        }
    )
in
    #"Измененный тип"
```

Рекомендуемые русские подписи в модели:
- `snapshot_date` -> `ДатаСреза`
- `counterparty_ref` -> `КодКонтрагента`
- `counterparty_name` -> `Контрагент`
- `contract_ref` -> `КодДоговора`
- `contract_name` -> `Договор`
- `contract_kind_ref` -> `КодВидаДоговора`
- `contract_kind_name` -> `ВидДоговора`
- `source_layer` -> `СлойРасчета`
- `current_balance` -> `ТекущийДолг`
- `event_count` -> `КоличествоСобытий`
- `last_event_at` -> `ДатаПоследнегоСобытия`

---

## 6. Query: `ДебиторкаНачальныеОстатки`

Эта таблица нужна именно для ручной сверки с 1С по всем контрагентам. Она берёт только импортированный opening-layer из файла `Ведомость по взаиморасчетам` и не смешивает его с текущими движениями.

### Вариант для прямого подключения к PostgreSQL

```powerquery
let
    SnapshotDate = "2025-01-01",
    Source = PostgreSQL.Database(
        "mm.offonika.ru:55433",
        "pricing",
        [
            Query =
"
select
  date '" & SnapshotDate & "' as snapshot_date,
  counterparty_ref,
  counterparty_name,
  contract_ref,
  contract_name,
  contract_kind_ref,
  contract_kind_name,
  sum(amount_delta) as opening_balance,
  count(*) as opening_row_count
from receivable_ledger_event
where source = 'onec_opening_import'
  and event_type = 'opening_balance'
  and external_document_date = date '" & SnapshotDate & "'
group by
  counterparty_ref,
  counterparty_name,
  contract_ref,
  contract_name,
  contract_kind_ref,
  contract_kind_name
having sum(amount_delta) <> 0
order by abs(sum(amount_delta)) desc, counterparty_ref, contract_ref
"
        ]
    ),
    #"Измененный тип" = Table.TransformColumnTypes(
        Source,
        {
            {"snapshot_date", type date},
            {"counterparty_ref", type text},
            {"counterparty_name", type text},
            {"contract_ref", type text},
            {"contract_name", type text},
            {"contract_kind_ref", type text},
            {"contract_kind_name", type text},
            {"opening_balance", Currency.Type},
            {"opening_row_count", Int64.Type}
        }
    )
in
    #"Измененный тип"
```

Рекомендуемые русские подписи в модели:
- `snapshot_date` -> `ДатаСреза`
- `counterparty_ref` -> `КодКонтрагента`
- `counterparty_name` -> `Контрагент`
- `contract_ref` -> `КодДоговора`
- `contract_name` -> `Договор`
- `contract_kind_ref` -> `КодВидаДоговора`
- `contract_kind_name` -> `ВидДоговора`
- `opening_balance` -> `НачальныйОстаток`
- `opening_row_count` -> `КоличествоСтрокИмпорта`

Что сверять в первую очередь:
- `SUM(НачальныйОстаток)` по всей таблице против 1С;
- конкретного контрагента по строкам `Контрагент + Договор`;
- buyer/employee кейсы через фильтр `ВидДоговора = С покупателем`.

---

## 7. Query: `ДебиторкаСверка1С`

Эта таблица нужна для буквальной сверки с `Ведомостью по взаиморасчетам` или `Ведомостью по сотрудникам`, когда в отчёте 1С есть не только положительные остатки, но и отрицательные строки. В отличие от `ДебиторкаТекущая`, она хранит `signed_balance`, а не только положительный `current_balance`.

### Вариант для прямого подключения к PostgreSQL

```powerquery
let
    SnapshotDate = "2026-02-28",
    Source = PostgreSQL.Database(
        "mm.offonika.ru:55433",
        "pricing",
        [
            Query =
"
select
  snapshot_date,
  counterparty_ref,
  counterparty_name,
  signed_balance,
  absolute_balance,
  current_manager_ref,
  current_manager_name
from receivable_reconciliation_snapshot
where snapshot_date = date '" & SnapshotDate & "'
order by absolute_balance desc, counterparty_name
"
        ]
    ),
    #"Измененный тип" = Table.TransformColumnTypes(
        Source,
        {
            {"snapshot_date", type date},
            {"counterparty_ref", type text},
            {"counterparty_name", type text},
            {"signed_balance", Currency.Type},
            {"absolute_balance", Currency.Type},
            {"current_manager_ref", type text},
            {"current_manager_name", type text}
        }
    )
in
    #"Измененный тип"
```

Рекомендуемые русские подписи в модели:
- `snapshot_date` -> `ДатаСреза`
- `counterparty_ref` -> `КодКонтрагента`
- `counterparty_name` -> `Контрагент`
- `signed_balance` -> `Остаток1С`
- `absolute_balance` -> `АбсолютныйОстаток`
- `current_manager_ref` -> `КодТекущегоМенеджера`
- `current_manager_name` -> `ТекущийМенеджер`

Как использовать:
- сверка с выгрузкой 1С по всем строкам отчёта, включая отрицательные;
- поиск расхождений, которые не видны в `ДебиторкаТекущая`;
- acceptance по сотрудникам и другим контурам, где важен полный signed-итог.

Важно:
- `ДебиторкаТекущая` теперь signed; для старого управленческого режима по открытой положительной дебиторке добавляй фильтр `current_balance > 0`;
- `ДебиторкаСверка1С` используем только для сверки с 1С и спорных расследований.

---

## 8. Связи в модели Power BI

Если в модели уже есть общий календарь, связывай фактовые таблицы только через него:
- `Календарь[Дата]` -> `ДебиторкаТекущая[ДатаСреза]`
- `Календарь[Дата]` -> `ДебиторкаНачальныеОстатки[ДатаСреза]`
- `Календарь[Дата]` -> `ДебиторкаСверка1С[ДатаСреза]`
- `Календарь[Дата]` -> `ДебиторкаКейсы[ДатаСреза]`
- `Календарь[Дата]` -> `ДебиторкаМенеджеры[ДатаСреза]`
- `Календарь[Дата]` -> `ДебиторкаДоговоры[ДатаСреза]`

Тип связи:
- `один-ко-многим`;
- направление фильтра только от календаря к фактам.

Для первой версии дашборда прямые связи между фактами лучше не делать. `ДебиторкаТекущая`, `ДебиторкаНачальныеОстатки`, `ДебиторкаСверка1С`, `ДебиторкаКейсы`, `ДебиторкаМенеджеры` и `ДебиторкаДоговоры` можно использовать как независимые факт-таблицы с общим календарём.

---

## 9. Первые визуализации

### 6.1. Таблица сверки остатков
- строки: `counterparty_name`
- значения:
  - `current_balance`
  - `aged_bucket`
  - `activity_segment`
  - `is_overdue`
  - `overdue_days`
  - `current_manager_name`

### 6.2. Разбивка по aged bucket
- ось: `aged_bucket`
- значение: `SUM(current_balance)`

### 6.3. Разбивка по сегментам кейсов
- ось: `segment`
- значения:
  - `COUNT(counterparty_ref)`
  - `SUM(current_balance)`

### 6.4. Менеджеры
- строки: `manager_name`
- значения:
  - `total_balance`
  - `counterparty_count`
  - `new_daily_count`
  - `adjustment_candidates_count`

### 6.5. Договорная детализация
- строки:
  - `counterparty_name`
  - `contract_name`
  - `contract_kind_name`
  - `source_layer`
- значения:
  - `current_balance`
  - `event_count`
  - `last_event_at`

Фильтры:
- `contract_kind_name`
- `source_layer`

---

## 10. Что сверять первым делом

1. Общую сумму `SUM(current_balance)` по `ReceivablesCurrent`.
2. Общую сумму `SUM(opening_balance)` по `ДебиторкаНачальныеОстатки`.
3. Общую сумму `SUM(signed_balance)` по `ДебиторкаСверка1С`, если сверяемся с полным signed-итогом 1С.
4. Top-20 контрагентов по сумме.
5. Кейсы `employee` и `fired_manager`.
6. Сумму и количество `adjustment_candidates`.
7. Портфель по менеджерам.
8. Расхождения сотрудников по таблице `ДебиторкаДоговоры`, начиная с фильтра:
   - `СлойРасчета = employee_summary`
   - `ВидДоговора = С покупателем`
9. Сверку `НачальныйОстаток -> ТекущийДолг` по одному контрагенту в разрезе договоров.
10. Для полной сверки с 1С использовать `ДебиторкаСверка1С`, а не `ДебиторкаТекущая`, если в отчёте есть отрицательные строки.

После этого уже можно решать:
- какие сегменты реально нужны в задачах;
- кому ставить;
- что оставить только в дашборде без Bitrix-шума.
