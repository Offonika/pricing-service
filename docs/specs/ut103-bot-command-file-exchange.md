---
spec_id: "ut103-bot-command-file-exchange"
title: "UT 10.3 Bot Command File Exchange"
doc_type: spec
domain: "receivables"
status: draft
owner: "finance"
source_of_truth: true
related_code:
  - app/services/exporters/ut103_forecast.py
  - tasks/export_ut103_forecast.py
related_tests:
  - tests/test_ut103_forecast_exporter.py
  - tests/test_export_ut103_forecast_task.py
contracts:
  - app/services/exporters/ut103_forecast.py
depends_on:
  - docs/specs/counterparty-folder-recommendations.md
supersedes: []
rollout_required: true
updated_at: "2026-05-29"
---

# Назначение

Зафиксировать будущий канал записи команд бота в `1С УТ 10.3`: установка глубины
кредита, кредитного лимита и, возможно, перенос папки контрагента после отдельного
подтверждения. Новый канал не создаем. Используем уже заведенный файловый обмен,
который применялся для передачи прогноза продаж в тестовую базу.

# Scope / Out of Scope

Входит:
- единый файловый корень `UT103_EXCHANGE_ROOT`;
- входящие команды от бота в `to_1c/new`;
- ответы 1С в `from_1c/new`;
- режимы `dry_run` и `apply`;
- идемпотентность по `message_id` и `idempotency_key`;
- команды `set_credit_depth`, `set_credit_limit`;
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

Текущий пример реализации этого паттерна - `forecast_sales.v1` в
`app/services/exporters/ut103_forecast.py`.

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

```text
UT103_EXCHANGE_ROOT/
  to_1c/
    new/
      bot_commands_<message_id>.ready.xml
  from_1c/
    new/
      bot_commands_<message_id>.result.xml
```

Новые подпапки под лимиты, глубину кредита или перенос папок не создаем.

## XML Команд `onec_commands.v1`

Черновой формат:

```xml
<?xml version="1.0" encoding="windows-1251"?>
<ExchangeMessage>
  <Header>
    <MessageId>bot-commands-20260529-001</MessageId>
    <Schema>onec_commands.v1</Schema>
    <CreatedAt>2026-05-29T10:00:00+03:00</CreatedAt>
    <Source>pricing-service</Source>
    <Target>1c_ut_10_3</Target>
    <Mode>dry_run</Mode>
    <ReportRevision>abc123</ReportRevision>
  </Header>
  <Commands>
    <Command>
      <IdempotencyKey>credit-depth:cp-ref:r1</IdempotencyKey>
      <CommandType>set_credit_depth</CommandType>
      <CounterpartyRef>0x...</CounterpartyRef>
      <CounterpartyCode>РБ0000001</CounterpartyCode>
      <ExpectedCurrentValue>7</ExpectedCurrentValue>
      <NewValue>14</NewValue>
      <Reason>Просрочка по правилу кредитного контроля</Reason>
      <ApprovedBy></ApprovedBy>
    </Command>
  </Commands>
</ExchangeMessage>
```

Обязательные поля:
- `MessageId` - уникальный пакет команд;
- `Schema=onec_commands.v1`;
- `Mode=dry_run|apply`;
- `IdempotencyKey` - уникальный ключ команды;
- `CommandType`;
- `CounterpartyRef` или `CounterpartyCode`;
- `NewValue`;
- `Reason`;
- `ReportRevision` - ревизия отчета/правила, из которого создана команда.

Рекомендуемые поля:
- `ExpectedCurrentValue` - защита от применения к уже измененной карточке;
- `ApprovedBy` - обязателен для `apply`;
- `CounterpartyName` - только для читаемости результата.

## Типы Команд

`set_credit_depth`:
- меняет поле глубины кредита у контрагента;
- `NewValue` - целое число дней;
- `ExpectedCurrentValue` желателен.

