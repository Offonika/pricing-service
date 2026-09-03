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
  - tests/test_compare_employee_receivable_report.py
  - tests/test_analyze_pickup_contract_settlements_script.py
  - tests/test_analyze_site_defect_working_cases_script.py
  - tests/test_analyze_manual_matching_feedback_task.py
  - tests/test_architecture_boundaries.py
  - tests/test_build_assortment_lifecycle_facts_task.py
  - tests/test_build_order_fulfillment_review_csv_script.py
  - tests/test_build_order_fulfillment_stage_outbox_script.py
  - tests/test_build_display_working_confirmation_overrides_task.py
  - tests/test_build_missing_display_quality_updates_task.py
  - tests/test_build_missing_onec_subject_updates_task.py
  - tests/test_build_ved_akb_master_register_task.py
  - tests/test_receivable_credit_profile.py
  - tests/test_receivable_decision_portrait.py
  - tests/test_check_onec_catalog_scope_script.py
  - tests/test_check_receivable_authoritative_snapshot_task.py
  - tests/test_cli_registry.py
  - tests/test_compare_receivable_current_report.py
  - tests/test_database_infrastructure.py
  - tests/test_export_display_matching_review_workbook_task.py
  - tests/test_export_display_quality_review_workbook_task.py
  - tests/test_export_receivable_work_report_task.py
  - tests/test_export_sms_journal_xlsx.py
  - tests/test_product_classification.py
  - tests/test_publish_weekly_kpi_reports_task.py
  - tests/test_release_builder.py
  - tests/test_report_display_auto_order_backtest.py
  - tests/test_report_display_auto_order_adaptive_lead_time_comparison_task.py
  - tests/test_report_display_supplier_lead_time_history_task.py
  - tests/test_report_exclusive_auto_detect_candidates_task.py
  - tests/test_report_parsed_models_task.py
  - tests/test_report_product_compatibility_sync_task.py
contracts: []
depends_on:
  - docs/specs/pricing-service-architecture-hardening.md
supersedes: []
rollout_required: true
updated_at: "2026-09-03"
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
- [x] Перевести `scripts/build_order_fulfillment_review_csv.py` на central read-only
  session scope и role-specific 1С factory с сохранением аргументов, read-only
  Bitrix enrichment и CSV-контракта.
- [x] Перевести `scripts/build_order_fulfillment_stage_outbox.py` на central
  read-only session scope и role-specific 1С factory с сохранением аргументов,
  read-only Bitrix stage enrichment, фильтров и CSV-контракта.
- [x] Перевести `scripts/analyze_site_defect_working_cases.py` на central read-only
  session scope с сохранением аргументов, JSON-контракта и опционального Bitrix
  `--apply`.
- [x] Перевести `scripts/check_onec_catalog_scope.py` на central read-only session
  scope с сохранением role-specific 1С engine, аргументов, JSON-контракта и exit
  codes.
- [x] Перевести `scripts/analyze_pickup_contract_settlements.py` на role-specific
  read-only 1С factory с bounded timeout и гарантированным dispose, сохранив SQL,
  аргументы и CSV/Markdown-контракты.
- [x] Перевести `tasks/report_display_auto_order_backtest.py` на central read-only
  session scope и role-specific 1С factory с bounded timeout и гарантированным
  dispose, сохранив формулы, аргументы и CSV/JSON-контракты.
- [x] Перевести `tasks/report_display_supplier_lead_time_history.py` на
  role-specific read-only 1С factory с bounded timeout и гарантированным dispose,
  сохранив SQL, аргументы и CSV/JSON-контракты.
- [x] Перевести `tasks/report_display_auto_order_adaptive_lead_time_comparison.py`
  на central read-only session scope с сохранением аргументов, fail-closed family
  registry overlay и CSV/JSON-контрактов.
- [x] Перевести `tasks/build_receivable_decision_portraits.py` на central read-only
  session scope и role-specific 1С factory с bounded timeout и гарантированным
  dispose, сохранив аргументы, folder filter и JSON/CSV-контракты.
- [x] Перевести `tasks/build_receivable_credit_profiles.py` на central read-only
  session scope и role-specific 1С factory с bounded timeout и гарантированным
  dispose, сохранив аргументы, folder filter и JSON/CSV-контракты.
- [x] Перевести `tasks/build_missing_display_quality_updates.py` на central
  read-only session scope и role-specific 1С factory с bounded timeout и
  гарантированным dispose, сохранив правила качества, аргументы и
  CSV/JSON/XML-контракты.
