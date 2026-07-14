---
spec_id: "pricing-service-architecture-hardening"
title: "Pricing Service Architecture Hardening"
doc_type: spec
domain: "architecture"
status: "implemented"
owner: "engineering"
source_of_truth: true
related_code:
  - app/api/dependencies.py
  - app/core/config.py
  - app/main.py
  - infra/cron/competitor_matching_nightly.sh
  - tasks/sync_onec_product_catalog.py
  - scripts/export_openapi.py
  - scripts/build_pricing_service_release.sh
  - scripts/switch_pricing_service_release.sh
related_tests:
  - tests/test_import_onec_products.py
  - tests/test_product_compatibility.py
  - tests/test_weekly_kpi_reports_api.py
contracts:
  - openapi.yaml
depends_on:
  - docs/architecture.md
  - docs/specs/README.md
supersedes: []
rollout_required: true
updated_at: "2026-07-14"
---

# Назначение

Сделать `pricing-service` управляемым модульным монолитом без изменения
бизнес-формул, внешних HTTP-маршрутов и действующих Bitrix24/Telegram-процессов.
Spec фиксирует вывод устаревшего источника из активного контура, единый слой подключений к БД,
границы между API/application/domain/infrastructure, тонкие cron/CLI entrypoints и
контрактный обмен с соседними проектами.

# Scope / Out of Scope

Входит:

- прямой read-only импорт каталога и свойств из `1С УТ 10.3 / Ekama`;
- удаление compatibility alias старой команды после успешного scheduled-run;
- центральные фабрики Postgres и MSSQL/1С, session factory и Unit of Work;
- постепенное устранение бизнес-логики из `infra/cron` и одноразовых `tasks`;
- доменные границы catalog, matching, pricing, assortment, procurement,
  receivables, management, expertise, logistics и telephony;
- API-ingest недельных KPI и версионированные shared snapshot contracts;
- architecture, OpenAPI, docs, migration и runtime quality gates;
- release retention: активный релиз плюс три последних проверенных.

Не входит:

- физический перенос delivery/STT/telephony между репозиториями;
- изменение формул pricing, procurement, receivables, KPI или dashboard;
- новые Bitrix24/Telegram side effects;
- destructive database migrations;
- изменение OFFONIKA-контуров.

# Change Summary / Spec Delta

- Было: устаревший источник оставался в активных именах и документации, хотя importer уже
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

- [x] Активный код, env, cron и каноничная документация не используют устаревший источник;
  исторические упоминания изолированы как legacy.
- [x] Compatibility alias удалён после успешного scheduled-run
  `tasks.sync_onec_product_catalog`.
- [x] Каталог из 1С обновляется без ухудшения row count и freshness.
- [x] Postgres и 1С engines создаются только разрешенными factories; CI запрещает
  прямой `create_engine` вне allowlist.
- [x] Write-use-cases подтверждают commit/rollback/idempotency тестами.
- [x] `infra/cron` не содержит SQL и бизнес-правил для мигрированных jobs.
- [x] Weekly KPI загружается в `pricing-service` через authenticated idempotent API,
  а не через прямую запись из `mm-compensation` в его БД.
- [x] Executive snapshots публикуются атомарно по versioned JSON Schema и читаются
  из нейтрального runtime-каталога.
- [x] OpenAPI, manifests, specs, architecture checks и regression tests проходят.
- [x] Release выкладывается неизменяемым каталогом с проверенным rollback и без
  удаления активного/rollback targets.

# Source of Truth

- `1С УТ 10.3 / Ekama` — торговый факт, каталог, документы, остатки и продажи.
- PostgreSQL `pricing-service` — derived state pricing/matching/procurement,
  receivables и management read models.
- `mm-compensation` — KPI/HR/payroll/finance calculations до публикации контракта.
- Корневой `/opt/MM` — schedules, locks, delivery registry и runtime orchestration,
  но не бизнес-расчеты.
- `Bitrix24` и `Telegram` — рабочие поверхности и каналы доставки, не аналитическая БД.

# Data Flow

```text
1C read-only -> 1C adapter -> application service -> pricing Postgres
external HTTP -> API -> application service -> Unit of Work -> domain/repositories
mm-compensation KPI -> internal authenticated API -> pricing publication tables
mm-compensation snapshots -> atomic shared contract -> pricing management reader
pricing read models -> root delivery adapter -> Bitrix24/Telegram
```

# API / Data Contracts

- Внешние API остаются совместимыми.
- Новый внутренний endpoint:
  `POST /api/management/internal/weekly-kpi-snapshots`.
- Endpoint использует bearer service token, обязательный `Idempotency-Key` и batch
  существующего weekly KPI contract; ответ содержит счетчики
  `inserted/updated/noop/quarantined`.
- Shared executive artifacts публикуются в `/var/lib/mm-data-contracts/` и имеют
  JSON Schema плюс manifest: `contract_version`, `generated_at`, `source_project`,
  `content_sha256`.
- OpenAPI `pricing-service` продолжает генерироваться из FastAPI и проверяться через
  `scripts/export_openapi.py --check`.

