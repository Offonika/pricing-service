---
spec_id: "customer-settlements-backend-v1"
title: "Customer Account Settlements Backend V1"
doc_type: spec
domain: receivables
status: accepted
owner: engineering
source_of_truth: true
related_code:
  - app/api/customer_settlements.py
  - app/models/customer_settlement.py
  - app/schemas/customer_settlement.py
  - app/services/customer_settlement_auth.py
  - app/services/customer_settlement_alerts.py
  - app/services/customer_settlement_mapping.py
  - app/services/customer_settlement_reconciliation.py
  - app/services/customer_settlement_source.py
  - app/services/customer_settlements.py
  - app/workers/customer_settlements.py
  - tasks/check_customer_settlement_health.py
  - tasks/cleanup_customer_settlements.py
  - tasks/import_customer_settlement_mappings.py
  - tasks/manage_customer_settlement_pilot.py
  - tasks/mock_customer_settlement_client.py
  - tasks/preflight_customer_settlement_shadow.py
  - tasks/reconcile_customer_settlements.py
  - tasks/sync_customer_settlement_mapping.py
  - tasks/sync_customer_settlements.py
  - infra/cron/customer_settlements.cron
  - alembic/versions/c3d4e5f6a7b9_add_customer_settlements.py
  - alembic/versions/d9e1f3a5b7c9_add_customer_account_guid_mapping.py
  - alembic/versions/4c6e8a0b2d3f_add_settlement_reconciliation_alerts.py
  - alembic/versions/6e8f0a2b4c6d_bind_settlement_reconciliation_context.py
  - integrations/master_mobile_site/customer_settlements
related_tests:
  - tests/test_customer_settlement_api.py
  - tests/test_customer_settlement_auth.py
  - tests/test_customer_settlement_mapping.py
  - tests/test_customer_settlement_migration.py
  - tests/test_customer_settlement_postgres.py
  - tests/test_customer_settlement_reconciliation_alerts.py
  - tests/test_customer_settlement_shadow_preflight.py
  - tests/test_customer_settlement_source.py
  - tests/test_customer_settlements.py
  - tests/test_import_customer_settlement_mappings.py
contracts:
  - openapi.yaml
depends_on:
  - docs/BI.Receivables.md
supersedes: []
rollout_required: true
updated_at: "2026-08-24"
---

# Назначение

Дать пилотным клиентам интернет-магазина read-only итог взаиморасчётов из
текущей `УТ 10.3`: долг, аванс или нулевой баланс. Backend скрывает внутренние
идентификаторы 1С, не доверяет параметрам браузера и продолжает отдавать
последний целостный срез при частичной ошибке очередного обновления.

# Scope / Out of Scope

Входит:

- отдельный почасовой snapshot-контур, не связанный с дневной витриной дебиторки;
- одна организация и только `RUB`;
- одна итоговая сумма по контрагенту без договоров;
- постоянный `customer_account_id` и GUID mapping
  `site_user_id -> customer_account_id -> source_system + CounterpartyGuid`;
- полный read-only CRM mapping, активируемый только для pilot whitelist;
- исторический ручной importer как rollback-инструмент, но не источник нового пилота;
- отдельный pilot whitelist;
- HMAC assertion между сервером сайта и `pricing-service`;
- replay-защита, key rotation, retention, advisory locks и health probe;
- автоматическая сверка новой ведомости на конец дня по Москве и безопасный alert outbox;
- server-side dev-адаптер личного кабинета с отдельным eligibility endpoint;
- OpenAPI, synthetic tests, тестовый вектор и безопасный mock-клиент.

Не входит:

- изменение `УТ 10.3`, CRM, production или сервера `master-mobile.ru`;
- установка адаптера на production `master-mobile.ru`;
- автоматическая связь по email, телефону, ИНН или названию;
- вход по телефону — он остаётся отдельной задачей `#2533`;
- несколько организаций, валют или контрагентов 1С в одном пилотном cluster.
- автоматическое создание пользователей сайта или включение whitelist.

# Change Summary / Spec Delta

- Было: личный кабинет не имел безопасного backend-контракта взаиморасчётов.
- Стало: `pricing-service` хранит атомарные почасовые revision и отдаёт
  серверу сайта только состояние текущего пользователя через постоянный
  customer account и активную GUID-связь.
- Не меняется: `1С` остаётся системой учёта; клиент не может менять данные.

# Acceptance Criteria

- [x] Нулевой остаток хранится явной строкой и возвращается как `zero`.
- [x] Частичный financial или mapping snapshot не заменяет активный.
- [x] Browser не передаёт `site_user_id`, cluster или `counterparty_ref`.
- [x] Mapping с несколькими cluster/counterparty закрывается как ambiguous.
- [x] Сумма видна до 6 часов, с 2 до 6 часов помечается stale.
- [x] После 6 часов API возвращает только `temporarily_unavailable`, без суммы,
  state и дат финансового среза.
- [x] Assertion живёт не более 60 секунд и имеет одноразовый `jti`.
- [x] Assertion требует отдельный scope `customer:settlements:read`.
- [x] Ответы API, включая ошибки авторизации, имеют `private, no-store`.
- [x] Retention не удаляет активные revision.
- [x] SQL не использует `NOLOCK`, принимает точный `as_of` и выбирает `< as_of`.
- [x] Live extractor закрыт отдельным флагом бухгалтерской сверки источника.
- [x] Ручной импорт по умолчанию работает как dry-run, ограничен 10 строками,
  требует `--approved-by` и оба SHA-256 из dry-run для apply, блокирует изменение
  CSV/controls между проверкой и применением, а также несовпадение controls/non-RUB.
- [x] Смена `CounterpartyGuid` сохраняет `customer_account_id`, а конфликт двух
  customer accounts и старый financial snapshot закрываются fail-closed.
- [x] PostgreSQL integration проверяет partial unique index, atomic rollback,
  advisory lock, конкурентный replay, `SKIP LOCKED` alert outbox и retention.
- [x] Исходная SQL-сверка выполнена на 10 реальных пилотах на конец
  `2026-07-29`: максимальная разница с ведомостью `0,00 RUB`. Результат остаётся
  доказательством extractor, но сотруднический пилот сверяется отдельно.
- [x] ОТМЕНЕНО (2026-08-11): для нового shadow-run была отобрана кандидатная
  десятка внешних клиентов с валидным ИНН; mapping/whitelist не применялись.
- [x] Отобраны 10 действующих сотрудников с точной проверяемой связью
  `Bitrix24 employee -> site user -> CRM cluster -> УТ counterparty`;
  mapping/whitelist ещё не применены.
- [x] Финальный importer dry-run проверил `10/10`, включая явный нулевой остаток;
  все settlement-таблицы после rollback остались пустыми.
- [x] ОТМЕНЕНО (2026-08-22): shadow-run, начатый в `20:43 MSK` на manual mapping,
  не засчитывается; cron остановлен, БД и revisions сохранены для диагностики.
- [ ] Пройден новый 72-часовой shadow-run на `crm_readonly` и письменная
  бухгалтерская приёмка по новым ведомостям.
