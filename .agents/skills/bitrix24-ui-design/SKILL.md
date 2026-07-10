---
name: bitrix24-ui-design
description: Design, implement, audit, or accessibility-test embedded Bitrix24 React interfaces in pricing-service. Use for /bitrix routes, executive dashboards, smart-process workspaces, iframe behavior, UI primitives, tokens, screenshots, and axe checks.
---

# Bitrix24 UI Design

Create compact, accessible Bitrix24 workspaces that remain safe inside an iframe and continue to work when Bitrix SDK context is unavailable locally.

## Workflow

1. Read `AGENTS.md`, `llms.txt`, `docs/index.md`, `docs/manifest.yml`, and the relevant Bitrix spec.
2. Trace the route from FastAPI through `ui/src/` and preserve its API and authorization contract.
3. Reuse generated pricing tokens and `ui/src/components/ui/` primitives; avoid new one-off visual constants.
4. Keep business states explicit: loading, empty, permission denied, partial data, error, and success.
5. Check iframe width, keyboard navigation, focus visibility, Russian labels, dense-table readability, and reduced motion.
6. Run the checks in [references/workflow.md](references/workflow.md); keep visual artifacts in `.local/design-qa/` only.

Never edit or publish the production Bitrix24 application, placement, data, or server assets without explicit approval in the current task.
