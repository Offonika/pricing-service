---
spec_id: "exchange-counterparty-daily-settlements"
title: "Exchange Counterparty Daily Settlements Report"
doc_type: spec
domain: receivables
status: implemented
owner: finance
source_of_truth: false
related_code:
  - app/api/management.py
  - app/services/exchange_counterparty_settlements.py
  - app/services/finance_cash_position.py
  - app/services/receivables.py
  - infra/cron/management_digest_from_a.py
related_tests:
  - tests/test_exchange_counterparty_settlements.py
  - tests/test_finance_cash_position.py
  - tests/test_management_digest_from_a.py
  - tests/test_receivables.py
contracts:
  - openapi.yaml
  - docs/BI.Receivables.md
depends_on:
  - docs/specs/receivables-smart-process-workflow.md
supersedes: []
rollout_required: true
updated_at: "2026-05-14"
---

# Назначение

Ежедневно предоставлять Карине короткий отчет по состоянию взаиморасчетов с
контрагентом `Обменник` на момент формирования отчета.

Отчет нужен как регулярный финансовый контроль, а не как разовый ad-hoc запрос:
после включения он должен формироваться каждый день без дополнительных
напоминаний.

# Scope / Out of Scope

Входит:

- текущие взаиморасчеты по контрагенту `Обменник` на точное время формирования;
- суммы в валютах договоров;
- отдельные итоги по каждой валюте договора без общего mixed-currency итога;
- рублевый эквивалент из `1С` как контрольный слой для валютных движений и
  остатков;
- контроль расхождения: рублевый приход против валютного расхода в рублевом
  эквиваленте;
- контроль итогового рублевого остатка/хвоста после обменных операций;
- ежедневный контроль документов, где курс в строке документа дает один
  рублевый эквивалент, а в регистр взаиморасчетов записана другая сумма;
- остатки денег по `1С` на счетах, в кассах и на картах/эквайринге, отдельно
  по категориям и валютам;
- краткая таблица итогов;
- детализация по договорам, движениям или остаткам, если объем данных больше
  короткого текстового блока;
- дата и точное московское время формирования в каждом сообщении и артефакте;
- ежедневный блок в утреннем отчете Карине.

Не входит:

- изменение сумм или правил учета в `1С`;
- смешивание валют договоров в один натуральный итог;
- самостоятельная переоценка валют вне правил `1С`; рублевый эквивалент берется
  из `1С` или сверенного `1С`-источника и используется как контроль;
- хранение состояния отчета в Telegram или Bitrix24 без service-side state;
- включение production-доставки до подтверждения точного `1С`-источника,
  карточки контрагента и порогов контроля.

Подтвержденный идентификатор контрагента в `1С`:

- `counterparty_code`: `РБ002085`
- `counterparty_name`: `Обменник`
- `counterparty_ref`: `0x93040025901e48ee11e3aa7223b36751`

# Source of Truth

- `1С` - источник истины по взаиморасчетам, договорам, валютам договоров,
  движениям и остаткам.
- Целевой эталон для цифр - `Ведомость по взаиморасчетам с контрагентами` или
  прямой SQL/read-only источник, сверенный с этим отчетом.
- `pricing-service` - владелец read-only API, нормализации строк, проверки
  валютных инвариантов и подготовки payload/artifact.
- Корневой management-orchestrator `/opt/MM` - владелец расписания, delivery
  state, dedupe и доставки Карине.

# Data Flow

```text
1С -> pricing-service read-only extractor/API -> report payload/XLSX
   -> management-digest -> утренний отчет Карине
```

Рекомендуемый режим первой версии:

1. Адаптер запускается утром по расписанию.
2. `pricing-service` формирует as-of snapshot по контрагенту `Обменник`.
3. Payload содержит обязательный `generated_at_msk`.
4. Сервис считает контрольные показатели: рублевый эквивалент валютного расхода,
   рублевый приход, разницу и итоговый рублевый остаток.
5. Карина получает краткий блок в своем ежедневном утреннем отчете.
6. Если строк много или есть ошибка, блок показывает короткую сводку `OK/ВНИМАНИЕ`
   и ссылку/вложение на детализацию, если она будет подключена.
7. Delivery state дедуплицирует отправку по дате, контрагенту и revision.

# API / Data Contracts

Read-only контракт по взаиморасчетам:

```text
GET /api/management/exchange-counterparty-settlements?counterparty_code=РБ002085&period_start=YYYY-MM-DD&as_of=YYYY-MM-DDTHH:MM:SS
```

`counterparty_code=РБ002085` используется как production-фильтр. При каждой
сборке payload также возвращает `counterparty_ref`, чтобы можно было сверить
карточку контрагента с `1С`.

Минимальный JSON payload:

```json
{
  "status": "ready",
  "control_status": "ok",
  "counterparty_ref": "0x93040025901e48ee11e3aa7223b36751",
  "counterparty_code": "РБ002085",
  "counterparty_name": "Обменник",
  "generated_at_msk": "2026-05-14T09:00:00+03:00",
  "source": "1c_mutual_settlements",
  "summary_by_currency": [
    {
      "contract_currency_code": "RUB",
      "contract_currency_name": "руб",
      "contract_count": 2,
      "opening_balance": "0.00",
      "inflow_amount": "0.00",
      "outflow_amount": "12345.67",
      "current_balance": "12345.67",
      "opening_balance_rub": "0.00",
      "inflow_amount_rub": "0.00",
      "outflow_amount_rub": "12345.67",
      "current_balance_rub": "12345.67",
      "effective_rate": "1.000000",
      "movement_count": 10,
      "last_movement_at": "2026-05-14T08:45:12"
    }
  ],
  "rub_control": {
    "rub_inflow": "73988336.00",
    "foreign_outflow_rub": "73988330.01",
    "movement_diff_rub": "5.99",
    "closing_balance_rub": "3522.70",
    "movement_tolerance_rub": "100.00",
    "closing_balance_tolerance_rub": "10000.00",
    "movement_status": "ok",
    "closing_status": "ok",
    "status": "ok"
  },
  "rate_mismatch_control": {
    "status": "warning",
    "check_from": "2026-01-01",
    "check_to_msk": "2026-05-14T09:00:00+03:00",
    "mismatch_count": 1,
    "total_diff_rub": "3000.00",
    "total_abs_diff_rub": "3000.00",
    "tolerance_rub": "1.00",
    "returned_count": 1,
    "items": [
      {
        "document_type": "Приходный кассовый ордер",
        "document_number": "РБГУ0020374",
        "document_at": "2026-01-24T10:56:31",
        "currency_name": "USD",
        "document_amount": "5000.00",
        "document_rate": "77.600000",
        "document_multiplicity": "1.000000",
        "expected_rub": "388000.00",
        "movement_rub": "385000.00",
        "diff_rub": "3000.00"
      }
    ]
  },
  "contract_balances": [
    {
      "contract_ref": "0x...",
      "contract_name": "Договор ...",
      "contract_currency_code": "RUB",
      "contract_currency_name": "руб",
      "opening_balance": "0.00",
      "inflow_amount": "0.00",
      "outflow_amount": "12345.67",
      "current_balance": "12345.67",
      "opening_balance_rub": "0.00",
      "inflow_amount_rub": "0.00",
      "outflow_amount_rub": "12345.67",
      "current_balance_rub": "12345.67",
      "effective_rate": "1.000000",
      "movement_count": 10,
      "last_movement_at": "2026-05-14T08:45:12"
    }
  ],
  "detail_rows": []
}
```

Read-only контракт по остаткам денег:

```text
GET /api/management/cash-position?period_start=YYYY-MM-DD&as_of=YYYY-MM-DDTHH:MM:SS&include_zero=false&top=15
```

Минимальный JSON payload:

```json
{
  "status": "ready",
  "generated_at_msk": "2026-05-14T09:00:00+03:00",
  "period_start": "2026-05-01",
  "period_end_msk": "2026-05-14T09:00:00+03:00",
  "source": "1c_money_places",
  "summary_by_category_currency": [
    {
      "category": "bank_accounts",
      "category_name": "счета",
      "currency_code": "643",
      "currency_name": "руб",
      "place_count": 3,
      "nonzero_place_count": 2,
      "opening_balance": "0.00",
      "inflow_amount": "0.00",
      "outflow_amount": "0.00",
      "current_balance": "15533775.87",
      "movement_count": 10
    }
  ],
  "top_balances": [],
  "money_place_count": 88
}
```

Для XLSX:

- лист `Итоги` - generated time, counterparty, status, отдельные строки по
  валютам и блок рублевого контроля;
- лист `Договоры` - contract-level balances с натуральными суммами и рублевым
  эквивалентом;
- лист `Движения` - optional детализация, если она нужна для сверки.

# Invariants

- Нельзя показывать один общий итог по всем валютам.
- Рублевый эквивалент можно показывать как отдельный контрольный блок, но он не
  заменяет отдельные валютные итоги.
- Любая сумма должна иметь валюту договора рядом с числом.
- Для валютных строк должны быть видны натуральная сумма, рублевый эквивалент и
  эффективный курс, если его можно корректно вывести из данных `1С`.
- Контроль `rub_inflow - foreign_outflow_rub` должен иметь статус `ok/warning/error`
  по настраиваемому порогу.
- Итоговый рублевый остаток/хвост должен иметь отдельный статус `ok/warning/error`
  по настраиваемому порогу.
