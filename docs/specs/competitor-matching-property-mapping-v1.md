---
spec_id: "pricing-competitor-matching-property-mapping-v1"
title: "Competitor Matching Property Mapping V1"
doc_type: spec
domain: matching
status: accepted
owner: engineering
source_of_truth: false
related_code:
  - app/api/matching.py
  - app/models/matching_property_mapping.py
  - app/services/matching_attributes.py
  - app/services/matching_property_mapping.py
  - app/schemas/matching.py
  - ui/
related_tests:
  - tests/test_matching_api.py
  - tests/test_embedding_matching.py
contracts: [openapi.yaml]
depends_on:
  - docs/specs/competitor-matching-ui-v1.md
  - docs/specs/competitor-matching-bitrix-app-v1.md
  - docs/competitor_matching.md
supersedes: []
rollout_required: true
updated_at: "2026-07-31"
---

# Назначение

Это расширение канонического item-level контура
`docs/specs/competitor-matching-ui-v1.md`; при расхождении приоритет у него.

Добавить в Bitrix Matching слой настройки и просмотра мапинга свойств нашего
товара и товара конкурента. V1 помогает оператору быстрее понять, почему
кандидат подходит или требует ручной проверки: сравнение свойств видно прямо
в окне `Подобрать`, а правила управляются в разделе `Настройки свойств`.

# Scope / Out of Scope

Входит:
- профили правил для дисплеев, аккумуляторов, кабелей/зарядок, камер, шлейфов и
  корпусных деталей;
- настройка правил сравнения: поля нашего товара и конкурента, режим сравнения,
  строгость и порядок;
- словарь значений конкурентов в наш канон;
- API для чтения и изменения профилей, правил, словарей и проверки пары;
- краткая сводка свойств в выдаче кандидатов по флагу
  `include_property_summary=true`;
- вкладки `Сводка`, `Свойства`, `История` в карточке кандидата.

Не входит:
- изменение авто-матчера, guardrails и live cache;
- автоматическое принятие или пересмотр уже принятых manual-связей;
- массовые операции над правилами и импорт словарей из XLSX.

# Source of Truth

`pricing-service` является источником правды для правил, словарей, результатов
оценки пары и аудита изменений. Bitrix24 используется только как рабочее место
оператора и точка входа в приложение.

# Data Flow

1. `matching_attributes` извлекает нормализованные свойства из `Product` и
   `CompetitorItem`.
2. `matching_property_mapping` выбирает активный профиль, применяет правила и
   словари значений.
3. API возвращает полную таблицу сравнения пары или короткую сводку для строки
   кандидата.
4. UI показывает оператору статусы строк: `совпадает`, `не хватает значения`,
   `конфликт`, `правило не настроено`.
5. Изменения правил пишутся в таблицу аудита.

# API / Data Contracts

- `GET /api/matching/property-profiles` возвращает активные профили правил.
- `GET /api/matching/property-rules` читает правила по `profile_id` или
  `profile_code`.
- `POST /api/matching/property-rules` создаёт правило.
- `PATCH /api/matching/property-rules/{rule_id}` обновляет правило.
- `GET /api/matching/property-value-maps` читает словарь по `rule_id` или
  `profile_code`.
- `POST /api/matching/property-value-maps` создаёт значение словаря.
- `PATCH /api/matching/property-value-maps/{value_map_id}` обновляет значение
  словаря.
- `GET /api/matching/products/{product_id}/candidates/{competitor_item_id}/properties`
  возвращает профиль, сводку и строки сравнения пары.
- `GET /api/matching/products/{product_id}/candidate-search` принимает
  `include_property_summary=true`; без флага контракт выдачи остаётся лёгким.

# Invariants

- Manual accepted связи не изменяются оценкой свойств.
- Отсутствие правила не блокирует кандидата, а помечается статусом
  `правило не настроено`.
- Словарь значений применяется только к правилу, к которому он привязан.
- Сравнение свойств не должно менять `CompetitorItemMatch` и журнал решений.
- Первый полноценно покрытый профиль V1: `Дисплеи`; остальные профили получают
  базовый каркас для последующего расширения.

# Errors / Edge Cases

- Если товар или кандидат не найден, endpoint сравнения пары возвращает `404`.
- Если профиль не найден, endpoint сравнения пары возвращает `404`.
- Конфликт значения с severity `block` отображается как конфликт, но не меняет
  статус связи без отдельного решения оператора.
- Пустые значения обеих сторон считаются отсутствующими данными, а не совпадением.

# Tests

- Backend API: создание и чтение правил, словарей и сравнения пары.
- Mapping service: match, missing, conflict, unmapped, mapped value.
- Regression: `candidate-search` без `include_property_summary` работает как
  раньше.
- UI: build/lint, состояния loading/error/empty/success вкладки `Свойства`,
  создание и изменение правил, проверка пары в настройках.

# Rollout

1. Применить Alembic-миграцию с таблицами профилей, правил, словарей и аудита.
2. Выпустить backend API и ленивое создание базовых профилей.
3. Выпустить UI-вкладку `Свойства` и раздел `Настройки свойств`.
4. Проверить профиль `Дисплеи` на реальных товарах перед расширением остальных
   профилей.
5. Rollback UI безопасен: таблицы правил можно оставить в БД, авто-матчер от них
   не зависит.

# Changelog

- 2026-05-06 — accepted draft created.
