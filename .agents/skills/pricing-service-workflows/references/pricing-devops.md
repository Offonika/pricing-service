# Workflow: pricing-devops

Использовать для GitHub Actions, Docker, cron, окружений, deployment и
эксплуатационных scripts.

## Контекст

- Основные пути: `infra/`, `.github/workflows/`, `scripts/`, конфигурация приложения.
- Читать `docs/architecture.md` и профильный runbook/spec, возвращённый root-router.
- Проверить существующие команды CI и безопасные dry-run/status режимы.

## Действия

1. Сохранить воспроизводимость между локальным запуском и CI.
2. Новую env-переменную документировать безопасным placeholder без реального значения.
3. Для cron проверить lock, timeout, retry, идемпотентность, лог и код возврата.
4. Для workflow ограничить path triggers реальными зависимостями проверки.
5. До изменения внешней среды подготовить read-only preflight, backup и rollback.
6. Для production release использовать только
   `/usr/local/sbin/mm-pricing-service-release`: `deploy` для полной сборки из
   clean worktree, `check` для manifest/provenance preflight и `switch` для уже
   собранного кандидата. Контроллер сам фиксирует active base, canonical mutable
   root, expected-active, locks и audit.

## Ограничения и проверки

- Не встраивать бизнес-логику в shell/CI и не трогать production без явного указания.
- Не запускать production cutover напрямую через
  `scripts/switch_pricing_service_release.sh` из checkout, worktree или release.
  Это низкоуровневый скрипт для тестов и отдельно подтверждённого break-glass.
- Не использовать `curl | bash` для внешнего бинарника; проверять checksum и
  provenance/attestation.
- Валидировать YAML/TOML, запустить соответствующий script в dry-run/check режиме и
  обязательные project checks.
