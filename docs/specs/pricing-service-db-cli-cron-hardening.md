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
  - tests/test_analyze_manual_matching_feedback_task.py
  - tests/test_architecture_boundaries.py
  - tests/test_build_display_working_confirmation_overrides_task.py
  - tests/test_check_receivable_authoritative_snapshot_task.py
  - tests/test_cli_registry.py
  - tests/test_compare_receivable_current_report.py
  - tests/test_database_infrastructure.py
  - tests/test_export_display_matching_review_workbook_task.py
  - tests/test_export_display_quality_review_workbook_task.py
  - tests/test_export_receivable_work_report_task.py
  - tests/test_export_sms_journal_xlsx.py
  - tests/test_product_classification.py
  - tests/test_report_exclusive_auto_detect_candidates_task.py
  - tests/test_report_parsed_models_task.py
  - tests/test_report_product_compatibility_sync_task.py
contracts: []
depends_on:
  - docs/specs/pricing-service-architecture-hardening.md
supersedes: []
rollout_required: true
updated_at: "2026-08-28"
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

- [x] Перевести read-only команды nightly matching на central read-only session scope.
- [x] Добавить `db_access` policy и проверку для мигрированных CLI.
- [x] Покрыть read-only rollback тестом DB infrastructure.
- [x] Перевести manual matching report и Bitrix task adapter на central read-only
  scope с сохранением application DB override для тестов и one-off запусков.
- [x] Перевести `export_manual_status_overrides.py` на central read-only session
  scope и зафиксировать DB access, dry-run и artifact idempotency в CLI registry.
- [x] Перевести `export_management_marks.py` на central read-only session scope;
  зафиксировать optional external write и отсутствие идемпотентности между запусками.
- [x] Перевести `report_procurement_feature_snapshot_quality.py` на central
  read-only session scope с сохранением `--database-url` override и CSV-контракта.
- [x] Перевести `report_logistics_rtu_manual_review.py` на central read-only
  session scope с сохранением аргументов и JSON-контракта.
- [x] Перевести `report_display_quality_mismatch_candidates.py` на central
  read-only session scope с сохранением CSV и JSON-контрактов.
- [x] Перевести `report_display_sale_auto_order_treatment_plan.py` на central
  read-only session scope с сохранением `--database-url`, CSV и JSON-контрактов.
- [x] Перевести `export_receivable_work_report.py` на central read-only session
  scope с сохранением `--date`, `--output-dir` и XLSX-контракта.
- [x] Перевести `analyze_manual_matching_feedback.py` на central read-only session
  scope с сохранением `--database-url`, `--no-files` и Markdown/JSON/CSV-контрактов.
- [x] Перевести `export_display_quality_review_workbook.py` на central read-only
  session scope с сохранением `--output` и XLSX-контракта.
- [x] Перевести `export_display_matching_review_workbook.py` на central read-only
  session scope с сохранением `--input`, `--output`, score/gap и XLSX-контракта.
- [x] Перевести `export_sms_journal_xlsx.py` на central read-only session scope с
  сохранением allowlist, явного подтверждения, шифрования, XLSX и audit-контрактов.
- [x] Перевести `check_receivable_authoritative_snapshot.py` на central read-only
  session scope с сохранением `--snapshot-date`, `--control-name` и JSON-контракта.
- [x] Перевести `build_display_working_confirmation_overrides.py` на central
  read-only session scope с сохранением `--database-url`, аргументов отбора и
  JSON-контракта.
- [x] Перевести `compare_receivable_current_report.py` на central read-only session
  scope и role-specific 1С factory с сохранением аргументов, входного файла и
  JSON-контракта.
- [x] Перевести `report_product_compatibility_sync.py` на central read-only session
  scope и role-specific 1С factory с сохранением аргументов, site JSON и
  JSON/CSV-контрактов.
- [ ] Перевести оставшиеся read-only CLI и scripts на role-specific factories/scopes.
- [ ] Перевести постоянные write-команды на Unit of Work.
- [ ] Убрать бизнес-логику из оставшихся Python cron entrypoints.
- [ ] Подтвердить идемпотентность каждой постоянной scheduled job.

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

