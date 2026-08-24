# Shadow-run взаиморасчётов на staging

Документ описывает только изолированный 72-часовой staging-запуск. Он не разрешает
изменения production, сайта `master-mobile.ru`, CRM или 1С. CRM и 1С в этом
сценарии только читаются, а клиентский API и eligibility остаются выключенными.

## Подтверждённая база запуска

На 2026-07-30 выполнены исходная бухгалтерская сверка и PostgreSQL-проверки.
Они относятся к прежней десятке пилотов и сохранены как доказательство корректности
SQL-источника, но не заменяют сверку нового пилотного набора:

- Alembic `upgrade -> downgrade -> upgrade` до `d9e1f3a5b7c9` на отдельном
  PostgreSQL `settlements_stage`;
- PostgreSQL integration suite: `5 passed`;
- бухгалтерская сверка на конец 2026-07-29 по организации `MASTER MOBILE`:
  `10/10`, максимальное расхождение `0,00 RUB`;
- в прежней БД `settlements_stage` был включён whitelist из 10 пилотов;
- исходное состояние: нет active mapping/financial revision и финансовых строк;
  health до первого sync ожидаемо `critical`.

Контрольный PostgreSQL gate от `2026-08-11` выполнен на отдельной одноразовой БД
`settlements_stage_pr36_20260811`, не изменяя прежнюю populated staging-БД:

- фактический цикл `c3d4e5f6a7b9 -> d9e1f3a5b7c9 -> c3d4e5f6a7b9 ->
  d9e1f3a5b7c9` завершён успешно;
- пять синтетических строк старого формата сохранены после upgrade и downgrade,
  GUID backfill проверен;
- PostgreSQL integration suite после перевода fixture на обе settlement migration:
  `5 passed`.

Для нового запуска создана отдельная чистая БД
`settlements_shadow_pr36_20260811` на head `d9e1f3a5b7c9`. В ней нет whitelist,
mapping/financial revision и balances.

ОТМЕНЕНО (2026-08-11): кандидатная десятка внешних клиентов с обязательным
валидным ИНН. Этот CSV прошёл dry-run, но не активировался и больше не используется.
Пилот ограничен сотрудниками с точной связью Bitrix–1С; ИНН необязателен.

ОТМЕНЕНО (2026-08-13): вывод read-only отбора от `2026-08-11` о том, что доступны
только 8 сотрудников. Тогда кадровый статус ошибочно ограничивался кадровой веткой
УТ; действующий сотрудник теперь определяется по структуре Bitrix24.

Финальный read-only отбор от `2026-08-13` дал следующий результат:

- полностью прочитаны `50 035` CRM-строк;
- прочитаны `31` подразделение и `97` действующих сотрудников Bitrix24;
- отобраны `10` сотрудников с точной связью site user/CRM/УТ; Арсений Кештов
  отдельно подтверждён двумя заказами сайта, ведущими к карточке УТ `РБ0000044`;
- ошибочная CRM-связь кабинета Сергея Бирюкова с карточкой Гагика Асатряна
  исключена, безопасной заменой выбран Эльвин Байрамов;
- текущие состояния — `7 debt / 2 advance / 1 zero`;
- importer dry-run успешно проверил `10/10` строк при `inn_control_count=0`;
- после rollback account bindings, mapping, whitelist, financial revision и
  balances в shadow-БД отсутствуют;
- точные hashes dry-run сохранены только в защищённом локальном файле вне репозитория.

Отбор `10/10` готов. Readiness gate остаётся закрытым до отдельного разрешения на
apply mapping/whitelist и бухгалтерской сверки десятки на одинаковый `as_of`.

Во время live dry-run уточнена структура справочников этой УТ: `_Reference66`
не содержит `_Folder`, поэтому организация проверяется по единственности и
`_Marked`; в иерархическом `_Reference54` значение `_Folder = 0x01` подтверждено
как элемент-контрагент, а `0x00` — как группа. Оба правила закреплены тестом.

ОТМЕНЕНО ДЛЯ НОВОГО ЗАПУСКА (2026-08-22): ручной импорт mapping из CSV и режим
`manual_confirmed`. Новый зачётный запуск использует полный `crm_readonly` read,
но активирует только пользователей из отдельного pilot whitelist.