# Invariants

- Проект не читает `.env` соседнего проекта и не пишет в чужую БД.
- API не выполняет SQL для мигрированных use-cases.
- Domain/application code не импортирует FastAPI и возвращает доменные ошибки.
- Side effects остаются dry-run, если они не были production-enabled до refactor.
- Первая волна миграций additive; удаление данных и колонок запрещено.
- Активный release symlink и rollback target не участвуют в cleanup.
- Release-builder принимает только clean Git tree, фиксирует commit, Alembic head,
  base release и content hash, исключает Python/test caches и делает весь release
  read-only; writable state остаётся только во внешних symlink-каталогах.
- Overlay разрешён только от release с `source_dirty=false`; любой UI-overlay
  полностью заменяет `ui/dist/assets`.

# Errors / Edge Cases

- Недоступна 1С: importer завершает job ошибкой, не публикует частичный результат и
  сохраняет предыдущий успешный snapshot.
- Повтор weekly KPI payload: тот же idempotency key и hash возвращает `noop`; другой
  payload с тем же ключом возвращает conflict.
- Snapshot отсутствует/устарел/не проходит schema/hash: dashboard возвращает
  `source_missing`, `stale` или `source_error`, а не подменяет данные нулями.
- Ошибка после первой DB-операции: Unit of Work выполняет rollback.
- Cron вызывает только актуальную команду каталога 1С.
- Cleanup обнаружил runtime/reference path: каталог пропускается и попадает в отчет.

# Implementation Checklist

- [x] Зафиксировать live baseline, active/rollback release и smoke URLs.
- [x] Исправить текущие docs quality ошибки и зарегистрировать этот spec.
- [x] Переименовать 1С catalog CLI, обновить cron/imports/tests и удалить alias
  после успешного scheduled-run.
- [x] Удалить настройки устаревшего источника и обновить каноничную документацию.
- [x] Добавить DB factories, Unit of Work и architecture tests.
- [x] Перевести weekly KPI publication с прямого DB URL на internal API.
- [x] Добавить shared snapshot schemas/manifest и нейтральные runtime paths.
- [x] Устранить дубли receivables/counterparty recommendations.
- [x] Добавить доменный skeleton и dependency rules без big-bang переносов.
- [x] Усилить management-job/retention validators и MasterMobile OpenAPI parity.
- [x] Прогнать regression, собрать immutable release, smoke и rollback.
- [x] После контрольного цикла применить safe retention.

# Review Notes / Risks

- Dashboard/UI-контур консолидирован вместе с архитектурными изменениями; canonical
  `main` зафиксирован merge commit `3915d47`, live release собран из полного clean
  source tree без overlay-цепочки.
- Live-сервис запускается через `/opt/MM/pricing-service-task43-current`, который
  указывает на `pricing-main-canonical-20260714-143050`.
- Финансовый `source_status=partial`, расхождение баланса и неполные источники
  остаются отдельной задачей качества данных и не меняют статус hardening-релиза.
- Direct 1C SQL и application Postgres — разные engines и разные access policies.
- Автоматический commit на каждый HTTP-запрос запрещен: транзакция соответствует
  application command, а не transport request вообще.

# Tests

- Unit: catalog importer, DB factories, Unit of Work, domain errors, idempotency.
- Integration: Postgres transaction rollback, read-only 1C adapter, weekly KPI ingest,
  shared artifact schema/hash/freshness.
- Architecture: forbidden imports, direct engines, sibling env/DB/build paths,
  устаревшее имя источника вне legacy/changelog.
- Regression: full pytest, UI tests, OpenAPI check, Alembic check, docs quality.
- Smoke: `/health`, matching, receivables, executive dashboard, procurement,
  1C catalog dry-run/sync and weekly KPI dry-run without external delivery.

# Rollout

1. Собрать immutable release из проверенного source tree.
2. Запустить offline validations и локальный smoke на отдельном порту.
3. Атомарно переключить `pricing-service-task43-current` и перезапустить service.
4. Проверить health/OpenAPI/UI/API без Bitrix/Telegram side effects.
5. Наблюдать один ночной catalog sync и один management daily cycle.
6. При ошибке вернуть symlink на предыдущий verified release; additive migrations
   допускают запуск старого кода.
7. После успешного цикла оставить active + 3 verified releases и архивировать/удалить
   остальные только через safe retention report.

# Changelog

- 2026-07-12 — accepted architecture-hardening plan created from live baseline.
- 2026-07-13 — новый catalog CLI прошёл scheduled-run, compatibility alias удалён.
- 2026-07-14 — каталог `28 717 / 28 717`, missing `0`, outside `0`; management
  snapshot `version=17` создан с уникальным content hash и одним audit `generated`.
- 2026-07-14 — clean release `pricing-clean-ui-consolidation-20260714-132116`
  переключён в production; сохранены три проверенных rollback, остальные release
  catalogs удалены после dry-run retention.
- 2026-07-14 — canonical clean release `pricing-main-canonical-20260714-143050`
  собран из merged `main` (`3915d47`) и переключён после API/UI smoke.
