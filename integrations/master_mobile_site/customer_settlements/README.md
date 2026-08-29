# Bitrix-компонент взаиморасчётов

Версионируемый deploy-пакет страницы `/personal/settlements/`. До backend
readiness он устанавливается на `dev.master-mobile.ru` только с внешним mock-конфигом.

## Файлы

- содержимое `local/` копируется в document root с сохранением путей;
- содержимое `personal/` копируется в document root;
- `dev-personal-menu.patch` добавляет условный пункт меню после `Мои заказы`;
- реальный конфиг всегда находится вне webroot по адресу
  `/etc/master-mobile/customer-settlements.json`, права `0640` или строже.

## Dev mock

На dev используется конфиг без секретов:

```json
{
  "mode": "mock",
  "mock_query_enabled": true,
  "mock_variant": "debt"
}
```

Доступные варианты для визуальной проверки:
`debt`, `advance`, `zero`, `stale`, `unavailable`, `not_linked`, `ambiguous`,
`disabled`. Query-переключение работает только при точном hostname
`dev.master-mobile.ru` и не должно включаться в production.

## Real provider

Переключение `mode=real` выполняется только после backend readiness. Конфиг содержит
HTTPS base URL, active `kid` и secret. Assertion создаётся только на сервере из
текущего `$USER->GetID()`, живёт 60 секунд и не попадает в HTML или JavaScript.

Перед установкой нужно сохранить резервные копии двух файлов меню, применить patch
с `--dry-run`, затем скопировать overlay и проверить `php -l`. Production этим
пакетом автоматически не изменяется.
