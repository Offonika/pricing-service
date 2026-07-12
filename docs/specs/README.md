# Pricing Service Specs

Project-level specs для `pricing-service` лежат в этом каталоге.

Создавайте spec для крупных изменений FastAPI, management API, Telegram/logistics,
Bitrix24 smart-processes, read-only 1C/1С витрин, OpenAPI и data contracts.

Порядок работы:

1. Скопировать шаблон `/opt/MM/docs/templates/spec.md`.
2. Назвать файл в формате `lower-kebab-case.md`.
3. Заполнить frontmatter, контракты, тесты и rollout.
4. Добавить spec в `docs/manifest.yml`.
5. Проверить `./.venv/bin/python scripts/validate_specs.py` из `/opt/MM`.