## Release integration 2026-08-22 — 2026-08-23

Settlement migrations объединены с фактически активным production-head
`1b9d3f5a7c21` новой no-op revision `2a4c6e8f0b1d`. Operational migration
`4c6e8a0b2d3f` добавляет reconciliation и alert outbox. Revision
`6e8f0a2b4c6d` привязывает сверку к точному mapping/source context и является
единственным head. Новый staging runtime должен выполнять
`alembic upgrade 6e8f0a2b4c6d`;
прежняя БД и runtime на `d9e1f3a5b7c9` доказательством нового 72-часового запуска
не являются.

## Решение о новом запуске 2026-08-24

Пользователь разрешил создать clean commit текущего проверенного backend-кода,
собрать из него отдельный immutable staging release и начать новый 72-часовой
`crm_readonly` shadow-run по этому runbook. Разрешение распространяется только на
изолированный staging-контур: production, `master-mobile.ru`, тестовые сайты, CRM и
1С не изменяются; CRM и 1С доступны только для чтения. Установка staging cron
разрешена только после успешного ручного цикла и `ready` preflight.

## Фактическая подготовка нового запуска 2026-08-24

- release source commit: `10977d3d90773b3b0e4a34230221bc2bada45fe5`;
- immutable release:
  `/opt/MM/releases/pricing-service/customer-settlements-shadow-20260824-10977d3-r2`;
- release включает active production base `99dc6dfe0510df85e8e5f06648d4d01e3d3f19c5`,
  имеет `source_dirty=false`, content hash и единственный Alembic head
  `6e8f0a2b4c6d`;
- создана новая изолированная БД `settlements_stage_shadow_20260824`, полностью
  поднятая до `6e8f0a2b4c6d`;
- whitelist перенесён штатной CLI-командой: dry-run/apply/readback `10/10`;
- bootstrap preflight прошёл `34/34` на чистом settlement-контуре;
- первый полный `crm_readonly` sync прочитал `50 035` строк и активировал ровно
  `10` pilot entries, ambiguous pilot entries — `0`;
- staging credential был немедленно ротирован после диагностического раскрытия его
  фрагмента; production credentials и production БД не затрагивались;
- client API, eligibility, source validation и alerts остаются выключенными;
- financial revision, reconciliation и staging cron ещё не запускались.

Readiness gate остановлен до новой ведомости за один завершённый день. Исторический
файл июля не используется. После получения ведомости обязательны reconciliation,
включение `CUSTOMER_SETTLEMENTS_SOURCE_VALIDATED=true`, первый financial sync и
`ready` preflight; только затем разрешена установка cron и фактический отсчёт 72 часов.

## ОТМЕНЁННЫЙ shadow-run 2026-08-22

- старт: `2026-08-22 20:43 MSK`;
- clean commit: `4458c90469522c5acd430de5a2833a7fc84a9eb2`;
- staging DB: `settlements_shadow_20260822`, Alembic head `2a4c6e8f0b1d`;
- client API остаётся выключенным, shadow flag включён;
- manual mapping и whitelist: `10/10`, ambiguous: `0`;
- первый и повторный snapshots: expected/loaded `10/10`, explicit zero: `1`;
- первый `ready` preflight: `30/30`, health: `ok`;
- cron установлен только в staging-контуре; контрольные точки — через 24, 48 и
  72 часа от времени старта.

Этот запуск остановлен до первой контрольной точки и не засчитывается: он
использовал manual mapping и head `2a4c6e8f0b1d`. Cron переименован в `.disabled`,
БД и revisions сохранены только для диагностики. Новый запуск получает другую
staging-БД, новый cron-файл, `crm_readonly` и четыре новые ведомости.

## 1. Отдельный secret-файл

Создать вне репозитория файл с правами `0600`. Не копировать целиком production
`.env` и не коммитить файл:

```dotenv
ENVIRONMENT=staging
DATABASE_URL=postgresql+psycopg2://settlements_stage:<password>@127.0.0.1:55439/settlements_stage
CUSTOMER_SETTLEMENTS_EXPECTED_DATABASE_NAME=settlements_stage
ONEC_DATABASE_URL=mssql+pyodbc://<readonly-user>:<password>@<t13-host>/<ut-database>

CUSTOMER_SETTLEMENTS_ENABLED=false
CUSTOMER_SETTLEMENTS_ELIGIBILITY_ENABLED=false
CUSTOMER_SETTLEMENTS_SHADOW_ENABLED=true
CUSTOMER_SETTLEMENTS_SOURCE_VALIDATED=false
CUSTOMER_SETTLEMENTS_ORGANIZATION_REF=0xb34a0025901e48ef11e211128227ea80
CUSTOMER_SETTLEMENTS_ORGANIZATION_GUID=8227ea80-1112-11e2-b34a-0025901e48ef
CUSTOMER_SETTLEMENTS_OPENING_ORGANIZATION_FIELD=_Fld7005RRef
CUSTOMER_SETTLEMENTS_MOVEMENT_ORGANIZATION_FIELD=_Fld7005RRef
CUSTOMER_SETTLEMENTS_COUNTERPARTY_INN_FIELD=_Fld611
CUSTOMER_SETTLEMENTS_SOURCE_MODE=onec_canonical_mutual_statement_7002
CUSTOMER_SETTLEMENTS_MAPPING_MODE=crm_readonly
CUSTOMER_SETTLEMENTS_CRM_WEBHOOK_URL=<existing-readonly-webhook-for-72h>

CUSTOMER_SETTLEMENTS_QUERY_TIMEOUT_SECONDS=30
CUSTOMER_SETTLEMENTS_CRM_TIMEOUT_SECONDS=6
CUSTOMER_SETTLEMENTS_STALE_AFTER_SECONDS=7200
CUSTOMER_SETTLEMENTS_HIDE_AFTER_SECONDS=21600
CUSTOMER_SETTLEMENTS_MAPPING_STALE_AFTER_SECONDS=7200
CUSTOMER_SETTLEMENTS_SUCCESS_RETENTION_DAYS=30
CUSTOMER_SETTLEMENTS_FAILED_RETENTION_DAYS=7
CUSTOMER_SETTLEMENTS_JTI_RETENTION_HOURS=24
CUSTOMER_SETTLEMENTS_JOB_TIMEOUT_SECONDS=90
CUSTOMER_SETTLEMENTS_MAPPING_JOB_TIMEOUT_SECONDS=360
CUSTOMER_SETTLEMENTS_RETRY_DELAY_SECONDS=600
CUSTOMER_SETTLEMENTS_ALERTS_ENABLED=false
CUSTOMER_SETTLEMENTS_ALERT_TASK_ID=2883
CUSTOMER_SETTLEMENTS_ALERT_WEBHOOK_URL=<existing-webhook-for-72h>
CUSTOMER_SETTLEMENTS_ALERT_REPEAT_SECONDS=21600
```

`ONEC_DATABASE_URL` использует только read-only доступ. Пароли и URL не выводить
в логи. Разрешённый webhook хранится только в защищённом staging secret-файле на
время запуска и удаляется из него после 72 часов. Запись в CRM запрещена; alert
может добавлять только обезличенный комментарий в задачу №2883.

## 2. Bootstrap preflight

Preflight выполняет только локальные проверки конфигурации и PostgreSQL. Он не
обращается к 1С и не выводит URL, user ID, customer account ID, counterparty GUID/ref
или суммы.

```bash
export REPO_DIR=/opt/MM/releases/pricing-service/<immutable-settlements-release>
export PYTHON_BIN="${REPO_DIR}/.venv/bin/python"
export CUSTOMER_SETTLEMENTS_ENV_FILE=/etc/pricing-service/customer-settlements-shadow.env

source "${REPO_DIR}/infra/cron/load_env.sh"
load_env_file_preserve_json "${CUSTOMER_SETTLEMENTS_ENV_FILE}"

cd "${REPO_DIR}"
"${PYTHON_BIN}" -m tasks.preflight_customer_settlement_shadow \
  --phase bootstrap \
  --expected-pilot-count 10
```

