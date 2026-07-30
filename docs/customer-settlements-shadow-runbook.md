# Shadow-run взаиморасчётов на staging

Документ описывает только изолированный 72-часовой staging-запуск. Он не разрешает
изменения production, сайта `master-mobile.ru`, CRM или 1С. Все внешние обращения
в этом сценарии read-only, а клиентский API остаётся выключенным.

## Подтверждённая база запуска

На 2026-07-30 выполнены:

- Alembic `upgrade -> downgrade -> upgrade` до `c3d4e5f6a7b9` на отдельном
  PostgreSQL `settlements_stage`;
- PostgreSQL integration suite: `5 passed`;
- бухгалтерская сверка на конец 2026-07-29 по организации `MASTER MOBILE`:
  `10/10`, максимальное расхождение `0,00 RUB`;
- staging whitelist: 10 включённых пилотов;
- исходное состояние: нет active mapping/financial revision и финансовых строк;
  health до первого sync ожидаемо `critical`.

Для запуска всё ещё нужен отдельный staging Bitrix24 webhook с read-only правами.
Одноразовый webhook из `mm-compensation` повторно использовать запрещено.

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
CUSTOMER_SETTLEMENTS_OPENING_ORGANIZATION_FIELD=_Fld7005RRef
CUSTOMER_SETTLEMENTS_MOVEMENT_ORGANIZATION_FIELD=_Fld7005RRef
CUSTOMER_SETTLEMENTS_SOURCE_MODE=onec_canonical_mutual_statement_7002
CUSTOMER_SETTLEMENTS_CRM_WEBHOOK_URL=https://<staging-portal>/rest/<readonly-user>/<token>

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

Webhook должен принадлежать staging-контуру, иметь только нужные CRM-read методы и
не использоваться из `mm-compensation`. Пароли и URL не выводить в логи.

## 2. Bootstrap preflight

Preflight выполняет только локальные проверки конфигурации и PostgreSQL. Он не
обращается к CRM или 1С и не выводит URL, client ID, cluster ID, counterparty ref
или суммы.

```bash
export REPO_DIR=/opt/MM/.worktrees/pricing-customer-settlements-backend-v1
export PYTHON_BIN=/opt/MM/pricing-service/.venv/bin/python
export CUSTOMER_SETTLEMENTS_ENV_FILE=/etc/pricing-service/customer-settlements-shadow.env

source "${REPO_DIR}/infra/cron/load_env.sh"
load_env_file_preserve_json "${CUSTOMER_SETTLEMENTS_ENV_FILE}"

cd "${REPO_DIR}"
"${PYTHON_BIN}" -m tasks.preflight_customer_settlement_shadow \
  --phase bootstrap \
  --expected-pilot-count 10
```

Допустимый результат перед первым sync: `status=ready`, 10 пилотов, ноль active
revision и подтверждение fail-closed health. Любой failed check блокирует запуск.
Особенно недопустимы `CUSTOMER_SETTLEMENTS_ENABLED=true`, не-staging окружение,
другая БД, другая организация или отсутствующий отдельный CRM webhook.

## 3. Первый ручной цикл

Выполнять только после успешного bootstrap preflight:

```bash
"${PYTHON_BIN}" -m tasks.sync_customer_settlement_mapping
"${PYTHON_BIN}" -m tasks.sync_customer_settlements
"${PYTHON_BIN}" -m tasks.preflight_customer_settlement_shadow \
  --phase ready \
  --expected-pilot-count 10
```

Mapping sync должен завершить полную пагинацию CRM. Financial sync должен вернуть
ровно все уникальные контрагенты активных пилотов, включая явные нулевые строки.
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
REPO_DIR=/opt/MM/.worktrees/pricing-customer-settlements-backend-v1
PYTHON_BIN=/opt/MM/pricing-service/.venv/bin/python
CUSTOMER_SETTLEMENTS_ENV_FILE=/etc/pricing-service/customer-settlements-shadow.env

5 * * * * ${REPO_DIR}/infra/cron/customer_settlement_mapping_sync.sh >> /var/log/pricing-staging/customer_settlement_mapping_sync.log 2>&1
12 * * * * ${REPO_DIR}/infra/cron/customer_settlement_financial_sync.sh >> /var/log/pricing-staging/customer_settlement_financial_sync.log 2>&1
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
