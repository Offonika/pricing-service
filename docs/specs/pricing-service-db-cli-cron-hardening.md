---
spec_id: "pricing-service-db-cli-cron-hardening"
title: "Pricing Service DB, CLI And Cron Hardening"
doc_type: spec
domain: "architecture"
status: "accepted"
owner: "engineering"
source_of_truth: true
related_code:
  - app/infrastructure/db/
  - tasks/
  - infra/cron/
  - scripts/validate_cli_registry.py
  - docs/registry/cli-jobs.json
related_tests:
  - tests/test_architecture_boundaries.py
  - tests/test_cli_registry.py
  - tests/test_database_infrastructure.py
contracts: []
depends_on:
  - docs/specs/pricing-service-architecture-hardening.md
supersedes: []
rollout_required: true
updated_at: "2026-08-15"
---

# Назначение

Завершить централизацию подключений к БД и транзакций в постоянных CLI/jobs,
оставив cron только слоем расписания, lock, запуска и технического лога.

# Scope / Out of Scope

Входит:

- перевод постоянных `tasks/`, `scripts/` и `infra/cron` на role-specific DB factories;
- `session_scope(read_only=True)` для read-only команд без commit;
- Unit of Work для команд, записывающих в PostgreSQL;
- машинно-проверяемые DB access, dry-run, idempotency и side-effect metadata;
- удаление SQL и бизнес-правил из cron entrypoints по мере миграции jobs.

Не входит:

- изменение бизнес-формул, внешних HTTP-контрактов или схем 1С;
- новые Bitrix24/Telegram side effects;
- изменение OFFONIKA-контуров;
- перенос доменов между репозиториями.

# Change Summary / Spec Delta

- Было: часть постоянных CLI использует generic `build_engine` и самостоятельно
  управляет Session/commit.
- Станет: тип доступа к БД объявлен в CLI registry, read-only команды используют
  централизованный scope, а write-команды — явный Unit of Work.
- Не меняется: входные аргументы команд, расписания, форматы артефактов и бизнес-результат.

# Acceptance Criteria

- [ ] Постоянные CLI не используют generic `build_engine`.
- [ ] Read-only CLI используют `session_scope(read_only=True)` и не выполняют commit.
- [ ] PostgreSQL write-команды выполняются внутри Unit of Work и откатываются целиком.
- [ ] 1С открывается только через read-only 1С factory.
- [ ] CLI registry проверяет DB access, dry-run, idempotency и side-effect metadata.
- [ ] В `infra/cron` нет SQL и бизнес-правил у мигрированных jobs.
- [ ] Повторный запуск постоянной job не создаёт дублей или частичного состояния.

# Source of Truth

- PostgreSQL `pricing-service` — application state.
- `1С УТ 10.3 / Ekama` — read-only торговый факт.
- `app/infrastructure/db/` — единственный слой создания engines, sessions и Unit of Work.
- `docs/registry/cli-jobs.json` — реестр эксплуатационных свойств CLI.

# Data Flow

```text
cron/systemd -> thin CLI adapter -> application service -> DB scope/UoW -> repository
1C read-only factory -> application service -> UoW -> pricing PostgreSQL
```

# API / Data Contracts

Внешние HTTP API и OpenAPI не меняются. CLI arguments и форматы существующих
JSON/CSV/XLSX артефактов сохраняются.

# Invariants

- Read-only scope всегда завершает транзакцию rollback, даже после успешного чтения.
- Исключение в write-команде откатывает всю команду.
- Cron не принимает бизнес-решения и не содержит SQL.
- Параметры пула задаются только типизированным конфигом DB infrastructure.

# Errors / Edge Cases

- Недоступность 1С не оставляет частично записанный PostgreSQL state.
- Повтор job после ошибки безопасен и следует заявленной idempotency policy.
- Нарушение DB access policy блокируется CI до merge.

# Implementation Checklist

- [x] 2026-08-11: контур выбран следующим приоритетом архитектурного аудита;
      аудит сначала проверяет фактическое состояние и не разрешает production-изменения.