- [x] Server-side mock-адаптер разрешён и подготовлен только для
  `dev.master-mobile.ru`; production не изменяется.

# Source of Truth

- `УТ 10.3` — источник истины по сумме взаиморасчётов.
- Согласованный бухгалтерский отчёт — эталон живой сверки SQL.
- Полностью прочитанный CRM cluster с service fields и точными Bitrix–1С hashes —
  источник связи нового пилота; ФИО, email, телефон, ИНН и название ключами не являются.
- Ручной CSV остаётся историческим rollback-инструментом и не активируется в новом запуске.
- PostgreSQL `pricing-service` — источник активных revision, whitelist и replay-state.
- Production Bitrix/PHP не изменяются; mock-адаптер на `dev.master-mobile.ru`
  использует тот же API-контракт, но не является финансовым source of truth.

# Data Flow

```text
full CRM read -> pilot whitelist filter -> durable account/GUID mapping -> atomic activate
                                                                     \
separate pilot whitelist -> УТ 10.3 (:17) -> financial revision -> atomic activate
                                                       \
Bitrix $USER session -> eligibility/summary assertion -> server-rendered block
```

- `:05` — полный CRM read и атомарная `crm_readonly` revision только по whitelist;
- `:17` — полный финансовый срез всех уникальных пилотных контрагентов;
- при реальной ошибке — один повтор через 600 секунд;
- `:35` — health probe и обезличенный transition alert только в Bitrix24 №2883;
- `03:25` — retention cleanup.

Cron-файлы являются deploy-артефактами. Их установка в production этим spec не
разрешается.

# API / Data Contracts

## Summary

```text
GET /api/customer/settlements/summary
Authorization: Bearer <server-generated assertion>
```

Query-параметров выбора клиента нет.

Пользовательские статусы:

- `available`;
- `stale`;
- `temporarily_unavailable`;
- `not_linked`;
- `ambiguous_link`;
- `pilot_disabled`.

Для `available/stale`:

```json
{
  "status": "available",
  "state": "debt",
  "amount": "14800.00",
  "currency": "RUB",
  "as_of": "2026-07-29T11:30:00Z",
  "synced_at": "2026-07-29T11:34:12Z",
  "is_stale": false
}
```

`amount` всегда неотрицателен и сериализуется строкой с двумя знаками.
`signed_balance > 0` — `debt`, `< 0` — `advance`, `= 0` — `zero`.

Все ответы должны содержать:

```text
Cache-Control: private, no-store
Pragma: no-cache
```

Сервер Bitrix показывает одинаковое безопасное сообщение для `not_linked` и
`ambiguous_link`; различие остаётся доступно только в защищённой диагностике.

## Eligibility

```text
GET /api/customer/settlements/eligibility
Authorization: Bearer <server-generated assertion>
```

Endpoint не принимает query-параметров и возвращает только `eligible`,
`not_eligible` либо `temporarily_unavailable`. Он проверяет pilot whitelist,
active account/source binding и свежую CRM mapping revision, но не раскрывает
сумму, GUID или точную причину отсутствия связи. PHP хранит результат только в
текущей пользовательской сессии не более 5 минут; общий component/composite cache
запрещён.

## Assertion

Header:

```json
{"alg":"HS256","typ":"MM-CUSTOMER-SETTLEMENTS","kid":"<active-kid>"}
```

Claims:

```json
{
  "iss": "master-mobile.ru",
  "aud": "pricing-service:customer-settlements",
  "sub": "12345",
  "site_user_id": "12345",
  "scope": "customer:settlements:read",
  "iat": 1785301200,
  "nbf": 1785301200,
  "exp": 1785301260,
  "jti": "contract_vector_20260729"
}
```

Инварианты:

- `sub == site_user_id`, ID — положительная десятичная строка;
- `kid`, `sub`, `site_user_id` и `jti` принимаются только как JSON-строки;
- compact assertion использует только непустые unpadded base64url-сегменты, а
  HS256-подпись имеет ровно 43 base64url-символа;
- `iss=master-mobile.ru` и `aud=pricing-service:customer-settlements` неизменяемы;
- `scope` должен в точности равняться `customer:settlements:read`;
- `1 <= exp - iat <= 60`;
- `iat <= nbf < exp`;
- clock skew не больше 30 секунд;
- `jti` принимается один раз и хранится только как SHA-256;
- принимаются active и previous `kid`, но они должны различаться;
- каждый HMAC secret содержит не менее 24 UTF-8 байт, active/previous secrets
  различаются и не имеют начальных/конечных пробелов;
- запрос дополнительно ограничен настроенным IP/CIDR сервера сайта.

### Детерминированный тестовый вектор

Это публичный synthetic vector, не production-секрет:

```text
secret = synthetic-contract-secret-v1
kid = settlements-test-1
iat = nbf = 1785301200 (2026-07-29T05:00:00Z)
exp = 1785301260 (2026-07-29T05:01:00Z)
site_user_id = 12345
scope = customer:settlements:read
jti = contract_vector_20260729
```

Ожидаемый compact token:

```text
eyJhbGciOiJIUzI1NiIsInR5cCI6Ik1NLUNVU1RPTUVSLVNFVFRMRU1FTlRTIiwia2lkIjoic2V0dGxlbWVudHMtdGVzdC0xIn0.eyJpc3MiOiJtYXN0ZXItbW9iaWxlLnJ1IiwiYXVkIjoicHJpY2luZy1zZXJ2aWNlOmN1c3RvbWVyLXNldHRsZW1lbnRzIiwic3ViIjoiMTIzNDUiLCJzaXRlX3VzZXJfaWQiOiIxMjM0NSIsInNjb3BlIjoiY3VzdG9tZXI6c2V0dGxlbWVudHM6cmVhZCIsImlhdCI6MTc4NTMwMTIwMCwibmJmIjoxNzg1MzAxMjAwLCJleHAiOjE3ODUzMDEyNjAsImp0aSI6ImNvbnRyYWN0X3ZlY3Rvcl8yMDI2MDcyOSJ9.9wNCjm02BBxwqiZln4bE2klctnn4zEA_6QBWfrlfYcw
```

Vector закреплён regression-тестом
`tests/test_customer_settlement_auth.py`.

## PHP server-side outline

Это контракт, а не разрешение менять сайт:

```php
<?php
$siteUserId = (string)$USER->GetID();
$now = time();
$header = ["alg" => "HS256", "typ" => "MM-CUSTOMER-SETTLEMENTS", "kid" => $activeKid];
$payload = [
    "iss" => "master-mobile.ru",
    "aud" => "pricing-service:customer-settlements",
    "sub" => $siteUserId,
    "site_user_id" => $siteUserId,
    "scope" => "customer:settlements:read",
    "iat" => $now,
    "nbf" => $now,
    "exp" => $now + 60,
    "jti" => rtrim(strtr(base64_encode(random_bytes(24)), "+/", "-_"), "="),
];
$b64url = static fn(string $raw): string =>
    rtrim(strtr(base64_encode($raw), "+/", "-_"), "=");
$head = $b64url(json_encode($header, JSON_UNESCAPED_SLASHES));
$body = $b64url(json_encode($payload, JSON_UNESCAPED_SLASHES));
$input = $head . "." . $body;
$signature = $b64url(hash_hmac("sha256", $input, $secret, true));
$assertion = $input . "." . $signature;
```

