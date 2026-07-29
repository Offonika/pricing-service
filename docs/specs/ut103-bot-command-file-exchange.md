---
spec_id: "ut103-bot-command-file-exchange"
title: "UT 10.3 Bot Command File Exchange"
doc_type: spec
domain: "receivables"
status: draft
owner: "finance"
source_of_truth: true
related_code:
  - app/services/exporters/ut103_exchange.py
  - app/services/exporters/ut103_forecast.py
  - app/services/exporters/ut103_nomenclature_properties.py
  - app/services/exporters/ut103_procurement_orders.py
  - app/services/exporters/ut103_credit_terms.py
  - app/services/receivable_credit_decisions.py
  - app/models/receivable_credit_decision.py
  - tasks/build_assortment_lifecycle_updates.py
  - tasks/export_ut103_forecast.py
  - tasks/export_ut103_nomenclature_properties.py
  - tasks/export_ut103_procurement_supplier_orders.py
  - tasks/export_ut103_credit_terms.py
  - tasks/run_receivable_credit_decision_worker.py
  - alembic/versions/e2b3c4d5e6f8_add_receivable_credit_decision_operation.py
related_tests:
  - tests/test_ut103_exchange.py
  - tests/test_ut103_forecast_exporter.py
  - tests/test_build_assortment_lifecycle_updates_task.py
  - tests/test_ut103_nomenclature_properties_exporter.py
  - tests/test_ut103_procurement_orders_exporter.py
  - tests/test_ut103_credit_terms_exporter.py
  - tests/test_export_ut103_credit_terms_task.py
  - tests/test_receivable_credit_decision_model.py
  - tests/test_receivable_credit_decision_worker.py
  - tests/test_receivable_credit_decision_process.py
  - tests/test_export_ut103_forecast_task.py
  - tests/test_export_ut103_nomenclature_properties_task.py
  - tests/test_export_ut103_procurement_supplier_orders_task.py
contracts:
  - app/services/exporters/ut103_exchange.py
  - app/services/exporters/ut103_forecast.py
  - app/services/exporters/ut103_nomenclature_properties.py
  - app/services/exporters/ut103_procurement_orders.py
  - app/services/exporters/ut103_credit_terms.py
depends_on:
  - docs/specs/counterparty-folder-recommendations.md
supersedes: []
rollout_required: true
updated_at: "2026-07-29"
---

# Назначение

Зафиксировать будущий канал записи команд бота в `1С УТ 10.3`: установка глубины
кредита, кредитного лимита и, возможно, перенос папки контрагента после отдельного
подтверждения. Новый канал не создаем. Используем уже заведенный файловый обмен,
который применялся для передачи прогноза продаж в тестовую базу.

# Scope / Out of Scope

Входит:
- единый файловый корень `UT103_EXCHANGE_ROOT`;
- фактический тестовый корень на сервере `УТ 10.3`: `E:\MMExchange\UT103`;
- входящие команды от бота в `to_1c/new`;
- ответы 1С в `from_1c/new`;
- режимы `dry_run` и `apply`;
- идемпотентность по `message_id` и `idempotency_key`;
- атомарная команда `set_credit_terms` для лимита и глубины;
- задел на команду `move_counterparty_folder` после отдельного acceptance.

Не входит:
- прямой `UPDATE/INSERT/DELETE` в SQL-таблицы 1С;
- новый сетевой HTTP-коннектор в 1С;
- новая отдельная папка обмена под лимиты или папки контрагентов;
- автоприменение без dry-run, approval и результата от 1С;
- обход 1С-прав через внешний SQL.

# Source of Truth

`1С УТ 10.3` остается source of truth для лимита, глубины кредита и папки
контрагента. `pricing-service` или другой бот-контур является источником
рекомендации и команды, но не пишет напрямую в базу 1С.

Существующий технический паттерн:
- Python пишет XML в `UT103_EXCHANGE_ROOT/to_1c/new`;
- файл сначала создается как временный, затем атомарно переименовывается в
  `*.ready.xml`;
