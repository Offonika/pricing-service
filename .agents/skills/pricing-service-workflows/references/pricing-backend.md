# Workflow: pricing-backend

Использовать для FastAPI endpoints, Pydantic schemas, сервисов, фоновой бизнес-логики
и тестов backend.

## Контекст

- Читать только профильный PRD/spec/contract, возвращённый root-router.
- Основные пути: `app/api/`, `app/services/`, `app/schemas/`, `app/models/`, `tests/`.
- До правки найти endpoint, service method, schema и существующие тесты через `rg`.

## Действия

1. Сохранить разделение: API валидирует и делегирует, service содержит логику,
   repository/model отвечает за хранение.
2. Поддержать явные ошибки и наблюдаемое поведение без утечки чувствительных данных.
3. При изменении входа или ответа синхронно обновить schemas, тесты и `openapi.yaml`.
4. Добавить regression test на исправляемый сценарий и негативные границы.
5. Архитектурно значимые изменения передать в `pricing-architect` workflow.

## Ограничения и проверки

- Не хардкодить секреты и внешние production endpoints.
- Не менять внешние контракты молча и не обходить service layer прямым SQL из API.
- Запустить профильные `pytest`, затем Ruff, Black, полный pytest и OpenAPI check для
  значимой правки.