- [x] Перевести read-only команды nightly matching на central read-only session scope.
- [x] Добавить `db_access` policy и проверку для мигрированных CLI.
- [x] Покрыть read-only rollback тестом DB infrastructure.
- [ ] Перевести оставшиеся read-only CLI и scripts на role-specific factories/scopes.
- [ ] Перевести постоянные write-команды на Unit of Work.
- [ ] Убрать бизнес-логику из оставшихся Python cron entrypoints.
- [ ] Подтвердить идемпотентность каждой постоянной scheduled job.
- [x] Выполнить isolated zero-regression canary для
      `pricing-display-supplier-lead-time-refresh`: пять артефактов совпали побайтово.
- [x] Переключить одну live cron-строку на active release symlink с готовым rollback.
- [x] Подтвердить первый штатный scheduled-run canary 2026-08-12 в 06:20 МСК:
      status `0`, пять ожидаемых артефактов созданы без изменения схемы.
- [x] Выбрать и изолированно проверить второй canary `onec_sales_kpi_sync`:
      mutable checkout и active release дали по 56 строк с одинаковым business-state
      SHA-256; повторный запуск не создал дублей.
- [x] Переключить только live cron-строку `onec_sales_kpi_sync` на active release
      symlink с сохранением расписания 03:20 МСК и адресным rollback-файлом.
- [x] Подтвердить первый штатный scheduled-run второго canary 2026-08-13 в 03:20 МСК:
      status `0`, 2012 строк за 36 дней, дублей по ключу дня/менеджера/магазина нет.
- [x] Выбрать и изолированно проверить третий canary `sku_result_sync_ut103`:
      mutable checkout и active release обработали одинаковые 104 XML-файла и
      30 289 SKU-результатов; повтор сохранил одинаковый state 38 334 товаров.
- [x] Переключить только live cron-строку `sku_result_sync_ut103` на active release
      symlink с сохранением ежечасного расписания `:45` и адресным rollback-файлом.
- [x] Подтвердить штатные scheduled-run третьего canary 2026-08-13 в 08:45,
      09:45, 10:45 и 11:45 МСК: status `0`, одинаковые 104 файла и 30 289
      SKU-результатов; итоговый state 38 334 товаров не изменился.
- [x] ОСТАНОВЛЕНО (2026-08-13): четвёртый кандидат
      `assortment_lifecycle_classification` не переключать. Mutable-код с 00:00
      ежечасно падает: production DB находится на Alembic revision
      `c3e5a7b9d1f2`, а код требует ещё не выпущенную миграцию
      `e5a7c9d1f3b4` с v2-полями и таблицей истории. Ошибки атомарно откатываются,
      частичных run/current/history записей нет. Возобновление — только отдельным
      согласованным release schema+code с zero-regression и rollback.
- [x] ОТМЕНЕНО (2026-08-11): `staffing_sync` был выбран вторым canary в режиме
      preparation-only. После диагностики job исключён из cron-canary: это
      незавершённая бизнес-интеграция без источников плана и факта смен.
- [x] Подтвердить побайтовое совпадение staffing wrapper/task/worker/service между
      mutable checkout и active release.
- [x] Подтвердить одинаковый isolated CLI-run обеих версий и идемпотентный повтор
      без дублей.
- [x] ОТМЕНЕНО (2026-08-11) в этом spec: выбор producer/system of record и настройка
      трёх JSON-входов вынесены в отдельную staffing-интеграцию.
- [x] ОТМЕНЕНО (2026-08-11) в этом spec: zero-regression проверка на реальных
      staffing-входах переносится в rollout отдельной интеграции.

# Review Notes / Risks

- Первый срез ограничен read-only командами nightly matching; write-команды не
  переводятся механически без проверки их текущих промежуточных commit.
- Нельзя заменять несколько commit одним Unit of Work, пока тестом не подтверждено,
  что job допускает атомарную транзакцию по полному batch.

# Tests

