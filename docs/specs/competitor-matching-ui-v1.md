---
spec_id: "pricing-competitor-matching-ui-v1"
title: "Competitor Matching UI V1"
doc_type: spec
domain: matching
status: accepted
owner: engineering
source_of_truth: true
related_code: [app/api/matching.py, app/schemas/matching.py, ui/]
related_tests: [tests/test_matching_api.py, tests/test_embedding_matching.py]
contracts: [openapi.yaml]
depends_on: [docs/TechDesign.CompetitorMatchingUI.md, docs/competitor_matching.md]
supersedes: []
rollout_required: true
updated_at: "2026-05-01"
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
- полноэкранная UI-форма подбора.

Не входит:
- bulk-принятие автопар;
- CSV/XLSX export;
- удаление legacy `ProductMatch` / `CompetitorPrice`.

# Source of Truth

- `Product` — наш каталог из 1С/TopControl.
- `CompetitorItem` — каноничный каталог позиций конкурентов для ручного UI.
- `CompetitorItemMatch` — текущее item-level сопоставление одного товара конкурента с одним
  нашим товаром.
- `ProductCompetitorItemDecision` — append-only журнал действий оператора.

# Data Flow

1. Embedding/LLM pipeline пишет `CompetitorItemMatch` со статусами `suggested`,
   `needs_review`, `ambiguous`, `rejected`.
2. UI открывает список наших товаров и считает статус по item-level связям.
3. Форма подбора ищет `CompetitorItem` и обогащает строки текущим статусом/score.
4. Оператор принимает, отклоняет или снимает конкретный `competitor_item_id`.
5. Backend обновляет `CompetitorItemMatch` и пишет событие в
   `ProductCompetitorItemDecision`.

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
- `POST /api/matching/products/{product_id}/matches` — принять `competitor_item_id`.
- `POST /api/matching/products/{product_id}/reject` — отклонить `competitor_item_id`.
- `POST /api/matching/products/{product_id}/revoke` — снять accepted/manual связь.
- `GET /api/matching/products/{product_id}/history` — история решений.

Legacy endpoints `/api/matching/products/{product_id}` и
`DELETE /api/matching/products/{product_id}/{competitor_id}` сохраняются как совместимость
для старого UI-каркаса, но новый UI их не использует.

# Invariants

- Один `CompetitorItem` может иметь только одну текущую связь `CompetitorItemMatch`.
- `accepted/manual` нельзя перетереть новым ручным accept на другой product без явного
  снятия текущей связи.
- Уверенный `suggested` автоматически считается принятой автосвязью, если у товара по
  этому конкуренту нет дублей-кандидатов; дубль у одного конкурента остаётся для ручного
  выбора.
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

# Rollout

1. Применить миграцию с журналом решений и поисковыми индексами.
2. Выпустить backend API с legacy-совместимостью.
3. Включить новый UI.
4. При rollback оставить журнал решений в БД; legacy расчётный контур не зависит от него.

# Changelog

- 2026-05-01 — accepted draft created.