Assertion передаётся только в server-to-server `Authorization` header и никогда
не попадает в HTML/JavaScript. HTTP-клиент: connect timeout 2 секунды, общий
timeout 3 секунды.

Для локальной contract-проверки:

```bash
python -m tasks.mock_customer_settlement_client --site-user-id 12345
python -m tasks.mock_customer_settlement_client \
  --site-user-id 12345 \
  --base-url http://127.0.0.1:8000 \
  --send
```

Mock-клиент не печатает assertion, `site_user_id` или финансовую сумму.

# Storage and revision lifecycle

Таблицы:

- `customer_account`;
- `customer_account_site_binding`;
- `customer_account_source_binding`;
- `customer_settlement_revision`;
- `customer_settlement_balance`;
- `customer_settlement_mapping_revision`;
- `customer_settlement_mapping_entry`;
- `customer_settlement_pilot_access`;
- `customer_settlement_assertion_jti`;
- `customer_settlement_reconciliation_run`;
- `customer_settlement_alert_state`;
- `customer_settlement_alert_outbox`.

Внутренние статусы revision: `loading`, `active`, `superseded`, `failed`.
`superseded` нужен для retention старых успешных срезов; одновременно активна
только одна financial и одна mapping revision.

`customer_account_id` — внутренний постоянный идентификатор кабинета. Активная
site-связь определяет пользователя, активная source-связь — `source_system`,
`CounterpartyGuid`, технический ref организации и контрольный hash. При подтверждённой
смене GUID старая source-связь отзывается, новая создаётся для того же account.
Если site user и новый GUID уже принадлежат разным account, импорт откатывается целиком.
Если общий account использовали несколько site users, remap одного пользователя
на другой GUID безопасно отделяет его в новый account и не меняет связь остальных.
Удаление пользователя из linked mapping, `not_linked` или `ambiguous` отзывает его
активную site-связь; отозванная связь не участвует в API, financial scope и health.

## Manual confirmed mapping — исторический rollback

ОТМЕНЕНО ДЛЯ НОВОГО ПИЛОТА (2026-08-22): режим `manual_confirmed` и применённая
им десятка не используются для нового зачётного shadow-run. Раздел ниже сохраняет
проверяемый rollback-процесс, но активный источник нового запуска — `crm_readonly`.

Для исторического manual-пилота использовались ровно 10 сотрудников с пользовательским
кабинетом и однозначной связью через точный идентификатор Bitrix–1С. Внешние
клиенты в пилот не включаются. ИНН не участвует в установлении связи и может
использоваться только как необязательный дополнительный контроль.

Действующий сотрудник определяется по структуре Bitrix24: `user.get`, `ACTIVE=Y`,
`USER_TYPE=employee` и заполненный `UF_DEPARTMENT`. УТ остаётся источником карточки
контрагента и взаиморасчётов, но не кадрового статуса. ОТМЕНЕНО (2026-08-13):
использовать кадровую ветку УТ как обязательный признак действующего сотрудника.

Решением владельца пилота от `2026-08-13` в десятку включён Арсений Кештов.
Его связь с действующей карточкой УТ `РБ0000044` подтверждена не совпадением ФИО,
а двумя заказами его сайта, которые в УТ ссылаются на одного и того же
контрагента. Карточка-дубль без заказов не используется. Арсений заменил
Владимира Шаповалова. Точный список и идентификаторы хранятся только в защищённом
локальном pilot CSV с правами `0600`.

ОТМЕНЕНО (2026-08-11): обязательное наличие валидного ИНН у каждого пилота и
блокировка pilot mapping только из-за отсутствующего ИНН. Причина — связь уже
задаётся точным идентификатором Bitrix–1С, а у сотрудников ИНН может отсутствовать.

Канонический CSV:

```text
site_user_id,counterparty_guid,organization_guid,source_system,expected_code,expected_name,expected_inn
```

`expected_inn` сохраняется как совместимая необязательная колонка и не является
ключом либо обязательным условием допуска сотрудника в пилот.

- максимум 10 строк, dry-run по умолчанию;
- apply разрешён только с `--apply --approved-by`, `--approved-input-hash` и
  `--approved-controls-hash`; оба SHA-256 должны совпасть с текущими CSV и live
  controls;
- `approved-by` не сохраняется и не выводится: audit использует SHA-256 с отдельной
  `CUSTOMER_SETTLEMENTS_CORRELATION_SALT`, отсутствие соли блокирует apply;
- ошибка соединения во время commit возвращает `mapping_commit_state_unknown`, а не
  ложное утверждение об откате; повтор с теми же hashes выполняет безопасный readback;
- УТ/QWE читается без записи и обязательно проверяет существование
  организации/контрагента, GUID↔ref, код, название и отсутствие активных договоров
  не в `643/RUB`; ИНН сверяется только при наличии;
- обычный JSON-вывод содержит только counts, boolean-признаки и SHA-256 hashes;
- активируется `source_name=manual_confirmed_pilot`;
- pilot whitelist включается отдельной командой и не является side effect importer.

Financial revision активируется только при полном совпадении expected/loaded
контрагентов, отсутствии дублей, `RUB`, валидной организации и явной строке
каждого нулевого баланса. Активация и перевод старой revision в `superseded`
происходят в одной PostgreSQL-транзакции.

Retention:

- successful/superseded — 30 дней;
- `failed/loading` — 7 дней;
- replay `jti` — до `exp + 24 часа`;
- reconciliation runs и отправленные/pending alert events — 30 дней; последняя
  reconciliation не удаляется отдельно от более старой цепочки: если её срок истёк,
  cleanup удаляет и все предыдущие runs, чтобы откат системных часов не мог вернуть
  более старый допуск;
- failed alert events — 7 дней, alert state сохраняется;
- active revision никогда не удаляется.
- cleanup перед любым запросом требует точные параметры `30/7/24`; иной env
  блокирует job и не может преждевременно удалить replay-маркеры либо историю.

# Extractor readiness gate

Extractor использует `_AccumRgT7009/_AccumRg7002` только как проверяемую основу:

- точный `as_of`, движения строго `< as_of`;
- `SYSUTCDATETIME()` SQL Server с преобразованием UTC в `Europe/Moscow` для
  границы регистра; локальные часы SQL host не определяют `as_of`;
- whitelist через параметризованную `#CustomerSettlementPilot`;
- `SNAPSHOT`, если разрешён, иначе `READ COMMITTED`;
- `source_db_time` читается из SQL Server уже внутри той же основной транзакции,
  которая формирует финансовый срез, после установки isolation level;
- `LOCK_TIMEOUT <= 30s`, без `NOLOCK`; для `pyodbc` отдельно задаётся
  `cursor.timeout`, а для `pytds` раздельно задаются query/login timeout;
- `COALESCE(..., 0)` для явного нуля;
- отсутствующий или помеченный контрагент блокирует revision.

