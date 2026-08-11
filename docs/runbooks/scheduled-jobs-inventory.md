---
title: Scheduled Jobs Inventory
doc_type: runbook
domain: operations
status: active
owner: pricing-platform
source_of_truth: true
updated_at: "2026-08-11"
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
| `pricing-assortment-lifecycle-classification` | ежечасно в :00 | рабочая папка |
| `pricing-sku-result-sync-ut103` | ежечасно в :45 | рабочая папка |
| `onec_assembly_crm_reconciler` | каждые 30 минут | рабочая папка |
| `order_fulfillment_sync` | каждые 30 минут, ежечасно в :05, ежедневно 11:00 | рабочая папка |
| `pricing-onec-stock-availability` | ежедневно 03:15, еженедельно вс 02:00 | релиз |
| `pricing-service-data-sync` | ежедневно 02:00, 02:30, 03:20 | смешанный: receivable — релиз; staffing и sales KPI — рабочая папка |
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

На 2026-08-11 система всё ещё работает из двух источников одновременно:

- служба API — из релиза `card-balance-ocr-proxy-20260811-ee077fb`;
- восемь cron entrypoints — из рабочей папки после первого canary-переключения.

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

Первый штатный запуск после cutover ожидается 2026-08-12 в 06:20 МСК. До его
успешного readback следующий job не переключать.

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

Переключение ветки в `/opt/MM/pricing-service` меняет код оставшихся восьми cron entrypoints.
После такого переключения проверить логи ближайших запусков — в первую очередь
`assortment_lifecycle_classification.log` и `order_fulfillment_sync.log`.
