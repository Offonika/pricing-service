---
spec_id: "order-payment-control-api"
title: "Order Payment Control API"
doc_type: spec
domain: "order_flow"
status: implemented
owner: "engineering"
source_of_truth: true
related_code:
  - app/api/order_payment_control.py
  - app/schemas/order_payment_control.py
  - app/services/order_payment_control.py
related_tests:
  - tests/test_order_payment_control.py
contracts:
  - openapi.yaml
depends_on:
  - docs/specs/pricing-service-architecture-hardening.md
  - docs/specs/site-order-fulfillment-control-contour.md
supersedes: []
rollout_required: true
updated_at: "2026-08-03"
---

# Назначение

Закрыть задачу Bitrix24 №1414 на стороне `pricing-service`: перед оплатой получать
актуальную сумму интернет-заказа напрямую из read-only базы УТ 10.3 и разрешать
оплату только при выполнении контракта `1С = сайт = платёж`.

# Scope / Out of Scope

Входит:

- защищённый `POST /api/order-payment-control/check`;
- свежий read-only SQL-запрос к `Документ.ЗаказПокупателя` по номеру заказа сайта;
- запрет оплаты при отсутствии, удалении, неоднозначности, непроведённости заказа
  или расхождении сумм;
- журнал решения с суммами, этапом проверки, причиной и ревизией 1С.

Не входит:

- изменение данных или конфигурации 1С;
- изменение штатного импортера Bitrix;
- подключение endpoint к сайту и CloudPayments;
- production release и передача секрета подрядчику.

# Change Summary / Spec Delta

- Было: CloudPayments сравнивал callback только с локальным заказом и платежом сайта.
- Станет: сайт вызывает отдельный backend-контроль перед формой оплаты и в callbacks
  `check`/`pay`; разрешение выдаётся только после свежего чтения 1С.
- Не меняется: штатный CommerceML-обмен и конфигурация УТ 10.3.

# Acceptance Criteria

- [x] API защищён отдельным bearer token с безопасными fallback-настройками.
- [x] Сумма читается из `_Document132._Fld2415`, номер сайта — из `_Fld2425`.
- [x] Запрос не использует `NOLOCK` и не записывает данные в 1С.
- [x] Удалённый, отсутствующий, непроведённый или неоднозначный заказ запрещает оплату.
- [x] Разрешение возвращается только при совпадении суммы 1С, сайта и платежа до копейки.
- [x] Недоступность 1С возвращает `503`; потребитель обязан трактовать любой неуспех
  вызова как запрет оплаты.
- [x] Контракт опубликован в OpenAPI и покрыт тестами.
- [ ] Endpoint включён в production и проверен на новом неоплаченном заказе.
- [ ] Сайт вызывает endpoint перед платёжной формой и в CloudPayments `check`/`pay`.

# Source of Truth

- УТ 10.3 — актуальная сумма и состояние заказа.
- Сайт Bitrix — локальная сумма заказа.
- CloudPayments callback — сумма попытки платежа.
- `pricing-service` принимает решение, но не становится источником торгового факта.

# Data Flow

```text
сайт / CloudPayments handler
  -> bearer-auth POST /api/order-payment-control/check
  -> pricing-service
  -> read-only SQL Server / УТ 10.3
  -> allow или deny + reason + revision
```

# API / Data Contracts

Запрос:

```json
{
  "site_order_number": "225550",
  "site_amount": "5461.95",
  "payment_amount": "5461.95",
  "stage": "cloudpayments_check",
  "payment_id": "provider-payment-id"
}
```

`stage`: `checkout`, `cloudpayments_check` или `cloudpayments_pay`.

Успешный технический ответ всегда содержит бизнес-решение `allowed` и `reason`.
`allowed=true` допустим только для `reason=amount_match`. Недоступность 1С возвращает
HTTP `503`; сайт обязан блокировать оплату и не использовать старый положительный ответ.

# Invariants

- SQL выполняется только через canonical `get_onec_engine()`.
- На каждый этап оплаты выполняется свежая проверка; положительный кеш не используется.
- В лог не попадают токены, connection strings и персональные данные клиента.
- API не изменяет заказ, платёж или состояние 1С.

# Errors / Edge Cases

- `onec_order_not_found`, `onec_order_deleted`, `onec_order_ambiguous`,
  `onec_order_unposted`, `onec_amount_invalid`, `onec_amount_mismatch` — запрет оплаты.
- `site_payment_mismatch` — запрет без запроса к 1С.
- `onec_unavailable` / HTTP `503` — fail closed, повтор только как новая проверка.
- У заказа `225550` на живом read-only срезе 03.08.2026 обнаружена единственная
  активная проведённая запись с суммой `3325,95`; историческая сумма `5461,95`
  текущим состоянием базы не подтверждается.

# Implementation Checklist

- [x] Добавить schemas, service и API router.
- [x] Добавить отдельную настройку токена и `.env.example`.
- [x] Добавить unit/API regression tests.
- [x] Обновить OpenAPI и manifest.
- [ ] Выполнить production release после отдельного решения о выкладке.
- [ ] Передать подрядчику URL, авторизацию и примеры после production smoke.

# Review Notes / Risks

- Запрет непроведённых заказов нужно подтвердить бизнес-сценарием на тестовом заказе.
- Нельзя заменять сумму документа суммой строк: скидки и дополнительные механизмы
  дают допустимые расхождения на части заказов.
- Прямой SQL-контур снижает риск устаревания, но доступность SQL становится частью
  платежного пути; сайт должен корректно показывать временную блокировку.

# Tests

- Совпадение трёх сумм.
- Расхождение сайта и платежа.
- Отсутствующий, удалённый, дублирующийся и непроведённый заказ.
- Некорректная и отличающаяся сумма 1С.
- Нет токена и недоступна 1С.
- Ручной smoke на новом неоплаченном заказе перед production-включением.

# Rollout

1. Собрать clean production release штатным контроллером.
2. Задать отдельный `ORDER_PAYMENT_CONTROL_INTERNAL_API_TOKEN`.
3. Проверить `deny` без токена, `allow` на совпадающем тестовом заказе и `deny` после
   изменения суммы в 1С.
4. Передать подрядчику контракт и секрет через защищённый канал.
5. Сначала подключить `checkout`, затем callbacks `check` и `pay`.
6. Rollback: убрать вызовы API на сайте и вернуть предыдущий release сервиса; данные
   и схема БД не меняются.

# Changelog

- 2026-08-03 — PoC на живой read-only базе, API, тесты и контракт реализованы локально.

