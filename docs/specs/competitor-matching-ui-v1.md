---
spec_id: "pricing-competitor-matching-ui-v1"
title: "Competitor Matching UI V1"
doc_type: spec
domain: matching
status: accepted
owner: engineering
source_of_truth: true
related_code: [app/api/matching.py, app/schemas/matching.py, ui/]
related_tests: [tests/test_matching_api.py, tests/test_embedding_matching.py, tests/test_manual_matching_decisions.py, tests/test_competitor_auto_accept_policy.py, tests/test_competitor_matching_replay.py]
contracts: [openapi.yaml]
depends_on: [docs/competitor_matching.md, docs/TechDesign.CompetitorFTPImport.md]
supersedes: [docs/TechDesign.CompetitorMatchingUI.md]
rollout_required: true
updated_at: "2026-07-31"
---

# Назначение

Сделать удобный ручной контур сопоставления наших товаров из 1С с конкретными
товарами конкурентов. Главный сценарий: оператор выбирает наш товар, открывает
полноэкранную форму подбора, ищет по содержанию, фильтрует кандидатов и принимает,
отклоняет или снимает сопоставление.

# Scope / Out of Scope

Входит:
- item-level сопоставление через `CompetitorItem` / `CompetitorItemMatch`;
- поиск кандидатов по названию, SKU, конкуренту, нормализованному названию и атрибутам;
- фильтры по конкуренту, наличию, группе категории, типу товара, бренду, модели, качеству,
  цвету и цене;
- журнал решений accept/reject/revoke;
- структурированные причины и воспроизводимый серверный snapshot каждого решения;
- версионируемая категорийная auto-accept policy и offline replay;
- полноэкранная UI-форма подбора.

Не входит:
- bulk-принятие автопар;
- CSV/XLSX export;
- удаление legacy `ProductMatch` / `CompetitorPrice`.

# Source of Truth

- `Product` — наш каталог из 1С.
- `CompetitorItem` — каноничный каталог позиций конкурентов для ручного UI.
- `CompetitorItemMatch` — текущее item-level сопоставление одного товара конкурента с одним
  нашим товаром.
- `ProductCompetitorItemDecision` — append-only журнал действий оператора.

# Data Flow

1. Embedding pipeline пишет `CompetitorItemMatch` со статусами `suggested`,
   `needs_review`, `ambiguous`, `rejected` и может создать `accepted` только через
   именованное категорийное правило, допущенное текущей `auto_accept_policy`.
2. UI открывает список наших товаров и считает статус по item-level связям.
3. Форма подбора ищет `CompetitorItem` и обогащает строки текущим статусом/score.
4. Оператор принимает, отклоняет или снимает конкретный `competitor_item_id`.
5. Backend обновляет `CompetitorItemMatch` и пишет событие в
   `ProductCompetitorItemDecision` вместе с `reason_code` и `snapshot_json`.

# Два независимых контура

1. Legacy SKU→цена: HTTPS XLSX → совместимые таблицы `competitor_ftp_*` →
   `CompetitorPrice` / `ProductMatch`. Он отвечает за цены и точное SKU-сопоставление.
2. Item-level: те же импортированные строки формируют `CompetitorItem`, после чего
   parser/compatibility/embeddings создают кандидатов `CompetitorItemMatch` для ручного UI.

`CompetitorItem` не удалён и остаётся каноничным источником ручного UI. Фактический
сетевой транспорт production — HTTPS; слово `ftp` в именах таблиц и freshness-полях
сохранено только как совместимый schema contract.

# API / Data Contracts

- `GET /api/matching/products` — список наших товаров с серверной пагинацией, поиском,
  статусом, фильтрами по категории, предмету и бренду совместимости устройства,
  facets для продуктовых фильтров, отдельным фильтром `matched` для успешно
  сопоставленных товаров, превью лучших сохранённых кандидатов, отдельным
  `live_candidate_count` для потенциальных кандидатов из живого поиска и корректным `total`.
- `GET /api/matching/products/{product_id}/candidate-search` — поиск позиций конкурентов
  с фильтрами, включая состояние кандидата, и facets. Если оператор явно не выбрал
  тип товара, поиск ограничивается типом, выведенным из нашего товара: например,
  для дисплея показываются только дисплеи конкурентов, а не аккумуляторы или микросхемы.
  Для дисплеев дополнительно требуется признак дисплея в содержании строки конкурента
  (`дисплей`, `LCD`, `OLED`, `экран`, `тачскрин` и аналоги), чтобы ошибочно размеченные
  сервисные модули не попадали в подбор.
- `POST /api/matching/products/{product_id}/matches` — принять `competitor_item_id`;
  `reason_code` необязателен для старых клиентов, новый UI отправляет
  `confirmed_attributes`.