Даже при заполненных именах полей worker не стартует, пока
`CUSTOMER_SETTLEMENTS_SOURCE_VALIDATED=true`. Этот флаг разрешается установить
только после проверки измерения организации, `_Fld7008`, `_RecordKind`,
начальных итогов и сверки с бухгалтерским отчётом.

## Live SQL validation 2026-07-30

Read-only диагностика базы `Ekama` подтвердила:

- единственная организация в актуальном контуре — `MASTER MOBILE`,
  код `РБ0000003`, ref `0xb34a0025901e48ef11e211128227ea80`;
- организация — `_Fld7005RRef` в opening и movements;
- договор — `_Fld7003RRef -> _Reference37`;
- контрагент — `_Fld7006RRef -> _Reference54`;
- рублёвый ресурс — `_Fld7008`;
- знак — `_RecordKind = 0` плюс, `_RecordKind = 1` минус;
- SQL Server не поддерживает snapshot isolation, поэтому extractor использует
  `READ COMMITTED`;
- monthly opening на `2026-06-01` плюс движения строго до `2026-07-01`
  совпал с opening на `2026-07-01` по всем `10 879` контрагентам без
  расхождений;
- срез на `2026-07-30 11:00:00Z` сформировал `11 130` строк, включая `1 762`
  явных нулевых результата;
- live smoke на трёх непомеченных контрагентах подтвердил `debt`, `advance`
  и `zero`, включая явную нулевую строку.

В полном техническом наборе обнаружены `10` ссылок без действующей записи
контрагента и `23` помеченные ссылки. Это не допускает автоматический выбор
пилотов из движений: каждый pilot mapping обязан пройти существующую проверку
`exists/marked_deleted`.

Live smoke также выявил две совместимости, закрытые regression-тестами:

- hex-параметр сначала приводится к `varchar(34)`, затем к `binary(16)`;
- isolation level задаётся SQL-командой, а не через несовместимый с текущим
  `sqlalchemy-pytds` вызов `execution_options`.

Исторический live dry-run отменённого набора внешних клиентов от `2026-08-11`
дополнительно подтвердил техническую схему УТ:

- `_Reference66` в этой УТ не имеет поля `_Folder`; организация проверяется по
  единственности записи и `_Marked`;
- `_Reference54._Folder = 0x01` означает элемент-контрагент, `0x00` — группу;
- ОТМЕНЕНО (2026-08-11): кандидатная десятка внешних клиентов прошла сверку
  GUID↔ref, кода, названия, ИНН, организации и валют активных договоров, но не
  активировалась и не используется в пилоте;
- dry-run откатил транзакцию: mapping, whitelist и financial revision в чистой
  shadow-БД не появились.

Readiness gate нового пилотного набора остаётся закрытым до apply проверенного
mapping/whitelist и независимой сверки всех 10 сотрудников с
`Ведомостью по взаиморасчётам с контрагентами` на одинаковый `as_of`.

## Employee pilot validation 2026-08-11 — superseded

Полное read-only пересечение CRM/Bitrix mapping и кадровых иерархий УТ подтвердило:

- прочитаны все `50 035` CRM-строк;
- `34` действующих employee-контрагента имеют допустимые RUB-договоры;
- только `8` из них имеют однозначную точную связь с пользовательским кабинетом;
- дополнительные пользователи на тех же восьми контрагентах отсутствуют;
- все восемь остатков имеют состояние `debt`, вариантов `advance/zero` нет;
- CSV с пустым `expected_inn` прошёл live importer dry-run `8/8`;
- транзакция откатилась, все settlement-счётчики чистой shadow-БД остались нулевыми.

Цель 10 сотрудников не достигнута, поэтому mapping/whitelist не активируются.
Автоматически добавлять бывших сотрудников или внешних клиентов запрещено. Нужен
отдельный выбор: пилот из 8 сотрудников либо создание двух тестовых кабинетов.

ОТМЕНЕНО (2026-08-13): вывод о доступных только восьми сотрудниках и варианты
пилота `8/8`/создания тестовых кабинетов. Полное чтение структуры Bitrix24 и CRM
дало 15 действующих сотрудников с точным mapping и допустимым контрагентом УТ.

## Employee pilot validation 2026-08-13

- полностью прочитаны `31` подразделение, `97` действующих сотрудников и
  `50 035` CRM mapping-строк;
- финальная десятка прошла точную проверку `Bitrix24 employee -> site user ->
  CRM cluster -> УТ counterparty`, кроме отдельно доказанной через два заказа
  связи Арсения Кештова с `РБ0000044`;
- выявленная строка `Бирюков Сергей -> Асатрян Гагик` исключена как ошибочный CRM
  mapping; безопасной заменой выбран Эльвин Байрамов с совпадающей идентичностью
  во всех трёх источниках;
- текущие состояния десятки: `7 debt / 2 advance / 1 zero`; явный `zero` относится
  к Арсению и подтверждает обязательный нулевой сценарий;
- live importer dry-run прошёл `10/10`, `inn_control_count=0`;
- после rollback строки account/site/source bindings, mapping, whitelist,
  financial revision и balances отсутствуют;
- точный CSV, отчёт, `input_hash` и `controls_hash` сохранены только в защищённом
  локальном каталоге и не входят в release.

Отбор готов к отдельному разрешению на apply mapping. Whitelist и financial sync
этим dry-run не включались; бухгалтерская сверка десятки ещё обязательна.

## Live CRM validation 2026-07-30

ОТМЕНЕНО (2026-08-22): решение держать `crm_readonly` выключенным и использовать
`manual_confirmed` в новом пилоте. Новый зачётный запуск использует полный
read-only CRM mapping; webhook разрешён только на время 72-часового shadow-run и
не даёт прав записи в CRM.

Read-only проверка CRM подтвердила все пять service fields и полный объём
`50 035` contact rows с `b_user`.

Первоначальная последовательная пагинация по 50 строк не укладывалась в
90-секундный job timeout. Importer переведён на полный cursor-read:

- первый запрос фиксирует `total`;
- Bitrix batch выполняет до 50 связанных страниц по 50 строк;
- каждая следующая страница использует `filter[>ID]` и `start=-1`;
- ID обязаны строго возрастать, дубли и неполные страницы запрещены;
- выполняются два полных последовательных чтения всех страниц; `total` и
  нормализованные семантические строки, включая source systems, обязаны совпасть;
- изменение CRM во время чтения не активирует mapping revision.

`UF_CRM_MM_ONEC_COUNTERPARTY_IDS` содержит не raw ref, а существующий
24-символьный hash `bitrix-crm-customer-audit-v1|onec-ref|<ref>`. Backend
строит read-only hash-index из `_Reference54`; совпадение остаётся точным и не
использует ФИО, название, email, телефон или ИНН. Отсутствующий hash либо
коллизия дают `ambiguous`.

Полный live результат после hash resolution:

- `28 736` linked;
- `21 288` not linked;
- `11` ambiguous/invalid;
- исторический один полный CRM read + короткая проверка занял меньше 90 секунд;
  обязательные два полных чтения и hash-resolution через 1С имеют отдельный
  process timeout 360 секунд.

