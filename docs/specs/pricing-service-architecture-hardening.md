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
  - tasks/sync_onec_product_catalog.py
  - scripts/export_openapi.py
  - scripts/build_pricing_service_release.sh
  - scripts/switch_pricing_service_release.sh
  - scripts/validate_release_api_compatibility.py
  - config/production_required_routes.json
related_tests:
  - tests/test_import_onec_products.py
  - tests/test_orchestration_api.py
  - tests/test_product_compatibility.py
  - tests/test_release_scripts.py
  - tests/test_release_api_compatibility.py
  - tests/test_retail_counterparty_balances.py
  - tests/test_weekly_kpi_reports_api.py
contracts:
  - openapi.yaml
depends_on:
  - docs/architecture.md
  - docs/specs/README.md
supersedes: []
rollout_required: true
updated_at: "2026-08-26"
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
- durable run/delivery state и authenticated orchestration API для root control plane;
- release-specific virtualenv, hash-locked dependencies и единый rollback backend/UI;
- strict provenance от фактически активного production source commit, проверка
  API-совместимости, race-safe switch и JSONL-аудит переключений;
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
- Было: release можно было собрать без зафиксированной production-базы и постоянного
  mutable-root, а switch допускал пустой expected-active и legacy manifest.
- Станет: builder подтверждает ancestry от source commit активного релиза, switch
  требует точного expected-active и совпадения `required_base_commit`, запрещает
  legacy hash и удаление production API operations.
- Решение от 2026-08-15: до продолжения DB/CLI/cron hardening привести
  каноническую Git-историю в соответствие с активным production source.
  Объединение выполнять только в отдельном clean worktree, сохраняя грязный
  mutable `main` без изменений; публикация ветки и PR сама по себе не является
  production cutover.
- Уточнение от 2026-08-15: canonical `main` обязан иметь фактически активный
  production source commit в своей ancestry. Patch-equivalent cherry-pick не
  заменяет этот provenance gate; для уже перенесённого содержимого допустим
  tree-preserving merge с активной production-цепочкой.
- Решение от 2026-08-15: workspace control-plane предоставляет штатный `build`,
  который валидирует clean source, собирает и повторно проверяет immutable
  candidate, но не запускает migration, switch или smoke активного сервиса.
  Версионированный source контроллера находится в
  `/opt/MM/scripts/pricing_release/pricing_service_release_controller.py`;
  установленный entrypoint остаётся `/usr/local/sbin/mm-pricing-service-release`.
- Решение от 2026-08-26: непрерывная параллельная разработка считается штатной
  и не требует ожидания общего простоя. Convergence выполняется в коротком окне,
  блокирующем только production `switch`/`deploy`; разработка и CI продолжаются.
  В начале окна фиксируется текущий `active_source_commit`. Итоговый commit обязан
  содержать его в ancestry и попасть в `origin/main` до сборки production candidate.
  Если active source изменился, convergence повторяется от нового commit.
- После convergence штатные production releases разрешены только из commit,
  входящего в ancestry канонического `origin/main`. Действующая проверка
  наследования полного active source сохраняется. Версионированный source
  контроллера и тесты должны быть восстановлены до изменения установленного
  entrypoint.
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
- [x] Release builder создаёт неизменяемый каталог со своим `.venv`, hash-locked
  dependencies и manifest hashes; `content_sha256` использует версионированную
  схему `sha256-files-v2-no-python-cache`, не зависящую от runtime `.pyc`.
- [x] Release builder требует `PRICING_SERVICE_RELEASE_REQUIRED_BASE_REF` и
  `PRICING_SERVICE_MUTABLE_ROOT`, проверяет ancestry через `git merge-base` и
  записывает подтверждённые `required_base_commit`, `mutable_root`,
  `source_verified=true`, `source_dirty=false`.
- [x] Release switch требует `PRICING_SERVICE_EXPECTED_ACTIVE_RELEASE` и
  `PRICING_SERVICE_MUTABLE_ROOT`, принимает только
  `sha256-files-v2-no-python-cache`, сверяет production lineage и блокирует stale
  candidate до изменения active symlink.
- [x] Switch проверяет актуальность OpenAPI, запрещает удаление operations активного
  релиза без явного policy-исключения и пишет JSONL events `rejected`, `attempt`,
  `rolled_back`, `verified` без секретов.
- [x] Обратный диапазон `period_start > as_of` отклоняется в service layer и
  management API с HTTP 422 до открытия соединения с 1С.
- [x] Forced smoke failure атомарно возвращает прежние backend и UI через один symlink.
- [x] Production release переключён с проверенным rollback и без
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
- `GET /api/management/retail-counterparty-zero-balances` возвращает HTTP 422, если
  `period_start` позже `as_of`.

# Invariants

