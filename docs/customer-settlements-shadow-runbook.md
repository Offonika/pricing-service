# Shadow-run взаиморасчётов на staging

Документ описывает только изолированный 72-часовой staging-запуск. Он не разрешает
изменения production, сайта `master-mobile.ru`, CRM или 1С. Все внешние обращения
в этом сценарии read-only, а клиентский API остаётся выключенным.

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

Пилотный mapping импортируется вручную из проверенного CSV. Bitrix24 webhook в
режиме `manual_confirmed` не нужен и не должен добавляться «на всякий случай».

## Release integration 2026-08-22

Settlement migrations объединены с фактически активным production-head
`1b9d3f5a7c21` новой no-op revision `2a4c6e8f0b1d`. Новый staging runtime должен
выполнять `alembic upgrade 2a4c6e8f0b1d`; прежняя пустая shadow-БД и runtime на
`d9e1f3a5b7c9` доказательством нового 72-часового запуска не являются.

## Активный shadow-run 2026-08-22

- старт: `2026-08-22 20:43 MSK`;
- clean commit: `4458c90469522c5acd430de5a2833a7fc84a9eb2`;
- staging DB: `settlements_shadow_20260822`, Alembic head `2a4c6e8f0b1d`;
- client API остаётся выключенным, shadow flag включён;
- manual mapping и whitelist: `10/10`, ambiguous: `0`;
- первый и повторный snapshots: expected/loaded `10/10`, explicit zero: `1`;
- первый `ready` preflight: `30/30`, health: `ok`;
- cron установлен только в staging-контуре; контрольные точки — через 24, 48 и
  72 часа от времени старта.

## 1. Отдельный secret-файл

Создать вне репозитория файл с правами `0600`. Не копировать целиком production
`.env` и не коммитить файл:

```dotenv
ENVIRONMENT=staging
DATABASE_URL=postgresql+psycopg2://settlements_stage:<password>@127.0.0.1:55439/settlements_stage
ONEC_DATABASE_URL=mssql+pyodbc://<readonly-user>:<password>@<t13-host>/<ut-database>

CUSTOMER_SETTLEMENTS_ENABLED=false
CUSTOMER_SETTLEMENTS_SHADOW_ENABLED=true
CUSTOMER_SETTLEMENTS_SOURCE_VALIDATED=true
CUSTOMER_SETTLEMENTS_ORGANIZATION_REF=0xb34a0025901e48ef11e211128227ea80
CUSTOMER_SETTLEMENTS_ORGANIZATION_GUID=8227ea80-1112-11e2-b34a-0025901e48ef
CUSTOMER_SETTLEMENTS_OPENING_ORGANIZATION_FIELD=_Fld7005RRef
CUSTOMER_SETTLEMENTS_MOVEMENT_ORGANIZATION_FIELD=_Fld7005RRef
CUSTOMER_SETTLEMENTS_COUNTERPARTY_INN_FIELD=_Fld611
CUSTOMER_SETTLEMENTS_SOURCE_MODE=onec_canonical_mutual_statement_7002
CUSTOMER_SETTLEMENTS_MAPPING_MODE=manual_confirmed

CUSTOMER_SETTLEMENTS_QUERY_TIMEOUT_SECONDS=30
CUSTOMER_SETTLEMENTS_CRM_TIMEOUT_SECONDS=6
CUSTOMER_SETTLEMENTS_STALE_AFTER_SECONDS=7200
CUSTOMER_SETTLEMENTS_HIDE_AFTER_SECONDS=21600
CUSTOMER_SETTLEMENTS_MAPPING_STALE_AFTER_SECONDS=7200
CUSTOMER_SETTLEMENTS_SUCCESS_RETENTION_DAYS=30
CUSTOMER_SETTLEMENTS_FAILED_RETENTION_DAYS=7
CUSTOMER_SETTLEMENTS_JTI_RETENTION_HOURS=24
CUSTOMER_SETTLEMENTS_JOB_TIMEOUT_SECONDS=90
CUSTOMER_SETTLEMENTS_RETRY_DELAY_SECONDS=600
```

`ONEC_DATABASE_URL` использует только read-only доступ. Пароли и URL не выводить
в логи. Режим `crm_readonly` остаётся совместимым, но не используется в этом пилоте.

## 2. Bootstrap preflight

Preflight выполняет только локальные проверки конфигурации и PostgreSQL. Он не
обращается к 1С и не выводит URL, user ID, customer account ID, counterparty GUID/ref
или суммы.

```bash
export REPO_DIR=/opt/MM/.worktrees/pricing-task-2883-customer-settlements-backend
export PYTHON_BIN=/opt/MM/pricing-service/.venv/bin/python
export CUSTOMER_SETTLEMENTS_ENV_FILE=/etc/pricing-service/customer-settlements-shadow.env

source "${REPO_DIR}/infra/cron/load_env.sh"
load_env_file_preserve_json "${CUSTOMER_SETTLEMENTS_ENV_FILE}"

cd "${REPO_DIR}"
"${PYTHON_BIN}" -m tasks.preflight_customer_settlement_shadow \
  --phase bootstrap \
  --expected-pilot-count 10
```

Дефицит сотруднических кабинетов устранён отбором `10/10`. Bootstrap запускать
только после отдельного разрешения на apply mapping и включения утверждённого
whitelist. Допустимый результат перед первым sync:
`status=ready`, 10 пилотов,
ноль active revision и подтверждение fail-closed health. Любой failed check блокирует запуск.
Особенно недопустимы `CUSTOMER_SETTLEMENTS_ENABLED=true`, не-staging окружение,
другая БД, другая организация или mapping mode вне утверждённого контура.