Сформирован локальный review-only shortlist из 10 разных cluster/counterparty:
4 `debt`, 3 `advance`, 3 `zero`. Все 10 контрагентов существуют, не помечены
на удаление и успешно прошли live extractor. Файл находится только в
игнорируемом `build/customer_settlements/pilot_candidates_review.json`, имеет
права `0600`, не является whitelist и не входит в release.

# Invariants

- Один pilot cluster имеет ровно одного контрагента 1С.
- Email, телефон, ИНН и название не участвуют в mapping.
- Новый mapping не выдаёт сумму, пока активная financial revision не содержит
  соответствующий `CounterpartyGuid` той же организации.
- CRM mapping старше 2 часов закрывает API как `temporarily_unavailable`;
  подтверждённый manual mapping остаётся действующим до явного remap/revoke.
- `source_name` входит в hash mapping revision: одинаковый payload из manual и CRM
  не может унаследовать чужое правило freshness.
- Последняя `matched`-сверка действительна только для точного context hash: hash
  активной mapping revision, организация, режим/поля SQL-источника и полный набор
  контрагентов. Изменение любого элемента требует новой полной сверки.
- Hash сверки дополнительно включает фактически прочитанный SQL-срез; одинаковый
  файл ведомости не переиспользует прежний результат после изменения источника.
- Повторное использование одинакового hash financial/mapping revision разрешено
  только после проверки фактических строк, counts, GUID, сумм и статусов; повреждённая
  или неполная revision блокирует активацию.
- Финансовый worker читает внешний SQL-срез без общего context lock, затем получает
  lock, повторно читает active mapping revision, полный список pilot users и
  counterparty scope и только после совпадения активирует revision в той же транзакции.
  Mapping worker аналогично получает context lock после двух стабильных CRM-read и
  удерживает его только на финальном whitelist/readback и активации.
- Mapping/whitelist mutation и финансовая активация используют общий transaction-level
  context lock, который удерживается до commit/rollback: после финальной проверки
  mapping и pilot scope не могут измениться до активации финансовой revision.
- Eligibility, summary, health и preflight используют shared-вариант context lock;
  параллельные клиенты не блокируют друг друга. Активации, whitelist, reconciliation и
  retention cleanup используют exclusive-вариант: клиентское чтение при занятом
  write-контексте закрывается как `temporarily_unavailable`, а cleanup не может удалить
  реактивированную active mapping revision.
- Любая неожиданная ошибка service/DB после успешной авторизации возвращается как
  обезличенный `503` с `private, no-store`; текст исключения не попадает клиенту.
- Health использует тот же context lock и повторно сверяет ID активных financial/mapping
  revision перед возвратом; смена snapshot во время расчёта закрывает оба health
  статуса как `critical`, а не оставляет частично ложный `ok`.
- Пустые financial/mapping scope запрещены. Health и summary требуют точного совпадения
  фактического набора financial balance refs с уникальными активными pilot
  counterparties, совпадения actual rows/zero с revision counters, канонических
  пар ref/GUID/source-system и текущего `mapping_revision_id` обеих bindings.
  Eligibility и summary дополнительно требуют внутренне полного mapping revision и
  явной entry текущего пользователя; отсутствующая entry не подменяется `not_linked`.
  `NaN`/Infinity и будущие timestamps не принимаются как валидные данные.
- CRM mapping считается стабильным только после двух полных последовательных чтений
  всех страниц с одинаковым total и семантически одинаковыми строками, включая
  `UF_CRM_MM_SYNC_SOURCE_SYSTEMS`; проверка одной первой страницы недостаточна.
  CRM и alert webhook base допускаются только как чистые HTTPS URL без credentials,
  query и fragment.
- Reconciliation task получает context lock только после read-only чтения отчёта/1С,
  повторно сверяет active mapping и pilot scope и сохраняет результат в той же
  транзакции. Financial worker под lock повторно читает последнюю reconciliation;
  удалённый либо заменённый результат не разрешает активацию.
- Обрыв соединения во время reconciliation commit возвращает
  `reconciliation_commit_state_unknown`; повтор того же input hash идемпотентен,
  только пока сохранённый результат остаётся последним run по монотонному `id`.
  Повтор более старого superseded-результата блокируется и не может вернуть CLI-код
  успешной сверки поверх более нового run.
- Runtime database guard обязателен для worker/CLI/health/cleanup и выполняется в API
  до записи replay `jti`; пустое ожидаемое имя БД также блокирует операцию.
- Сохранение одинаковой reconciliation и создание одного health-alert остаются
  идемпотентными при параллельном запуске: конфликт уникальности не превращается в
  ложную ошибку процесса и не создаёт дубль.
- Частичная revision никогда не активируется.
- Feature flag по умолчанию выключен; shadow flag не открывает клиентский API.
- Секреты существуют только в локальном env/secret-контуре.

# Errors / Edge Cases

- Ошибка обновления сохраняет предыдущую active revision.
- Advisory lock исключает параллельный запуск.
- Settlement worker/CLI сверяет PostgreSQL `current_database()` с обязательным для
  cron `CUSTOMER_SETTLEMENTS_EXPECTED_DATABASE_NAME` и fail-closed блокирует job при
  несовпадении, не раскрывая connection details.
- Каждый cron-артефакт ограничен внешним process timeout; после TERM применяется
  принудительное завершение через 5 секунд.
- Checked-in `/etc/cron.d`-шаблон не указывает на mutable-root и production `.env`:
  до установки оператор обязан подставить точный immutable clean release со своим
  hash-locked `.venv`, отдельный staging secret-file и staging-каталог логов.
  Runtime-пути обёрток read-only и не могут быть переопределены secret-файлом.
- Точный повтор payload идемпотентен.
- Финансовый cron повторяет только `error`; `blocked/disabled` не запускают retry.
- В историческом `manual_confirmed` mapping cron только проверяет active revision.
- В активном `crm_readonly` cron после `error`, внешнего process timeout или transient
  `skipped_lock/context_lock` выполняет один повтор через 600 секунд. Job-lock другого
  уже работающего экземпляра, `blocked` и `disabled` повтор не запускают.
- Ровно с `2:00:00` financial health — `warning`, а API — `stale`; ровно в
  `6:00:00` сумма ещё видна как stale, любое превышение 6 часов даёт `critical`
  и скрывает сумму. Сравнение выполняется по точному `timedelta`, без округления
  возраста вниз до целых секунд.
- Stale/missing mapping имеет `critical` health.
- Свежий, но неполный (`not_linked`, `ambiguous` или отозванная site-связь) pilot
  mapping также имеет `critical` health.
- Неизвестное или неполное значение health metric трактуется как `critical`, а в
  alert-текст попадают только нормализованные уровни и неотрицательные counts.
- Health probe возвращает exit code `0/1/2`, чтобы внешний мониторинг мог
  сформировать alert.
- Alert создаётся только при переходе уровня, recovery или раз в 6 часов при
  продолжающемся critical; PostgreSQL `FOR UPDATE SKIP LOCKED` исключает двойную
  отправку параллельными health worker.
- Каждый комментарий получает безопасный event marker; перед записью выполняется
  bounded paginated readback задачи, поэтому неизвестный результат DB commit не
  создаёт повторный комментарий.
