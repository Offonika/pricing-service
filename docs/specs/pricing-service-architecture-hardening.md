---
spec_id: "pricing-service-architecture-hardening"
title: "Pricing Service Architecture Hardening"
doc_type: spec
domain: "architecture"
status: "accepted"
owner: "engineering"
source_of_truth: true
related_code:
  - app/api/dependencies.py
  - app/api/orchestration.py
  - app/core/config.py
  - app/domains/management/orchestration/
  - app/infrastructure/orchestration.py
  - app/main.py
  - infra/cron/competitor_matching_nightly.sh
  - tasks/import_topcontrol_products_db.py
  - scripts/export_openapi.py
  - scripts/build_pricing_service_release.sh
  - scripts/switch_pricing_service_release.sh
related_tests:
  - tests/test_import_onec_products.py
  - tests/test_orchestration_api.py
  - tests/test_product_compatibility.py
  - tests/test_release_scripts.py
  - tests/test_weekly_kpi_reports_api.py
contracts:
  - openapi.yaml
depends_on:
  - docs/architecture.md
  - docs/specs/README.md
supersedes: []
rollout_required: true
updated_at: "2026-07-16"
---

# Назначение

Сделать `pricing-service` управляемым модульным монолитом без изменения
бизнес-формул, внешних HTTP-маршрутов и действующих Bitrix24/Telegram-процессов.
Spec фиксирует вывод TopControl из активного контура, единый слой подключений к БД,
границы между API/application/domain/infrastructure, тонкие cron/CLI entrypoints и
контрактный обмен с соседними проектами.

# Scope / Out of Scope

Входит:

- прямой read-only импорт каталога и свойств из `1С УТ 10.3 / Ekama`;
- compatibility alias старой команды TopControl на один релиз;
- центральные фабрики Postgres и MSSQL/1С, session factory и Unit of Work;
- постепенное устранение бизнес-логики из `infra/cron` и одноразовых `tasks`;
- доменные границы catalog, matching, pricing, assortment, procurement,
  receivables, management, expertise, logistics и telephony;
- API-ingest недельных KPI и версионированные shared snapshot contracts;
- architecture, OpenAPI, docs, migration и runtime quality gates;
- durable run/delivery state и authenticated orchestration API для root control plane;
- release-specific virtualenv, hash-locked dependencies и единый rollback backend/UI;
- release retention: активный релиз плюс три последних проверенных.

Не входит:

- физический перенос delivery/STT/telephony между репозиториями;
- изменение формул pricing, procurement, receivables, KPI или dashboard;
- новые Bitrix24/Telegram side effects;
- destructive database migrations;
- изменение OFFONIKA-контуров.

# Change Summary / Spec Delta

- Было: TopControl оставался в активных именах и документации, хотя importer уже
  читает `1С` через `ONEC_DATABASE_URL`.
- Станет: единственный активный источник каталога называется `1С`, старая CLI-команда
  один релиз делегирует новой и затем удаляется.
- Было: API, services, tasks и cron создавали собственные engines и самостоятельно
  определяли транзакционные границы.
- Станет: подключения создаются в центральных factories, write-use-case работает
  через явный Unit of Work, read-only use-case не делает commit.
- Было: часть бизнес-логики и интеграций жила параллельно в `app`, `tasks` и
  `infra/cron`, а соседние проекты обменивались DB URL и путями `build/`.
- Станет: cron/CLI остаются тонкими entrypoints, а межпроектный обмен идет через
  authenticated API или schema-validated artifacts в нейтральном runtime-каталоге.
- Не меняется: внешние HTTP-маршруты, business rules, роли и production delivery.

# Acceptance Criteria

- [x] Активный код, env, cron и каноничная документация не используют TopControl;
  исторические упоминания изолированы как legacy.
- [x] `tasks.import_topcontrol_products_db` один релиз вызывает
  `tasks.sync_onec_product_catalog`, после успешного scheduled-run alias удаляется.