`set_credit_limit`:
- меняет кредитный лимит у контрагента;
- `NewValue` - сумма в рублях;
- `ExpectedCurrentValue` желателен.

`move_counterparty_folder`:
- в v1 только `dry_run`;
- `NewValue` - ссылка/код рекомендуемой папки;
- `apply` запрещен до отдельного решения по автопереносу и проверки защиты ролей.

## XML Результата

Черновой формат:

```xml
<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>bot-commands-20260529-001</MessageId>
  <Schema>onec_commands.v1</Schema>
  <Status>success</Status>
  <ProcessedAt>29.05.2026 10:05:00</ProcessedAt>
  <Loaded>1</Loaded>
  <Failed>0</Failed>
  <Errors></Errors>
  <CommandResults>
    <CommandResult>
      <IdempotencyKey>credit-depth:cp-ref:r1</IdempotencyKey>
      <Status>validated</Status>
      <CurrentValue>7</CurrentValue>
      <NewValue>14</NewValue>
      <Message>dry_run: изменение возможно</Message>
    </CommandResult>
  </CommandResults>
</ExchangeResult>
```

Статусы строки:
- `validated` - dry-run прошел, изменение возможно;
- `applied` - команда применена;
- `already_actual` - в 1С уже стоит нужное значение;
- `skipped` - команда пропущена без ошибки;
- `needs_review` - нужен ручной разбор;
- `failed` - ошибка применения или валидации.

# Invariants

- Используем только существующий `UT103_EXCHANGE_ROOT`.
- Python-контур не пишет напрямую в SQL 1С.
- В v1 все новые команды запускаются в `dry_run`.
- `apply` требует `ApprovedBy` и отдельного включения.
- Повторный `IdempotencyKey` не должен применять действие второй раз.
- Для `move_counterparty_folder` автоприменение запрещено до отдельного acceptance.
- Обработка 1С должна писать результат по каждой команде, даже если весь пакет
  завершился с ошибкой.

# Errors / Edge Cases

- Контрагент не найден по `CounterpartyRef/CounterpartyCode`: `needs_review`.
- Текущее значение не совпало с `ExpectedCurrentValue`: `needs_review`, без
  изменения.
- Неизвестный `CommandType`: `failed`.
- `Mode=apply`, но `ApprovedBy` пустой: `failed`.
- Файл с тем же `MessageId` уже обработан: вернуть прежний результат или статус
  duplicate без повторного применения.
- 1С не может записать объект штатным API: `failed` с безопасным текстом ошибки.

# Tests

До реализации:
- unit: генерация XML `onec_commands.v1` в `windows-1251`;
- unit: атомарная запись `*.ready.xml` в `to_1c/new`;
- unit: парсинг `*.result.xml` из `from_1c/new`;
- unit: запрет `apply` без `ApprovedBy`;
- unit: запрет повторного `IdempotencyKey`;
- smoke в тестовой 1С: dry-run для 1-2 контрагентов без изменений;
- smoke в тестовой 1С: apply для `set_credit_depth` на тестовом контрагенте;
- manual acceptance: оператор видит понятный отчет по примененным и отклоненным
  командам.

# Rollout

1. Утвердить этот spec как единый канал команд бота в УТ 10.3.
2. Реализовать Python exporter/parser рядом с `ut103_forecast.py`, используя тот
   же `UT103_EXCHANGE_ROOT`.
3. Подготовить внешнюю обработку или регламентное задание 1С для
   `onec_commands.v1`.
4. Запустить только `dry_run` в `Ekama_Test_Arsen`.
5. После приемки включить `apply` сначала для `set_credit_depth`.
6. Затем отдельно включать `set_credit_limit`.
7. `move_counterparty_folder` оставить отдельным этапом после проверки отчетов по
   папкам контрагентов и решения по автопереносу.

# Changelog

- 2026-05-29 - draft created; закреплено использование существующего
  `UT103_EXCHANGE_ROOT` вместо нового канала обмена.