- `POST /api/matching/products/{product_id}/reject` — отклонить `competitor_item_id`;
  новый UI требует структурированную причину.
- `POST /api/matching/products/{product_id}/revoke` — снять accepted/manual связь;
  новый UI требует структурированную причину.
- `GET /api/matching/products/{product_id}/history` — история с `reason_code` и краткими
  полями snapshot; полный `snapshot_json` наружу не возвращается.

Legacy endpoints `/api/matching/products/{product_id}` и
`DELETE /api/matching/products/{product_id}/{competitor_id}` сохраняются как совместимость
для старого UI-каркаса, но новый UI их не использует.

# Invariants

- Один `CompetitorItem` может иметь только одну текущую связь `CompetitorItemMatch`.
- `accepted/manual` нельзя перетереть новым ручным accept на другой product без явного
  снятия текущей связи.
- Общий unique-auto-accept запрещён. Автопринятие возможно только через именованное
  правило категории/конкурента с версией policy в `rationale_json`.
- Запрошенный режим `auto` фактически остаётся `shadow`, пока в категории нет минимум
  50 новых snapshot-примеров и precision ≥95%; для точных кодов/партномеров — ≥99,5%.
- Конфликт типа/семейства, модели/варианта, manual-связи, партномера/ёмкости,
  конструкции дисплея или устаревшие источники всегда блокирует downstream и auto-accept.
- `battery` требует совпадения предмета, модели, партномера/ёмкости и Premium tier;
  `display` — модели, конструкции, качества, цвета и рамки; `flex` — модели и назначения.
- Rejected-кандидаты скрываются из обычной выдачи только для конкретного нашего товара.
- Legacy `ProductMatch` / `CompetitorPrice` остаются для старого FTP/ценового контура.

# Errors / Edge Cases

- Accept item, который уже accepted/manual к другому товару, возвращает `409`.
- Reject accepted/manual связи возвращает `409`: сначала нужно снять связь.
- Revoke несуществующей связи возвращает `404`.
- Поиск без `q` возвращает релевантные suggested/review кандидаты для выбранного товара
  и свежие позиции конкурентов.

# Tests

- Backend unit/API: список товаров, candidate search, accept/reject/revoke/history,
  конфликт accepted/manual.
- Frontend: build, lint, smoke-тесты формы подбора.
- Regression: `tests/test_matching.py`, `tests/test_embedding_matching.py`,
  `tests/test_competitor_matching.py`, OpenAPI export.
- Decision data: миграция/backfill, API-совместимость, обязательность причин в UI,
  полнота snapshot и безопасное history summary.
- Policy/replay: hard blockers, shadow→auto gate, временной split без утечки будущих
  решений, precision/coverage и отдельный порог точных кодов.

# Обратная связь и replay

- Стабильные причины: `wrong_model`, `wrong_item_type`, `wrong_quality`, `wrong_color`,
  `wrong_frame`, `wrong_part_number`, `wrong_capacity`, `duplicate_or_irrelevant`,
  `confirmed_exact_code`, `confirmed_attributes`, `auto_false_positive`, `other`.
- Старые решения и запросы без кода получают `legacy_unspecified`.
- Snapshot schema v1 содержит top-K и выбранный rank, score/gap, признаки обеих позиций,
  rationale, версии embeddings/parser/guardrails и даты источника.
- `tasks.evaluate_competitor_matching_policy` строит JSON-артефакт NumPy reranker с
  версией признаков, коэффициентами и порогами. Split только хронологический 80/20;
  старые решения без snapshot учитываются как rule-discovery/hard negatives, но не как
  полноценная оценочная выборка.
- Тот же отчёт детерминированно формирует 10% audit sample текущих auto-accepted;
  категория получает рекомендацию отключения при error rate >5% или любом
  систематическом конфликте модели/типа.
- Ручная очередь ранжируется по бизнес-ценности, неопределённости и дефициту примеров;
  одинаковые товарные семейства выводятся рядом.

# Rollout

1. Recovery HTTPS/nightly и fail-closed freshness gate.
2. Миграция `reason_code`/`snapshot_json`, backend API и UI-причины.
3. Shadow-policy, replay и отчёты ручной очереди.
4. Категорийный auto-accept только после выполнения validation gate.

Каждый этап выпускается отдельным clean release через штатный release-контроллер.
При rollback additive-колонки журнала остаются в БД; legacy ценовой контур от них не зависит.

# Changelog

- 2026-05-01 — accepted draft created.
- 2026-07-31 — документ сделан каноническим для item-level контура; зафиксированы
  HTTPS transport, decision snapshots, category policy, replay и staged rollout.