- 1С забирает `*.ready.xml`, обрабатывает платформенными API и кладет ответ в
  `UT103_EXCHANGE_ROOT/from_1c/new`;
- результат читается сервером как `*.result.xml`.

Текущий legacy-пример реализации этого паттерна - `forecast_sales.v1` в
`app/services/exporters/ut103_forecast.py`. Новый прикладной пакет для закупки и
ассортимента - `nomenclature_property_updates.v1` в
`app/services/exporters/ut103_nomenclature_properties.py`; прогноз продаж больше
не развиваем как целевой процесс. Для создания непроведенных черновиков
`ЗаказПоставщику` используется пакет `procurement_onec_file_exchange.v1` в
`app/services/exporters/ut103_procurement_orders.py`.

Фактически найденный Windows-контур `УТ 10.3` использует папку
`E:\MMExchange\UT103`. Старый `run_forecast_import.vbs` рядом с этой папкой
запускает COM-соединение и вызывает `MMForecastImport.RunForecastImport()`, а
лог пишет в `E:\MMExchange\UT103\logs\forecast_scheduler.log`. Этот VBS не
используется для статусов ассортимента, но подтверждает рабочий паттерн:
файл кладет сервер, 1С забирает его штатным запуском.

# Data Flow

```text
бот/management rule
-> approved command batch
-> UT103_EXCHANGE_ROOT/to_1c/new/*.ready.xml
-> внешняя обработка или регламентное задание 1С
-> проверка команды в 1С
-> dry_run/apply через штатные объекты 1С
-> UT103_EXCHANGE_ROOT/from_1c/new/*.result.xml
-> бот читает результат и пишет отчет/статус
```

Для v1 обработка 1С должна поддерживать `dry_run` как основной режим. `apply`
включается только после ручного acceptance на тестовой базе и отдельного включения
флага в регламентном задании/обработке.

# API / Data Contracts

## Папки

Используем тот же корень, что и прогноз продаж:

Рабочий Windows-путь:

```text
E:\MMExchange\UT103
```

На тесте этот путь был подключен к `Ekama_Test_Arsen`. Для боевого переноса
принято решение не создавать `UT103_PROD`, а использовать ту же папку после
отключения тестового планировщика, очистки `to_1c/new` и замены `OneCRef` в
локальном `run_nomenclature_properties_import.vbs` на боевую базу. Пошаговый
чеклист переноса живет в
`1C_Dev_Workflow/docs/order_flow/ut103-nomenclature-property-file-exchange-runbook-2026-06-24.md`.

В `pricing-service` этот путь задается переменной `UT103_EXCHANGE_ROOT`. Если
задача запускается на Windows-машине, где доступен диск `E:`, код использует
`E:\MMExchange\UT103` как дефолт. Если задача запускается на Linux-сервере,
`UT103_EXCHANGE_ROOT` должен указывать на смонтированную папку/шару, которая
ведет в тот же Windows-каталог.

Текущий рабочий вариант без SMB-монтажа:

```text
pricing-service Linux outbox:
/opt/MM/pricing-service/.local/ut103_exchange/UT103

UT 10.3 Windows root:
E:\MMExchange\UT103
```

В этом режиме `pricing-service` пишет XML в локальный Linux outbox, а
Windows-планировщик перед запуском 1С забирает
`to_1c/new/nomenclature_properties_*.ready.xml` с Linux и после обработки
возвращает `from_1c/new/nomenclature_properties_*.result.xml` обратно в Linux
outbox. Windows-скрипты лежат в `1C_Dev_Workflow/scripts/windows/`:
`sync_nomenclature_properties_exchange.ps1` и
`run_nomenclature_properties_exchange.bat`.

CLI-задачи `tasks.export_ut103_forecast` и
`tasks.export_ut103_nomenclature_properties` перед запуском подхватывают только
`UT103_*` ключи из проектного `.env`, если они еще не заданы окружением процесса.