- Контроль курсовых ошибок сравнивает расчет `сумма документа * курс /
  кратность` со значением рублевого эквивалента, записанным в регистр
  взаиморасчетов. Расхождение выше `1.00` ₽ делает общий статус `warning`.
- `generated_at_msk` обязателен и не заменяется только датой.
- Денежные остатки выводятся отдельно по категории (`счета`, `кассы`,
  `карты/эквайринг`, `прочее`) и валюте; разные валюты не суммируются.
- В отчете должны сохраняться знаки остатков: долг и переплата не схлопываются
  в абсолютное значение.
- Production-фильтр должен использовать стабильный идентификатор контрагента
  из `1С`, а не только строку `Обменник`.
- Если `1С` недоступна или данные не прошли сверку, отчет должен уйти в статус
  `degraded/missing`, а не публиковать старые цифры как текущие.

# Errors / Edge Cases

- Контрагент `Обменник` не найден: отчет не отправляется как ready, уходит
  техническое уведомление владельцу finance.
- Найдено несколько контрагентов с похожим именем: production-джоба блокируется
  до выбора `counterparty_ref`.
- Есть несколько валют договора: выводить отдельные строки по каждой валюте,
  без общего mixed-currency total.
- Рублевый эквивалент валютного расхода не сходится с рублевым приходом:
  сообщение начинается с `ВНИМАНИЕ`, указывается сумма расхождения и строки,
  которые формируют отличие.
- Итоговый рублевый остаток выше порога: отчет считается `warning/error`, даже
  если движения периода сошлись.
- Курс в документе изменен задним числом, но регистр взаиморасчетов остался с
  прежним рублевым эквивалентом: в утренний отчет попадает документ, сумма по
  курсу документа, сумма в регистре и разница.
- По валютной строке нет рублевого эквивалента или невозможно вывести курс:
  строка попадает в детализацию как ошибка данных.
- Есть нулевые остатки: в краткой таблице можно скрывать, в XLSX оставлять
  опционально для аудита.
- Детализация слишком большая для текста: отправлять сводку + XLSX.
- Источник 1С stale/unavailable: не использовать предыдущий отчет без явной
  пометки `данные не обновлены`.

# Tests

- Unit: aggregation groups by `contract_currency_code/name` and never sums
  different currencies into one row.
- Unit: rub control calculates `rub_inflow`, `foreign_outflow_rub`,
  `movement_diff_rub` and statuses by thresholds.
- Unit: closing rub balance control marks non-zero residual above threshold as
  warning/error.
- Unit: renderer shows rate mismatch warnings with document number, currency,
  expected rub amount, register rub amount and difference.
- Unit: effective rate is calculated only when native movement amount is non-zero
  and rub equivalent is present.
- Unit: renderer always includes `generated_at_msk`.
- Unit: negative and positive balances keep sign.
- API: exchange endpoint filters by `counterparty_code=РБ002085` and returns
  resolved `counterparty_ref`.
- API: cash position endpoint groups balances by category and currency without
  mixed-currency totals.
- Adapter: dry-run builds text summary and XLSX without Telegram/Bitrix side
  effects.
- Adapter: dedupe key changes when balances/details change and stays stable when
  payload is unchanged.
- Manual acceptance: compare output for `Обменник` with `1С` report
  `Ведомость по взаиморасчетам с контрагентами` at the same as-of time.

# Rollout

1. Точный `counterparty_ref` для `Обменник` в `1С` найден и сверяется в payload.
2. Источник цифр подтвержден прямым read-only SQL по регистрам `1С`,
   совпадающим с ручной ведомостью по взаиморасчетам.
3. Блок встроен в ежедневный утренний отчет Карине.
4. Пороги первой версии: расхождение движений `100.00` ₽, рублевый хвост
   `10000.00` ₽, курсовое расхождение документа и регистра `1.00` ₽.
5. Read-only API и adapter реализованы и проверены в dry-run.
6. Дальше: сделать 2-3 ручные сверки с `1С` на одинаковое время формирования.
7. Дальше: через неделю проверить объем строк и решить финальный формат:
   короткая таблица в утреннем отчете или отдельный XLSX/таблица.

# Открытые Вопросы

- Нужна ли отдельная детализация по местам хранения денег, если блок
  `прочее` остается существенным.
- Нужна ли детализация движений каждый день или только при изменениях/ненулевых
  остатках.
- Нужны ли нулевые валютные строки в daily XLSX.

# Changelog

- 2026-05-14 - draft created from finance request.
- 2026-05-14 - implemented read-only exchange/cash APIs and CFO morning digest
  integration for `РБ002085`.
- 2026-05-14 - added daily rate mismatch control for `Обменник` documents in
  Karina's morning report.