- Alert enqueue до обращения к outbox требует `repeat_seconds=21600`; иное значение
  блокирует доставку, чтобы ошибка env не создала поток повторных комментариев.
- Включённые alerts без утверждённой задачи/webhook, ошибка доставки и exhausted
  outbox после пяти попыток дают `critical` и остаются видимыми оператору.
- Внешняя доставка жёстко ограничена HTTPS и задачей Bitrix24 №2883.
- CRM response читается с жёстким пределом 16 MiB; oversized/не-UTF-8 ответ не
  активирует mapping revision.
- Бухгалтерская сверка принимает только непустой scope до 10 контрагентов,
  фиксированный допуск `0,01 RUB` и действующие канонические ref/GUID в RUB.
- Pilot whitelist dry-run получает общий context lock до чтения текущего состояния,
  выполняет реальную mutation/readback в транзакции и обязан подтвердить rollback.

# Observability and data safety

Worker/health/importer JSON содержит только:

- возраст financial/mapping revision;
- duration SQL;
- expected/loaded/zero rows;
- mapping/ambiguous counts;
- retry/lock/error status.
- количества и hashes ручного импорта без ID/GUID/названий/ИНН.

API пишет структурированные события `summary`, `auth_failure`, `expired`,
`future`, `replay` и `eligibility`. Для пользователя допустим только необратимый
hash с отдельной солью.

В Bitrix24 №2883 отправляются только level, freshness и агрегаты
expected/loaded/zero. Финансовые суммы и идентификаторы пилотов в комментарий не
попадают. Delivery использует outbox, recovery и шестичасовой critical reminder.

Новая бухгалтерская сверка выполняется командой
`tasks.reconcile_customer_settlements`: ведомость относится к завершённому дню,
а SQL-срез берётся строго `< 00:00:00` следующего дня по `Europe/Moscow`.
Сохраняются только дата, SHA-256 файла, context/source/input hashes, counts, status
и максимальная абсолютная разница; допуск — `0,01 RUB`. Финансовый worker принимает
только последнюю полную `matched`-сверку, чей context hash совпадает с текущими
mapping, организацией, SQL-настройками и точным набором пилотных контрагентов.
Отсутствующая в ведомости нулевая строка допускается только для нативного XLSX с
печатным полным scope без строки `Отборы:` и при явном `0.00 RUB` из точного SQL
scope. Фильтрованный или неизвестный формат и любая отсутствующая ненулевая строка
остаются fail-closed.

Никогда не логируются сумма, ФИО, email, телефон, полный ID пользователя,
cluster/counterparty ref, assertion, подпись, сырой `jti` или секрет.

# Implementation Checklist

- [x] SQLAlchemy models и Alembic migration.
- [x] Financial/mapping revision lifecycle и retention.
- [x] CRM importer с полной пагинацией.
- [x] CRM importer активирует только whitelist и отзывает отсутствующую связь как
  `not_linked`, не теряя проверку полноты всего источника.
- [x] Durable customer account и версионные GUID bindings.
- [x] Manual-confirmed importer с live controls, dry-run/apply gate и лимитом 10.
- [x] `expected_inn` сделан необязательным; при наличии он по-прежнему сверяется.
- [x] Pilot whitelist CLI с dry-run, audit timestamp и readback.
- [x] Assertion verifier, rotation, IP allowlist и replay store.
- [x] Summary API и OpenAPI schema.
- [x] Eligibility API без клиентских идентификаторов и PHP-session cache 5 минут.
- [x] Worker, advisory locks, retry и cron-артефакты.
- [x] Health probe и безопасные structured events.
- [x] Reconciliation CLI, operational migration, transition alerts и PostgreSQL outbox.
- [x] Synthetic regression tests и contract vector.
- [x] Dedicated assertion scope `customer:settlements:read`.
- [x] Живая read-only сверка SQL: 10/10 пилотов, максимальная разница `0,00 RUB`.
- [x] ОТМЕНЕНО (2026-08-11): кандидатная десятка внешних клиентов с обязательным
  валидным ИНН; пилот заменён на сотрудников с точной связью Bitrix–1С.
- [x] Отбор 10 сотрудников и importer dry-run `10/10` без записи.
- [x] ОТМЕНЕНО (2026-08-22): apply manual mapping/whitelist и начатый на нём
  shadow-run не входят в новую приёмку.
- [ ] Бухгалтерская сверка сотруднического пилота на контрольных точках shadow-run.
- [ ] Shadow-run, security/cache acceptance и бухгалтерская приёмка.
- [ ] Отдельная установка Bitrix server adapter.

# Review Notes / Risks

- Имена регистров и полей 1С нельзя считать подтверждёнными только по коду.
- `CUSTOMER_SETTLEMENTS_SOURCE_VALIDATED` — обязательный ручной readiness gate.
- При reverse proxy allowlist должен проверять фактический доверенный peer;
  клиентский `X-Forwarded-For` не используется.
- PHP-компонент должен отключить component cache, composite cache и
  reverse-proxy cache; одного `Cache-Control` недостаточно.
- Alembic-цепочка однозначна: базовая revision взаиморасчётов
  `c3d4e5f6a7b9` следует за `b2d4f6a8c0e1`, GUID/account revision
  `d9e1f3a5b7c9` следует за ней, а no-op revision `2a4c6e8f0b1d` объединяет
  settlement-ветку с активным production-head `1b9d3f5a7c21`; operational revision
  `4c6e8a0b2d3f` следует за merge, а context-binding revision `6e8f0a2b4c6d`
  следует за ней и остаётся единственным Alembic head.

# Tests

Ограничение на автоматические проверки снято пользователем 2026-08-23. Перед
готовностью обязательны профильный и полный `pytest`, PostgreSQL integration,
Ruff, Black, OpenAPI и docs validation; отсутствие любого обязательного прогона
означает, что текущий diff не готов к release.

Покрыты:

- debt/advance/zero, округление, `-0.00` и запрет non-finite amounts;
- atomic supersede, incomplete revision, idempotency и retention;
- stale 2/6, stale mapping, удаление связи и отсутствующий compatible balance;
- GUID round-trip, постоянство account при remap, конфликт accounts и запрет старого snapshot;
- manual import dry-run/apply, control mismatch и non-RUB rejection;
- несколько cluster/counterparty, manual mapping и совместимая CRM pagination;
- issuer/audience/alg/kid/IP/TTL/future/expired/replay/rotation;
- server-derived identity, отсутствие IDOR-параметров и `no-store`, включая
  неожиданный service failure после авторизации;
- eligibility states, session-only cache, host/TLS/timeouts и отключение composite cache;
- точный `< as_of`, SQL clock внутри основной transaction, temp whitelist, zero SQL
  и запрет `NOLOCK`;
- migration upgrade/downgrade и partial unique active indexes;
- reconciliation end-of-day boundary, duplicate controls/source rows, idempotency и
  запрет ложного успеха при повторе superseded input;
- reconciliation context/source binding, финальный context lock/recheck и запрет
  повторного допуска при изменении пилотов, mapping, reconciliation либо SQL-среза;