- Unit: commit/rollback Unit of Work и rollback read-only session scope.
- Architecture: direct engine construction и заявленный CLI DB access.
- Regression: полный pytest, Ruff, Black, CLI registry и architecture boundaries.
- Rollout smoke: одна nightly job с теми же counters и artifact hashes.

# Rollout

РЕШЕНИЕ (2026-08-11): отказ от массового переключения scheduled jobs. Первый
переход выполняется одним canary с неизменяемым release, isolated dry-run,
проверкой результата и готовым возвратом одной cron-строки. Обязательный gate —
отсутствие регрессий: одинаковые входы должны давать эквивалентные counters,
артефакты, изменения состояния и внешние side effects до и после переключения.

ОТМЕНЕНО (2026-08-15): требование не возвращать никакую версию job до выпуска v2
schema+code оказалось избыточным после проверки active release. Его заменяет
восстановление проверенного v1-кода, совместимого с текущей production-схемой;
обязательный совместный schema+code release сохраняется только для будущего v2.

РЕШЕНИЕ (2026-08-15): после временного отключения падающего mutable cron контур
восстановлен на schema-compatible active release `task-2985-matching-rejection-fix-
20260814-25d0f9d`. Перед включением выполнены dry-run, backup двух production-таблиц
и live readback; v2 и его миграции остаются отдельным rollout.

1. Выпускать небольшими slices по одному job-контру.
2. Перед переключением сравнить counters/artifacts с текущим production.
3. Сначала выполнить scheduled job в штатном режиме без новых external side effects.
4. Rollback выполняется переключением release symlink; миграций схемы в этом релизе нет.

# Changelog

- 2026-08-15 — `assortment_lifecycle_classification` восстановлен на active release:
  dry-run и live обработали по 2 734 товара, live создал run `1210`, сохранил
  47 613 current-строк без дублей и завершился со status `0`; v2 отделён от
  восстановительного rollout.
- 2026-08-15 — пользователь подтвердил начало исправления; падающий hourly cron
  `assortment_lifecycle_classification` временно отключён с адресным backup, а
  возврат разрешён только после совместимого schema+code release.
- 2026-08-13 — третий canary `sku_result_sync_ut103` подтверждён четырьмя штатными
  запусками; четвёртый кандидат `assortment_lifecycle_classification` остановлен до
  отдельного выпуска schema+code: действующий mutable-код несовместим с production DB.
- 2026-08-13 — второй canary `onec_sales_kpi_sync` прошёл штатный readback; rollout
  продолжен третьим canary `sku_result_sync_ut103`, переключённым на active release
  после одинакового isolated результата и проверки итогового состояния при повторе.
- 2026-08-12 — первый canary прошёл штатный scheduled readback; rollout продолжен
  вторым canary `onec_sales_kpi_sync`, переключённым на active release после
  одинакового isolated результата и идемпотентного повтора.
- ОТМЕНЕНО (2026-08-11): формулировка о «неуспешной предыдущей попытке» была
  ошибочной трактовкой сообщения пользователя.
- 2026-08-11 — rollout переведён на canary-first с обязательным zero-regression gate.
- 2026-08-11 — первый canary `pricing-display-supplier-lead-time-refresh` прошёл
  побайтовое сравнение и переключён на active release; scheduled readback ожидается.
- ОТМЕНЕНО (2026-08-11): `staffing_sync` был выбран вторым canary в режиме
  preparation-only; решение заменено отдельной задачей по интеграции смен.
- 2026-08-11 — подготовка `staffing_sync` подтвердила code parity и isolated
  idempotency; обнаружен блокер — production JSON-входы не настроены.
- 2026-08-11 — `staffing_sync` исключён из cron-canary; интеграция Staffing v2,
  плановых смен и фактических выходов вынесена в отдельную задачу.
- 2026-08-11 — DB/CLI/cron/idempotency выбран следующим архитектурным аудитом
  `pricing-service`.
- 2026-07-14 — accepted Release B spec; started read-only nightly matching slice.