```text
UT103_EXCHANGE_ROOT/
  to_1c/
    new/
      onec_commands_<message_id>.ready.xml
  from_1c/
    new/
      onec_commands_<message_id>.result.xml
```

Новые подпапки под лимиты, глубину кредита или перенос папок не создаем.

## XML Команд `onec_commands.v1`

Для задачи №2494 лимит и глубина передаются только одной атомарной командой
`set_credit_terms`. Две независимые команды для этих реквизитов запрещены.

```xml
<?xml version="1.0" encoding="windows-1251"?>
<ExchangeMessage>
  <Header>
    <MessageId>rcd-1200-2494-7902699be42c-aaaaaaaaaaaa-dry-run</MessageId>
    <Schema>onec_commands.v1</Schema>
    <CreatedAt>2026-07-28T10:00:00+03:00</CreatedAt>
    <Source>pricing-service</Source>
    <Target>1c_ut_10_3</Target>
    <Mode>dry_run</Mode>
  </Header>
  <Commands>
    <Command>
      <IdempotencyKey>receivable-decision:1200:2494:7</IdempotencyKey>
      <CommandType>set_credit_terms</CommandType>
      <DecisionId>2494</DecisionId>
      <DecisionHash>64-symbol-lowercase-sha256</DecisionHash>
      <ReportRevision>7</ReportRevision>
      <CounterpartyRef>0x...</CounterpartyRef>
      <CounterpartyGuid>00000000-0000-0000-0000-000000000000</CounterpartyGuid>
      <CounterpartyCode>РБ030337</CounterpartyCode>
      <CounterpartyName>Тестовый контрагент</CounterpartyName>
      <ExpectedCurrentLimit>100000.00</ExpectedCurrentLimit>
      <ExpectedCurrentDepth>7</ExpectedCurrentDepth>
      <NewLimit>150000.00</NewLimit>
      <NewDepth>14</NewDepth>
      <Currency>RUB</Currency>
      <Reason>Утвержденные кредитные условия</Reason>
      <ApprovedBy>115204</ApprovedBy>
      <ApprovedAt>2026-07-28T09:55:00+03:00</ApprovedAt>
    </Command>
  </Commands>
</ExchangeMessage>
```

Обязательные поля:

- `MessageId` - единый ASCII-идентификатор длиной не более 120:
  `rcd-<entity>-<item>-<revision_hash12>-<decision_hash12>-<suffix>`;
- `Schema=onec_commands.v1`;
- `Mode=dry_run|apply`;
- `IdempotencyKey` - уникальный ключ команды;
- `CommandType=set_credit_terms`;
- `DecisionId`, `DecisionHash`, `ReportRevision`;
- ref, GUID, код и имя контрагента;
- ожидаемая и новая пара лимит/глубина;
- `Currency=RUB`, основание, согласующий и время согласования.

Суффиксы: `dry-run`, `apply`, `readback`. `revision_hash12` — первые 12
hex-символов SHA-256 UTF-8 строки ревизии, `decision_hash12` — первые 12
символов полного `DecisionHash`. Python, БД и 1С используют идентификатор
дословно: обрезание, замена символов и дополнительная нормализация запрещены.
Один файл кредитных условий содержит ровно одну атомарную команду.

## Типы Команд

`set_credit_terms`:

- одновременно меняет кредитный лимит и глубину;
- любое несовпадение expected current блокирует обе записи;
- нулевые значения допустимы;
- пустые/отрицательные значения, дробная глубина, сумма точнее двух знаков и
  валюта не `RUB` отклоняются;
- сумма ограничена типом `Число(18,2)`, глубина — `Число(5,0)`;
- `apply` возвращает старую, запрошенную и прочитанную обратно пару;
- повтор уже примененного решения возвращает `already_actual`.

`move_counterparty_folder`:
- в v1 только `dry_run`;
- `NewValue` - ссылка/код рекомендуемой папки;
- `apply` запрещен до отдельного решения по автопереносу и проверки защиты ролей.

## XML Свойств Номенклатуры `nomenclature_property_updates.v1`

