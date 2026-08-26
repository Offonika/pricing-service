---
spec_id: "sms-journal-api"
title: "Encrypted SMS Journal API"
doc_type: spec
domain: "communications"
status: draft
owner: "engineering-and-operations"
source_of_truth: true
related_code:
  - app/api/sms_journal.py
  - app/models/sms_journal.py
  - app/schemas/sms_journal.py
  - app/services/sms_journal.py
  - app/services/sms_journal_export.py
  - tasks/export_sms_journal_xlsx.py
  - alembic/versions/c3e5a7b9d1f2_add_sms_journal.py
related_tests:
  - tests/test_sms_journal_api.py
  - tests/test_export_sms_journal_xlsx.py
contracts:
  - openapi.yaml
depends_on: []
supersedes: []
rollout_required: true
updated_at: "2026-08-26"
---

# Назначение

Реализовать локальный backend MVP единого журнала исходящих SMS для задачи
Bitrix24 №2662. Журнал принимает факты от `УТ 10.3`, служебной `УТ 11` и сайта,
но не отправляет SMS и не меняет business source of truth этих систем.

# Scope / Out of Scope

Входит:

- защищённое создание записи до отправки;
- отдельная фиксация ответа провайдера и статуса доставки;
- идемпотентность каждого write-запроса;
- AES-GCM шифрование телефона и разрешённого текста;
- HMAC fingerprint телефона, маска и readback без полного телефона и текста;
- обязательная редакция OTP/пароля до шифрования и записи;
- оценка сегментов GSM-7 и UCS-2;
- закрытая Excel-выгрузка по запросу с allowlist исполнителей, маской телефона,
  разрешённым полным текстом, сводкой одинаковых текстов и audit-sidecar;
- срок хранения чувствительных полей 13 месяцев.

Не входит:

- подключение production 1С, сайта или МегаФона;
- отправка реальных SMS;
- включение отключённых SMS «заказ готов к выдаче»;
- очередь на стороне 1С/сайта и автоматическая очистка после 13 месяцев;
- пользовательский интерфейс Bitrix, автоматическая рассылка Excel и публичная
  выдача расшифрованного текста через API.

# Change Summary / Spec Delta

- Было: общий backend-журнал SMS в `pricing-service` отсутствовал.
- Станет: источники могут безопасно фиксировать попытку, результат отправки и доставку.
- Станет: назначенный аналитик может сформировать закрытый Excel за период до
  31 дня без полного телефона и без изменения исходных записей журнала.
- Не меняется: провайдер, текущие точки отправки и production-поведение.
- Решение на будущий пилот: владельцем SMS готовности внутреннего самовывоза
  становится Bitrix24 после readback стадии `PICKUP_WAITING`. До отдельного
  production-разрешения робот работает только в shadow, а отключенная рассылка
  не включается.

# Acceptance Criteria

- [x] API защищён отдельным bearer token.
- [x] Полный телефон и текст отсутствуют в ответе API.
- [x] Телефон и разрешённый текст хранятся только как AES-GCM ciphertext.
- [x] OTP/пароль требуют `redaction_values`; исходный секрет не записывается.
- [x] Повтор с тем же `Idempotency-Key` и payload не создаёт вторую запись.
- [x] Повтор ключа с другим payload возвращает HTTP `409`.
- [x] Сегменты проверены на границах GSM-7 `160/161` и UCS-2 `70/71`, `134/135`.
- [x] Локальная Excel-выгрузка требует allowlist и явного подтверждения, не
      перезаписывает файл, создаёт файлы с правами `0600` и безопасный audit JSON.
- [x] Excel содержит сводку одинаковых текстов и детализацию с маской телефона;
      строки, похожие на формулы, не исполняются как формулы Excel.
- [x] Схема оформлена новой Alembic-миграцией.
- [ ] Контракт проверен полным набором тестов и OpenAPI drift-check.
- [ ] Подготовлена защищённая очередь источника без повторной SMS.
- [ ] Реализована автоматическая очистка чувствительных полей по истечении 13 месяцев.

# Source of Truth

- `pricing-service/PostgreSQL` — технический журнал SMS.
- УТ 10.3, УТ 11 и сайт — источники бизнес-событий.
- МегаФон — источник факта приёма, доставки и тарификации.

# API / Data Contracts

Префикс: `/api/internal/sms-journal`.

- `POST /attempts` — зафиксировать событие до обращения к провайдеру;
- `POST /attempts/{event_id}/send-result` — записать ответ отправки;
- `POST /attempts/{event_id}/delivery` — записать статус доставки;
- `GET /attempts/{event_id}` — безопасный readback без телефона и текста.