Дефицит сотруднических кабинетов устранён отбором `10/10`. Bootstrap запускать
после включения утверждённого whitelist, но до первого CRM mapping sync.
Допустимый результат перед первым sync:
`status=ready`, 10 пилотов,
ноль active revision и подтверждение fail-closed health. Любой failed check блокирует запуск.
Особенно недопустимы `CUSTOMER_SETTLEMENTS_ENABLED=true`, не-staging окружение,
другая БД, другая организация или mapping mode не `crm_readonly`. На bootstrap
`CUSTOMER_SETTLEMENTS_SOURCE_VALIDATED` обязан оставаться `false`, eligibility
также обязана быть выключена; прежний `manual_confirmed` preflight не проходит.
`CUSTOMER_SETTLEMENTS_EXPECTED_DATABASE_NAME` обязателен, загружается из того же
secret-файла и должен дословно совпасть с PostgreSQL `current_database()`.
Несовпадение блокирует preflight, worker, health и cleanup до любых изменений БД.

## 3. Первый ручной цикл

Whitelist содержит только утверждённые пилотные `site_user_id` и управляется
`tasks.manage_customer_settlement_pilot` с dry-run, `--apply` и readback. Точные ID
не помещаются в runbook или логи. После bootstrap выполнить полный CRM read,
сверку новой ведомости и только затем financial sync:

```bash
"${PYTHON_BIN}" -m tasks.sync_customer_settlement_mapping
"${PYTHON_BIN}" -m tasks.reconcile_customer_settlements \
  /secure/vedomost-<completed-date>.xlsx
# Только после status=matched вручную установить
# CUSTOMER_SETTLEMENTS_SOURCE_VALIDATED=true.
"${PYTHON_BIN}" -m tasks.sync_customer_settlements
"${PYTHON_BIN}" -m tasks.preflight_customer_settlement_shadow \
  --phase ready \
  --expected-pilot-count 10
```

Mapping worker обязан дважды полностью прочитать CRM и подтвердить одинаковые total
и семантическое содержимое всех страниц, затем разрешить hashes через read-only
`_Reference54` и активировать только 10 строк whitelist. Проверки только первой
страницы недостаточно. Отсутствующий пилот получает `not_linked`; несколько cluster
или counterparty дают `ambiguous` и блокируют ready gate. Email, телефон, ФИО,
название и ИНН не используются как ключ.

Reconciliation принимает ведомость только за один завершённый день. Технический
срез — строго `< 00:00:00` следующего дня по Москве, допуск `0,01 RUB`. Команда не
пишет в 1С и сохраняет лишь дату, безопасные hashes и агрегаты сверки. Результат
привязан к hash активной mapping revision, организации, режиму и полям SQL-источника,
точному набору контрагентов, прочитанному SQL-срезу и файлу ведомости. Изменение
пилота, mapping или настроек источника требует новой ведомости и новой полной
сверки: одинаковый файл не может повторно разрешить изменившийся контекст.
Перед сохранением reconciliation получает общий context lock и повторно сверяет
active mapping и pilot scope; financial worker под тем же lock повторно читает
последнюю reconciliation непосредственно перед активацией.
Историческая ведомость `2026-07-29` не заменяет ни одну из новых контрольных
ведомостей.

Financial sync должен вернуть ровно все уникальные контрагенты включённых пилотов,
включая явные нулевые строки.
`ready` требует:

- ровно одну свежую active mapping revision;
- ровно одну свежую active financial revision;
- 10 linked и 0 ambiguous пилотов;
- совместимую финансовую строку для каждого пилота;
- последнюю сохранённую сверку ведомости со статусом `matched` именно для текущего
  mapping/source context и полного текущего набора пилотов;
- совпадение expected/loaded и отсутствие зависших loading revision;
- `freshness_status=ok` и `mapping_status=ok`.

Если любой шаг вернул `blocked` или `error`, cron не устанавливать. Предыдущую active
revision не удалять.

После первого `ready` установить `CUSTOMER_SETTLEMENTS_ALERTS_ENABLED=true` и
повторно запустить health. Первый комментарий допустим только при warning/critical;
в №2883 не должны попасть суммы или идентификаторы.

## 4. Расписание 72 часов

Перед установкой cron зафиксировать clean commit и собрать из него отдельный
immutable staging release со своим hash-locked `.venv`. Settlement-обёртки
принимают отдельный secret-файл через `CUSTOMER_SETTLEMENTS_ENV_FILE`; production
`.env` не нужен.
Все обёртки сначала переходят в зафиксированный `REPO_DIR`, безопасно загружают
env, требуют ожидаемое имя БД и запускают job с внешним timeout. Mapping имеет
отдельный лимит 360 секунд для двух полных CRM-read и разрешения hash через 1С;
остальные jobs используют
общий лимит. Ошибка `cd`,
невалидное имя env-переменной, несовпадение БД или timeout завершают job ненулевым
кодом; checkpoint также ограничен тем же process timeout.

