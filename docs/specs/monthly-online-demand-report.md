---
spec_id: "monthly-online-demand-report"
title: "Monthly Online Demand Report"
doc_type: spec
domain: "management"
status: implemented
owner: "analytics"
source_of_truth: true
related_code:
  - infra/cron/monthly_online_demand_report.py
  - app/services/online_demand_metrics.py
related_tests:
  - tests/test_monthly_online_demand_report.py
  - tests/test_online_demand_metrics.py
contracts: []
depends_on:
  - docs/specs/weekly-manager-sales-online-demand.md
supersedes: []
rollout_required: true
updated_at: "2026-05-16"
---

# Назначение

Ежемесячно отправлять коммерческому директору, генеральному директору и директору
по развитию блок `Онлайн-спрос и продажи сайта`: спрос, покупки, воронку,
основной источник продаж, топ посадочных страниц и страницы, где есть трафик, но
нет покупок.

# Scope / Out of Scope

Входит:
- read-only сбор данных из Яндекс Метрики по сайту `master-mobile.ru`;
- Telegram-доставка через server B / Openclaw management route;
- dedupe по месяцу и локальный `.txt` artifact;
- fallback на Telegram-настройки weekly manager sales, если monthly-настройки не
  заданы.

Не входит:
- финансовая выручка 1С;
- изменение целей Метрики;
- вложение отчёта в retail-director workbook;
- расчёт рекламного CPA/ROI до появления расхода Директа.

# Source of Truth

- Яндекс Метрика `49993429` — источник онлайн-показателей сайта.
- `1С` / pricing-service остаются источником финансовой выручки и управленческих
  продаж.
- State доставки хранится на server B / Openclaw в
  `MONTHLY_ONLINE_DEMAND_STATE_PATH`.

# Data Flow

```text
server B Openclaw cron -> pricing-service monthly_online_demand_report.py
  -> Yandex Metrika read-only API
  -> local text artifact + Telegram message
```

# API / Data Contracts

Новых публичных API нет.

Env:

```text
MONTHLY_ONLINE_DEMAND_TELEGRAM_TOKEN
MONTHLY_ONLINE_DEMAND_TELEGRAM_CHAT_ID
MONTHLY_ONLINE_DEMAND_METRIKA_TOKEN
MONTHLY_ONLINE_DEMAND_METRIKA_COUNTER_ID=49993429
MONTHLY_ONLINE_DEMAND_STATE_PATH
MONTHLY_ONLINE_DEMAND_REPORT_DIR
MONTHLY_ONLINE_DEMAND_PAGE_LIMIT=20
```

Fallback:
- Telegram token/chat берутся из `WEEKLY_MANAGER_SALES_*`, если monthly env не
  задан;
- Метрика-токен берётся из `WEEKLY_MANAGER_SALES_METRIKA_TOKEN` или
  `YANDEX_METRIKA_TOKEN`.

# Invariants

- Токены не выводятся в отчёт, summary и state.
- Повторная отправка того же месяца не выполняется без `--force`.
- Отчёт подписывает, что данные Метрики не являются финансовой выручкой 1С.
- Job не пишет в Метрику, Bitrix24, 1С или pricing DB.

# Errors / Edge Cases

- Если нет Telegram-настроек и запуск не `--dry-run`, job завершается до side
  effects.
- Если нет токена Метрики, отчёт не строится.
- Telegram-сообщение ограничено безопасной длиной, полный текст сохраняется в
  local artifact.

# Tests

- `python -m pytest tests/test_monthly_online_demand_report.py tests/test_online_demand_metrics.py`
- `python -m py_compile infra/cron/monthly_online_demand_report.py app/services/online_demand_metrics.py`
- `python scripts/validate_docs_manifest.py` из `/opt/MM`;
- `python scripts/validate_specs.py` из `/opt/MM`.

# Rollout

1. Положить токен Метрики в env server B.
2. Проверить `monthly_online_demand_report.sh --month YYYY-MM --dry-run`.
3. Проверить текст artifact и summary.
4. Включить cron на 2-е число месяца после закрытия периода.

# Changelog

- 2026-05-16 — implemented: standalone monthly online demand Openclaw adapter.
