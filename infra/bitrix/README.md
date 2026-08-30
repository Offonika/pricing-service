# Bitrix Sale shipment artifacts

Эти файлы являются versioned source для отдельного rollout на Bitrix Box. Они
не устанавливаются вместе с release `pricing-service` автоматически.

- `mm_sale_shipment_gateway.php` устанавливается как
  `local/tools/mm_sale_shipment_gateway.php`. Bearer token хранится только в
  `local/php_interface/mm_sale_shipment_gateway.config.php` и не коммитится.
  Клиент дублирует тот же секрет в `X-MM-Shipment-Token` для Apache `mod_fcgid`,
  который по умолчанию удаляет `Authorization`; gateway принимает fallback только
  после той же exact `hash_equals`-проверки.
  Read-only действие `snapshot` ищет заказ по точному `ACCOUNT_NUMBER`. Для
  structured audit в конфиге задаётся `log_file` вне публичного web-root.
- `mm_site_cdek_track_sync.php` заменяет текущий CLI-мост после backup и dry-run.
  Версия fail-closed: при нескольких физических отгрузках единый трек сделки не
  записывается ни в заказ, ни во все его части.

Порядок rollout:

1. Снять backup двух production-файлов и проверить `php -l`.
2. Установить gateway с новым случайным token, оставить
   `ORDER_FULFILLMENT_SHIPMENTS_GATEWAY_APPLY_ENABLED=false`.
3. Проверить `snapshot` и `list` на тестовом заказе, затем идемпотентный повтор `ensure` и
   адресный `update_tracking` по одному `shipment_id`.
4. Заменить legacy sync, выполнить `--dry-run` на одном обычном и одном
   разделённом заказе. Для разделённого ожидается `multiple_shipments` без записи.

## Контракт workflow уведомления части

До включения `ORDER_FULFILLMENT_SHIPMENTS_NOTIFICATIONS_ENABLED` отдельные email-
и SMS-шаблоны Bitrix должны принимать параметры `ORDER_NUMBER`, `SHIPMENT_ID`,
`TRACKING_NUMBER`, `PART_NUMBER`, `PART_COUNT`, `ITEMS_TEXT`, `CHANNEL`,
`IDEMPOTENCY_KEY`, `MARKER`.

Шаблон обязан:

1. повторно проверить отсутствие `MARKER` в timeline сделки;
2. отправить сообщение только для переданной части;
3. после подтверждённой отправки записать `MARKER` в timeline;
4. вызвать authenticated callback
   `/api/order-fulfillment/shipments/notifications/status` со статусом
   `sent`, `delivered` либо `failed` и исходным `IDEMPOTENCY_KEY`.

До readback параметров, marker и callback оба ID шаблонов остаются пустыми, а
флаги email/SMS — `false`. Stale `submitted` повторяется только после проверки
marker; один лишь запуск workflow не считается отправкой.
5. Включать gateway apply, стадии и уведомления независимыми флагами только после
   readback. При ошибке выключить apply; созданные штатные отгрузки не удалять.

Production deployment этих файлов требует отдельной операционной команды и не
входит в обычный backend release.