- Проект не читает `.env` соседнего проекта и не пишет в чужую БД.
- API не выполняет SQL для мигрированных use-cases.
- Domain/application code не импортирует FastAPI и возвращает доменные ошибки.
- Side effects остаются dry-run, если они не были production-enabled до refactor.
- Первая волна миграций additive; удаление данных и колонок запрещено.
- Активный release symlink и rollback target не участвуют в cleanup.
- Каждый release-кандидат основан на source commit текущего active manifest;
  параллельное изменение active symlink блокирует switch как до, так и после preflight.
- Mutable state указывает на постоянный абсолютный root вне source worktree и release root.
- Forward switch не принимает legacy manifest и не удаляет production API operations
  без отдельного изменения policy-файла, прошедшего review.
- Release-builder принимает только clean Git tree, фиксирует commit, Alembic head и
  content hash, исключает Python/test caches и делает весь release read-only;
  writable state остаётся только во внешних symlink-каталогах.
- Индексы `embeddings` относятся к persistent mutable state: builder не копирует их
  в release, а подключает через canonical mutable-root так же, как `build` и `data`.
- Scheduler не включает timer для job, пока runtime registry не переведён из
  `cron_active_timer_disabled` в разрешённое переходное/целевое состояние.
- Delivery со статусом `unknown` не повторяется автоматически.
- Страницы встроенных Bitrix24-приложений и logistics fallback отдают frontend из
  `ui/dist` активного релиза; `/var/www/pricing-service` допустим только как
  запасной путь и выведен из обращения.

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
- Production source commit не является предком кандидата, active release сменился,
  manifest не подтверждён или mutable-root временный: build/switch завершается ошибкой.
- После переключения smoke не прошёл: rollback выполняется только если active link всё
  ещё указывает на этот candidate; результат фиксируется в JSONL audit.

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
- [x] Добавить durable orchestration models/API, idempotency и expired-lease guard.
- [x] Добавить release-specific venv, dependency hashes и единый UI/backend rollback.
- [x] Исключить Python cache из release digest и проверять v2 digest перед switch.
- [x] Добавить strict production provenance, обязательный persistent mutable-root,
  expected-active guard, OpenAPI route compatibility и switch audit.
- [x] Пересоздавать read-only `.release-verified` безопасно и отклонять обратный
  диапазон дат retail zero-balance endpoint.
- [x] Прогнать regression, собрать immutable release, smoke и rollback.
- [x] Удалить deprecated catalog alias после успешного контрольного scheduled-run.
- [ ] После следующего критического цикла strict-релиза применить safe retention.

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
- Release integrity: стабильность `content_sha256` после появления runtime `.pyc` и
  marker, отказ при stale/dirty/unverified candidate, неверной базе, mutable-root,
  legacy hash, смене active release и изменении содержимого кандидата.
- API compatibility: удалённый operation блокируется; явно разрешённое policy-исключение
  допускается; обязательный retail zero-balance route нельзя удалить.
- Management API: корректный диапазон дат проходит, обратный возвращает 422 до 1С.
- Smoke: `/health`, matching, receivables, executive dashboard, procurement,
  1C catalog dry-run/sync and weekly KPI dry-run without external delivery.

# Rollout

0. Deploy-freeze блокирует только `switch` и `deploy`, имеет явную причину и срок
   действия; `status`, `check`, разработка и CI остаются доступными. Перед switch
   controller повторно проверяет freeze, active source и принадлежность candidate
   к `origin/main`.
1. Приоритет №1 перед дальнейшим DB/CLI/cron hardening — объединить canonical
   `main` с проверенной цепочкой активного production source в отдельной
   интеграционной ветке и провести её через PR. До merge и отдельного cutover
   production не меняется, грязный mutable checkout остаётся без изменений.
   Фактически активный source commit должен быть предком merge-результата;
   совпадения patch-id после cherry-pick недостаточно.
   Production-only display-family цепочка `30d7d1f`, `5f25201`, `a643f62`,
   `49b66ac`, `cdb5da6` переносится в canonical `main` только самостоятельным
   PR после отдельного архитектурного и регрессионного аудита. Механическое
   слияние без проверки запрещено. Merge PR и production cutover требуют
   отдельных решений пользователя.
2. Создать отдельный clean worktree, внести и закоммитить изменения. Нельзя
   использовать грязный параллельный checkout как source или mutable-root.
3. Для подготовки immutable-кандидата без cutover использовать только штатный
   build-only режим workspace control-plane:

   ```bash
   sudo /usr/local/sbin/mm-pricing-service-release build \
     --source-root /opt/MM/.worktrees/<clean-worktree> \
     --release-name <release-name>
   ```

   Контроллер сам читает production-базу из фактически активного manifest и
   передаёт builder обязательные `PRICING_SERVICE_RELEASE_REQUIRED_BASE_REF` и
   `PRICING_SERVICE_MUTABLE_ROOT`, повторно валидирует candidate и возвращает
   `switched=false`.