- отзыв site-binding, безопасное разделение общего account при remap и запрет
  повторного использования повреждённых revision;
- runtime database guard и обезличивание ошибок драйвера;
- transition alerts, approved task guard, `SKIP LOCKED`, retention operational rows;
- стабильное двойное CRM-чтение, включая позднюю страницу/source systems,
  запрет пустого full-read и повреждённых webhook URL, клиентский context lock,
  exact actual financial scope, актуальную revision обеих bindings, обязательный
  runtime database guard и retention/reactivation race;
- единый API/worker/health/preflight-предикат актуальной сверки, завершённая
  report boundary и порядок reconciliation-run по монотонному `id`;
- readiness gate, retry policy, health exit codes и mock-client secrecy;
- bootstrap с закрытым source gate, запрет `manual_confirmed` для нового запуска и
  обязательная последняя `matched`-сверка перед ready.

PostgreSQL advisory lock, partial indexes, транзакции, конкурентный `jti` и alert
claim прошли integration suite на изолированной схеме staging (`6 passed`).

# Rollout

1. Применить migration на staging PostgreSQL.
2. Выполнить synthetic и PostgreSQL integration tests.
3. Создать новую staging-БД на head `6e8f0a2b4c6d`; прежнюю БД и revisions оставить
   только для диагностики.
4. Включить согласованный whitelist и выполнить полный `crm_readonly` import;
   multi-counterparty cluster и неоднозначные связи блокируют пилота.
5. Сформировать новую ведомость за завершённый день и выполнить
   `tasks.reconcile_customer_settlements` на одинаковой границе `< next midnight MSK`.
6. После сверки включить только `CUSTOMER_SETTLEMENTS_SOURCE_VALIDATED=true`,
   сохранив клиентский feature flag выключенным.
7. Выполнить новый 72-часовой shadow-run с CRM sync на `:05`, financial sync на
   `:17` и health/alerts на `:35`; eligibility и client API оставить выключенными.
8. Сверить всех пилотов с допуском `0.01 RUB`.
9. Подготовить чистый backend release candidate от актуальной production-base;
   до прохождения readiness gate сохранять `CUSTOMER_SETTLEMENTS_ENABLED=false`.
10. Провести security/cache isolation review.
11. Получить письменную бухгалтерскую приёмку.
12. Проверить server-side Bitrix adapter только на `dev.master-mobile.ru`; основной
    `master-mobile.ru` не изменять.
13. После успешной проверки тестового магазина отдельно разрешить подключение
    основного `master-mobile.ru`.
14. После backend readiness gate поставить frontend-задачу.

Rollback:

- выключить `CUSTOMER_SETTLEMENTS_ENABLED` и shadow flag;
- убрать cron installation;
- сохранить active revision для диагностики;
- Alembic downgrade использовать только до появления зависимых migrations.

# Changelog

- 2026-08-24 — reconciliation новой ведомости завершилась `matched 10/10`; staging
  source gate включён, financial snapshot активирован `10/10` с `1 zero`, ready
  прошёл `36/36`. Из clean commit `210ebf0` собран immutable release и начат новый
  72-часовой shadow-run с выключенными client API/eligibility и защитным
  автоотключением `2026-08-27 10:22:35 MSK`.
- 2026-08-24 — новая полная ведомость без отбора подтвердила `9/9` ненулевых
  пилотов (`6 debt / 3 advance`) с допуском `0,01 RUB`; десятый явный SQL `zero`
  отсутствует в стандартном отчёте 1С. Reconciliation gate научен принимать такой
  пропуск только для доказанно нефильтрованного нативного XLSX и точного `0.00 RUB`;
  прежняя ведомость с отбором `ПОКУПАТЕЛИ` остаётся заблокированной.
- 2026-08-24 — новый staging-контур подготовлен до бухгалтерского gate: release
  commit `10977d3d90773b3b0e4a34230221bc2bada45fe5`, immutable release
  `customer-settlements-shadow-20260824-10977d3-r2`, новая БД на head
  `6e8f0a2b4c6d`, whitelist `10/10`, bootstrap `34/34`, полный CRM read `50 035`
  строк и mapping `9 linked / 1 not_linked / 0 ambiguous`. `not_linked` — Арсений
  Кештов: подтверждённая заказами связь ещё не отражена в CRM cluster и не может
  использоваться как неявный override. Credential staging-роли ротирован после
  диагностического раскрытия фрагмента. Financial sync и cron заблокированы до
  исправления/замены mapping и новой ведомости за завершённый день.
- 2026-08-24 — пользователь разрешил создать clean commit проверенного backend,
  собрать отдельный immutable staging release и начать новый 72-часовой
  `crm_readonly` shadow-run. Разрешение ограничено staging; production, сайты, CRM
  и 1С не изменяются, внешние источники читаются только read-only, а cron можно
  установить только после успешного ручного цикла и `ready` preflight.
- 2026-08-24 — после cron/runtime исправлений завершён обязательный тестовый gate:
  профильный settlement-набор повторно прошёл после форматирования; отдельные
  PostgreSQL integration-тесты дали `11 passed`; все `243` test-файла полного
  `pytest` прошли в девяти непересекающихся группах после того, как единый процесс
  был остановлен внешним часовым лимитом на 72% без ошибок. Ruff, Black check,
  OpenAPI, docs validation и `git diff --check` прошли. Staging release и cron не
  устанавливались.
- 2026-08-23 — после повторного аудита cron-шаблон приведён к фактическому формату
  `/etc/cron.d` с явным пользователем `root`; mutable-root Python заменён на
  release-specific `.venv`, а settlement-обёртки запрещают secret-файлу подменять
  `REPO_DIR`, `PYTHON_BIN` и путь env-файла. Пользователь разрешил полный тестовый
  и quality-прогон после исправления.
- 2026-08-23 — подтверждённый пользователем повторный аудит закрыл ложный CLI-успех
  при повторе superseded reconciliation, перенёс SQL Server clock внутрь основной
  transaction финансового среза и заменил mutable-root cron-артефакт безопасным
  immutable-release/staging шаблоном. Регрессионные заготовки обновлены, но тесты и
  quality-проверки по действующему решению пользователя ещё не запускались.
- 2026-08-23 — очередной ручной аудит исправил групповой remap нескольких site users
  одного account на новый GUID, потребовал точного уникального совпадения control и
  SQL scope при бухгалтерской сверке и запретил активацию financial/mapping revision
  с временем впереди backend clock. Отсутствующая в ещё не обновлённом mapping
  entry нового whitelist-пользователя теперь даёт `temporarily_unavailable`, а не
  маскируется под `not_linked`; activation больше не принимает scope свыше 10.
  Регрессионные заготовки добавлены, но по решению пользователя ещё не запускались.
- 2026-08-23 — дополнительный ручной аудит исправил точные границы freshness
  `2h/6h` без секундного усечения, ужесточил canonical assertion и secret config,
  привязал границу УТ к SQL UTC clock, ограничил CRM response 16 MiB, запретил
  ослабление допуска/пустой scope/невалидную identity в reconciliation и сделал
  whitelist dry-run атомарным относительно общего context lock. Добавлены
  regression-заготовки; по решению пользователя они ещё не запускались.