## 3. Первый ручной цикл

CSV содержит ровно семь колонок:
`site_user_id,counterparty_guid,organization_guid,source_system,expected_code,expected_name,expected_inn`.
Допускается не более 10 строк. В обычный вывод команды не попадают ID, GUID, названия,
ИНН или суммы — только количества и SHA-256 hashes.
`expected_inn` — совместимая необязательная колонка; пустое значение допустимо.

Выполнять только после успешного bootstrap preflight. Сначала обязательный dry-run,
затем отдельное применение с зафиксированным согласующим и точными SHA-256 из
успешного dry-run. Если CSV или live controls изменились, apply блокируется и
dry-run нужно повторить:

```bash
"${PYTHON_BIN}" -m tasks.import_customer_settlement_mappings /secure/pilot-mapping.csv
"${PYTHON_BIN}" -m tasks.import_customer_settlement_mappings /secure/pilot-mapping.csv \
  --apply \
  --approved-by '<role-or-ticket>' \
  --approved-input-hash '<input_hash-from-dry-run>' \
  --approved-controls-hash '<controls_hash-from-dry-run>'
"${PYTHON_BIN}" -m tasks.sync_customer_settlement_mapping
"${PYTHON_BIN}" -m tasks.sync_customer_settlements
"${PYTHON_BIN}" -m tasks.preflight_customer_settlement_shadow \
  --phase ready \
  --expected-pilot-count 10
```

Importer обязательно сверяет GUID, организацию, код и название с live read-only УТ,
ИНН — только если он передан, а также блокирует контрагента при любом активном
договоре не в `643/RUB`. Whitelist включается
отдельно через `tasks.manage_customer_settlement_pilot`. Mapping sync в ручном режиме
только проверяет наличие active `manual_confirmed_pilot` и не перезаписывает его.
Financial sync должен вернуть ровно все уникальные контрагенты включённых пилотов,
включая явные нулевые строки.
`ready` требует:

- ровно одну свежую active mapping revision;
- ровно одну свежую active financial revision;
- 10 linked и 0 ambiguous пилотов;
- совместимую финансовую строку для каждого пилота;
- совпадение expected/loaded и отсутствие зависших loading revision;
- `freshness_status=ok` и `mapping_status=ok`.

Если любой шаг вернул `blocked` или `error`, cron не устанавливать. Предыдущую active
revision не удалять.

## 4. Расписание 72 часов

Перед установкой cron зафиксировать clean commit и использовать один выделенный
staging checkout. Settlement-обёртки принимают отдельный secret-файл через
`CUSTOMER_SETTLEMENTS_ENV_FILE`; production `.env` не нужен.

```cron
CRON_TZ=Europe/Moscow
REPO_DIR=/opt/MM/.worktrees/pricing-task-2883-customer-settlements-backend
PYTHON_BIN=/opt/MM/pricing-service/.venv/bin/python
CUSTOMER_SETTLEMENTS_ENV_FILE=/etc/pricing-service/customer-settlements-shadow.env

5 * * * * ${REPO_DIR}/infra/cron/customer_settlement_mapping_sync.sh >> /var/log/pricing-staging/customer_settlement_mapping_sync.log 2>&1
17 * * * * ${REPO_DIR}/infra/cron/customer_settlement_financial_sync.sh >> /var/log/pricing-staging/customer_settlement_financial_sync.log 2>&1
35 * * * * ${REPO_DIR}/infra/cron/customer_settlement_health.sh >> /var/log/pricing-staging/customer_settlement_health.log 2>&1
25 3 * * * ${REPO_DIR}/infra/cron/customer_settlement_cleanup.sh >> /var/log/pricing-staging/customer_settlement_cleanup.log 2>&1
```

Это шаблон, а не разрешение устанавливать cron. Установку выполнять отдельно только
после успешного ручного цикла. Каталог логов должен быть staging-отдельным и не
содержать секретов или сумм.

## 5. Контрольные точки

В момент старта, через 24, 48 и 72 часа выполнить:

```bash
"${PYTHON_BIN}" -m tasks.preflight_customer_settlement_shadow \
  --phase ready \
  --expected-pilot-count 10
"${PYTHON_BIN}" -m tasks.check_customer_settlement_health
```

На каждой точке дополнительно сверить 10 пилотов с ведомостью 1С на одинаковый
`as_of`. Допуск — `0,01 RUB`. В журнал контроля записывать только агрегаты:
expected/loaded/zero, возраст revision, duration, retry/timeout/lock и число
расхождений. Суммы и идентификаторы пилотов в cron-логи не писать.

Shadow-run принимается, если 72 часа:

- `CUSTOMER_SETTLEMENTS_ENABLED=false`;
- не было потери active revision или частичной активации;
- все четыре сверки дали расхождение не более `0,01 RUB`;
- нет critical security/data-quality ошибок;
- fault-проверки timeout, retry, lock и replay прошли.

## 6. Остановка и rollback

При critical:

1. оставить `CUSTOMER_SETTLEMENTS_ENABLED=false`;
2. установить `CUSTOMER_SETTLEMENTS_SHADOW_ENABLED=false`;
3. удалить только staging cron-записи взаиморасчётов;
4. не удалять active/failed revision до разбора;
5. вернуть предыдущий clean staging commit;
6. зафиксировать тип ошибки без секретов, сумм и идентификаторов.

Следующий этап после успешных 72 часов — отчёт, письменная приёмка бухгалтером и
отдельное разрешение пользователя на server-side адаптер сайта.
