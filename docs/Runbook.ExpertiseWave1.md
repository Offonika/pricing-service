# Runbook: Expertise Wave 1

Дата фиксации: `2026-04-11`

Этот runbook нужен для первого боевого включения контура `Экспертиза`:

- `1С -> backend -> Bitrix24`;
- smart-process + `Bitrix Disk` + задача на уведомление клиента;
- backend-будильники и повторная синхронизация после ошибок.

Основной код контура:

- [TechDesign.ExpertiseCaseMVP.md](/opt/MM/pricing-service/docs/TechDesign.ExpertiseCaseMVP.md)
- [TechDesign.Bitrix24.ExpertiseSmartProcessMVP.md](/opt/MM/pricing-service/docs/TechDesign.Bitrix24.ExpertiseSmartProcessMVP.md)
- [IntegrationContract.Expertise1C.md](/opt/MM/pricing-service/docs/IntegrationContract.Expertise1C.md)

## 1. Что подготовить в Bitrix24

До первого запуска backend в `Bitrix24` должны быть созданы вручную:

- smart-process `Экспертиза`;
- нужная category, если smart-process работает не в дефолтной категории;
- стадии процесса;
- custom fields карточки;
- root-папка `Bitrix Disk` для кейсов экспертиз;
- технический пользователь, который будет fallback-координатором notify-task, если у подразделения не найден руководитель;
- список пользователей-аудиторов `OKK/management`;
- mapping `store_external_id -> bitrix department id`, где `store_external_id` для экспертиз берется из кода подразделения `1С` вида `РБ...`.
- mapping `store_external_id -> geo_group` для SLA доставки и SLA ответа `ОКК`.

Операционное решение `Wave 1`:

- постановщик notify-task — система / технический пользователь интеграции;
- `RESPONSIBLE_ID` у notify-task — автор документа экспертизы, если он найден среди активных не-курьеров подразделения;
- если автор не найден или не подходит, `RESPONSIBLE_ID` берется из активных менеджеров подразделения-инициатора;
- руководитель подразделения используется как fallback только если он не относится к исключенным должностям;
- если подходящих сотрудников подразделения нет, `RESPONSIBLE_ID` уходит на fallback-координатора `ОКК` из конфига;
- `ACCOMPLICES` у notify-task — остальные активные менеджеры подразделения-инициатора, которые должны реально уведомить клиента;
- сотрудники с исключенными должностями, например курьеры, не попадают ни в исполнителя, ни в соисполнители notify-task;
- `AUDITORS` — контрольные пользователи `ОКК` / руководители, которым нужен обзор и эскалации, но не ежедневное исполнение;
- `client_notified` в `Wave 1` означает, что подразделение-инициатор подтвердило факт уведомления клиента;
- автоматическая `SMS` из `Bitrix24` не входит в первый запуск и рассматривается как следующий этап автоматизации.

### 1.1. Стадии smart-process

Рекомендуемый mapping статусов backend:

```json
{
  "created": "DT187_1:CREATED",
  "received_by_okk": "DT187_1:PREPARATION",
  "under_review": "DT187_1:PREPARATION",
  "decision_ready": "DT187_1:DECISION",
  "client_notified": "DT187_1:NOTIFIED",
  "returned_to_central_defect": "DT187_1:SUCCESS",
  "returned_to_store": "DT187_1:FAIL",
  "manual_review": "DT187_1:MANUAL"
}
```

Где:

- `187` — пример `entityTypeId`;
- `1` — пример `categoryId`;
- правые значения надо заменить на реальные `stageId` конкретного портала.

### 1.2. Поля карточки

Минимальный `field_map`, который ожидает backend:

```json
{
  "title": "TITLE",
  "expertise_ref": "UF_CRM_25_EXPERTISEREF",
  "expertise_number": "UF_CRM_25_EXPERTISENUMBER",
  "case_id": "UF_CRM_25_CASEID",
  "sale_ref": "UF_CRM_25_SALEREF",
  "order_ref": "UF_CRM_25_ORDERREF",
  "order_number": "UF_CRM_25_ORDERNUMBER",
  "organization_ref": "UF_CRM_25_ORGANIZATIONREF",
  "contract_ref": "UF_CRM_25_CONTRACTREF",
  "store": "UF_CRM_25_STORE",
  "customer": "UF_CRM_25_CUSTOMER",
  "phone": "UF_CRM_25_PHONE",
  "problem": "UF_CRM_25_PROBLEM",
  "decision_code": "UF_CRM_25_DECISIONCODE",
  "decision_label": "UF_CRM_25_DECISIONLABEL",
  "decision_comment": "UF_CRM_25_DECISIONCOMMENT",
  "status": "UF_CRM_25_STATUS",
  "owner_ext": "UF_CRM_25_OWNEREXT",
  "owner_name": "UF_CRM_25_OWNERNAME",
  "due_at": "UF_CRM_25_DUEAT",
  "overdue": "UF_CRM_25_OVERDUE",
  "client_notified": "UF_CRM_25_CLIENTNOTIFIED",
  "sync_at": "UF_CRM_25_SYNCAT",
  "source": "UF_CRM_25_SOURCE",
  "folder_url": "UF_CRM_25_FOLDERURL",
  "assigned_by": "ASSIGNED_BY_ID"
}
```

Практическое замечание:

- `order_ref`, `order_number`, `organization_ref`, `contract_ref`, `folder_url` полезны и поддерживаются кодом, но при необходимости могут быть скрыты в UI;
- если `assigned_by` не нужен, его можно не задавать, но тогда smart-process item будет создаваться без явного `assigned_by`;
- названия `UF_*` должны совпадать с реальными кодами полей портала.
- на текущем `Box`-портале создание пользовательских полей через incoming webhook работает, но есть два важных нюанса:
  - для smart-process нужно использовать `entityId = CRM_<type_id>`, а не `CRM_<entityTypeId>`;
  - `userfieldconfig.add/update` в этом портале корректно отрабатывает с `camelCase`-ключами (`entityId`, `fieldName`, `userTypeId`, ...), а `UPPER_CASE`-payload может приводить к ложной ошибке прав.

### 1.3. Mapping магазинов на отделы

Пример:

```json
{
  "РБ0000033": 3279,
  "РБ0000034": 3272,
  "РБ0000028": 3276
}
```

Правило:

- ключ — это `store_external_id`, который приходит в `expertise_case`;
- значение — это `department id` в `Bitrix24`, по которому backend возьмет руководителя отдела через `department.get` и активных сотрудников через `user.get`.

Если маппинга нет:

- кейс и smart-process все равно будут созданы;
- notify-task уйдет без `ACCOMPLICES`;
- в историю кейса попадет `automation_error`.

Практика для `Wave 1`:

- не полагаться только на совпадение названий подразделений `1С` и отделов `Bitrix24`;
- для `Wave 1` использовать mapping по коду подразделения `1С` (`store_external_id` / `РБ...`), а не по `Ref`;
- source of truth для ручного маппинга можно вести в Google Sheets `Штатное расписание -> Подразделения`;
- после стабилизации новой оргструктуры можно будет вернуться к полуавтоматическому сопоставлению.

### 1.4. SLA по этапам

В `Wave 1` больше нет одного общего SLA `14 дней на всё`.

Теперь backend считает дедлайн текущего этапа:

- `created` -> срок доставки до `ОКК`;
- `received_by_okk`, `under_review`, `manual_review` -> срок ответа `ОКК`;
- `decision_ready` -> срок уведомления клиента;
- `client_notified` и финальные статусы -> активный дедлайн очищается.

Нормативы по умолчанию считаются в календарных днях:

- доставка до `ОКК`: `moscow = 2`, `spb = 8`, `other = 8`;
- ответ `ОКК`: `moscow = 3`, `spb = 14`, `other = 14`.
- будильники ответа `ОКК`:
  - `moscow`: через `2` дня — менеджеру `ОКК`, через `4` дня — Омару / Арсену / Максиму, через `12` дней — Эльдару;
  - `spb` / `other`: через `13` дней — менеджеру `ОКК`, через `15` дней — Омару / Арсену / Максиму, через `23` дня — Эльдару;
  - после первого срабатывания каждый уровень повторяется ежедневно, пока нет решения.

Если у подразделения нет geo-mapping:

- backend применяет группу `other`;
- бизнес-процесс не блокируется;
- в историю кейса пишется служебный `automation_error`.

## 2. Что заполнить в `.env`

Шаблон уже добавлен в [.env.example](/opt/MM/pricing-service/.env.example). Для запуска нужны минимум:

```env
EXPERTISE_INTERNAL_API_TOKEN=shared-service-token
ONEC_DATABASE_URL=mssql+pytds://user:password@host:1433/dbname
EXPERTISE_ONEC_SQL_FILE=/opt/MM/pricing-service/docs/sql/expertise_wave1.sql

EXPERTISE_BITRIX_WEBHOOK_URL=https://bitrix.example/rest/1/token
EXPERTISE_BITRIX_ENTITY_TYPE_ID=187
EXPERTISE_BITRIX_CATEGORY_ID=1
EXPERTISE_BITRIX_ROOT_FOLDER_ID=77
EXPERTISE_BITRIX_NOTIFY_RESPONSIBLE_USER_ID=900
EXPERTISE_BITRIX_NOTIFY_AUDITOR_USER_IDS=[130750]
EXPERTISE_BITRIX_NOTIFY_OWNER_USER_MAP={}
EXPERTISE_BITRIX_NOTIFY_EXCLUDED_POSITION_KEYWORDS=["курьер"]
EXPERTISE_BITRIX_NOTIFY_MANAGER_POSITION_KEYWORDS=["менедж","управля"]

EXPERTISE_BITRIX_STAGE_MAP={"created":"DT187_1:CREATED","received_by_okk":"DT187_1:PREPARATION","under_review":"DT187_1:PREPARATION","decision_ready":"DT187_1:DECISION","client_notified":"DT187_1:NOTIFIED","returned_to_central_defect":"DT187_1:SUCCESS","returned_to_store":"DT187_1:FAIL","manual_review":"DT187_1:MANUAL"}
EXPERTISE_BITRIX_FIELD_MAP={"title":"TITLE","expertise_ref":"UF_CRM_25_EXPERTISEREF","expertise_number":"UF_CRM_25_EXPERTISENUMBER","case_id":"UF_CRM_25_CASEID","sale_ref":"UF_CRM_25_SALEREF","sale_number":"UF_CRM_25_SALENUMBER","order_ref":"UF_CRM_25_ORDERREF","order_number":"UF_CRM_25_ORDERNUMBER","organization_ref":"UF_CRM_25_ORGANIZATIONREF","contract_ref":"UF_CRM_25_CONTRACTREF","store":"UF_CRM_25_STORE","customer":"UF_CRM_25_CUSTOMER","phone":"UF_CRM_25_PHONE","problem":"UF_CRM_25_PROBLEM","decision_code":"UF_CRM_25_DECISIONCODE","decision_label":"UF_CRM_25_DECISIONLABEL","decision_comment":"UF_CRM_25_DECISIONCOMMENT","status":"UF_CRM_25_STATUS","owner_ext":"UF_CRM_25_OWNEREXT","owner_name":"UF_CRM_25_OWNERNAME","due_at":"UF_CRM_25_DUEAT","overdue":"UF_CRM_25_OVERDUE","client_notified":"UF_CRM_25_CLIENTNOTIFIED","sync_at":"UF_CRM_25_SYNCAT","source":"UF_CRM_25_SOURCE","folder_url":"UF_CRM_25_FOLDERURL","assigned_by":"ASSIGNED_BY_ID"}
EXPERTISE_BITRIX_STORE_DEPARTMENT_MAP={"РБ0000033":3279,"РБ0000034":3272,"РБ0000028":3276}
EXPERTISE_SLA_STORE_GROUP_MAP={"РБ0000033":"moscow","РБ0000034":"spb","РБ0000028":"other"}
EXPERTISE_SLA_DELIVERY_DAYS_MAP={"moscow":2,"spb":8,"other":8}
EXPERTISE_SLA_REVIEW_DAYS_MAP={"moscow":3,"spb":14,"other":14}

EXPERTISE_ALARM_REVIEW_WARNING_HOURS=24
EXPERTISE_ALARM_NOTIFY_WARNING_HOURS=48
EXPERTISE_ALARM_NOTIFY_ESCALATION_HOURS=48
```