- [x] Перевести `tasks/build_missing_onec_subject_updates.py` на central read-only
  session scope и role-specific 1С factory с bounded timeout и гарантированным
  dispose, сохранив классификацию предметов, аргументы и JSON/XML-контракты.
- [x] Перевести `tasks/build_ved_akb_master_register.py` на central read-only
  session scope и role-specific 1С factory с bounded timeout и гарантированным
  dispose, сохранив SQL, аргументы и XLSX-контракт.
- [x] Перевести `tasks/build_assortment_lifecycle_facts.py` на central read-only
  session scope и role-specific 1С factory с bounded timeout и гарантированным
  dispose, сохранив аргументы, offline SQLite fixture и JSON-контракт.
- [x] Перевести `tasks/check_logistics_stage_outbox_health.py` на central
  read-only session scope с сохранением `--max-delay-seconds`, JSON-контракта и
  exit code для critical status.
- [x] Перевести `tasks/compare_employee_receivable_report.py` на role-specific
  read-only 1С factory с bounded timeout и гарантированным dispose, сохранив
  `--onec-url`, фильтры, временный SQLite snapshot и TSV-контракт.
- [x] Перевести `scripts/validate_receivables_release.py` на central read-only
  session scope с сохранением fail-closed UI, bundle, open-debt checks, JSON и
  exit code контрактов.
- [x] Перевести `scripts/validate_executive_dashboard_release.py` на central
  read-only session scope с сохранением UI, routes, snapshots, Alembic revision,
  JSON и exit code контрактов.
- [x] Перевести `tasks/publish_weekly_kpi_reports.py` с ручных
  `build_engine` / `Session` / `commit` на application Unit of Work; объявить
  `application_write` и атомарную транзакцию в CLI registry, подтвердить полный
  rollback и идемпотентный повтор. Production, cron и миграции в срез не входят.
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
32. После зелёного CI разрешён merge PR №94. Production migration, deploy и cutover
    в это решение не входят.
33. Для `scripts/build_order_fulfillment_review_csv.py` разрешены реализация в
    отдельной ветке, push и создание отдельного PR после локальных проверок. Merge
    и production release требуют отдельных подтверждений.
34. После зелёного CI разрешён merge PR №97. Production migration, deploy и cutover
    в это решение не входят.
35. Для `scripts/build_order_fulfillment_stage_outbox.py` разрешены реализация в
    отдельной ветке, push и создание отдельного PR после локальных проверок. Merge
    и production release требуют отдельных подтверждений.
36. После зелёного CI разрешён merge PR №98. Production migration, deploy и cutover
    в это решение не входят.
37. Для `scripts/analyze_site_defect_working_cases.py` разрешены реализация в
    отдельной ветке, push и создание отдельного PR после локальных проверок. Merge
    и production release требуют отдельных подтверждений.
38. После зелёного CI разрешён merge PR №99. Production migration, deploy и cutover
    в это решение не входят.
39. Для `scripts/check_onec_catalog_scope.py` разрешены реализация в отдельной
    ветке, push и создание отдельного PR после локальных проверок. Merge и
    production release требуют отдельных подтверждений.
40. После зелёного CI разрешён merge PR №101. Production migration, deploy и cutover
    в это решение не входят.
41. Для `scripts/analyze_pickup_contract_settlements.py` разрешены реализация в
    отдельной ветке, push и создание отдельного PR после локальных проверок. Merge
    и production release требуют отдельных подтверждений.
42. После зелёного CI разрешён merge PR №102. Production migration, deploy и
    cutover в это решение не входят.
43. Для `tasks/report_display_auto_order_backtest.py` разрешены push ветки и
    создание отдельного PR после локальных проверок. Merge и production release
    требуют отдельных подтверждений.
44. После зелёного CI разрешён merge PR №108. Production migration, deploy и
    cutover в это решение не входят.
45. Для `tasks/report_display_supplier_lead_time_history.py` разрешены push ветки
    и создание отдельного PR после локальных проверок. Merge и production release
    требуют отдельных подтверждений.
46. После зелёного CI разрешён merge PR №110. Production migration, deploy и
    cutover в это решение не входят.
47. Для `tasks/report_display_auto_order_adaptive_lead_time_comparison.py`
    разрешены push ветки и создание отдельного PR после локальных проверок. Merge
    и production release требуют отдельных подтверждений.
48. После зелёного CI разрешён merge PR №111. Production migration, deploy и
    cutover в это решение не входят.
49. Для `tasks/build_receivable_decision_portraits.py` разрешены push ветки и
    создание отдельного PR после локальных проверок. Merge и production release
    требуют отдельных подтверждений.
