---
spec_id: "receivables-smart-process-workflow"
title: "Receivables Smart Process Workflow"
doc_type: spec
domain: receivables
status: draft
owner: product
source_of_truth: false
related_code:
  - app/api/receivables.py
  - app/api/management.py
  - app/models/receivable_case.py
  - app/models/receivable_work.py
  - app/services/receivables.py
  - app/services/receivable_workflow.py
  - app/services/management_rules.py
  - app/workers/receivable_workflow.py
  - scripts/ensure_receivable_bitrix_process.py
  - tasks/sync_receivable_workflow.py
  - tasks/export_receivable_work_report.py
  - infra/cron/new_daily_receivables_from_a.py
related_tests:
  - tests/test_receivables.py
  - tests/test_receivable_workflow.py
  - tests/test_receivables_worker.py
  - tests/test_management_rules.py
  - tests/test_management_tasks_api.py
  - tests/test_new_daily_receivables_from_a.py
contracts:
  - docs/Onepage.ReceivablesWorkProcess.md
  - docs/BI.Receivables.md
  - docs/TechDesign.ManagementControlTower.md
depends_on: []
supersedes: []
rollout_required: true
updated_at: "2026-05-07"
---

# Назначение

Перевести работу с покупательской дебиторкой из Excel и Bitrix-задач в
`Bitrix24` smart-process, где одна карточка отвечает за один открытый рабочий
кейс контрагента.

Цель первой волны:

- долг и документы считаются из `1С`;
- менеджер работает в карточке smart-process;
- комментарии, обещанные даты оплаты, статусы контакта и SMS-история не живут в
  Excel;
- еженедельный Excel остается отчетом, а не местом ручного ведения процесса.

# Scope / Out of Scope

Входит:

- smart-process `Дебиторка покупателей`;
- idempotent create/update карточки по контрагенту;
- daily sync суммы, сроков, ответственного, подразделения и документов из
  текущего receivables-контура;
- создание карточки, когда долг стал просроченным;
- статусы работы, SMS-факты, комментарии и обещанные даты оплаты в карточке;
- правило отсутствующего телефона;
- эскалация после 15 дней просрочки;
- недельный Excel по подразделениям с первым листом для руководителя;
- выключение receivables-кейсов как самостоятельных Bitrix-задач.

Не входит:

- изменение формулы суммы долга в `1С`;
- ручная корректировка суммы долга из `Bitrix24`;
- полный зарплатный контур удержаний;
- BI-дашборд вместо smart-process;
- обязательная телефония в первой волне, если нет готового технического
  маппинга звонка к карточке.

# Текущий Контекст

В `pricing-service` уже есть фундамент дебиторки:

- `receivable_balance_snapshot` - текущий signed/positive balance по
  контрагенту на дату;
- `receivable_case` - persisted cases по сегментам `buyers`, `new_daily`,
  `overdue`, `inactive`, `employee`, `fired_manager`,
  `adjustment_candidates`;
- `chain_documents` - список документов роста долга по контрагенту;
- `/api/receivables/*` - private read-only API для кейсов;
- `/api/bi/receivables-*` - BI-витрины для Power BI/сверок;
- `/api/management/task-payloads` - текущий rules output для служебных задач,
  где receivables надо заменить на smart-process sync;
- `new_daily` уже не создает Bitrix-задачи, а доставляется как утренний
  Telegram/XLSX через `infra/cron/new_daily_receivables_from_a.py`.

Соседние проекты не содержат готовой замены:

- `mm-compensation` использует Bitrix/задачи для KPI и содержит контекст по
  дебиторке, но не является system of record по покупательской дебиторке;
- `mastermobile` содержит смежные процессы доставки/долга клиента, но не
  текущий receivables ledger.

# Source of Truth

- `1С` - источник истины по сумме долга, документам, сроку оплаты и закрытию
  долга.
- Справочник контрагента `1С` - первичный источник телефона клиента; дальше
  телефон синхронизируется с `Bitrix24`.
- `pricing-service` - источник истины по derived state, дедупликации SMS,
  technical history, sync status и правилам эскалации.
- `Bitrix24 smart-process` - операционная карточка и пользовательская история
  работы менеджера.
- Excel/Telegram - только канал доставки отчета или уведомления.

# Data Flow

Целевой поток:

