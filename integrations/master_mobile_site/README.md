# Мост сервисных обращений сайта

Этот каталог содержит версионируемый исходник site-side части задачи Bitrix24
№3223. Само наличие файлов в `pricing-service` ничего не меняет на
`master-mobile.ru`: bridge не подключён к `init.php`, Agent не зарегистрирован,
таблица и пользовательские поля на сайте не создавались.

## Состав

- `service_ticket_bridge.php` — outbox, HMAC-клиент, Agent обмена, доставка файлов,
  dedupe исходящих команд через `EXTERNAL_FIELD_1` и явные install-функции;
- `service_ticket_component_params.php` — список SUPPORT user fields для параметра
  `SET_SHOW_USER_FIELD` существующего компонента `bitrix:support.ticket.edit`;
- `fixtures/` — локальные contract/static fixtures подписи, извлечения события,
  command dedupe и idempotent DDL dry-run.

## Безопасные свойства

- подключение файла само по себе не выполняет DDL, сетевые запросы или запись
  Bitrix Option;
- event handler пишет только IDs и безопасный код события в
  `b_mm_service_ticket_outbox`; тексты и контакты читаются перед отправкой Agent;
- клиентский POST тикета не ждёт `pricing-service`;
- emit и outbound replies включаются разными Bitrix Option и по умолчанию
  выключены;
- ответ добавляется отдельным активным support-team пользователем, а повтор
  команды определяется по `mm-site-service-command:<command_id>`;
- в outbox и обычные логи не записываются тексты, телефон, e-mail, имя файла,
  HMAC secret или URL с секретом.

## Отдельно подтверждаемый rollout

После явного разрешения на изменение сайта оператор должен сначала сохранить
backup и выполнить read-only preflight. Затем в управляемом include-файле можно:

1. подключить `service_ticket_bridge.php` и вызвать `registerHandlers()`;
2. отдельно вызвать `installSchema()` и `installSupportUserFields()`, после чего
   проверить readback;
3. объединить массив из `service_ticket_component_params.php` с параметрами
   существующего `support.ticket.edit`; если штатный шаблон не показывает поля —
   создать отдельную копию шаблона и вывести те же три user fields. Подпись автора
   сообщения выводить через `mm_site_service_ticket_author_label($arMessage)`,
   чтобы клиент видел «Поддержка MASTER MOBILE», а не имя служебного пользователя;
4. настроить API base URL, HMAC secret и ID отдельного support user только через
   Bitrix Option, не в Git;
5. зарегистрировать `mm_site_service_ticket_agent();` с минутным интервалом в
   штатном cron-driven Agent;
6. сначала оставить оба feature flag выключенными и включать их по rollout из
   канонического spec.

Bridge объявляет capability `command-files-v1`, проверяет размер/SHA-256 каждого
исходящего файла и при повторе дозагружает недостающие вложения к сообщению с тем
же command marker. Метод `backfillConversationSnapshot($ticketId)` предназначен
только для отдельно подтверждённого адресного backfill существующей карточки.

Перед live-включением обязательны `php -l`, повторный DDL dry-run, readback полей,
один synthetic inbound ticket и только затем отдельный outbound smoke.