- [ ] Каталог из 1С обновляется без ухудшения row count и freshness.
- [ ] Postgres и 1С engines создаются только разрешенными factories; CI запрещает
  прямой `create_engine` вне allowlist.
- [x] Write-use-cases подтверждают commit/rollback/idempotency тестами.
- [ ] `infra/cron` не содержит SQL и бизнес-правил для мигрированных jobs.
- [x] Weekly KPI загружается в `pricing-service` через authenticated idempotent API,
  а не через прямую запись из `mm-compensation` в его БД.
- [x] Executive snapshots публикуются атомарно по versioned JSON Schema и читаются
  из нейтрального runtime-каталога.
- [ ] OpenAPI, manifests, specs, architecture checks и regression tests проходят.
- [x] Release builder создаёт неизменяемый каталог со своим `.venv`, hash-locked
  dependencies и manifest hashes.
- [x] Forced smoke failure атомарно возвращает прежние backend и UI через один symlink.
- [ ] Production release переключён с проверенным rollback и без
  удаления активного/rollback targets.

# Source of Truth

- `1С УТ 10.3 / Ekama` — торговый факт, каталог, документы, остатки и продажи.
- PostgreSQL `pricing-service` — derived state pricing/matching/procurement,
  receivables, management read models и durable orchestration runs/delivery intents.
- `mm-compensation` — KPI/HR/payroll/finance calculations до публикации контракта.
- Корневой `/opt/MM` — runtime registry, systemd schedules и thin runners; durable
  orchestration state читается и меняется только через internal API.
- `Bitrix24` и `Telegram` — рабочие поверхности и каналы доставки, не аналитическая БД.

# Data Flow

```text
1C read-only -> 1C adapter -> application service -> pricing Postgres
external HTTP -> API -> application service -> Unit of Work -> domain/repositories
mm-compensation KPI -> internal authenticated API -> pricing publication tables
mm-compensation snapshots -> atomic shared contract -> pricing management reader
pricing read models -> root delivery adapter -> Bitrix24/Telegram
root systemd timer -> registered runner -> orchestration API -> project command
project command -> delivery intent -> Bitrix24/Telegram -> delivery attempt result
```

# API / Data Contracts

- Внешние API остаются совместимыми.
- Новый внутренний endpoint:
  `POST /api/management/internal/weekly-kpi-snapshots`.
- Endpoint использует bearer service token, обязательный `Idempotency-Key` и batch
  существующего weekly KPI contract; ответ содержит счетчики
  `inserted/updated/noop/quarantined`.
- Durable orchestration API доступен по
  `/api/management/internal/orchestration`; mutating requests требуют отдельный bearer
  token и `Idempotency-Key`. Повторный claim не разрешает повторное исполнение/отправку,
  а истёкшая отправка переходит в `unknown` для ручной сверки.
- Shared executive artifacts публикуются в `/var/lib/mm-data-contracts/` и имеют
  JSON Schema плюс manifest: `contract_version`, `generated_at`, `source_project`,
  `schema`, `schema_sha256`, `content_sha256` и `artifact`.
- OpenAPI `pricing-service` продолжает генерироваться из FastAPI и проверяться через
  `scripts/export_openapi.py --check`.

# Invariants

- Проект не читает `.env` соседнего проекта и не пишет в чужую БД.
- API не выполняет SQL для мигрированных use-cases.
- Domain/application code не импортирует FastAPI и возвращает доменные ошибки.
- Side effects остаются dry-run, если они не были production-enabled до refactor.
- Первая волна миграций additive; удаление данных и колонок запрещено.
- Активный release symlink и rollback target не участвуют в cleanup.
- Scheduler не включает timer для job, пока runtime registry не переведён из
  `cron_active_timer_disabled` в разрешённое переходное/целевое состояние.
- Delivery со статусом `unknown` не повторяется автоматически.

# Errors / Edge Cases

- Недоступна 1С: importer завершает job ошибкой, не публикует частичный результат и
  сохраняет предыдущий успешный snapshot.
- Повтор weekly KPI payload: тот же idempotency key и hash возвращает `noop`; другой
  payload с тем же ключом возвращает conflict.
