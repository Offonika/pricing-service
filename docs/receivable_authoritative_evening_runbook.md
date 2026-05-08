# Вечерний runbook: пересборка дебиторки по сводной формуле

> Статус на 2026-04-22: production rebuild переведен на daily extractor 1С
> `_AccumRgT7009` + `_AccumRg7002`. Ledger остается enrichment-контуром, но
> больше не является primary source для суммы долга.

Документ фиксирует прежнюю рабочую гипотезу дебиторки:

```text
конечный остаток = начальный остаток на 2025-01-01 + движения 1С до даты snapshot
```

Формула должна быть единой для всех контрагентов. Покупатели, сотрудники, новая
дебиторка, просрочка и adjustment-кейсы являются срезами поверх одного
authoritative snapshot, а не отдельными расчётами.

## Почему старый rebuild был заблокирован

Контрольная ведомость 1С за период `01.01.2025` - `19.04.2026` без фильтров
показывает:

- начальный остаток: `-57 250 106,12`;
- приход: `4 023 335 162,68`;
- расход: `3 872 634 281,39`;
- конечный остаток: `93 450 775,17`.

Проверки 2026-04-22 показали:

- текущий layer extractor после технического CTE-фикса сходится по контрольным
  сотрудникам (`Байрамов`, `Хыдыров`, `Куценко`), но дает сводный итог около
  `58,39 млн`, то есть недобирает сводный контур;
- candidate `Tn7571 + _AccumRg7614` без фильтра по виду договора и с полем
  `_Fld7621` (`СуммаУпр`, рубли) дает сводный итог `93 357 931,31`, то есть
  близко к контрольным `93 450 775,17`, но не сходится по контрольным
  сотрудникам (`Хыдыров`, `Куценко`, `Байрамов`), поэтому production rebuild
  на нем запускать нельзя;
- raw `_AccumRg7614` + seed `2025-01-01` дает около `350,58 млн`, потому что
  физический регистр содержит технические движения заказов/корректировок;
- поле `_Fld7620` для этой ведомости использовать нельзя: на период
  `01.04.2026` - `19.04.2026` оно раздувает движение примерно до `31,68 млн`;
  для рублевой суммы отчета нужно `_Fld7621`;
- исключение технических регистраторов почти сводит общий итог, но ломает
  контрольных сотрудников, поэтому это не может быть production-формулой;
- крупный источник раздувания raw-пути - технические контрагенты вида
  `Потребности ...`, но исключение по имени тоже не является завершенной
  authoritative формулой.

Фикс 2026-04-22:

- найден физический слой полной ведомости без фильтров:
  - opening/current totals: `_AccumRgT7009`;
  - movements: `_AccumRg7002`;
  - контрагент: `_Fld7006RRef`;
  - рублевая сумма: `_Fld7008`;
  - знак движения: `_RecordKind = 0` плюс, `_RecordKind = 1` минус;
- `fetch_current_balances_from_onec(snapshot_date)` теперь строит один signed
  balance из этого слоя;
- `run_receivable_read_model_rebuild` больше не требует seeded ledger как
  условие production rebuild;
- контроль `2026-04-19` после пересборки:
  - snapshot: `93 451 078,17`;
  - reconciliation snapshot: `93 451 078,17`;
  - Excel full report: `93 450 775,17`;
  - остаточный хвост: `303,00` по мелким строкам/переименованиям;
  - synthetic refs: `0`;
  - `Байрамов Эльвин Эйваз Оглы`: `674 126,00`;
  - `Хыдыров Ахмет`: `316 295,00`;
  - `Куценко Дмитрий Алексеевич`: `174 081,40`.

## Источник суммы

- Начальный остаток внутри extractor-а: latest monthly totals
  `_AccumRgT7009` на первое число месяца snapshot.
- Движения внутри extractor-а: `_AccumRg7002` от opening period включительно до
  конца snapshot date.
- Текущие Excel-файлы покупателей/сотрудников: только для сверки, не для
  production-баланса.
- Все договоры и виды взаиморасчётов складываются сводно в рублях.
- В primary path запрещен фильтр по `Вид договора`: `С покупателем`,
  `С поставщиком`, `Прочее`, займы и прочие договоры должны попадать в один
  signed balance, а сегменты строятся уже после snapshot.

## Старая диагностическая последовательность, не production

```bash
cd /opt/MM/pricing-service
./.venv/bin/python -m tasks.sync_receivable_ledger \
  --sql-file samples/onec_receivables_hybrid_opening_plus_detail.sql \
  --snapshot-date 2026-04-19 \
  --opening-balance-date 2025-01-01 \
  --opening-import-path docs/ВзаиморасчетыВсе.normalized.csv \
  --replace-ledger
```

Команда очищает receivables-контур, поэтому до снятия блока ее нельзя запускать
на production БД.

## Пересборка snapshot-ов без 1С

После загрузки ledger диапазонный rebuild не должен ходить в 1С:

```bash
cd /opt/MM/pricing-service
./.venv/bin/python -m tasks.rebuild_receivable_snapshots_range \
  --date-from 2026-04-01 \
  --date-to 2026-04-21
```

Точечная пересборка даты тоже строится только из `receivable_ledger_event`:

```bash
cd /opt/MM/pricing-service
./.venv/bin/python -m tasks.rebuild_receivable_read_models \
  --snapshot-date 2026-04-19
```

## Быстрая загрузка без очистки

Если ledger уже очищен или нужно догрузить поверх текущего состояния:

```bash
cd /opt/MM/pricing-service
./.venv/bin/python -m tasks.sync_receivable_ledger \
  --sql-file samples/onec_receivables_hybrid_opening_plus_detail.sql \
  --snapshot-date 2026-04-19 \
  --opening-balance-date 2025-01-01 \
  --opening-import-path docs/ВзаиморасчетыВсе.normalized.csv
```

## Что проверить после прогона

- Сводный signed total на дату совпадает с ведомостью 1С.
- `receivable_ledger_event` начинается с `2025-01-01`.
- В ledger есть строки `opening_import_1c`.
- В primary ledger после чистого прогона нет месячных opening-слоёв 2026.
- Один контрагент на одну дату имеет одну сумму во всех витринах.
- Срезы `buyers`, `employees`, `new_daily`, `overdue`,
  `adjustment_candidates` строятся фильтрами поверх одного snapshot.
- `current_import_path` и `employee_current_import_path` не используются в
  production-балансе.