50. После зелёного CI разрешён merge PR №114. Production migration, deploy и
    cutover в это решение не входят.
51. Для `tasks/build_receivable_credit_profiles.py` разрешены push ветки и
    создание отдельного PR после локальных проверок. Merge и production release
    требуют отдельных подтверждений.
52. После зелёного CI разрешён merge PR №115. Production migration, deploy и
    cutover в это решение не входят.
53. Для `tasks/build_missing_display_quality_updates.py` разрешены push ветки и
    создание отдельного PR после локальных проверок. Merge и production release
    требуют отдельных подтверждений.
54. После зелёного CI разрешён merge PR №116. Production migration, deploy и
    cutover в это решение не входят.
55. Для `tasks/build_missing_onec_subject_updates.py` разрешены push ветки и
    создание отдельного PR после локальных проверок. Merge и production release
    требуют отдельных подтверждений.
56. После зелёного CI разрешён merge PR №121. Production migration, deploy и
    cutover в это решение не входят.
57. Для `tasks/build_ved_akb_master_register.py` разрешены push ветки и создание
    отдельного PR после локальных проверок. Merge и production release требуют
    отдельных подтверждений.
58. После зелёного CI разрешён merge PR №124. Production migration, deploy и
    cutover в это решение не входят.
59. Для `tasks/build_assortment_lifecycle_facts.py` разрешены push ветки и
    создание отдельного PR после локальных проверок. Merge и production release
    требуют отдельных подтверждений.
60. После зелёного CI разрешён merge PR №125. Production migration, deploy и
    cutover в это решение не входят.
61. Для `tasks/check_logistics_stage_outbox_health.py` разрешены реализация в
    отдельной ветке, push и создание отдельного PR после локальных проверок.
    Merge и production release требуют отдельных подтверждений.
62. После зелёного CI разрешён merge PR №128. Production migration, deploy и
    cutover в это решение не входят.
63. Для `tasks/compare_employee_receivable_report.py` разрешены commit проверенного
    среза, push ветки и создание отдельного PR. Merge и production release требуют
    отдельных подтверждений.
64. После зелёного CI разрешён merge PR №129. Production migration, deploy и
    cutover в это решение не входят.
65. Следующим локальным read-only срезом Release B выбран
    `scripts/validate_receivables_release.py`; разрешена реализация в отдельной
    clean worktree. Commit, push, создание PR и production release требуют
    отдельных подтверждений.
66. Для `scripts/validate_receivables_release.py` разрешены commit проверенного
    среза, push ветки и создание отдельного PR. Merge и production release требуют
    отдельных подтверждений.
67. После зелёного CI разрешён merge PR №133. Production migration, deploy и
    cutover в это решение не входят.
68. Следующим локальным read-only срезом Release B выбран
    `scripts/validate_executive_dashboard_release.py`; разрешена реализация в
    отдельной clean worktree. Commit, push, создание PR и production release
    требуют отдельных подтверждений.
69. Для `scripts/validate_executive_dashboard_release.py` разрешены commit
    проверенного среза, push ветки и создание отдельного PR. Merge и production
    release требуют отдельных подтверждений.
70. После зелёного CI разрешён merge PR №137. Production migration, deploy и
    cutover в это решение не входят.
71. Для `tasks/publish_weekly_kpi_reports.py` разрешены commit проверенного среза,
    push ветки и создание отдельного PR. Merge и production release требуют
    отдельных подтверждений.
72. После зелёного CI разрешён merge PR №167. Production migration, deploy и
    cutover в это решение не входят.

# Changelog

- 2026-09-03 — разрешён merge PR №167; production release оставлен отдельным
  решением.
- 2026-09-03 — разрешены commit, push и создание отдельного PR для первого Unit
  of Work среза; merge и production оставлены отдельными решениями.
- 2026-09-02 — утверждён и реализован первый Unit of Work срез для
  `publish_weekly_kpi_reports.py`; production, cron и миграции исключены.
- 2026-08-31 — разрешён merge PR №137; production release оставлен отдельным
  решением.
- 2026-08-31 — разрешены commit проверенного read-only среза
  `scripts/validate_executive_dashboard_release.py`, push ветки и создание отдельного
  PR; merge и production release оставлены отдельными решениями.
- 2026-08-31 — `scripts/validate_executive_dashboard_release.py` переведён на
  central read-only session scope; UI, routes, snapshots, Alembic revision, JSON
  и exit code контракты сохранены и покрыты профильным тестом.