```text
1С -> pricing-service DB -> receivables snapshots/cases -> workflow rules
   -> Bitrix24 smart-process -> manager actions / comments
   -> pricing-service sync/readback -> weekly Excel / Telegram / BI
```

Daily sync:

1. `pricing-service` пересчитывает receivables snapshots.
2. Для каждого покупательского долга строится рабочий кейс.
3. Интеграционный адаптер ищет карточку smart-process по стабильному ключу.
4. Если карточки нет, создает ее.
5. Если карточка есть, обновляет read-only поля из `1С`.
6. Если долг закрыт в `1С`, переводит карточку в `Закрыто`.
7. Если сумма изменилась, пишет событие в историю и technical log.

# API / Data Contracts

Текущие контракты, которые нельзя ломать:

- `GET /api/receivables/new-daily?date=YYYY-MM-DD`
- `GET /api/receivables/cases?date=YYYY-MM-DD&segment=...`
- `GET /api/receivables/employee-cases?date=YYYY-MM-DD`
- `GET /api/receivables/manager-summary?date=YYYY-MM-DD`
- `GET /api/bi/receivables-current?date=YYYY-MM-DD`
- `GET /api/bi/receivable-cases?date=YYYY-MM-DD&segment=...`
- `GET /api/bi/receivables-manager-summary?date=YYYY-MM-DD`
- `GET /api/bi/receivables-contract-balances?date=YYYY-MM-DD`

Новый технический контракт нужен для sync-а smart-process:

- stable key карточки: `receivables|buyers|{counterparty_ref}`;
- read-only fields из `1С`: контрагент, подразделение, сумма, даты, документы,
  менеджер из `1С`, срок оплаты, дни просрочки, телефон из справочника
  контрагента;
- writable/work fields из `Bitrix24`: статус работы, комментарий/результат
  контакта, обещанная дата оплаты, дата следующего действия, телефон, признак
  спора.

Для `Bitrix24` использовать уже проверенный подход проекта:

- `crm.type.*` для создания smart-process;
- `userfieldconfig.*` для пользовательских полей;
- `crm.item.*` для create/update карточек;
- `crm.item.details.configuration.*` для операторского layout;
- `entityId` пользовательских полей вида `CRM_<type_id>`.

# Smart Process Model

Одна активная карточка smart-process = один контрагент с открытой покупательской
дебиторкой в рабочем контуре.

Карточка создается, когда долг стал просроченным.

День 6 и день 7 считаются от даты реализации. Если SMS на 6-й день от даты
реализации уходит до появления просрочки и до создания карточки, факт SMS
сначала хранится в `pricing-service`, а после создания карточки переносится или
отображается в smart-process.

Минимальные группы полей:

- идентификация: stable key, `counterparty_ref`, `counterparty_name`, source,
  sync time;
- `1С` read-only: сумма, дата возникновения, срок оплаты, дни просрочки,
  подразделение, менеджер из `1С`, документы, телефон из справочника
  контрагента;
- контакт: телефон, статус телефона, SMS status/date/error/dedupe key;
- работа: рабочий ответственный, статус, результат контакта/комментарий,
  обещанная дата оплаты, следующее действие, дата последнего обновления;
- контроль: days without update, escalation level/date, dispute flag/reason.

# State Machine

Минимальная воронка:

- `Новый долг`
- `Ожидаем оплату`
- `SMS отправлено`
- `Нет телефона`
- `Менеджер прозванивает`
- `Клиент обещал оплатить`
- `Спор / проверка суммы`
- `На эскалации`
- `Закрыто`

`На корректировку` не использовать как пользовательский статус первой волны.
Если сумма спорная, карточка идет в `Спор / проверка суммы`, а корректировка
делается в `1С` или отдельном финансовом процессе.

# SMS

Шаблон первой версии:

```text
{Имя}, добрый день!
Завтра истекает срок оплаты по заказу №{номер}.
Сумма к оплате: {сумма} руб.
Напоминаем, что при наличии просрочки платежа отгрузки будут приостановлены и не возобновляются до отдельного распоряжения руководства.
Благодарим за сотрудничество компания Master Mobile.
```

Правила:

- отправлять на 6-й день от даты реализации;
- не отправлять без телефона;
- при отсутствии телефона фиксировать `нет телефона` в карточке;
- запрещать повторную SMS по тому же долгу в пределах одного календарного дня;
- факт отправки писать в карточку, timeline и technical log.

# Эскалация И Спор

