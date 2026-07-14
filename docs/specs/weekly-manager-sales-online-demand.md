---
spec_id: "weekly-manager-sales-online-demand"
title: "Weekly Manager Sales Online Demand Block"
doc_type: spec
domain: "management"
status: implemented
owner: "analytics"
source_of_truth: true
related_code:
  - infra/cron/weekly_manager_sales_reports_from_a.py
  - app/services/online_demand_metrics.py
  - tasks/send_weekly_manager_sales_report.py
related_tests:
  - tests/test_online_demand_metrics.py
  - tests/test_weekly_manager_sales_reports_from_a.py
contracts: []
depends_on:
  - docs/TechDesign.ManagementControlTower.md
supersedes: []
rollout_required: true
updated_at: "2026-05-16"
---

# Назначение

Добавить в еженедельную управленческую отправку для коммерческого директора,
генерального директора и директора по развитию короткий блок `Онлайн-спрос и
конверсия` после финансовых/продажных показателей.

Блок нужен, чтобы рядом с дебиторкой и выручкой видеть ранние сигналы спроса на
сайте: визиты, покупки, клики `Купить`, начало оформления, звонки и внутренний
поиск.

# Scope / Out of Scope

Входит:
- read-only запросы к Яндекс Метрике по счётчику сайта `master-mobile.ru`;
- добавление блока в caption sales-документа при доставке через server B /
  Openclaw adapter `weekly-manager-sales`;
- недельное сравнение текущей закрытой недели с предыдущей;
- graceful degradation: если Метрика недоступна, weekly-отчёт всё равно
  отправляется.

Не входит:
- создание или изменение целей Метрики;
- отправка отчёта напрямую с backend server A;
- смешивание e-commerce сайта и счётчика Яндекс Бизнеса;
- расчёт финансовой выручки по данным Метрики.

# Source of Truth

- `1С` / pricing-service DB остаются источником управленческой выручки,
  продаж и дебиторки.
- Яндекс Метрика `49993429` является источником только для блока онлайн-спроса.
- Server B / Openclaw delivery остаётся каналом отправки weekly-отчёта
  руководителям.

# Data Flow

```text
server A pricing-service -> weekly-manager-sales manifest/artifacts
server B Openclaw adapter -> Yandex Metrika read-only API
server B Openclaw adapter -> Telegram document caption
```

Adapter получает weekly bundle как раньше. Для artifact `sales` он читает период
из manifest, забирает агрегаты Метрики за текущую и предыдущую неделю и добавляет
текстовый блок в caption перед отправкой документа.

# API / Data Contracts

Новых публичных API нет.

Новые env-настройки server B / Openclaw:

```text
WEEKLY_MANAGER_SALES_ONLINE_DEMAND_ENABLED=true
WEEKLY_MANAGER_SALES_METRIKA_TOKEN=...
WEEKLY_MANAGER_SALES_METRIKA_COUNTER_ID=49993429
YANDEX_METRIKA_TIMEOUT_SECONDS=20
```

Fallback для совместимости:
- `YANDEX_METRIKA_TOKEN`;
- `YANDEX_METRIKA_COUNTER_ID`.

Метрики блока:
- визиты;
- посетители;
- e-commerce покупки;
- `Клик по кнопке Купить`;
- `Автоцель: Начало оформления заказа`;
- `Автоцель: клик по номеру телефона`;
- `Автоцель: поиск по сайту`;
- основной источник продаж.

# Invariants

- OAuth-токен Метрики не выводится в логи и не включается в артефакты.
- При ошибке Метрики отправка weekly-отчёта не падает.
- Данные блока подписываются как `Яндекс Метрика, не финансовая выручка 1С`.
- Блок добавляется только к sales artifact, не к employee debt artifact.

# Errors / Edge Cases

- Если токен не настроен, блок не добавляется.
- Если цель переименована или удалена, adapter добавляет короткую заметку о
  недоступности Метрики и продолжает отправку.
- Если Метрика отвечает медленно, срабатывает `YANDEX_METRIKA_TIMEOUT_SECONDS`.
- Если отчёт отправляется повторно как correction, online-блок добавляется после
  текста исправленной версии.

# Tests

- `python -m py_compile app/services/online_demand_metrics.py infra/cron/weekly_manager_sales_reports_from_a.py`
- `python -m pytest tests/test_online_demand_metrics.py tests/test_weekly_manager_sales_reports_from_a.py`
- `python scripts/validate_docs_manifest.py` из `/opt/MM`;
- `python scripts/validate_specs.py` из `/opt/MM`.

# Rollout

1. Добавить `WEEKLY_MANAGER_SALES_METRIKA_TOKEN` в env server B /
   Openclaw-ассистента.
2. Оставить `WEEKLY_MANAGER_SALES_ONLINE_DEMAND_ENABLED=true`.
3. Запустить smoke-проверку caption enrichment без Telegram side effects.
4. После первой боевой отправки проверить лог: weekly delivery должен быть `ok`,
   без публикации токена.

# Changelog

- 2026-05-16 — implemented: server B weekly caption enrichment from Yandex Metrika.
