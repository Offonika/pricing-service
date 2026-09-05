# Pricing Service Specs

Project-level specs для `pricing-service` лежат в этом каталоге.

Канонический lifecycle, risk matrix, phase/evidence/review contracts находятся в
[Workspace Spec Lifecycle](../../../docs/specs/README.md). Этот файл — project
reference к теме `workspace.spec-lifecycle`, а не отдельный источник общих правил.

Создавайте spec для крупных изменений FastAPI, management API, Telegram/logistics,
Bitrix24 smart-processes, read-only 1C/1С витрин, OpenAPI и data contracts.

Порядок работы:

1. Скопировать шаблон `/opt/MM/docs/templates/spec.md`.
2. Назвать файл в формате `lower-kebab-case.md`.
3. Заполнить frontmatter, контракты, тесты и rollout.
4. Добавить spec в `docs/manifest.yml`.
5. Проверить `./.venv/bin/python scripts/validate_project_docs.py pricing-service`
   из `/opt/MM`: команда проверяет manifest, структуру specs и delivery evidence.

Для управляемого изменения используйте шаблоны phase/evidence/review из общего
канона. Внутри project spec и records пути задаются относительно `pricing-service`;
`canonical_owner` в manifest задаётся относительно workspace. Проверка ownership:
`./.venv/bin/python scripts/validate_canonical_ownership.py` из `/opt/MM`.

Локальная команда проекта применяет существующие правила совместимости к старым
specs. Удалённый workflow `.github/workflows/docs-manifest.yml` использует другой
validator; его успешный запуск сам по себе не подтверждает delivery evidence.

## Changelog

- 2026-09-05 — пилот 6B: lifecycle связан с workspace canonical owner; штатная
  локальная команда проекта включает проверку фаз и evidence.