Эскалация включается через 15 дней просрочки.

Правило первой версии:

- уведомление уходит руководителю розничной сети;
- руководитель розничной сети становится контролером/наблюдателем эскалации;
- рабочий ответственный в карточке переводится на руководителя розничной сети;
- исходный менеджер остается видимым в карточке как менеджер из `1С` / участник
  работы.

Переход спорного долга к проверке/корректировке подтверждает руководитель
розничной сети. Сумма исправляется только в `1С`, не в `Bitrix24`.

# Отчеты

Еженедельный Excel формируется по подразделениям.

Получатели: Арсений и Арсен. Точные `Bitrix24`/Telegram identifiers нужно
подтянуть перед rollout.

Первый лист:

- клиент;
- сумма, ушедшая в просрочку;
- сколько дней просрочки;
- ответственный.

Дополнительные листы могут содержать документы, комментарии, SMS, статус,
обещанную дату оплаты, дату следующего действия и days without update.

# Invariants

- Сумма долга не редактируется в `Bitrix24`.
- Закрытие долга происходит только после закрытия долга в `1С`.
- У одного контрагента не должно быть нескольких активных карточек по одному
  покупательскому receivables-контуру.
- Комментарии и обещанные даты оплаты не должны вводиться вручную в Excel.
- Bitrix-задача не является источником истории работы с долгом.
- SMS должна иметь idempotency key.
- Ошибки интеграции не должны терять долг: если `Bitrix24` недоступен, backend
  сохраняет retryable sync error.

# Errors / Edge Cases

- Нет телефона: карточка помечается `Нет телефона`, SMS не отправляется.
- Менеджер уволен или не найден: карточка назначается руководителю
  подразделения.
- Нет руководителя подразделения: fallback на согласованный финансовый/сетевой
  пул.
- Долг закрыт между SMS и обзвоном: карточка закрывается daily sync-ом.
- Сумма изменилась: запись в history и technical log.
- Клиент спорит с суммой: статус `Спор / проверка суммы`, сумма не меняется в
  карточке, проверку подтверждает руководитель розничной сети.
- Bitrix sync упал: retry, technical log, health degradation.
- SMS provider упал: карточка получает SMS error, повтор разрешен после снятия
  same-day dedupe или отдельного ручного решения.

# Tests

Нужны проверки:

- unit: stable key карточки, field mapping, state transitions, SMS dedupe;
- integration: create/update/close smart-process item через mocked Bitrix API;
- regression: текущие `/api/receivables/*` и `/api/bi/*` не меняют контракт;
- workbook: первый лист weekly Excel содержит только согласованные короткие
  поля;
- permissions/manual: менеджер и руководитель могут перевести карточку в
  `Спор / проверка суммы`, а подтверждение проверки/корректировки доступно
  руководителю розничной сети;
- smoke: один контрагент с несколькими реализациями получает одну карточку со
  списком документов.

# Rollout

1. Заполнить справочник руководителей подразделений с `Bitrix24 user id`.
2. Завести smart-process и layout в тестовом `Bitrix24`.
3. Применить миграцию `7b6c5d4e3f2a_add_receivable_workflow_tables.py`.
4. Создать/обновить smart-process в тестовом `Bitrix24` через
   `scripts/ensure_receivable_bitrix_process.py`; реальные webhook URL хранить
   только локально, а напечатанные `RECEIVABLE_BITRIX_*` значения перенести в
   окружение сервиса.
5. Включить backend sync adapter с `RECEIVABLE_SMS_MODE=dry_run` и
   `--dry-run-bitrix`.
6. Подключить телефон из справочника контрагента `1С` и проверить, что
   карточки без телефона попадают в статус `Нет телефона`, а не в live SMS.
7. Включить sync в `Bitrix24` без `--dry-run-bitrix`.
8. Включить SMS live с same-day dedupe только после подтверждения endpoint и
   контракта SMS/1С.
9. Отключить receivables-кейсы из `/api/management/task-payloads` или оставить
   только служебные non-workflow уведомления.
10. Переключить weekly Excel на данные smart-process по комментариям и статусам.
11. Провести pilot по 1-2 подразделениям.

Rollback:

- выключить smart-process sync feature flag;
- оставить текущие read-only receivables API и Telegram/XLSX delivery;
- не менять формулу суммы долга в `1С`.

# Changelog

- 2026-05-06 - draft created from receivables OnePage and discussion notes.
