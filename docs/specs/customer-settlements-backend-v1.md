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
  - app/services/customer_settlement_mapping.py
  - app/services/customer_settlement_source.py
  - app/services/customer_settlements.py
  - app/workers/customer_settlements.py
  - tasks/check_customer_settlement_health.py
  - tasks/cleanup_customer_settlements.py
  - tasks/manage_customer_settlement_pilot.py
  - tasks/mock_customer_settlement_client.py
  - tasks/preflight_customer_settlement_shadow.py
  - tasks/sync_customer_settlement_mapping.py
  - tasks/sync_customer_settlements.py
  - infra/cron/customer_settlements.cron
  - alembic/versions/c3d4e5f6a7b9_add_customer_settlements.py
related_tests:
  - tests/test_customer_settlement_api.py
  - tests/test_customer_settlement_auth.py
  - tests/test_customer_settlement_mapping.py
  - tests/test_customer_settlement_migration.py
  - tests/test_customer_settlement_postgres.py
  - tests/test_customer_settlement_shadow_preflight.py
  - tests/test_customer_settlement_source.py
  - tests/test_customer_settlements.py
contracts:
  - openapi.yaml
depends_on:
  - docs/BI.Receivables.md
supersedes: []
rollout_required: true
updated_at: "2026-07-30"
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
- CRM mapping `b_user -> customer cluster -> counterparty`;
- отдельный pilot whitelist;
- HMAC assertion между сервером сайта и `pricing-service`;
- replay-защита, key rotation, retention, advisory locks и health probe;
- OpenAPI, synthetic tests, тестовый вектор и безопасный mock-клиент.

Не входит:

- изменение `УТ 10.3`, CRM, production или сервера `master-mobile.ru`;
- PHP-компонент и frontend личного кабинета;
- автоматическая связь по email, телефону, ИНН или названию;
- вход по телефону — он остаётся отдельной задачей `#2533`;
- несколько организаций, валют или контрагентов 1С в одном пилотном cluster.

# Change Summary / Spec Delta

- Было: личный кабинет не имел безопасного backend-контракта взаиморасчётов.
- Стало: `pricing-service` хранит атомарные почасовые revision и отдаёт
  серверу сайта только состояние текущего пользователя.
- Не меняется: `1С` остаётся системой учёта; клиент не может менять данные.

# Acceptance Criteria

- [x] Нулевой остаток хранится явной строкой и возвращается как `zero`.
- [x] Частичный financial или mapping snapshot не заменяет активный.
- [x] Browser не передаёт `site_user_id`, cluster или `counterparty_ref`.
- [x] Mapping с несколькими cluster/counterparty закрывается как ambiguous.
- [x] Сумма видна до 6 часов, с 2 до 6 часов помечается stale.
- [x] Assertion живёт не более 60 секунд и имеет одноразовый `jti`.
- [x] Ответы API, включая ошибки авторизации, имеют `private, no-store`.
- [x] Retention не удаляет активные revision.
- [x] SQL не использует `NOLOCK`, принимает точный `as_of` и выбирает `< as_of`.
- [x] Live extractor закрыт отдельным флагом бухгалтерской сверки источника.
- [x] PostgreSQL integration проверяет partial unique index, atomic rollback,
  advisory lock, конкурентный replay и retention активной revision.
- [ ] SQL сверён с согласованным отчётом `УТ 10.3` на реальных пилотах.
- [ ] Пройден 72-часовой shadow-run и письменная бухгалтерская приёмка.
- [ ] Получено отдельное разрешение на установку PHP-адаптера сайта.

# Source of Truth

- `УТ 10.3` — источник истины по сумме взаиморасчётов.
- Согласованный бухгалтерский отчёт — эталон живой сверки SQL.
- CRM cluster с полями `UF_CRM_MM_*` — источник явных связей.
- PostgreSQL `pricing-service` — источник активных revision, whitelist и replay-state.
- Bitrix/PHP — только server-side адаптер и представление, не хранилище суммы.