- 2026-08-23 — повторный ручной аудит запретил возврат старой reconciliation при
  retention и откате системных часов, ужесточил CRM `total`, засолил audit hash
  approver, добавил destructive retention guard `30/7/24`, guard alert repeat `21600`
  и явные unknown-commit состояния, а rollback/close/dispose settlement CLI сделал
  best-effort, чтобы вторичная ошибка соединения не перекрывала безопасный JSON.
- 2026-08-23 — по решению пользователя тестовые и quality-прогоны отложены до
  завершения ручного аудита и отдельного разрешения.
- 2026-08-23 — текущий ручной аудит унифицировал readiness сверки между
  API/worker/health/preflight, проверил завершённую report boundary и порядок run
  по `id`, запретил пустой CRM full-read и нормализовал повреждённые webhook URL;
  тестовые seed/mock приведены к обязательной matched-сверке.
- 2026-08-23 — следующий ручной аудит закрыл service-failure cache leak,
  расхождение actual financial rows с revision counters, stale source-binding,
  non-finite суммы, reconciliation TOCTOU и недостаточный timeout двойного CRM-read.
- 2026-08-23 — повторный статический аудит закрыл смешанное клиентское чтение,
  неполную проверку CRM pagination, пустой/лишний financial scope, будущие timestamps,
  source-system/ref-GUID integrity, cleanup/reactivation race и необязательный DB guard.
- 2026-08-23 — решено закрыть остаточное TOCTOU-окно финансовой активации и
  ложный зелёный health при переключении revision; обязательны PostgreSQL
  regression-тесты общего context lock и стабильности snapshot.
- 2026-08-23 — подтверждено закрыть три повторно воспроизведённые гонки перед
  продолжением пилота: context recheck финансового worker, compatible-balance health,
  конкурентно-идемпотентные reconciliation store и health-alert enqueue.
- 2026-08-23 — сверка привязана к mapping/source/pilot context и фактическому
  SQL-срезу; добавлены runtime guard ожидаемой PostgreSQL БД, проверка целостности
  повторно используемых revision, отзыв устаревших site bindings, безопасный split
  общего account, fail-closed health и Alembic head `6e8f0a2b4c6d`.
- 2026-08-22 — новый пилот переведён на `crm_readonly`; добавлены eligibility API,
  автоматическая end-of-day сверка, operational retention, outbox alerts только в
  Bitrix24 №2883 и защита source-aware mapping hash. Прежний manual shadow-run
  остановлен и не засчитывается.
- 2026-08-22 — ОТМЕНЕНО: staging-контур поднят на head `2a4c6e8f0b1d`, manual mapping
  и whitelist применены `10/10`, snapshot загрузил `10/10` с одним explicit zero;
  shadow-run был запущен в `20:43 MSK`, но исключён из новой приёмки.
- 2026-08-22 — settlement migrations объединены с активным production-head
  `1b9d3f5a7c21` через additive no-op revision `2a4c6e8f0b1d`; новый shadow-run
  обязан начинаться на этой revision.
- 2026-07-29 — backend V1 implemented behind disabled feature/readiness gates;
  live 1С/CRM/Bitrix rollout remains blocked pending business inputs.
- 2026-07-30 — live SQL подтвердил организацию, физические поля, знак,
  closed-month continuity и explicit zero; readiness gate оставлен закрытым
  до сверки пилотов с бухгалтерской ведомостью.
- 2026-07-30 — бухгалтерская сверка 10/10 завершена без расхождений, PostgreSQL
  staging и whitelist из 10 пилотов подготовлены; клиентский feature flag выключен,
  ОТМЕНЕНО (2026-08-11): ожидание отдельного CRM webhook заменено ручным
  `manual_confirmed` mapping для backend-среза №2883.
- 2026-08-10 — согласовано добавить один retry CRM mapping через 600 секунд,
  подготовить чистый backend release candidate с выключенным клиентским флагом и
  первым подключить `test.master-mobile.ru`, не изменяя основной магазин.
- 2026-08-11 — задача №2883 ограничена backend `pricing-service`: добавлены
  постоянный `customer_account_id`, GUID bindings, manual-confirmed pilot importer,
  отдельный auth scope и fail-closed stale/remap; Bitrix, личный кабинет, production
  и cron installation не изменяются.
- 2026-08-11 — перед ревью устранена документационная неоднозначность: зафиксированы
  завершённая исходная сверка 10/10, обязательность повторной проверки в новом
  shadow-run и фактическая Alembic-цепочка `b2d4f6a8c0e1 -> c3d4e5f6a7b9 ->
  d9e1f3a5b7c9`.
- 2026-08-11 — apply ручного pilot mapping привязан к `input_hash` и
  `controls_hash` успешного dry-run; изменение CSV или live controls требует
  новой проверки и нового подтверждения.
- 2026-08-11 — PostgreSQL staging gate повторён на отдельной одноразовой БД:
  `upgrade -> downgrade -> upgrade` до `d9e1f3a5b7c9` сохранил синтетические
  строки и корректно выполнил GUID backfill; fixture integration suite обновлён
  с `c3d4e5f6a7b9` до полной цепочки `c3d4e5f6a7b9 -> d9e1f3a5b7c9`, результат
  `5 passed`.
- 2026-08-11 — ОТМЕНЕНО (2026-08-11): старый pilot CSV был заблокирован из-за
  отсутствия валидного ИНН у 9 из 10 строк, после чего была отобрана новая десятка
  внешних клиентов. Этот набор не активирован и заменён сотрудническим пилотом.
- 2026-08-11 — live dry-run уточнил физическую схему УТ: `_Reference66` не имеет
  `_Folder`, а в `_Reference54` `0x01` является элементом и `0x00` группой;
  extractor controls и regression-тест исправлены.
- 2026-08-11 — подтверждено использовать в пилоте ровно 10 сотрудников вместо
  внешних клиентов; связь определяется точным идентификатором Bitrix–1С,
  обязательная проверка ИНН отменена.
- 2026-08-11 — importer поддерживает пустой `expected_inn`, сохраняя проверку при
  наличии; read-only отбор нашёл только 8 однозначно связанных действующих
  сотрудников, все со статусом `debt`. Dry-run `8/8` прошёл без записи, readiness
  остаётся закрытым до решения по недостающим двум кабинетам.
- 2026-08-13 — ОТМЕНЕНО: кадровый статус по ветке УТ и вывод о доступных только
  восьми сотрудниках. Сотрудник определяется по активной структуре Bitrix24;
  полный отбор дал проверяемую десятку.
- 2026-08-13 — Арсений Кештов включён в пилотную десятку по подтверждённой связке
  двух заказов сайта с одной карточкой УТ `РБ0000044`; Владимир Шаповалов заменён.
- 2026-08-13 — устранено расхождение старого отчёта и CSV: ошибочная связь
  `Бирюков Сергей -> Асатрян Гагик` исключена, выбран однозначно связанный
  действующий сотрудник Эльвин Байрамов. Финальный dry-run `10/10` прошёл без
  записи, состояния `7 debt / 2 advance / 1 zero`.