- Snapshot отсутствует/устарел/не проходит schema/hash: dashboard возвращает
  `source_missing`, `stale` или `source_error`, а не подменяет данные нулями.
- Ошибка после первой DB-операции: Unit of Work выполняет rollback.
- Старый cron вызывает deprecated module: wrapper делегирует новой команде и пишет
  warning, не меняя exit code.
- Cleanup обнаружил runtime/reference path: каталог пропускается и попадает в отчет.

# Implementation Checklist

- [x] Зафиксировать live baseline, active/rollback release и smoke URLs.
- [x] Исправить текущие docs quality ошибки и зарегистрировать этот spec.
- [x] Переименовать 1С catalog CLI, обновить cron/imports/tests и добавить alias.
- [x] Удалить активные TopControl settings и обновить каноничную документацию.
- [x] Добавить DB factories, Unit of Work и architecture tests.
- [x] Перевести weekly KPI publication с прямого DB URL на internal API.
- [x] Добавить shared snapshot schemas/manifest и нейтральные runtime paths.
- [x] Устранить дубли receivables/counterparty recommendations.
- [x] Добавить доменный skeleton и dependency rules без big-bang переносов.
- [x] Усилить management-job/retention validators и MasterMobile OpenAPI parity.
- [x] Добавить durable orchestration models/API, idempotency и expired-lease guard.
- [x] Добавить release-specific venv, dependency hashes и единый UI/backend rollback.
- [ ] Прогнать regression, собрать immutable release, smoke и rollback.
- [ ] После контрольного цикла применить safe retention и удалить deprecated alias
  в следующем релизе.

# Review Notes / Risks

- В рабочем tree есть незакоммиченный dashboard-контур; refactor обязан сохранять его
  изменения и избегать механического переписывания соответствующих файлов.
- Live-сервис запускается через `/opt/MM/pricing-service-task43-current`; source
  checkout не равен runtime truth до явного release switch.
- Во время hardening-работ 2026-07-12 другой процесс переключил production на
  `sales-dashboard-volume-bars-20260712-173002`; архитектурный rollout поэтому
  не должен менять symlink, пока параллельный dashboard-релиз не зафиксирован.
- Direct 1C SQL и application Postgres — разные engines и разные access policies.
- Автоматический commit на каждый HTTP-запрос запрещен: транзакция соответствует
  application command, а не transport request вообще.

# Tests

- Unit: TopControl alias, DB factories, Unit of Work, domain errors, idempotency.
- Integration: Postgres transaction rollback, read-only 1C adapter, weekly KPI ingest,
  shared artifact schema/hash/freshness.
- Architecture: forbidden imports, direct engines, sibling env/DB/build paths,
  TopControl outside legacy.
- Regression: full pytest, UI tests, OpenAPI check, Alembic check, docs quality.
- Smoke: `/health`, matching, receivables, executive dashboard, procurement,
  1C catalog dry-run/sync and weekly KPI dry-run without external delivery.

# Rollout

1. Собрать immutable release из проверенного source tree.
2. Запустить offline validations и локальный smoke на отдельном порту.
3. Атомарно переключить `pricing-service-task43-current` и перезапустить service.
4. Проверить health/OpenAPI/UI/API без Bitrix/Telegram side effects и только
   после успешного smoke создать immutable marker `.release-verified`.
5. Наблюдать один ночной catalog sync и один management daily cycle.
6. При ошибке вернуть symlink на предыдущий verified release; additive migrations
   допускают запуск старого кода.
7. После успешного цикла оставить active + 3 verified releases и архивировать/удалить
   остальные только через safe retention report.

# Changelog

- 2026-07-20 — successful release switch создает `.release-verified`, чтобы
  ежедневный safe retention мог сохранять active + 3 rollback и удалять только
  ранее проверенные лишние релизы.
- 2026-07-12 — accepted architecture-hardening plan created from live baseline.
- 2026-07-16 — durable orchestration and atomic release hardening implemented in repository; production cutover remains gated.