Для управления ассортиментом используется отдельный пакет в тех же папках
обмена:

```text
UT103_EXCHANGE_ROOT/
  to_1c/new/nomenclature_properties_<message_id>.ready.xml
  from_1c/new/nomenclature_properties_<message_id>.result.xml
```

Минимальная строка:

```xml
<Item>
  <IdempotencyKey>nom-prop:РБ000074721:Статус ассортимента:2026-06-23:r1</IdempotencyKey>
  <NomenclatureCode>РБ000074721</NomenclatureCode>
  <PropertyName>Статус ассортимента</PropertyName>
  <ValueType>property_value</ValueType>
  <NewValueName>Новинка</NewValueName>
  <NewValueTag>new_item</NewValueTag>
  <ExpectedCurrentValueName></ExpectedCurrentValueName>
  <Reason>Первый приход, товар моложе 90 дней</Reason>
</Item>
```

Поддержанные типы v1: `property_value`, `string`, `date`, `number`, `boolean`.
В `apply` обязателен `ApprovedBy` в шапке или строке пакета.

Список имен свойств в XML не ограничивается whitelist: экспортёр проверяет, что
`PropertyName` заполнен, а 1С-обработка уже на своей стороне проверяет, что
такое свойство реально существует в `ПланыВидовХарактеристик.СвойстваОбъектов`.
Для `property_value` значение также должно существовать в 1С у выбранного
свойства по имени или `Тэг`.
- `Ручной минимальный остаток`;
- `Дата пересмотра правила наличия`.

Статус ассортимента хранит машинный код в поле `Тэг` значения свойства, поэтому
отдельное свойство для кода статуса на первом этапе не нужно.
`Эксклюзив` передается не как статус ассортимента, а как строковый коммерческий
признак `exclusive` в свойстве `Коммерческие признаки`.

Python CLI:

```bash
python -m tasks.export_ut103_nomenclature_properties \
  --mode dry_run \
  --input-json property-updates.json
```

`--exchange-root` можно передать явно, но штатный способ - держать
`UT103_EXCHANGE_ROOT` в окружении сервиса.

На текущем Linux-сервере без смонтированной Windows-папки штатное значение:

```bash
UT103_EXCHANGE_ROOT=/opt/MM/pricing-service/.local/ut103_exchange/UT103
```

## XML Черновиков Заказа Поставщику `procurement_onec_file_exchange.v1`

Для закупочного контура используется отдельный пакет в тех же папках обмена:

```text
UT103_EXCHANGE_ROOT/
  to_1c/new/procurement_supplier_orders_<message_id>.ready.xml
  from_1c/new/procurement_supplier_orders_<message_id>.result.xml
```

Минимальный заказ:

```xml
<SupplierOrder>
  <IdempotencyKey>proc-order:DISPLAY-AUTO-203-RB1:r1</IdempotencyKey>
  <DraftOnly>true</DraftOnly>
  <OrderDate>2026-07-05</OrderDate>
  <ProcurementContour>Обычный</ProcurementContour>
  <Currency>RUB</Currency>
  <Supplier>
    <Code>SUP-001</Code>
  </Supplier>
  <Contract>
    <Ref>0xcontract</Ref>
  </Contract>
  <Warehouse>
    <Code>MAIN</Code>
  </Warehouse>
  <BitrixItemUrl>https://master.bitrix24.ru/crm/type/1132/details/777/</BitrixItemUrl>
  <ConfirmationId>bitrix-approval-777</ConfirmationId>
  <CalculationId>display-auto-order-run-203</CalculationId>
  <Lines>
    <Line>
      <LineNumber>1</LineNumber>
      <Nomenclature>
        <Code>РБ000074721</Code>
      </Nomenclature>
      <Quantity>5</Quantity>
      <Price>1250.50</Price>
      <Currency>RUB</Currency>
    </Line>
  </Lines>
</SupplierOrder>
```

Python CLI:

```bash
python -m tasks.export_ut103_procurement_supplier_orders \
  --mode apply \
  --approved-by "Омар" \
  --input-json supplier-order.json
```

В `apply` обязателен `ApprovedBy`, `ConfirmationId` и `DraftOnly=true`. 1С должна
создать только непроведенный черновик и вернуть result-файл с номером/ссылкой
или понятной ошибкой по заказу.

## XML Результата

Формат:

```xml
<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>rcd-1200-2494-7902699be42c-aaaaaaaaaaaa-dry-run</MessageId>
  <Schema>onec_commands.v1</Schema>
  <Status>success</Status>
  <ProcessedAt>29.05.2026 10:05:00</ProcessedAt>
  <Loaded>1</Loaded>
  <Failed>0</Failed>
  <Errors></Errors>
  <CommandResults>
    <CommandResult>
      <IdempotencyKey>receivable-decision:1200:2494:7</IdempotencyKey>
      <DecisionId>2494</DecisionId>
      <DecisionHash>64-symbol-lowercase-sha256</DecisionHash>
      <Status>validated</Status>
      <OldLimit>100000.00</OldLimit>
      <OldDepth>7</OldDepth>
      <RequestedLimit>150000.00</RequestedLimit>
      <RequestedDepth>14</RequestedDepth>
      <ReadbackLimit>100000.00</ReadbackLimit>
      <ReadbackDepth>7</ReadbackDepth>
      <Message>Пара проверена; запись не выполнялась</Message>
    </CommandResult>
  </CommandResults>
</ExchangeResult>
```

Статусы строки:
- `validated` - dry-run прошел, изменение возможно;
- `applied` - команда применена;
- `already_actual` - в 1С уже стоит нужное значение;
- `needs_review` - нужен ручной разбор;
- `failed` - ошибка применения или валидации.

# Invariants

- Используем только существующий `UT103_EXCHANGE_ROOT`.
- Python-контур не пишет напрямую в SQL 1С.
- `set_credit_terms` всегда начинает с `dry_run`.
- `apply` требует allowlist согласующего, повторное чтение карточки, feature flag
  и pilot allowlist.
- durable operation уникальна по `Bitrix entity + item + revision`; повторное
  использование ревизии с другим hash отклоняется.
- параллельные решения одного GUID контрагента блокируются nullable unique-lock.
- Повторный `IdempotencyKey` не должен применять действие второй раз.
- Неопределенный исход `apply` запрещает слепую повторную отправку до
  result/readback.
- Новая ревизия не отменяет `apply_sent/applying` и не освобождает lock
  контрагента до доказанного result/readback.
- Recovery `readback` повторяет безопасный `dry_run` с тем же `MessageId` не
  более трех раз; `apply` всегда имеет ровно одну попытку публикации.
- Worker читает result только из `from_1c/new` и после проверки переносит его в
  `from_1c/archive`.
- После успешного 1С-result карточка Bitrix читается повторно: измененная
  карточка получает `Ошибка 1С`, а доказанный факт `applied` сохраняется в БД.
- Для `move_counterparty_folder` автоприменение запрещено до отдельного acceptance.
- Обработка 1С должна писать результат по каждой команде, даже если весь пакет
  завершился с ошибкой.
- Пакет `nomenclature_property_updates.v1` не использует `forecast_sales.v1`;
  общий только технический паттерн файлового обмена.
- Пакет `procurement_onec_file_exchange.v1` не проводит заказ поставщику:
  допускается только непроведенный черновик.

# Errors / Edge Cases

- Контрагент не найден по `CounterpartyRef/CounterpartyCode`: `needs_review`.
- Текущая пара не совпала с `ExpectedCurrentLimit/Depth`: `needs_review`, без
  изменения.
- Неизвестный `CommandType`: `failed`.
- `Mode=apply`, но `ApprovedBy` пустой: `failed`.
- Файл с тем же `MessageId` уже обработан: вернуть прежний результат или статус
  duplicate без повторного применения.
- 1С не может записать объект штатным API: `failed` с безопасным текстом ошибки.

