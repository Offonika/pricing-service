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
updated_at: "2026-08-22"
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
- `scope` должен в точности равняться `customer:settlements:read`;
- `1 <= exp - iat <= 60`;
- `iat <= nbf < exp`;
- clock skew не больше 30 секунд;
- `jti` принимается один раз и хранится только как SHA-256;
- принимаются active и previous `kid`, но они должны различаться;
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
- reconciliation runs и отправленные/pending alert events — 30 дней;
- failed alert events — 7 дней, alert state сохраняется;
- active revision никогда не удаляется.

# Extractor readiness gate

Extractor использует `_AccumRgT7009/_AccumRg7002` только как проверяемую основу:

- точный `as_of`, движения строго `< as_of`;
- `SYSUTCDATETIME()/SYSDATETIME()` SQL Server;
- whitelist через параметризованную `#CustomerSettlementPilot`;
- `SNAPSHOT`, если разрешён, иначе `READ COMMITTED`;
- `LOCK_TIMEOUT <= 30s`, без `NOLOCK`;
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
- после чтения повторно проверяются `total` и первая страница;
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
- полный цикл CRM read + проверка занял меньше 90 секунд.

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
- Частичная revision никогда не активируется.
- Feature flag по умолчанию выключен; shadow flag не открывает клиентский API.
- Секреты существуют только в локальном env/secret-контуре.

# Errors / Edge Cases

- Ошибка обновления сохраняет предыдущую active revision.
- Advisory lock исключает параллельный запуск.
- Каждый cron-артефакт ограничен внешним process timeout; после TERM применяется
  принудительное завершение через 5 секунд.
- Точный повтор payload идемпотентен.
- Финансовый cron повторяет только `error`; `blocked/disabled` не запускают retry.
- В историческом `manual_confirmed` mapping cron только проверяет active revision.
- В активном `crm_readonly` cron после `error` или внешнего process timeout
  выполняет один повтор через 600 секунд; `blocked`, `disabled` и `skipped_lock`
  повтор не запускают.
- После 2 часов financial health — `warning`, после 6 — `critical`; API скрывает сумму.
- Stale/missing mapping имеет `critical` health.
- Health probe возвращает exit code `0/1/2`, чтобы внешний мониторинг мог
  сформировать alert.
- Alert создаётся только при переходе уровня, recovery или раз в 6 часов при
  продолжающемся critical; PostgreSQL `FOR UPDATE SKIP LOCKED` исключает двойную
  отправку параллельными health worker.
- Внешняя доставка жёстко ограничена HTTPS и задачей Bitrix24 №2883.

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
Сохраняются только дата, SHA-256 файла, counts, status и максимальная абсолютная
разница; допуск — `0,01 RUB`.

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
  `4c6e8a0b2d3f` следует за merge и остаётся единственным Alembic head.

# Tests

Покрыты:

- debt/advance/zero, округление и `-0.00`;
- atomic supersede, incomplete revision, idempotency и retention;
- stale 2/6, stale mapping, удаление связи и отсутствующий compatible balance;
- GUID round-trip, постоянство account при remap, конфликт accounts и запрет старого snapshot;
- manual import dry-run/apply, control mismatch и non-RUB rejection;
- несколько cluster/counterparty, manual mapping и совместимая CRM pagination;
- issuer/audience/alg/kid/IP/TTL/future/expired/replay/rotation;
- server-derived identity, отсутствие IDOR-параметров и `no-store`;
- eligibility states, session-only cache, host/TLS/timeouts и отключение composite cache;
- точный `< as_of`, temp whitelist, zero SQL и запрет `NOLOCK`;
- migration upgrade/downgrade и partial unique active indexes;
- reconciliation end-of-day boundary, duplicate controls/source rows и idempotency;
- transition alerts, approved task guard, `SKIP LOCKED`, retention operational rows;
- readiness gate, retry policy, health exit codes и mock-client secrecy.
- bootstrap с закрытым source gate, запрет `manual_confirmed` для нового запуска и
  обязательная последняя `matched`-сверка перед ready.

PostgreSQL advisory lock, partial indexes, транзакции, конкурентный `jti` и alert
claim прошли integration suite на изолированной схеме staging (`6 passed`).

# Rollout

1. Применить migration на staging PostgreSQL.
2. Выполнить synthetic и PostgreSQL integration tests.
3. Создать новую staging-БД на head `4c6e8a0b2d3f`; прежнюю БД и revisions оставить
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