1. Выпускать небольшими slices по одному job-контру.
2. Перед переключением сравнить counters/artifacts с текущим production.
3. Сначала выполнить scheduled job в штатном режиме без новых external side effects.
4. Rollback выполняется переключением release symlink; миграций схемы в этом релизе нет.
5. Для manual matching slice разрешены push ветки и создание PR для полного GitHub CI.
   ОТМЕНЕНО (2026-08-25): требование отдельного подтверждения merge выполнено для
   PR №57; разрешён merge в `main`. Production release по-прежнему требует
   отдельного подтверждения.
6. Для `export_manual_status_overrides.py` после зелёного CI разрешён и выполнен
   merge PR №63 коммитом `0496ad30cf7a366e4bb68baa9f34af6d756b4e81`.
   Production migration, deploy и cutover в это решение не входят и не выполнялись.
7. Для `export_management_marks.py` после зелёного CI разрешён merge PR №65.
   Production migration, deploy и cutover в это решение не входят.
8. Для `report_logistics_rtu_manual_review.py` разрешены реализация в отдельной
   ветке, push и создание PR после локальных проверок. Merge и production release
   требуют отдельного подтверждения.
9. После зелёного CI разрешён merge PR №70. Production migration, deploy и cutover
   в это решение не входят.
10. Для `report_display_quality_mismatch_candidates.py` разрешены реализация в
    отдельной ветке, push и создание PR после локальных проверок. Merge и
    production release требуют отдельного подтверждения.
11. После зелёного CI разрешён merge PR №73. Production migration, deploy и cutover
    в это решение не входят.
12. 2026-08-27 разрешён production release текущего `main`, включающего PR №70–73.
    Разрешение не включает активацию автоматического движения стадий и SMS:
    `LOGISTICS_STAGE_AUTOMATION_ENABLED=false` и
    `PICKUP_READY_SMS_ENABLED=false` должны сохраниться после cutover.
13. Для `report_display_sale_auto_order_treatment_plan.py` разрешены реализация в
    отдельной ветке, push и создание PR после локальных проверок. Merge и
    production release требуют отдельного подтверждения.
14. После зелёного CI разрешён merge PR №76. Production migration, deploy и cutover
    в это решение не входят.
15. Для `export_receivable_work_report.py` разрешены реализация в отдельной ветке,
    push и создание отдельного PR после локальных проверок. Merge и production
    release требуют отдельных подтверждений.
16. После зелёного CI разрешён merge PR №78. Production migration, deploy и cutover
    в это решение не входят.
17. Для `analyze_manual_matching_feedback.py` разрешены реализация в отдельной
    ветке, push и создание отдельного PR после локальных проверок. Merge и
    production release требуют отдельных подтверждений.
18. После зелёного CI разрешён merge PR №80. Production migration, deploy и cutover
    в это решение не входят.
19. Для `export_display_quality_review_workbook.py` разрешены реализация в отдельной
    ветке, push и создание отдельного PR после локальных проверок. Merge и
    production release требуют отдельных подтверждений.
20. После зелёного CI разрешён merge PR №83. Production migration, deploy и cutover
    в это решение не входят.
21. Для `export_display_matching_review_workbook.py` разрешены реализация в
    отдельной ветке, push и создание отдельного PR после локальных проверок. Merge
    и production release требуют отдельных подтверждений.
22. После зелёного CI разрешён merge PR №85. Production migration, deploy и cutover
    в это решение не входят.
23. Для `export_sms_journal_xlsx.py` разрешены реализация в отдельной ветке, push и
    создание отдельного PR после локальных проверок. Merge и production release
    требуют отдельных подтверждений.
24. После зелёного CI разрешён merge PR №86. Production migration, deploy и cutover
    в это решение не входят.
25. Для `check_receivable_authoritative_snapshot.py` разрешены реализация в
    отдельной ветке, push и создание отдельного PR после локальных проверок. Merge
    и production release требуют отдельных подтверждений.
26. После зелёного CI разрешён merge PR №88. Production migration, deploy и cutover
    в это решение не входят.