```cron
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
CRON_TZ=Europe/Moscow
REPO_DIR=/opt/MM/releases/pricing-service/customer-settlements-shadow-release
CUSTOMER_SETTLEMENTS_ENV_FILE=/etc/pricing-service/customer-settlements-shadow.env

5 * * * * root ${REPO_DIR}/infra/cron/customer_settlement_mapping_sync.sh >> /var/log/pricing-staging/customer_settlement_mapping_sync.log 2>&1
17 * * * * root ${REPO_DIR}/infra/cron/customer_settlement_financial_sync.sh >> /var/log/pricing-staging/customer_settlement_financial_sync.log 2>&1
35 * * * * root ${REPO_DIR}/infra/cron/customer_settlement_health.sh >> /var/log/pricing-staging/customer_settlement_health.log 2>&1
25 3 * * * root ${REPO_DIR}/infra/cron/customer_settlement_cleanup.sh >> /var/log/pricing-staging/customer_settlement_cleanup.log 2>&1
```

Tracked-файл `infra/cron/customer_settlements.cron` повторяет этот безопасный
шаблон именно для `/etc/cron.d` и намеренно указывает на отдельный immutable
release, а не на mutable-root. `PYTHON_BIN` не задаётся в crontab: обёртка выводит
его как `${REPO_DIR}/.venv/bin/python` и блокирует попытку переопределить путь через
secret-файл. До установки путь необходимо заменить на фактически собранный release
и проверить readback `REPO_DIR`, производного `PYTHON_BIN` и secret-файла.

Это шаблон, а не разрешение устанавливать cron. Установку выполнять отдельно только
после успешного ручного цикла. Каталог логов должен быть staging-отдельным и не
содержать секретов или сумм.

## 5. Контрольные точки

В момент старта, через 24, 48 и 72 часа выполнить:

```bash
"${PYTHON_BIN}" -m tasks.reconcile_customer_settlements \
  /secure/vedomost-<completed-date-for-this-checkpoint>.xlsx
"${PYTHON_BIN}" -m tasks.preflight_customer_settlement_shadow \
  --phase ready \
  --expected-pilot-count 10
"${PYTHON_BIN}" -m tasks.check_customer_settlement_health
```

Для каждой точки формируется новая ведомость за ближайший завершённый день; файл
предыдущей точки повторно не используется. SQL и отчёт обязаны иметь одну границу
`as_of`, допуск — `0,01 RUB`. В журнал контроля записывать только агрегаты:
expected/loaded/zero, возраст revision, duration, retry/timeout/lock и число
расхождений. Суммы и идентификаторы пилотов в cron-логи не писать.

Shadow-run принимается, если 72 часа:

- `CUSTOMER_SETTLEMENTS_ENABLED=false`;
- `CUSTOMER_SETTLEMENTS_ELIGIBILITY_ENABLED=false`;
- не было потери active revision или частичной активации;
- все четыре сверки дали расхождение не более `0,01 RUB`;
- нет critical security/data-quality ошибок;
- fault-проверки timeout, retry, lock и replay прошли;
- mapping все 72 часа обновлялся из полного CRM read, alerts публиковались только
  в №2883 и не содержали финансовых сумм/идентификаторов.

## 6. Остановка и rollback

При critical:

1. оставить `CUSTOMER_SETTLEMENTS_ENABLED=false`;
2. установить `CUSTOMER_SETTLEMENTS_SHADOW_ENABLED=false`;
3. удалить только staging cron-записи взаиморасчётов;
4. не удалять active/failed revision до разбора;
5. вернуть предыдущий clean staging commit;
6. удалить временные CRM/alert webhook из shadow secret-файла;
7. зафиксировать тип ошибки без секретов, сумм и идентификаторов.

Следующий этап после успешных 72 часов — отчёт, письменная приёмка бухгалтером и
отдельное разрешение пользователя на real-подключение dev-адаптера; production
`master-mobile.ru` остаётся неизменным.
