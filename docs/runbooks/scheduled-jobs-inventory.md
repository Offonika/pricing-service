---
title: Scheduled Jobs Inventory
doc_type: runbook
domain: operations
status: active
owner: pricing-platform
source_of_truth: true
updated_at: "2026-08-26"
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
| `pricing-assortment-lifecycle-classification` | ежечасно в :00 | релиз |
| `pricing-sku-result-sync-ut103` | ежечасно в :45 | релиз |
| `onec_assembly_crm_reconciler` | каждые 30 минут | релиз |
| `order_fulfillment_sync` | каждые 30 минут, ежечасно в :10, ежедневно 11:00 | релиз |
| `pricing-onec-stock-availability` | ежедневно 03:15, еженедельно вс 02:00 | релиз |
| `pricing-service-data-sync` | ежедневно 02:00, 02:30, 03:20 | релиз |
| `pricing-sku-generation-ut103` | ежедневно 02:30 | релиз |
| `pricing-service-competitors` | ежедневно 04:10, 04:45, 05:20 | релиз |
| `pricing-display-supplier-lead-time-refresh` | ежедневно 06:20 | релиз |
| `sync_telephony_mapping` | ежедневно 08:35 | релиз |
| `sync_open_procurement_supplier_orders_to_bitrix` | каждые 30 минут, 08:00–21:59 | релиз |
| `manual_matching_bitrix_tasks` | по будням 09:10 | релиз |
| `pricing-executive-procurement-snapshot` | ежедневно 10:35 | релиз |
| `pricing-executive-management-balance` | ежедневно 11:40 | релиз |
| `bronze_price_type_monthly_inventory` | первого числа месяца 06:45 | релиз |
| `pricing-expertise-alarm-scan` | каждые 15 минут | релиз |
| `pricing-expertise-sync` | каждые 10 минут в :02 | релиз |
| `pricing-expertise-sync-watchdog` | каждые 5 минут | релиз |
| `pricing-executive-dashboard-monitor` | каждые 5 минут | релиз |

Логи всех заданий — в `/var/log/pricing/`, имя файла совпадает с именем задания.

`onec_assembly_crm_reconciler` поддерживает два транспорта. До отдельного
production-cutover используется `legacy-php`. Целевой `service-db` сохраняет
append-only события `1С` в `pricing-service`; переходы стадий затем принимает единая
state machine и применяет durable outbox. Переключение разрешено только вместе с
`ORDER_FULFILLMENT_EXECUTION_MASTER_ENABLED=true` и
`ORDER_FULFILLMENT_EXECUTION_INGEST_ENABLED=true` после dry-run/canary.

## Неактивные файлы

| Файл | Состояние |
|---|---|
| `pricing-order-fulfillment-sync` | Отключён 2026-05-26, все строки закомментированы. Заменён на `order_fulfillment_sync`. Причина отключения — дублирование отчётов |
| `pricing-service-data-sync.backup-20260701-154947` | Резервная копия. Содержит активные строки, но cron его не выполняет: файлы с точкой в имени игнорируются. Безвреден, кандидат на удаление |

## Известное расхождение

УСТРАНЕНО (2026-08-24): все активные scheduled jobs исполняются из единого
immutable source `/opt/MM/pricing-service-task43-current`. Последние два контура
переведены после guarded deploy clean release
`runtime-split-brain-final-20260824-c9f1469`. Штатный procurement-запуск в 10:00
подтвердил release interpreter и завершился со статусом `0`.

Решение 2026-08-24: устранение runtime split-brain назначено приоритетом №1.
Все активные scheduled jobs `pricing-service` переводятся с изменяемого
`/opt/MM/pricing-service` на единый проверенный immutable release source. Cutover
выполняется по одному job-контуру с backup, readback, контрольным запуском и
проверенным rollback. Production jobs из mutable checkout после перевода запрещены.

ОТМЕНЕНО (2026-08-24): временное исключение для двух mutable-контуров завершено
после выпуска совместимого clean release. До финального cutover из рабочей папки
оставались два активных контура:

- служба API и остальные scheduled jobs — из проверенного immutable release через
  `/opt/MM/pricing-service-task43-current`;
- `manual_matching_bitrix_tasks` временно остаётся в рабочей папке, потому что его
  текущая команда содержит ещё не выпущенное правило исключения ответственных;
- `sync_open_procurement_supplier_orders_to_bitrix` временно остаётся в рабочей
  папке, потому что его downstream lead-time pipeline отличается от release.

Эти контуры нельзя переключать на текущий release механически: manual matching
потеряет правило исключения ответственных, а procurement pipeline вернётся к другой
версии расчёта lead time. Сначала изменения должны попасть в проверенный immutable
release, после чего пути cron переключаются с тем же backup, readback и
rollback-порядком.

Решение 2026-08-24: для `manual_matching_bitrix_tasks` и
`sync_open_procurement_supplier_orders_to_bitrix` собирается отдельный clean
immutable release, сохраняющий текущее правило исключения ответственных и актуальный
downstream lead-time pipeline. Кандидат строится от активной production-цепочки,
проходит адресные и обязательные проверки и выкатывается только через
`/usr/local/sbin/mm-pricing-service-release deploy`. После успешного smoke последние
две строки cron переводятся на release; rollback возвращает прежний release и
сохранённые cron-конфигурации.

Решение 2026-08-24: финальная release-цепочка runtime cutover интегрируется в
`main` merge-коммитом, который одновременно является потомком active production
source и актуального `origin/main`. Cherry-pick runtime-коммитов поверх расходящейся
ветки запрещён: такой source потеряет release provenance и будет отклонён guarded
controller при следующей production-сборке.

## Проверка после изменения расписания

1. Убедиться, что скрипт существует по новому пути: `ls <путь>/infra/cron/<скрипт>`.
2. Дождаться ближайшего запуска и прочитать хвост лога в `/var/log/pricing/`.
3. Признак нормального завершения — строка `finished` и статус `0`.

## Проверка после переключения ветки в рабочей папке

Переключение ветки в `/opt/MM/pricing-service` меняет код девяти боевых заданий.
После такого переключения проверить логи ближайших запусков — в первую очередь
`assortment_lifecycle_classification.log` и `order_fulfillment_sync.log`.

## Changelog

- 2026-08-24 — Устранение runtime split-brain назначено приоритетом №1; принят
  поэтапный перевод всех активных jobs на единый immutable release source.
- 2026-08-24 — API monitor, expertise timers, competitor matching, 1С assembly,
  staffing и SKU jobs переведены на immutable release; mutable source сохранён только
  для контуров с ещё не выпущенными изменениями.
- 2026-08-24 — Найден и проаудирован root crontab; telephony и monthly bronze jobs
  переведены на release, procurement supplier sync оставлен на mutable source до
  выпуска совместимого downstream lead-time pipeline.
- 2026-08-24 — Подтверждена сборка и guarded deploy отдельного clean release для
  последних двух mutable scheduled jobs.
- 2026-08-24 — Clean release `runtime-split-brain-final-20260824-c9f1469`
  развёрнут через guarded controller; `manual_matching_bitrix_tasks` и
  `sync_open_procurement_supplier_orders_to_bitrix` переведены на release,
  production cron-ссылок на mutable checkout больше нет.
- 2026-08-24 — Подтверждена интеграция runtime cutover в `main` merge-коммитом с
  сохранением ancestry active production source и актуального `origin/main`.