27. Для `build_display_working_confirmation_overrides.py` разрешены реализация в
    отдельной ветке, push и создание отдельного PR после локальных проверок. Merge
    и production release требуют отдельных подтверждений.
28. После зелёного CI разрешён merge PR №90. Production migration, deploy и cutover
    в это решение не входят.
29. Для `compare_receivable_current_report.py` разрешены реализация в отдельной
    ветке, push и создание отдельного PR после локальных проверок. Merge и
    production release требуют отдельных подтверждений.
30. После зелёного CI разрешён merge PR №92. Production migration, deploy и cutover
    в это решение не входят.
31. Для `report_product_compatibility_sync.py` разрешены реализация в отдельной
    ветке, push и создание отдельного PR после локальных проверок. Merge и
    production release требуют отдельных подтверждений.

# Changelog

- 2026-08-28 — `report_product_compatibility_sync.py` переведён на central read-only
  session scope и role-specific 1С factory, зарегистрирован как
  `application_read_only`; аргументы, site JSON и JSON/CSV-контракты сохранены.
- 2026-08-28 — разрешена подготовка отдельного read-only slice для
  `report_product_compatibility_sync.py` с push и отдельным PR; merge и production
  оставлены отдельными решениями.
- 2026-08-28 — разрешён merge PR №92; production release оставлен отдельным
  решением.
- 2026-08-28 — `compare_receivable_current_report.py` переведён на central read-only
  session scope и role-specific 1С factory, зарегистрирован как
  `application_read_only`; аргументы, входной файл и JSON-контракт сохранены.
- 2026-08-28 — разрешена подготовка отдельного read-only slice для
  `compare_receivable_current_report.py` с push и отдельным PR; merge и production
  оставлены отдельными решениями.
- 2026-08-28 — разрешён merge PR №90; production release оставлен отдельным
  решением.
- 2026-08-28 — `build_display_working_confirmation_overrides.py` переведён на
  central read-only session scope и зарегистрирован как `application_read_only`;
  аргументы и JSON-контракт сохранены.
- 2026-08-28 — разрешена подготовка отдельного read-only slice для
  `build_display_working_confirmation_overrides.py` с push и отдельным PR; merge и
  production оставлены отдельными решениями.
- 2026-08-28 — разрешён merge PR №88; production release оставлен отдельным
  решением.
- 2026-08-28 — `check_receivable_authoritative_snapshot.py` переведён на central
  read-only session scope и зарегистрирован как `application_read_only`; аргументы
  и JSON-контракт сохранены.
- 2026-08-28 — разрешена подготовка отдельного read-only slice для
  `check_receivable_authoritative_snapshot.py` с push и отдельным PR; merge и
  production оставлены отдельными решениями.
- 2026-08-28 — разрешён merge PR №86; production release оставлен отдельным
  решением.
- 2026-08-28 — `export_sms_journal_xlsx.py` переведён на central read-only session
  scope и зарегистрирован как `application_read_only`; защитные проверки, XLSX и
  audit-контракты сохранены.
- 2026-08-28 — разрешена подготовка отдельного read-only slice для
  `export_sms_journal_xlsx.py` с push и отдельным PR; merge и production оставлены
  отдельными решениями.
- 2026-08-28 — разрешён merge PR №85; production release оставлен отдельным
  решением.
- 2026-08-28 — `export_display_matching_review_workbook.py` переведён на central
  read-only session scope и зарегистрирован как `application_read_only`; CLI и
  XLSX-контракт сохранены.
- 2026-08-28 — разрешена подготовка отдельного read-only slice для
  `export_display_matching_review_workbook.py` с push и отдельным PR; merge и
  production оставлены отдельными решениями.
- 2026-08-27 — разрешён merge PR №83; production release оставлен отдельным
  решением.
- 2026-08-27 — `export_display_quality_review_workbook.py` переведён на central
  read-only session scope и зарегистрирован как `application_read_only`; `--output`
  и XLSX-контракт сохранены.
- 2026-08-27 — разрешена подготовка отдельного read-only slice для
  `export_display_quality_review_workbook.py` с push и отдельным PR; merge и
  production оставлены отдельными решениями.