- 2026-08-31 — следующим локальным read-only срезом Release B выбран
  `scripts/validate_executive_dashboard_release.py`; commit, push, PR и production
  release оставлены отдельными решениями.
- 2026-08-31 — разрешён merge PR №133; production release оставлен отдельным
  решением.
- 2026-08-31 — разрешены commit проверенного read-only среза
  `scripts/validate_receivables_release.py`, push ветки и создание отдельного PR;
  merge и production release оставлены отдельными решениями.
- 2026-08-31 — `scripts/validate_receivables_release.py` переведён на central
  read-only session scope; fail-closed UI, bundle, open-debt checks, JSON и exit
  code контракты сохранены и покрыты профильным тестом.
- 2026-08-31 — следующим локальным read-only срезом Release B выбран
  `scripts/validate_receivables_release.py`; commit, push, PR и production release
  оставлены отдельными решениями.
- 2026-08-31 — разрешён merge PR №129; production release оставлен отдельным
  решением.
- 2026-08-31 — разрешены commit проверенного read-only среза
  `compare_employee_receivable_report.py`, push ветки и создание отдельного PR;
  merge и production release оставлены отдельными решениями.
- 2026-08-31 — `tasks/compare_employee_receivable_report.py` переведён на
  role-specific read-only 1С factory с bounded timeout и гарантированным dispose;
  временный SQLite snapshot переведён на central session scope, CLI и TSV-контракт
  сохранены.
- 2026-08-31 — следующим read-only срезом Release B выбран
  `compare_employee_receivable_report.py`; production и расписания не затрагиваются.
- 2026-08-31 — разрешён merge PR №128; production release оставлен отдельным
  решением.
- 2026-08-30 — `tasks/check_logistics_stage_outbox_health.py` переведён на
  central read-only session scope; `--max-delay-seconds`, JSON-контракт и
  critical exit code сохранены, DB access зафиксирован в CLI registry.
- 2026-08-30 — разрешена подготовка logistics stage-outbox health read-only
  slice с push и отдельным PR; merge и production оставлены отдельными решениями.
- 2026-08-30 — разрешён merge PR №125; production release оставлен отдельным
  решением.
- 2026-08-30 — разрешена подготовка assortment lifecycle facts read-only slice
  с push и отдельным PR; merge и production оставлены отдельными решениями.
- 2026-08-30 — `tasks/build_assortment_lifecycle_facts.py` переведён на central
  read-only session scope и role-specific 1С factory с bounded timeout и
  гарантированным dispose; аргументы, offline SQLite fixture и JSON-контракт
  сохранены.
- 2026-08-30 — разрешён merge PR №124; production release оставлен отдельным
  решением.
- 2026-08-30 — разрешена подготовка VED AKB master register read-only slice с
  push и отдельным PR; merge и production оставлены отдельными решениями.
- 2026-08-30 — `tasks/build_ved_akb_master_register.py` переведён на central
  read-only session scope и role-specific 1С factory с bounded timeout и
  гарантированным dispose; SQL, аргументы и XLSX-контракт сохранены.
- 2026-08-30 — разрешён merge PR №121; production release оставлен отдельным
  решением.
- 2026-08-30 — разрешена подготовка missing 1C subject updates read-only slice с
  push и отдельным PR; merge и production оставлены отдельными решениями.
- 2026-08-30 — `tasks/build_missing_onec_subject_updates.py` переведён на central
  read-only session scope и role-specific 1С factory с bounded timeout и
  гарантированным dispose; классификация предметов, аргументы и JSON/XML-контракты
  сохранены.
- 2026-08-29 — разрешён merge PR №116; production release оставлен отдельным
  решением.
- 2026-08-29 — разрешена подготовка display quality updates read-only slice с
  push и отдельным PR; merge и production оставлены отдельными решениями.
- 2026-08-29 — `tasks/build_missing_display_quality_updates.py` переведён на
  central read-only session scope и role-specific 1С factory с bounded timeout и
  гарантированным dispose; правила качества, аргументы и CSV/JSON/XML-контракты
  сохранены.
- 2026-08-29 — разрешён merge PR №115; production release оставлен отдельным
  решением.
- 2026-08-29 — разрешена подготовка receivable credit profiles read-only slice с
  push и отдельным PR; merge и production оставлены отдельными решениями.
- 2026-08-29 — `tasks/build_receivable_credit_profiles.py` переведён на central
  read-only session scope и role-specific 1С factory с bounded timeout и
  гарантированным dispose; аргументы, folder filter и JSON/CSV-контракты сохранены.
- 2026-08-29 — разрешён merge PR №114; production release оставлен отдельным
  решением.