Примечание по папке `Bitrix Disk`:

- `EXPERTISE_BITRIX_ROOT_FOLDER_ID` должен указывать именно на папку `Экспертиза`;
- физически эта папка может лежать не в корне общего диска, а внутри родительской папки, например `Отдел контроля качества / Экспертиза`;
- важно, что ID самой папки `Экспертиза` при переносе не меняется, поэтому env-переменную обычно обновлять не нужно;
- в `Wave 1` кейсовые папки создаются как прямые дочерние элементы папки `Экспертиза`, без дополнительной вложенности по месяцам, статусам или отделам.

Не коммитить в репозиторий:

- реальный `webhook`;
- реальные `user id`;
- реальный `ONEC_DATABASE_URL`.

## 3. Порядок первого запуска

### 3.1. Применить миграции

```bash
cd /opt/MM/pricing-service
./.venv/bin/alembic upgrade head
```

### 3.2. Проверить локальные expertise-тесты

```bash
cd /opt/MM/pricing-service
./.venv/bin/python -m pytest tests/test_expertise_api.py tests/test_expertise_onec_sync.py tests/test_expertise_bitrix.py
```

### 3.3. Первый read-only sync из `1С`

```bash
cd /opt/MM/pricing-service
./.venv/bin/python - <<'PY'
from app.workers.expertise import run_expertise_onec_sync
print(run_expertise_onec_sync())
PY
```

Ожидание:

- в `expertise_case` появились/обновились кейсы;
- `created` и `updated` выглядят правдоподобно;
- ошибки SQL или контракта `1С` отсутствуют.

### 3.4. Первый outbound sync в `Bitrix24`

```bash
cd /opt/MM/pricing-service
./.venv/bin/python - <<'PY'
from app.workers.expertise import run_expertise_bitrix_sync
print(run_expertise_bitrix_sync())
PY
```

Ожидание:

- созданы карточки smart-process;
- у кейсов заполнились `bitrix_entity_id`, `bitrix_disk_folder_id`, `bitrix_last_sync_at`;
- `bitrix_last_error` пустой.

### 3.5. Первый прогон будильников

```bash
cd /opt/MM/pricing-service
./.venv/bin/python - <<'PY'
from app.workers.expertise import run_expertise_alarm_scan
print(run_expertise_alarm_scan())
PY
```

Ожидание:

- для просроченных кейсов появляются automation events;
- старые открытые кейсы пересчитывают `due_at` по новой двухэтапной модели;
- кейсы в `created`, у которых истек норматив доставки, автоматически переходят в `received_by_okk`;
- для кейсов в `decision_ready` при необходимости создаются или обновляются notify-task;
- будильники ответа `ОКК` уходят личными Bitrix-уведомлениями по лестнице эскалации;
- reminder/escalation после принятия решения уходят координатору `ОКК` и наблюдателям, а если по кейсу уже есть notify-task, дублируются комментарием в саму задачу;
- не появляется лавины дублей при повторном прогоне.