- 2026-08-27 — разрешён merge PR №80; production release оставлен отдельным
  решением.
- 2026-08-27 — `analyze_manual_matching_feedback.py` переведён на central
  read-only session scope и зарегистрирован как `application_read_only`; аргументы
  и Markdown/JSON/CSV-контракты сохранены.
- 2026-08-27 — разрешена подготовка отдельного read-only slice для
  `analyze_manual_matching_feedback.py` с push и отдельным PR; merge и production
  оставлены отдельными решениями.
- 2026-08-27 — разрешён merge PR №78; production release оставлен отдельным
  решением.
- 2026-08-27 — `export_receivable_work_report.py` переведён на central read-only
  session scope и зарегистрирован как `application_read_only`; аргументы и
  XLSX-контракт сохранены.
- 2026-08-27 — разрешена подготовка отдельного read-only slice для
  `export_receivable_work_report.py` с push и отдельным PR; merge и production
  оставлены отдельными решениями.
- 2026-08-27 — разрешён merge PR №76; production release оставлен отдельным
  решением.
- 2026-08-27 — разрешён production release текущего `main` с PR №70–73 без
  включения автоматического движения стадий и SMS.
- 2026-08-27 — `report_display_sale_auto_order_treatment_plan.py` переведён на
  central read-only session scope и зарегистрирован как `application_read_only`.
- 2026-08-27 — разрешена подготовка отдельного read-only slice для
  `report_display_sale_auto_order_treatment_plan.py`; merge и production оставлены
  отдельными решениями.
- 2026-08-27 — разрешён merge PR №73; production release оставлен отдельным
  решением.
- 2026-08-27 — `report_display_quality_mismatch_candidates.py` переведён на
  central read-only session scope и зарегистрирован как `application_read_only`.
- 2026-08-27 — разрешена подготовка отдельного read-only slice для
  `report_display_quality_mismatch_candidates.py`; merge и production оставлены
  отдельными решениями.
- 2026-08-27 — разрешён merge PR №70; production release оставлен отдельным
  решением.
- 2026-08-27 — `report_logistics_rtu_manual_review.py` переведён на central
  read-only session scope и зарегистрирован как `application_read_only`.
- 2026-08-27 — разрешена подготовка отдельного read-only slice для
  `report_logistics_rtu_manual_review.py`; merge и production оставлены отдельными
  решениями.
- 2026-08-27 — `report_procurement_feature_snapshot_quality.py` переведён на
  central read-only session scope и зарегистрирован как `application_read_only`.
- 2026-08-26 — разрешён merge PR №65; production release оставлен отдельным
  решением.
- 2026-08-26 — `export_management_marks.py` переведён на central read-only session
  scope; registry фиксирует optional external write и новый `message_id` на запуск.
- 2026-08-26 — разрешён и выполнен merge PR №63; production release оставлен
  отдельным решением.
- 2026-08-26 — `export_manual_status_overrides.py` переведён на central read-only
  session scope; CLI registry фиксирует application DB read-only, dry-run и
  byte-stable artifact merge.
- 2026-08-25 — после зелёного GitHub CI разрешён merge PR №57 в `main`;
  production release оставлен за отдельным подтверждением.
- 2026-08-25 — разрешены push manual matching slice и создание PR для полного
  GitHub CI; merge и production release оставлены за отдельным подтверждением.
- 2026-08-24 — `manual_matching_control.py` и `manual_matching_bitrix_tasks.py`
  переведены на central read-only session scope; их DB access и side effects
  зафиксированы в CLI registry.
- 2026-08-21 — `report_exclusive_auto_detect_candidates.py` переведён на центральный
  read-only session scope и зарегистрирован как `application_read_only`.
- 2026-08-21 — `report_product_classification_diff.py` переведён на центральный
  read-only session scope и зарегистрирован как `application_read_only`.
- 2026-08-21 — `report_parsed_models.py` переведён на центральный read-only
  session scope и добавлен в CLI registry как `application_read_only`.
- 2026-07-14 — accepted Release B spec; started read-only nightly matching slice.
