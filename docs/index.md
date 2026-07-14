# Pricing Service Docs Index

Этот индекс помогает быстро выбрать каноничный контекст по проекту `pricing-service`.
Машинно-читаемый список документов хранится в `docs/manifest.yml`.

## Читать сначала

1. `../AGENTS.md` — короткие инварианты, навигация и обязательные проверки.
2. `../.agents/skills/pricing-service-workflows/SKILL.md` — выбор ролевого workflow;
   подробности роли читаются из одного подходящего reference.
3. `../README.md` — быстрый старт и текущие операционные сценарии.
4. `PRD.md` — бизнес-требования.
5. `architecture.md` — high-level архитектура и основные контуры.
6. `../openapi.yaml` — зафиксированный API-контракт FastAPI.
7. `plan.md` — статусы задач и дальнейшая очередь работ.
8. `specs/README.md` — lifecycle новых крупных спецификаций проекта.
9. `specs/pricing-service-architecture-hardening.md` — принятый план архитектурного
   оздоровления, вывода устаревшего источника и усиления границ между проектами.
9. `registry/cli-jobs.json` — обязательные owner/dry-run/idempotency/side-effect
   metadata для постоянных CLI и одноразовых backfill-команд.

## Основные домены

| Домен | Документы |
| --- | --- |
| Pricing core | `PRD.md`, `architecture.md`, `price-strategies.md`, `TechDesign.CompetitorMatching.md` |
| Competitors/LLM | `competitor_matching.md`, `TechDesign.CompetitorFTPImport.md`, `competitor-matching-nightly-audit-2026-05-02.md`, `agents-market-research.md`, `TechDesign.AgentsMarketDemand.md` |
| Management/BI | `TechDesign.ManagementControlTower.md`, `BI.Receivables.md`, `specs/receivables-smart-process-workflow.md`, `Onepage.ReceivablesWorkProcess.md` (исторический onepager), `specs/executive-management-dashboard-bitrix.md`, `specs/counterparty-folder-recommendations.md`, `specs/ut103-bot-command-file-exchange.md`, `specs/exchange-counterparty-daily-settlements.md`, `BI.ModelDemand.md`, `receivable_authoritative_evening_runbook.md` |
| Procurement | `Onepage.ProcurementManagementContour.md`, `specs/procurement-order-auto-order-unified-contour.md`, `specs/procurement-order-formation-smart-process.md` (OAuth-приложение, имя legacy), `specs/procurement-decision-contract-roadmap.md`, `specs/display-auto-order-project-brief.md`, `specs/ved-akb-import-pilot.md`, `../scripts/ensure_procurement_bitrix_process.py` |
| Speech/Audio | `Onepage.OfflineStoreAudioAnalytics.md`, `specs/offline-store-audio-analytics.md`, `imports/openclaw-b-offline-dialog-recording-onepage.md` |
| Logistics/Telegram | `TechDesign.LogisticsTelegramMVP.md`, `IntegrationContract.Logistics1C.md`, `IntegrationContract.LogisticsSiteOrders1C.md`, `Onepage.LogisticsTelegramMVP.md`, `specs/logistics-control-contour.md`, `specs/transfer-assistant-readonly-v1.md` |
| Expertise/order flow | `Onepage.ExpertiseCaseMVP.md`, `TechDesign.ExpertiseCaseMVP.md`, `IntegrationContract.Expertise1C.md`, `Runbook.ExpertiseWave1.md`, `order_flow/README.md`, `specs/site-order-fulfillment-control-contour.md`, `specs/site-defect-archive-search.md` |
| SKU/1C | `sku_policy.md`, `sku_dev_mapping.md`, `sku_dictionary_for_buyers.md`, `1c_sql_mapping.md`, `bank-payment-classifier-one-pager.md` |
| Specs | `specs/README.md`, новые specs в `docs/specs/` |
| Architecture hardening | `specs/pricing-service-architecture-hardening.md`, `architecture.md`, `../openapi.yaml` |

## Правила расширения

- Новые API-контракты должны обновлять FastAPI schemas, тесты и документацию.
- `openapi.yaml` генерируется из FastAPI командой `python scripts/export_openapi.py`; CI проверяет drift через `--check`.
- Новые крупные спецификации оформляйте по lifecycle из `docs/specs/README.md` и шаблону `/opt/MM/docs/templates/spec.md`.
- README не расширяйте как бесконечный журнал; новые большие сценарии выносите в `docs/`.