## 4. Smoke-check после включения

Проверить на 1-2 живых кейсах:

1. Кейс пришел из `1С` в backend.
2. В `Bitrix24` создалась карточка `Экспертиза` в стадии `Создано`.
3. Создалась папка `Bitrix Disk`.
4. Поля карточки совпадают с `1С` и backend:
   - номер;
   - подразделение;
   - клиент;
   - телефон;
   - решение;
   - статус;
   - дедлайн.
5. После истечения норматива доставки кейс автоматически попадает в `В работе ОКК`, даже если менеджер `ОКК` не двигал его вручную.
6. При переводе кейса в `decision_ready` создалась одна notify-task.
   Исполнитель задачи — руководитель подразделения-инициатора, соисполнители — сотрудники подразделения, fallback при отсутствии руководителя — координатор `ОКК`.
7. При `client_notified` задача закрывается, а не создается заново.
8. Повторный sync не плодит дублей карточек, папок и задач.

## 5. Если что-то пошло не так

### 5.1. Повторная синхронизация только проблемных кейсов

```bash
cd /opt/MM/pricing-service
./.venv/bin/python - <<'PY'
from app.workers.expertise import run_expertise_bitrix_sync
print(run_expertise_bitrix_sync(only_failed=True))
PY
```

Это безопасный reconciliation-проход:

- берет кейсы без `bitrix_last_sync_at`;
- берет кейсы с заполненным `bitrix_last_error`;
- не должен создавать дубль карточки, если `onec_expertise_ref` уже найден в smart-process.

### 5.2. Что смотреть первым

- `bitrix_last_error` в `expertise_case`;
- `automation_error` в `expertise_case_event`;
- корректность `EXPERTISE_BITRIX_FIELD_MAP`;
- корректность `EXPERTISE_BITRIX_STAGE_MAP`;
- наличие `store_external_id` в `EXPERTISE_BITRIX_STORE_DEPARTMENT_MAP`;
- права webhook на:
  - `crm.item.*`
  - `disk.folder.*`
  - `tasks.task.*`
  - `task.commentitem.*`
  - `im.notify.personal.add`
  - `department.get`
  - `user.get`

## 6. Планировщик будильников

Для production включать только один вариант планировщика:

- либо `cron`;
- либо `systemd timer`.

Не включать оба одновременно.
Shell-wrapper уже защищен локом от параллельного запуска на одном хосте, но операционно лучше держать один источник расписания.

Рекомендуемая частота для `Wave 1`: каждые `15` минут.

### 6.1. Cron

Готовые артефакты:

- shell-wrapper: [expertise_alarm_scan.sh](/opt/MM/pricing-service/infra/cron/expertise_alarm_scan.sh)
- шаблон cron: [expertise_alarm_scan.cron](/opt/MM/pricing-service/infra/cron/expertise_alarm_scan.cron)

Важно:

- wrapper намеренно не делает `source .env`;
- настройки читает сам `pydantic` из `/opt/MM/pricing-service/.env`;
- это нужно потому, что expertise-конфиг содержит JSON mapping-поля, которые shell-парсинг ломает.

Пример установки:

```bash
sudo chmod +x /opt/MM/pricing-service/infra/cron/expertise_alarm_scan.sh
sudo cp /opt/MM/pricing-service/infra/cron/expertise_alarm_scan.cron /etc/cron.d/pricing-expertise-alarm-scan
sudo mkdir -p /var/log/pricing
```

### 6.2. systemd timer

Готовые unit-файлы:

- service: [pricing-expertise-alarm-scan.service](/opt/MM/pricing-service/infra/systemd/pricing-expertise-alarm-scan.service)
- timer: [pricing-expertise-alarm-scan.timer](/opt/MM/pricing-service/infra/systemd/pricing-expertise-alarm-scan.timer)

Пример установки:

```bash
sudo chmod +x /opt/MM/pricing-service/infra/cron/expertise_alarm_scan.sh
sudo cp /opt/MM/pricing-service/infra/systemd/pricing-expertise-alarm-scan.service /etc/systemd/system/
sudo cp /opt/MM/pricing-service/infra/systemd/pricing-expertise-alarm-scan.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pricing-expertise-alarm-scan.timer
sudo systemctl list-timers pricing-expertise-alarm-scan.timer
```

### 6.3. Ручной smoke-run

```bash
cd /opt/MM/pricing-service
./.venv/bin/python -m tasks.run_expertise_alarm_scan
```

Ожидание:

- команда печатает JSON summary;
- exit code `0`, если `errors = 0`;
- при наличии ошибок команда возвращает non-zero exit code, чтобы планировщик и мониторинг это увидели.

### 6.4. Планировщик sync `1С -> backend -> Bitrix24`

Отдельно от будильников нужен регулярный sync-контур.

Готовые артефакты:

- CLI: [run_expertise_sync.py](/opt/MM/pricing-service/tasks/run_expertise_sync.py)
- shell-wrapper: [expertise_sync.sh](/opt/MM/pricing-service/infra/cron/expertise_sync.sh)
- шаблон cron: [expertise_sync.cron](/opt/MM/pricing-service/infra/cron/expertise_sync.cron)
- systemd service: [pricing-expertise-sync.service](/opt/MM/pricing-service/infra/systemd/pricing-expertise-sync.service)
- systemd timer: [pricing-expertise-sync.timer](/opt/MM/pricing-service/infra/systemd/pricing-expertise-sync.timer)

Текущая рабочая частота для `Wave 1`: каждые `30` минут со сдвигом `+2` минуты относительно часа, чтобы снизить фоновое чтение `1С` в период расследования SQL-блокировок.

Пример ручного запуска:

```bash
cd /opt/MM/pricing-service
./.venv/bin/python -m tasks.run_expertise_sync
```

Ожидание:

- сначала выполняется read-only sync из `1С`;
- затем выполняется outbound sync в `Bitrix24`;
- команда печатает объединенный JSON:
  - `onec_sync`
  - `bitrix_sync`

Если выбираем `systemd timer`, использовать только его, не дублируя cron.

### 6.5. Watchdog на случай остановки sync-таймера

Иногда `pricing-expertise-sync.timer` может быть случайно остановлен вручную.
Чтобы избежать этого, добавлен watchdog, который каждые 5 минут проверяет,
что таймер активен, и включает его обратно, если он выключен.

Готовые unit-файлы:

- service: [pricing-expertise-sync-watchdog.service](/opt/MM/pricing-service/infra/systemd/pricing-expertise-sync-watchdog.service)
- timer: [pricing-expertise-sync-watchdog.timer](/opt/MM/pricing-service/infra/systemd/pricing-expertise-sync-watchdog.timer)
- shell-wrapper: [expertise_sync_watchdog.sh](/opt/MM/pricing-service/infra/cron/expertise_sync_watchdog.sh)

Пример установки:

```bash
sudo chmod +x /opt/MM/pricing-service/infra/cron/expertise_sync_watchdog.sh
sudo cp /opt/MM/pricing-service/infra/systemd/pricing-expertise-sync-watchdog.service /etc/systemd/system/
sudo cp /opt/MM/pricing-service/infra/systemd/pricing-expertise-sync-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pricing-expertise-sync-watchdog.timer
sudo systemctl list-timers pricing-expertise-sync-watchdog.timer
```

## 7. Что не входит в первый запуск

Сознательно не делаем в этом runbook:

- миграцию старого чата `Экспертиза` `chat70077`;
- перенос старых фото и видео из чата в новые папки `Bitrix Disk`;
- двустороннее управление lifecycle из `Bitrix24` обратно в backend;
- сложные Bitrix robots вместо backend-оркестрации.