4. Для уже собранного immutable-кандидата выполнить provenance preflight и только
   после отдельного подтверждения — guarded switch:

   ```bash
   sudo /usr/local/sbin/mm-pricing-service-release check \
     /opt/MM/releases/pricing-service/<release-name>
   sudo /usr/local/sbin/mm-pricing-service-release switch \
     /opt/MM/releases/pricing-service/<release-name>
   ```

   В production нельзя напрямую вызывать low-level builder/switch из checkout,
   worktree или release. Controller закрепляет canonical paths, повторно сверяет
   active и передаёт switch обязательный `PRICING_SERVICE_EXPECTED_ACTIVE_RELEASE`.
5. Штатный `deploy` оставлен только для отдельно подтверждённого единого цикла
   build + cutover; он не используется для подготовки кандидата:

   ```bash
   sudo /usr/local/sbin/mm-pricing-service-release deploy \
     --source-root /opt/MM/.worktrees/<clean-worktree> \
     --release-name <release-name>
   ```

6. Controller до смены active-ссылки выполняет миграции кандидата через
   `alembic upgrade head`, затем требует точного совпадения database/code head.
   Любая ошибка миграции или отдельного validator останавливает cutover.
7. Проверить health/OpenAPI/UI/API без Bitrix/Telegram side effects; marker
   `.release-verified` создаётся switch-скриптом только после успешного smoke.
8. Наблюдать один ночной catalog sync и один management daily cycle. Известный
   dashboard status `owner cash transfer control has a high unresolved issue`
   сравнивать с дорелизным baseline и не считать новой технической регрессией.
9. При ошибке guarded rollback возвращает symlink на предыдущий verified release;
   additive migrations
   допускают запуск старого кода.
10. Retention не выполнять до следующего критического планового цикла; после него
   оставить active + 3 verified releases и удалять остальные только через safe
   retention report.

# Changelog

- 2026-08-26 — утверждена постоянная convergence-схема с коротким deploy-freeze,
  main-only releases и сохранением active-source ancestry guard.
- 2026-08-19 — frontend встроенных Bitrix24-приложений закреплён за сборкой
  активного релиза: порядок `_INDEX_PATHS` исправлен в `bitrix_matching` и
  `logistics_web`, легаси-каталог `/var/www/pricing-service` выведен из
  обращения после месяца подмены свежего build билдом от 2026-07-16.
- 2026-08-17 — для production-only display-family цепочки утверждены отдельный
  архитектурный и регрессионный аудит и самостоятельный PR; механическое
  слияние запрещено.
- 2026-08-15 — утверждён штатный build-only режим для подготовки immutable
  release candidate без production cutover; provenance требует ancestry от
  фактически активного source commit, а не только patch-equivalence.
- 2026-08-15 — Git/production reconciliation выбран архитектурным приоритетом №1.
- 2026-07-22 — switch стал выполнять Alembic migration до cutover, повторно
  проверять database/code head и fail-closed обрабатывать каждый release-validator.
- 2026-07-22 — после консолидации `main` две независимые additive Alembic-ветки
  объединены пустой merge-revision; release-builder снова требует единственную head
  перед production cutover.
- 2026-07-21 — production release переведён на workspace control-plane с
  pinned strict-switch; direct low-level cutover объявлен только test/break-glass
  интерфейсом.
- 2026-07-21 — strict release provenance, mandatory persistent mutable-root and
  expected-active, route compatibility, JSONL switch audit, safe marker refresh и
  HTTP 422 для обратного retail balance period.
- 2026-07-20 — successful release switch создает `.release-verified`, чтобы
  ежедневный safe retention мог сохранять active + 3 rollback и удалять только
  ранее проверенные лишние релизы.
- 2026-07-12 — accepted architecture-hardening plan created from live baseline.
- 2026-07-16 — durable orchestration and atomic release hardening implemented in repository; production cutover remains gated.
- 2026-07-20 — release digest made stable against Python caches and enforced before atomic switch.
- 2026-07-13 — новый catalog CLI прошёл scheduled-run, compatibility alias удалён.
- 2026-07-14 — каталог `28 717 / 28 717`, missing `0`, outside `0`; management
  snapshot `version=17` создан с уникальным content hash и одним audit `generated`.
- 2026-07-14 — clean release `pricing-clean-ui-consolidation-20260714-132116`
  переключён в production; сохранены три проверенных rollback, остальные release
  catalogs удалены после dry-run retention.
- 2026-07-14 — canonical clean release `pricing-main-canonical-20260714-143050`
  собран из merged `main` (`3915d47`) и переключён после API/UI smoke.
