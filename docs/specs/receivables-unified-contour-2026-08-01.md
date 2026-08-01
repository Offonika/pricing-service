---
spec_id: receivables-unified-contour
title: "Единый контур работы с дебиторкой"
doc_type: spec
domain: receivables
status: accepted
owner: operations-and-engineering
source_of_truth: true
related_code:
  - app/api/management.py
  - app/api/receivable_workplace.py
  - app/models/receivable_bitrix_link.py
  - app/models/receivable_credit_decision.py
  - app/models/receivable_folder_change_operation.py
  - app/services/counterparty_folder_recommendations.py
  - app/services/receivable_credit_decisions.py
  - app/services/receivable_folder_changes.py
  - app/services/receivable_workflow.py
  - infra/cron/counterparty_folder_recommendations_from_a.py
  - infra/cron/counterparty_folder_recommendations_v2.cron.example
  - scripts/build_receivable_work_blueprint.py
related_tests:
  - tests/test_counterparty_folder_recommendations.py
  - tests/test_counterparty_folder_recommendations_from_a.py
  - tests/test_receivable_contour_migration.py
  - tests/test_receivable_credit_decision_process.py
  - tests/test_receivable_folder_changes.py
  - tests/test_receivable_work_blueprint.py
contracts:
  - openapi.yaml
depends_on:
  - docs/specs/receivables-smart-process-workflow.md
supersedes: []
rollout_required: true
updated_at: "2026-08-01"
---

# Единый контур работы с дебиторкой

## Роли систем

- `1С УТ 10.3` — источник долга, оплат, возвратов, папок, точных договоров и
  фактически применённых кредитных условий.
- `pricing-service` — расчётные очереди, рабочие кейсы, события, durable
  операции и синхронизация.
- `crm.company` — постоянная карточка клиента.
- `receivable_work` / «Работа с дебиторкой» — новый операционный процесс.
- `receivable_decision` / «Кредитное решение» — отдельное согласование
  кредитных условий.
- legacy `type/1132` — доступная история до завершения перехода; карточки не
  удаляются и не переписываются.

Расчётный fallback `7 дней` всегда маркируется «7 дней расчётно» и не считается
утверждённой глубиной кредита. Автоматический расчёт не переводит кредитное
решение в `Утверждено`.

## №756 и закрепление клиентов

Название задачи после отдельного согласования публикации:

> Контроль источника долга и закрепления контрагентов в рабочем месте
> «Дебиторка»

Точное новое описание:

> Задача контролирует качество источника долга и работу с подтверждёнными
> сигналами закрепления клиентов. Ежедневно публикуются только новые или
> изменившиеся подтверждённые случаи; полный XLSX остаётся локальным аудитом.
> Ошибки источника и неоднозначные бизнес-кейсы ведутся в отдельных очередях.
> Изменение папки выполняется только отдельной согласованной операцией
> dry-run → approve → apply с проверкой исходной папки и readback.

Публикация этого текста в Bitrix24 не входит в локальный apply и требует команды
пользователя `публикуй`.

Очереди `folder-recommendations`:

- `actionable` — подтверждённая структура открытого долга, свежий источник,
  разные папки, просрочка и сумма не менее 500 ₽;
- `business_review` — межпапочный СПБ и иные бизнес-конфликты;
- `data_quality` — отсутствующая ведомость/структура/mapping, stale и суммы;
- `excluded` — сайт, сотрудники, поставщики, опт и утверждённые исключения.

API фильтрует права подразделения и очередь до `limit`, возвращает
`signal_key`, `queue`, `action_required` и `queue_counts`. Старый `status` и
`queue=all` сохраняются.

## Доставка

CSV/XLSX формируются ежедневно. Новый режим доставки хранит активные
`signal_key` и hash содержимого:

- 10:35 — только новые/изменённые `actionable`;
- до 20 строк — комментарий, больше 20 — XLSX;
- понедельник 10:45 — новые, закрытые, оставшиеся, бизнес-проверки и причины DQ;
- stale, timeout и скачок более 50% при абсолютном изменении от 100 строк
  подавляют внешнюю публикацию.

Production-расписание переключается только после трёх успешных dry-run дней.

## Операционный процесс

Стадии: `Новый`, `В работе`, `Ожидаем оплату`, `Спор`, `Эскалация`, `Закрыто`.
SMS, телефон, обещанная дата и следующий контакт остаются полями/событиями.