# Tests

Автоматические проверки:
- unit: генерация XML `onec_commands.v1` в `windows-1251`;
- unit: генерация XML `nomenclature_property_updates.v1` в `windows-1251`;
- unit: генерация XML `procurement_onec_file_exchange.v1` в `windows-1251`;
- unit: атомарная запись `*.ready.xml` в `to_1c/new`;
- unit: парсинг `*.result.xml` из `from_1c/new`;
- unit: лимит+глубина находятся в одной команде;
- unit: валидация RUB, нулевых, отрицательных и дробной глубины;
- unit: allowlist, decision hash, дедупликация и конкурентный lock;
- unit: уникальность `Bitrix item + revision` и границы типов `18.2 / 5.0`;
- unit: запрет apply при изменении карточки после dry-run;
- unit: потерянный apply-result не вызывает повторную отправку;
- unit: recovery readback ограничен тремя попытками с тем же MessageId;
- unit: result после проверки архивируется и не читается повторно из archive;
- unit: изменение карточки во время apply не дает ложную стадию `Применено`;
- unit: pending-синхронизация Bitrix повторяется для `failed` и `applied`;
- unit: запрет повторного `IdempotencyKey`;
- smoke в тестовой 1С: dry-run для 1-2 контрагентов без изменений;
- smoke в тестовой 1С: apply для `set_credit_terms` на `РБ030337`;
- manual acceptance: оператор видит понятный отчет по примененным и отклоненным
  командам.

# Rollout

1. Утвердить этот spec как единый канал команд бота в УТ 10.3.
2. Реализовать Python exporter/parser рядом с `ut103_forecast.py`, используя тот
   же `UT103_EXCHANGE_ROOT`.
3. Установить `MMOneCCommandsImport`, регистр утвержденных условий и legacy
   guards в `Ekama_Test_Arsen`.
4. Запустить `set_credit_terms` только в `dry_run` на `РБ030337`.
5. Для `nomenclature_property_updates.v1` после тестового `dry_run/apply`
   отключить тестовый планировщик, переключить `run_nomenclature_properties_import.vbs`
   на боевую базу и повторить боевой `dry_run` на одном товаре.
6. После приемки включить `set_credit_terms apply` только для `РБ030337` и
   наблюдать не менее одного рабочего дня.
7. Расширять pilot allowlist без разделения лимита и глубины.
8. `move_counterparty_folder` оставить отдельным этапом после проверки отчетов по
   папкам контрагентов и решения по автопереносу.

# Changelog

- 2026-07-29 - aligned durable uniqueness with `Bitrix item + revision`, switched
  the active counterparty lock to GUID and added strict result identity and
  `Numeric(18,2) / Numeric(5,0)` bounds.
- 2026-07-28 - implemented atomic `set_credit_terms`, result readback and durable
  Bitrix worker contract for task #2494; live rollout remains gated.
- 2026-07-05 - added `procurement_onec_file_exchange.v1` exporter/CLI for
  draft supplier-order creation through the existing `UT103_EXCHANGE_ROOT`.
- 2026-06-26 - clarified production cutover: reuse `E:\MMExchange\UT103` after
  disabling the test scheduler and switching the local VBS `OneCRef` to the
  production UT 10.3 database.
- 2026-06-25 - added `tasks/build_assortment_lifecycle_updates.py` as the
  dry-run builder from normalized assortment facts to
  `nomenclature_property_updates.v1` rows.
- 2026-06-28 - removed hardcoded property-name whitelist from
  `nomenclature_property_updates.v1`; 1C now accepts any existing
  nomenclature property while keeping `dry_run`/`apply`, `ApprovedBy`, type
  and current-value checks.
- 2026-06-23 - added `nomenclature_property_updates.v1` exporter/CLI for
  updates of 1C nomenclature properties; `forecast_sales.v1` remains
  only as legacy exchange sample.
- 2026-05-29 - draft created; закреплено использование существующего
  `UT103_EXCHANGE_ROOT` вместо нового канала обмена.