- 2026-08-29 — разрешена подготовка receivable decision portraits read-only slice
  с push и отдельным PR; merge и production оставлены отдельными решениями.
- 2026-08-29 — `tasks/build_receivable_decision_portraits.py` переведён на central
  read-only session scope и role-specific 1С factory с bounded timeout и
  гарантированным dispose; аргументы, folder filter и JSON/CSV-контракты сохранены.
- 2026-08-29 — разрешён merge PR №111; production release оставлен отдельным
  решением.
- 2026-08-29 — разрешена подготовка adaptive lead-time comparison read-only slice
  с push и отдельным PR; merge и production оставлены отдельными решениями.
- 2026-08-29 — `tasks/report_display_auto_order_adaptive_lead_time_comparison.py`
  переведён на central read-only session scope; аргументы, fail-closed family
  registry overlay и CSV/JSON-контракты сохранены.
- 2026-08-29 — разрешён merge PR №110; production release оставлен отдельным
  решением.
- 2026-08-29 — разрешена подготовка supplier lead-time read-only slice с push и
  отдельным PR; merge и production оставлены отдельными решениями.
- 2026-08-29 — `tasks/report_display_supplier_lead_time_history.py` переведён на
  role-specific read-only 1С factory с bounded timeout и гарантированным dispose;
  SQL, аргументы и CSV/JSON-контракты сохранены.
- 2026-08-29 — разрешён merge PR №108; production release оставлен отдельным
  решением.
- 2026-08-29 — разрешена подготовка отдельного read-only slice для
  `tasks/report_display_auto_order_backtest.py` с push и отдельным PR; merge и
  production оставлены отдельными решениями.
- 2026-08-29 — `tasks/report_display_auto_order_backtest.py` переведён на central
  read-only session scope и role-specific 1С factory с bounded timeout и
  гарантированным dispose; формулы, аргументы и CSV/JSON-контракты сохранены.
- 2026-08-29 — разрешён merge PR №102; production release оставлен отдельным
  решением.
- 2026-08-28 — `scripts/analyze_pickup_contract_settlements.py` переведён на
  role-specific read-only 1С factory с bounded timeout и гарантированным dispose;
  SQL, аргументы и CSV/Markdown-контракты сохранены.
- 2026-08-28 — разрешена подготовка отдельного read-only slice для
  `scripts/analyze_pickup_contract_settlements.py` с push и отдельным PR; merge и
  production оставлены отдельными решениями.
- 2026-08-28 — разрешён merge PR №101; production release оставлен отдельным
  решением.
- 2026-08-28 — `scripts/check_onec_catalog_scope.py` переведён на central read-only
  session scope; role-specific 1С engine, аргументы, JSON-контракт и exit codes
  сохранены.
- 2026-08-28 — разрешена подготовка отдельного read-only slice для
  `scripts/check_onec_catalog_scope.py` с push и отдельным PR; merge и production
  оставлены отдельными решениями.
- 2026-08-28 — разрешён merge PR №99; production release оставлен отдельным
  решением.
- 2026-08-28 — `scripts/analyze_site_defect_working_cases.py` переведён на central
  read-only session scope; аргументы, JSON-контракт и опциональный Bitrix `--apply`
  сохранены.
- 2026-08-28 — разрешена подготовка отдельного read-only slice для
  `scripts/analyze_site_defect_working_cases.py` с push и отдельным PR; merge и
  production оставлены отдельными решениями.
- 2026-08-28 — разрешён merge PR №98; production release оставлен отдельным
  решением.
- 2026-08-28 — `scripts/build_order_fulfillment_stage_outbox.py` переведён на
  central read-only session scope и role-specific 1С factory; аргументы, read-only
  Bitrix stage enrichment, фильтры, CSV-контракт и counters сохранены.
- 2026-08-28 — разрешена подготовка отдельного read-only slice для
  `scripts/build_order_fulfillment_stage_outbox.py` с push и отдельным PR; merge и
  production оставлены отдельными решениями.
- 2026-08-28 — разрешён merge PR №97; production release оставлен отдельным
  решением.
- 2026-08-28 — `scripts/build_order_fulfillment_review_csv.py` переведён на central
  read-only session scope и role-specific 1С factory; аргументы, read-only Bitrix
  enrichment и CSV-контракт сохранены.
- 2026-08-28 — разрешена подготовка отдельного read-only slice для
  `scripts/build_order_fulfillment_review_csv.py` с push и отдельным PR; merge и
  production оставлены отдельными решениями.
- 2026-08-28 — разрешён merge PR №94; production release оставлен отдельным
  решением.
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
