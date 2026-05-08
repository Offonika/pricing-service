# Pricing Service Docs Index

Этот индекс помогает быстро выбрать каноничный контекст по проекту `pricing-service`.
Машинно-читаемый список документов хранится в `docs/manifest.yml`.

## Читать сначала

1. `../AGENTS.md` — роли агентов, workflow и проверки.
2. `../README.md` — быстрый старт и текущие операционные сценарии.
3. `PRD.md` — бизнес-требования.
4. `architecture.md` — high-level архитектура и основные контуры.
5. `../openapi.yaml` — зафиксированный API-контракт FastAPI.
6. `plan.md` — статусы задач и дальнейшая очередь работ.
7. `specs/README.md` — lifecycle новых крупных спецификаций проекта.

## Основные домены

| Домен | Документы |
| --- | --- |
| Pricing core | `PRD.md`, `architecture.md`, `price-strategies.md`, `TechDesign.CompetitorMatching.md` |
| Competitors/LLM | `competitor_matching.md`, `TechDesign.CompetitorFTPImport.md`, `competitor-matching-nightly-audit-2026-05-02.md`, `agents-market-research.md`, `TechDesign.AgentsMarketDemand.md` |
| Management/BI | `TechDesign.ManagementControlTower.md`, `BI.Receivables.md`, `Onepage.ReceivablesWorkProcess.md`, `specs/receivables-smart-process-workflow.md`, `BI.ModelDemand.md`, `receivable_authoritative_evening_runbook.md` |
| Logistics/Telegram | `TechDesign.LogisticsTelegramMVP.md`, `IntegrationContract.Logistics1C.md`, `Onepage.LogisticsTelegramMVP.md` |
| Expertise/order flow | `Onepage.ExpertiseCaseMVP.md`, `TechDesign.ExpertiseCaseMVP.md`, `IntegrationContract.Expertise1C.md`, `Runbook.ExpertiseWave1.md`, `order_flow/README.md` |
| SKU/1C | `sku_policy.md`, `sku_dev_mapping.md`, `sku_dictionary_for_buyers.md`, `1c_sql_mapping.md` |
| Specs | `specs/README.md`, новые specs в `docs/specs/` |

## Правила расширения

- Новые API-контракты должны обновлять FastAPI schemas, тесты и документацию.
- `openapi.yaml` генерируется из FastAPI командой `python scripts/export_openapi.py`; CI проверяет drift через `--check`.
- Новые крупные спецификации оформляйте по lifecycle из `docs/specs/README.md` и шаблону `/opt/MM/docs/templates/spec.md`.
- README не расширяйте как бесконечный журнал; новые большие сценарии выносите в `docs/`.
