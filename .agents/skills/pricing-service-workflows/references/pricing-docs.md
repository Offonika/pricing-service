# Workflow: pricing-docs

Использовать для PRD, architecture, strategies, specs, contracts, plan, task files,
manifest и runbooks.

## Контекст

- Начать с `docs/index.md` и документов, возвращённых root-router.
- Полный `docs/manifest.yml` открывать только при изменении documentation governance,
  маршрутизации или регистрации документа.
- Для крупного решения использовать lifecycle из `docs/specs/README.md` и шаблон
  `/opt/MM/docs/templates/spec.md`.

## Действия

1. Определить canonical source и не дублировать его содержание в README/AGENTS/skill.
2. При добавлении документа зарегистрировать status, owner, source_of_truth, связи с
   кодом/тестами и поисковые keywords.
3. Проверить ссылки, статус lifecycle и соответствие фактическому коду.
4. Обновлять `docs/plan.md` только по реальному статусу задачи.
5. При API-изменении проверить `openapi.yaml`; при интеграции — профильный contract.

## Ограничения и проверки

- Не объявлять draft implemented без доказательства в коде и тестах.
- Не помещать секреты, локальные пути с данными и generated artifacts в документацию.
- Запустить root project-docs validator, spec validator при изменении specs и проверку
  skill references при изменении `.agents/`.
