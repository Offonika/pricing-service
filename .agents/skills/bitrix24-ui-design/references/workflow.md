# Embedded Bitrix24 UI workflow

## Source map

- React UI: `ui/src/`
- Bitrix route handlers: `app/api/`
- UI primitives: `ui/src/components/ui/`
- Design tokens: `ui/tokens/` and `ui/src/styles/generated/`
- Route contracts: `openapi.yaml` and `docs/specs/`
- Local QA only: `.local/design-qa/`

## Design rules

- Assume the app can render in a narrow Bitrix iframe.
- Use Russian business wording and explain unavoidable English technical terms.
- Do not rely on color alone for statuses.
- Keep primary actions visible, destructive actions separated, and data freshness explicit.
- Virtualized grids must retain an accessible summary and keyboard-operable surrounding controls.

## Checks

```bash
cd ui
npm run tokens:build
npm run test
npm run build
npm run test:a11y
```

Backend regression check:

```bash
PYTHONPATH=.:src .venv/bin/python -m pytest tests/test_web_app.py -q
```

Store screenshots, traces, and axe JSON under `.local/design-qa/`; never commit them.