Каждый `POST` требует `Idempotency-Key` длиной 8–255 символов. Все endpoints
требуют `Authorization: Bearer ...` с отдельным
`SMS_JOURNAL_INTERNAL_API_TOKEN`.

Закрытая Excel-выгрузка создаётся только локальной CLI-командой:

```bash
SMS_JOURNAL_EXPORT_ALLOWED_ACTORS=<authorized-actor> \
./.venv/bin/python -m tasks.export_sms_journal_xlsx \
  --date-from 2026-08-01 \
  --date-to 2026-08-31 \
  --actor <authorized-actor> \
  --output reports/sms-journal/sensitive/sms-2026-08.xlsx \
  --confirm-sensitive-export
```

Период ограничен 31 днём. Файл содержит листы `Сводка`, `SMS` и `Параметры`.
Рядом создаётся `.audit.json` с actor, периодом, числом строк и SHA-256 Excel,
но без телефона и текста. Полный телефон не расшифровывается и не попадает в
Excel; используется сохранённая маска.

# Invariants

- API не вызывает МегаФон и не является механизмом отправки.
- Полный телефон и текст не возвращаются клиенту и не включаются в URL.
- `SMS_JOURNAL_ENCRYPTION_KEY`, `SMS_JOURNAL_PHONE_HASH_KEY` и bearer token —
  независимые секреты окружения.
- Для `secret_kind=otp|password` каждый секрет должен быть перечислен в
  `redaction_values` и присутствовать в тексте.
- Сохранённый idempotency payload содержит только безопасные hashes, но не телефон,
  текст или redaction values.
- Расшифровка разрешённого текста доступна только локальной export-команде после
  allowlist-проверки и явного `--confirm-sensitive-export`.
- Excel и audit-sidecar создаются с правами `0600`; существующие файлы не
  перезаписываются. Автоматической отправки файла нет.

# Errors / Edge Cases

- `401` — токен отсутствует, неверен или не настроен;
- `404` — `event_id` не найден;
- `409` — конфликт идемпотентности, повтор `event_id` или некорректный переход;
- `422` — невалидный контракт, включая OTP/password без `redaction_values`;
- `503` — отсутствуют ключи шифрования или недоступна БД.

# Rollout / Rollback

ОТМЕНЕНО (2026-08-26): до отдельного разрешения миграция и API оставались
локальными. Это было исходным pre-rollout ограничением; разрешение на подключение
старого сайта получено и реализовано отдельно от остальных источников.

2026-08-26 в production подключена post-send синхронизация старого сайта из
`d_smsmegafon_list`. Штатная отправка не изменена, callback-статусы
синхронизируются best-effort, незаказные тексты полностью маскируются. Пилот
начат после ID 8497 без отправки тестовой SMS.

2026-08-26 выполнен одноразовый backfill статусов 70 ранее записанных SMS
Bitrix24: 66 подтверждены как delivered, 2 как undelivered, 2 оставлены pending
из-за отсутствия callback. Повторная отправка SMS не выполнялась.

Rollback приложения — отключить потребителей и предыдущий release. Таблицы журнала
не удалять автоматически: они содержат аудит и требуют отдельного решения по данным.

# Test Plan

- профильные API, negative и security tests;
- тесты Excel: allowlist, подтверждение, период, расшифровка, маска телефона,
  права файла, audit-sidecar и защита от Excel-formula injection;
- Alembic heads/history и проверка upgrade/downgrade на временной БД;
- Ruff и Black;
- полный `pytest`;
- `scripts/export_openapi.py --check`;
- workspace docs validators.

# Changelog

- 2026-08-26 — выполнен backfill старых delivery-статусов Bitrix24.
- 2026-08-26 — разрешено и выполнено production-подключение старого сайта к
  центральному SMS-журналу.
- 2026-08-26 — зафиксирован будущий владелец SMS готовности: Bitrix24 после
  подтвержденной приемки и readback `PICKUP_WAITING`. Решение не включает
  production SMS и не отменяет действующий запрет реальной отправки.
- 2026-08-11 — по решению владельца первой формой отчёта выбрана закрытая
  Excel-выгрузка; реализованы локальная CLI-команда, сводка одинаковых текстов,
  детализация, allowlist, права `0600` и безопасный audit-sidecar. Bitrix UI и
  production rollout не выполнялись.
- 2026-08-10 — создан локальный backend MVP без production-подключений.