# Data Flow

```text
CRM (:05) -> полная mapping revision -> atomic activate
                                  \
pilot whitelist -> УТ 10.3 (:12) -> financial revision -> atomic activate
                                                     \
Bitrix $USER session -> 60s HMAC assertion -> summary API -> server-rendered block
```

- `:05` — полное чтение CRM с проверкой пагинации и `total`.
- `:12` — полный финансовый срез всех уникальных пилотных контрагентов.
- при реальной ошибке — один повтор через 600 секунд;
- `:35` — health probe, exit code `0/1/2` для `ok/warning/critical`;
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
  "iat": 1785301200,
  "nbf": 1785301200,
  "exp": 1785301260,
  "jti": "contract_vector_20260729"
}
```

Инварианты:

- `sub == site_user_id`, ID — положительная десятичная строка;
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
jti = contract_vector_20260729
```

Ожидаемый compact token:

```text
eyJhbGciOiJIUzI1NiIsInR5cCI6Ik1NLUNVU1RPTUVSLVNFVFRMRU1FTlRTIiwia2lkIjoic2V0dGxlbWVudHMtdGVzdC0xIn0.eyJpc3MiOiJtYXN0ZXItbW9iaWxlLnJ1IiwiYXVkIjoicHJpY2luZy1zZXJ2aWNlOmN1c3RvbWVyLXNldHRsZW1lbnRzIiwic3ViIjoiMTIzNDUiLCJzaXRlX3VzZXJfaWQiOiIxMjM0NSIsImlhdCI6MTc4NTMwMTIwMCwibmJmIjoxNzg1MzAxMjAwLCJleHAiOjE3ODUzMDEyNjAsImp0aSI6ImNvbnRyYWN0X3ZlY3Rvcl8yMDI2MDcyOSJ9.GhCE-qIikJ2Im0xZimMcpJEf3PZALN1yGNkoHxEycwk
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

- `customer_settlement_revision`;
- `customer_settlement_balance`;
- `customer_settlement_mapping_revision`;
- `customer_settlement_mapping_entry`;
- `customer_settlement_pilot_access`;
- `customer_settlement_assertion_jti`.

Внутренние статусы revision: `loading`, `active`, `superseded`, `failed`.
`superseded` нужен для retention старых успешных срезов; одновременно активна
только одна financial и одна mapping revision.

Financial revision активируется только при полном совпадении expected/loaded
контрагентов, отсутствии дублей, `RUB`, валидной организации и явной строке
каждого нулевого баланса. Активация и перевод старой revision в `superseded`
происходят в одной PostgreSQL-транзакции.

Retention:

- successful/superseded — 30 дней;
- `failed/loading` — 7 дней;
- replay `jti` — до `exp + 24 часа`;
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

Readiness gate остаётся закрытым до независимой сверки 5–10 пилотов с
`Ведомостью по взаиморасчётам с контрагентами` на одинаковый `as_of`.

## Live CRM validation 2026-07-30

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
  соответствующего контрагента.
- Mapping старше 2 часов закрывает API как `temporarily_unavailable`.
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
- После 2 часов financial health — `warning`, после 6 — `critical`; API скрывает сумму.
- Stale/missing mapping имеет `critical` health.
- Health probe возвращает exit code `0/1/2`, чтобы внешний мониторинг мог
  сформировать alert.

# Observability and data safety

Worker/health JSON содержит только:

- возраст financial/mapping revision;
- duration SQL;
- expected/loaded/zero rows;
- mapping/ambiguous counts;
- retry/lock/error status.

API пишет структурированные события `summary`, `auth_failure`, `expired`,
`future`, `replay`. Для пользователя допустим только необратимый hash с отдельной
солью.

Никогда не логируются сумма, ФИО, email, телефон, полный ID пользователя,
cluster/counterparty ref, assertion, подпись, сырой `jti` или секрет.

# Implementation Checklist

- [x] SQLAlchemy models и Alembic migration.
- [x] Financial/mapping revision lifecycle и retention.
- [x] CRM importer с полной пагинацией.
- [x] Pilot whitelist CLI с dry-run, audit timestamp и readback.
- [x] Assertion verifier, rotation, IP allowlist и replay store.
- [x] Summary API и OpenAPI schema.
- [x] Worker, advisory locks, retry и cron-артефакты.
- [x] Health probe и безопасные structured events.
- [x] Synthetic regression tests и contract vector.
- [x] Живая read-only сверка SQL: 10/10 пилотов, максимальная разница `0,00 RUB`.
- [ ] Shadow-run, security/cache acceptance и бухгалтерская приёмка.
- [ ] Отдельная установка Bitrix server adapter.

# Review Notes / Risks

- Имена регистров и полей 1С нельзя считать подтверждёнными только по коду.
- `CUSTOMER_SETTLEMENTS_SOURCE_VALIDATED` — обязательный ручной readiness gate.
- При reverse proxy allowlist должен проверять фактический доверенный peer;
  клиентский `X-Forwarded-For` не используется.
- PHP-компонент должен отключить component cache, composite cache и
  reverse-proxy cache; одного `Cache-Control` недостаточно.
- Alembic revision следует непосредственно за опубликованным head
  `d1a2b3c4e5f7` и не зависит от незавершённой задачи кредитных решений.

# Tests

Покрыты:

- debt/advance/zero, округление и `-0.00`;
- atomic supersede, incomplete revision, idempotency и retention;
- stale 2/6, stale mapping, удаление связи и отсутствующий compatible balance;
- несколько cluster/counterparty и CRM pagination;
- issuer/audience/alg/kid/IP/TTL/future/expired/replay/rotation;
- server-derived identity, отсутствие IDOR-параметров и `no-store`;
- точный `< as_of`, temp whitelist, zero SQL и запрет `NOLOCK`;
- migration upgrade/downgrade и partial unique active indexes;
- readiness gate, retry policy, health exit codes и mock-client secrecy.

PostgreSQL advisory lock, partial indexes, транзакции и конкурентный `jti`
должны дополнительно пройти integration suite на PostgreSQL staging.

# Rollout

1. Применить migration на staging PostgreSQL.
2. Выполнить synthetic и PostgreSQL integration tests.
3. Получить точный отчёт и 5–10 однозначных пилотов; организация и поля
   регистров уже подтверждены live SQL.
4. Однократно сверить read-only SQL с бухгалтерским отчётом.
5. После сверки включить только `CUSTOMER_SETTLEMENTS_SOURCE_VALIDATED=true`,
   сохранив клиентский feature flag выключенным.
6. Выполнить 72-часовой shadow-run.
7. Сверить всех пилотов с допуском `0.01 RUB`.
8. Провести security/cache isolation review.
9. Получить письменную бухгалтерскую приёмку.
10. Отдельно разрешить установку server-side Bitrix adapter.
11. После backend readiness gate поставить frontend-задачу.

Rollback:

- выключить `CUSTOMER_SETTLEMENTS_ENABLED` и shadow flag;
- убрать cron installation;
- сохранить active revision для диагностики;
- Alembic downgrade использовать только до появления зависимых migrations.

# Changelog

- 2026-07-29 — backend V1 implemented behind disabled feature/readiness gates;
  live 1С/CRM/Bitrix rollout remains blocked pending business inputs.
- 2026-07-30 — live SQL подтвердил организацию, физические поля, знак,
  closed-month continuity и explicit zero; readiness gate оставлен закрытым
  до сверки пилотов с бухгалтерской ведомостью.
- 2026-07-30 — бухгалтерская сверка 10/10 завершена без расхождений, PostgreSQL
  staging и whitelist из 10 пилотов подготовлены; клиентский feature flag выключен,
  shadow-run ожидает отдельный read-only staging webhook CRM.
