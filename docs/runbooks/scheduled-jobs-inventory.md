---
title: Scheduled Jobs Inventory
doc_type: runbook
domain: operations
status: active
owner: pricing-platform
source_of_truth: true
updated_at: "2026-08-15"
---

# Инвентарь заданий по расписанию

Что запускается на сервере по расписанию, откуда берётся код и куда пишется лог.

Документ появился 2026-08-08: до этого ни одно из заданий не было описано, и узнать
об их существовании можно было только просмотром `/etc/cron.d/` на сервере.

## Как читать таблицу

Колонка «источник» — ключевая. Задание берёт программу либо из **релиза**
(`/opt/MM/pricing-service-task43-current`, симлинк на неизменяемую сборку), либо из
**рабочей папки** (`/opt/MM/pricing-service`, изменяемый checkout для разработки).

Второе — источник риска: содержимое рабочей папки меняется при переключении ветки,
и боевое задание молча начинает выполнять другой код. См. раздел «Известное
расхождение».

## Активные задания

| Задание | Расписание (МСК) | Источник |
|---|---|---|
| `pricing-assortment-lifecycle-classification` | ежечасно в :00 | active release, восстановлен 2026-08-15 на совместимом v1 |
| `pricing-sku-result-sync-ut103` | ежечасно в :45 | релиз, canary с 2026-08-13 |
| `onec_assembly_crm_reconciler` | каждые 30 минут | рабочая папка |
| `order_fulfillment_sync` | каждые 30 минут, ежечасно в :05, ежедневно 11:00 | рабочая папка |
| `pricing-onec-stock-availability` | ежедневно 03:15, еженедельно вс 02:00 | релиз |
| `pricing-service-data-sync` | ежедневно 02:00, 02:30, 03:20 | смешанный: receivable и sales KPI — релиз; staffing — рабочая папка |
| `pricing-sku-generation-ut103` | ежедневно 02:30 | рабочая папка |
| `pricing-service-competitors` | ежедневно 04:10, 04:45, 05:20 | релиз |
| `pricing-display-supplier-lead-time-refresh` | ежедневно 06:20 | релиз, canary с 2026-08-11 |
| `manual_matching_bitrix_tasks` | по будням 09:10 | рабочая папка |
| `pricing-executive-procurement-snapshot` | ежедневно 10:35 | релиз |
| `pricing-executive-management-balance` | ежедневно 11:40 | релиз |

Логи всех заданий — в `/var/log/pricing/`, имя файла совпадает с именем задания.

## Неактивные файлы

| Файл | Состояние |
|---|---|
| `pricing-order-fulfillment-sync` | Отключён 2026-05-26, все строки закомментированы. Заменён на `order_fulfillment_sync`. Причина отключения — дублирование отчётов |
| `pricing-service-data-sync.backup-20260701-154947` | Резервная копия. Содержит активные строки, но cron его не выполняет: файлы с точкой в имени игнорируются. Безвреден, кандидат на удаление |

## Известное расхождение

На 2026-08-13 система всё ещё работает из двух источников одновременно:

- служба API — из релиза `customer-price-types-data-routing-20260812-a3937d6`;
- шесть cron entrypoints — из рабочей папки после третьего canary-переключения.

Между mutable checkout и active release на момент аудита было 117 различающихся
путей. Экраны и ночные расчёты могут по-разному интерпретировать одни и те же данные.

Источники сводятся к одному canary-first: только после isolated zero-regression
сравнения, с переключением одной cron-строки и адресным rollback.

## Canary: display supplier lead-time

2026-08-11 `pricing-display-supplier-lead-time-refresh` переведён на
`/opt/MM/pricing-service-task43-current` с явным `REPO_DIR`. Перед cutover:

- wrapper и task в mutable checkout и active release совпали побайтово;
- isolated release-run завершился с status `0`;
- пять CSV/JSON артефактов побайтово совпали с production baseline, включая SHA-256;
- release `reports/` подтверждён как symlink на постоянный
  `/opt/MM/pricing-service/reports`;
- rollback-копия cron сохранена в
  `/opt/MM/backups/pricing-service-cron-canary-20260811/`.

Первый штатный запуск после cutover состоялся 2026-08-12 в 06:20 МСК и завершился
в 06:24 со status `0`. Все пять ожидаемых CSV/JSON созданы, их схемы и внутренние
счётчики согласованы; rollback не потребовался.

## Canary: 1С Sales KPI

2026-08-12 `onec_sales_kpi_sync` выбран вторым canary и переведён на
`/opt/MM/pricing-service-task43-current` с явным `REPO_DIR`. Перед cutover:

- wrapper, task, worker, service, модель и env-loader в mutable checkout и active
  release совпали побайтово;
- обе версии прочитали один и тот же день 2026-08-11 из 1С и записали во временные
  SQLite-базы по 56 строк с одинаковым business-state SHA-256;
- повтор каждой версии удалил и вставил ровно те же 56 строк без дублей;
- production PostgreSQL во время isolated проверки не изменялся;
- сохранена полная копия `/etc/cron.d/pricing-service-data-sync` в
  `/opt/MM/backups/pricing-service-cron-canary-20260812/`;
- расписание 03:20 МСК, строки receivable и staffing не изменены.

Cron перечитал конфигурацию 2026-08-12 в 08:39 МСК. Первый штатный запуск второго
canary состоялся 2026-08-13 в 03:20 МСК и завершился в 03:23 со status `0`:
обработано 2012 строк за 36 дней, дублей по ключу дня/менеджера/магазина нет.
Rollback не потребовался.

## Canary: SKU result sync UT 10.3

2026-08-13 `pricing-sku-result-sync-ut103` выбран третьим canary и переведён на
`/opt/MM/pricing-service-task43-current` с явным `REPO_DIR`. Перед cutover:

- семь задействованных файлов mutable checkout и active release совпали побайтово;
- 104 входных XML-файла заморожены в отдельной копии;
- обе версии на копиях 38 334 товаров и 143 292 SKU-планов дали одинаковые counters:
  30 289 SKU-результатов, 29 929 уже синхронизированных и 147 конфликтов SKU;
- после первого и повторного запуска business-state SHA-256 всех товаров совпал
  между версиями и не изменился при повторе;
- production PostgreSQL во время isolated проверки открывался только для чтения;
- сохранена полная копия `/etc/cron.d/pricing-sku-result-sync-ut103` в
  `/opt/MM/backups/pricing-service-cron-canary-20260813/`;
- ежечасное расписание `:45` не изменено.

Cron перечитал конфигурацию 2026-08-13 в 08:10 МСК. Штатные запуски 08:45, 09:45,
10:45 и 11:45 завершились со status `0`: каждый обработал 104 файла и 30 289
SKU-результатов. Итоговый SHA-256 состояния 38 334 товаров во всех проверках:
`532217e25e9a53a5a841f05cf5868194a2bc7e50afc465063aa872d64172119d`.
Rollback не потребовался.

## Остановленная canary-подготовка: assortment lifecycle classification

2026-08-13 `assortment_lifecycle_classification` рассматривался четвёртым canary,
но live cron не переключался. Диагностика показала старый production-дефект:

- действующий cron каждый час запускает mutable checkout; с 00:00 все завершённые
  запуски имеют status `1`;
- production DB находится на Alembic revision `c3e5a7b9d1f2`: в таблице current
  нет v2-полей, включая `demand_state`, и отсутствует таблица
  `assortment_lifecycle_classification_history`;
- mutable-код уже требует миграцию `e5a7c9d1f3b4`, которой нет в active release;
- сначала ошибки были `UndefinedColumn`, после изменения mutable-кода —
  `UndefinedTable`; запуск 12:00 завершился в 12:17 тем же status `1`;
- run, history и current пишутся внутри одной DB-транзакции. При ошибке она
  откатывается целиком: последний успешный run остаётся `1158` от 2026-08-12
  19:00, частичных записей от неудачных запусков нет.

Применение v2-миграции запрещено этим runbook. Выпускать v2 можно только отдельным
неизменяемым release, содержащим совместимые schema+code, после backup, проверки
миграции на копии production-схемы, zero-regression и готового rollback. Переход к
jobs с внешними side effects без отдельной оценки также не разрешён.

2026-08-15 пользователь подтвердил начало исправления. Mutable cron временно
отключили, не прерывая запуск 07:00, и сохранили его копию в
`/opt/MM/backups/pricing-service-assortment-disable-20260815/`. Проверка показала,
что active release и production DB имеют одну schema revision `c3e5a7b9d1f2`.

Восстановление выполнено без v2-миграции:

- active release dry-run обработал 2 734 товара, ничего не записал и завершился
  со status `0`;
- перед live-run создан backup таблиц run/current размером около 23 МБ;
- live-run с ключом `assortment-recovery-20260815-0720` создал run `1210`, записал
  2 734 результата и сохранил общий объём 47 613 current-строк;
- dry-run и live дали одинаковую сводку статусов; live завершился status `0`;
- cron направлен на `/opt/MM/pricing-service-task43-current` с явным `REPO_DIR`.

Rollback восстановления — вернуть файл
`pricing-assortment-lifecycle-classification.disabled`; rollback к исходному
mutable-пути допускается только после устранения несовместимости v2.

## Отменённая canary-подготовка: staffing sync

2026-08-11 `staffing_sync` был подготовлен как второй canary, но live cron не
переключался. После диагностики job исключён из cron-canary и вынесен в отдельную
межпроектную задачу: сначала требуется спроектировать источники плановых смен и
фактических выходов. Проверка показала:

- wrapper, task, worker, staffing service и тесты в mutable checkout и active
  release совпадают побайтово;
- isolated CLI-run на SQLite дал одинаковые counters и итоговое состояние в обеих
  версиях; повторный запуск не создал дублей, SHA-256 результата совпал;
- подготовленная staffing-строка cron-шаблона возвращена на mutable checkout и не
  направляет незавершённую интеграцию в active release;
- live cron по-прежнему запускает staffing из `/opt/MM/pricing-service`;
- переменные `STAFFING_SYNC_STAFF_FILE`, `STAFFING_SYNC_PLAN_FILE` и
  `STAFFING_SYNC_FACT_FILE` в production `.env` отсутствуют; сохранённые логи с
  2026-05-22 по 2026-08-11 показывают только раннюю остановку до обращения к БД;
- в production DB есть 308 записей сотрудников с последним обновлением
  2026-04-17, но нет планов смен, факта смен и staffing snapshots.

Дальнейшая работа ведётся в
`/opt/MM/mm-compensation/docs/specs/bitrix-staffing-v2-shift-coverage-integration.md`.
Этот runbook не разрешает live cutover `staffing_sync`.

Если отдельная интеграция будет принята и реализована, её rollout и rollback должны
быть согласованы в её spec после проверки реальных входов.

## Проверка после изменения расписания

1. Убедиться, что скрипт существует по новому пути: `ls <путь>/infra/cron/<скрипт>`.
2. Дождаться ближайшего запуска и прочитать хвост лога в `/var/log/pricing/`.
3. Признак нормального завершения — строка `finished` и статус `0`.

## Проверка после переключения ветки в рабочей папке

Переключение ветки в `/opt/MM/pricing-service` меняет код оставшихся шести cron entrypoints.
После такого переключения проверить логи ближайших запусков — в первую очередь
`assortment_lifecycle_classification.log` и `order_fulfillment_sync.log`.
