# Bitrix Sale shipment artifacts

Эти файлы являются versioned source для отдельного rollout на Bitrix Box. Они
не устанавливаются вместе с release `pricing-service` автоматически.

- `mm_sale_shipment_gateway.php` устанавливается как
  `local/tools/mm_sale_shipment_gateway.php`. Bearer token хранится только в
  `local/php_interface/mm_sale_shipment_gateway.config.php` и не коммитится.
- `mm_site_cdek_track_sync.php` заменяет текущий CLI-мост после backup и dry-run.
  Версия fail-closed: при нескольких физических отгрузках единый трек сделки не
  записывается ни в заказ, ни во все его части.

Порядок rollout:

1. Снять backup двух production-файлов и проверить `php -l`.
2. Установить gateway с новым случайным token, оставить
   `ORDER_FULFILLMENT_SHIPMENTS_GATEWAY_APPLY_ENABLED=false`.
3. Проверить `list` на тестовом заказе, затем идемпотентный повтор `ensure` и
   адресный `update_tracking` по одному `shipment_id`.
4. Заменить legacy sync, выполнить `--dry-run` на одном обычном и одном
   разделённом заказе. Для разделённого ожидается `multiple_shipments` без записи.
5. Включать gateway apply, стадии и уведомления независимыми флагами только после
   readback. При ошибке выключить apply; созданные штатные отгрузки не удалять.

Production deployment этих файлов требует отдельной операционной команды и не
входит в обычный backend release.