`receivable_bitrix_link` хранит связь одного `receivable_work_item` с каждым
контуром и не отменяет legacy-поля переходного периода. Пилот ограничен папкой
`Покупатели` и подразделением Арсена Сагияна. Расширение — после пяти рабочих
дней без дублей и расхождения суммы с 1С.

## Кредитное решение №2494

Решение связано с компанией CRM, рабочим кейсом, точным контрагентом, договором
и организацией 1С. Утверждается точная пара лимит/глубина и флаг контроля суммы.
`set_credit_terms` выполняет dry-run, повторную проверку hash, атомарную запись
регистра, глубины и штатных полей суммы договора, затем полный readback.

Production rollout: shadow без XML → dry-run → allowlist одного контрагента →
рабочий день наблюдения → постепенное расширение. Live-проверки и установка 1С
являются отдельными approval-gates.

## Изменение папки

`receivable_folder_change_operation` хранит неизменяемый сигнал, data version,
согласующего и состояния. Параллельная активная операция контрагента запрещена.
`move_counterparty_folder` требует expected old/new GUID, `DecisionHash`, роль
1С, повторную проверку исходной папки под блокировкой и точный readback.
Восстановление — только новой согласованной операцией.

## Связанные контуры

- клиентская витрина взаиморасчётов — отдельный shadow 72 часа, допуск 0,01 ₽ и
  бухгалтерская приёмка; внутренние причины №756 клиенту не выдаются;
- банк/эквайринг из `mm-compensation` создаёт событие «оплата на сверке», но не
  меняет долг до факта в 1С;
- звонки/SMS включаются после service-side журнала и утверждённого контракта;
- KPI переговорщиков не влияет на оплату труда без отдельного регламента.

## Gates

Локальный код, миграции, blueprint и тесты не являются разрешением на:

- публикацию переименования/описания №756;
- создание smart-process и полей в Bitrix24;
- переключение cron/systemd;
- live-тест или установку в production 1С;
- включение SMS/АТС, auto-apply или расширение пилота.

# Change Summary / Spec Delta

Спецификация разделяет рабочий процесс, кредитное решение и изменение папки,
вводит четыре очереди качества сигнала и заменяет ежедневный полный список №756
на доставку новых или изменённых подтверждённых случаев.

# Source of Truth

Долг, оплаты, возвраты, папки, договоры и применённые условия принадлежат 1С.
Расчётное состояние, durable-операции и синхронизация принадлежат
`pricing-service`; Bitrix24 хранит пользовательские карточки и согласования.

# API / Data Contracts

Контракт включает `queue`, `signal_key`, `action_required`, summary очередей,
API durable-операции папки, `receivable_bitrix_link`, XML-команды
`set_credit_terms` и `move_counterparty_folder`. Публичная схема закреплена в
`openapi.yaml`; обе команды 1С требуют dry-run и точный readback.

# Acceptance Criteria

- Ошибка данных не классифицируется как `actionable`.
- Неизменившийся сигнал не создаёт повторный ежедневный комментарий.
- Один рабочий кейс имеет не более одной карточки на контур.
- Apply кредитных условий невозможен без точного договора и полного readback.
- Apply папки невозможен при drift исходного родителя или без согласующего.
- Legacy-карточки не изменяются и не удаляются при переходе.

# Implementation Checklist

- [x] Очереди, UI и обратная совместимость API.
- [x] Delta/weekly delivery с защитой stale/anomaly/timeout.
- [x] Модели, миграции и dry-run blueprints двух процессов.
- [x] Durable-операции кредитного решения и смены папки.
- [x] Локальные команды и статические контракты УТ 10.3.
- [ ] Три успешных production-like dry-run дня.
- [ ] Отдельно утверждённые Bitrix24, cron и production 1С rollout-шаги.

# Tests

Обязательны Ruff, Black, полный pytest, OpenAPI check, frontend tests/build,
один Alembic head, upgrade/downgrade новых таблиц и статические тесты УТ 10.3.
Live-сценарии `Ekama_Test_Arsen` выполняются отдельно после явного разрешения.

# Rollout

Сначала три дня dry-run доставки. Затем blueprint и пилот Арсена без изменения
legacy. Кредитные условия проходят shadow → dry-run → allowlist → наблюдение.
Изменение папки всегда остаётся явной операцией dry-run → approve → apply.
Каждый production-шаг требует backup, rollback и readback.
