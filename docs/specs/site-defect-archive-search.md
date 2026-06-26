---
spec_id: "site-defect-archive-search"
title: "Site Defect Archive Search"
doc_type: spec
domain: "order_flow"
status: implemented
owner: "operations"
source_of_truth: true
related_code:
  - app/api/site_defect_archive.py
  - app/models/site_defect_archive.py
  - app/schemas/site_defect_archive.py
  - app/services/site_defect_archive.py
  - scripts/ensure_site_defect_archive_bitrix_process.py
  - scripts/import-old-bitrix-site-defects
  - scripts/import_old_bitrix_site_defects.py
related_tests:
  - tests/test_site_defect_archive.py
contracts: []
depends_on: []
supersedes: []
rollout_required: true
updated_at: "2026-06-18"
---

# Назначение

Старый чат Bitrix Cloud `chat69465` используется как исторический источник по
“Браки сайт”. Вместо переноса его в новый чат создаётся управляемый архив:
одна старая публикация становится одним кейсом архива, а тексты комментариев и
имена файлов попадают в локальный поисковый индекс `pricing-service`.

# Scope / Out of Scope

Входит:
- чтение экспорта `comments-store-raw.json`;
- индекс в таблицах `site_defect_archive_case`, `site_defect_archive_message`,
  `site_defect_archive_file`;
- идемпотентный импорт по ключу `old_bitrix:chat69465:post:{parent_message_id}`;
- API и внутренняя страница `/site-defects/archive`;
- подготовка `history.md` и `metadata.json` для папки Bitrix Disk;
- опциональная синхронизация карточек smart-process и Disk при явном
  `--apply-bitrix`.

Не входит:
- OCR по фото;
- транскрибация аудио и видео;
- автоматическое AI-резюме;
- автоматическое создание smart-process на портале без ручной проверки прав.

# Change Summary / Spec Delta

- было: история “Браки сайт” доступна только через старый чат и ручное открытие
  выгрузки;
- станет: менеджеры ищут по тексту, номерам, автору, датам, типу проблемы и
  наличию фото/видео в отдельном архиве;
- не меняется: старый Cloud после импорта остаётся только историческим
  источником, бизнес-учёт новых кейсов ведётся в новом Bitrix Box.

# Acceptance Criteria

- [x] Dry-run показывает ожидаемые счётчики по публикациям, комментариям и файлам.
- [x] Повторный `--apply` обновляет локальные записи без дублей.
- [x] Поиск находит номера, фразы и имена файлов.
- [x] В локальный индекс не сохраняются старые временные download-ссылки Cloud.
- [ ] Live `--apply --apply-bitrix` выполнен только после проверки тестовой
  категории и прав менеджеров.

# Source of Truth

Исторический источник: экспорт старого Bitrix Cloud `chat69465`.

Операционный источник поиска после импорта: база `pricing-service`.

Новый рабочий контур для живых рекламаций: Bitrix Box smart-process
“Рекламации сайта / Браки сайта”.

Для новых рабочих рекламаций карточка содержит отдельное множественное поле
`Файлы клиента / фото / видео`. Менеджер прикрепляет фото и видео прямо в
карточку, а Bitrix хранит эти файлы в своем файловом хранилище. Поле
`Ссылка на файлы` остаётся для случаев, когда по кейсу нужна отдельная папка Disk
или большая подборка вложений.

Рабочая форма карточки настраивается отдельно от архивной формы. В рабочей
воронке менеджеру показываются только поля разбора: CRM-контакт/компания,
сделка или заказ CRM, номер заказа/РБГУ/перемещения, модель, описание проблемы,
файлы клиента, следующее действие, связанная экспертиза и итог. Поле
`Связанная экспертиза` заведено как CRM-связь с карточкой smart-process
`Экспертиза`, чтобы менеджер выбирал существующую экспертизу из списка, а не
вводил номер или ссылку вручную. Старое текстовое поле связи остаётся
техническим для совместимости и не выводится в основную рабочую форму. Поля
`Что требует клиент`, `Тип проблемы` и `Приоритет` заведены как списки, чтобы
снизить ручной ввод и ошибки. Старые текстовые поля анализа и архивные ID
остаются техническими и не выводятся в основной рабочей форме.

На стадии `Разобраться` рабочая карточка содержит блок `Экономика возврата`.
Менеджер заполняет полезную стоимость товара и оценку обратной доставки, а
`pricing-service` подсвечивает рекомендацию:
- если обратная доставка дороже или равна полезной стоимости товара, товар
  выгоднее оставить у клиента;
- если доставка составляет от 70% до 100% полезной стоимости, нужна оценка
  старшего / ОКК;
- если доставка меньше 70%, возврат экономически оправдан.

Финальное решение остаётся ручным в поле `Товар возвращать?`. Если выбран
вариант `Забрать товар`, но `Трек-номер возврата` пустой, анализатор создаёт
задачу логистике на оформление трека. Если трек уже заполнен, система ставит
статус возврата `Создан` и не создаёт повторную задачу. В v1 нет интеграции с
API перевозчиков и нет автоматической отправки трека клиенту.

# Data Flow

`comments-store-raw.json` и `comment-files-download-log.csv` читаются импортёром.
Для каждого `parent_message_id` строится один кейс архива. Текст публикации,
комментарии, авторы, даты, найденные номера и имена файлов сохраняются в базе.
При включённом `--apply-bitrix` создаётся или обновляется папка Disk:

```text
Disk / Архив / Браки сайт / chat69465 / post-{old_message_id}/
```

В папку загружаются `history.md`, `metadata.json` и локальные файлы из `files/`.
Карточка smart-process хранит ссылку на папку.

# API / Data Contracts

Команды:

```bash
scripts/import-old-bitrix-site-defects --source <export_folder> --dry-run
scripts/import-old-bitrix-site-defects --source <export_folder> --apply
scripts/import-old-bitrix-site-defects --source <export_folder> --apply --apply-bitrix
```

API:

```text
GET /api/site-defects/archive
GET /api/site-defects/archive/{case_id}
```

HTML:

```text
GET /site-defects/archive
```

Фильтры API и страницы:
- `q`;
- `date_from`, `date_to`;
- `author`;
- `problem_type`;
- `number`;
- `has_file`, `has_photo`, `has_video`;
- `has_linked_expertise`.

Настройки:
- `SITE_DEFECT_ARCHIVE_INTERNAL_API_TOKEN`;
- `SITE_DEFECT_ARCHIVE_BITRIX_WEBHOOK_URL`;
- `SITE_DEFECT_ARCHIVE_BITRIX_ENTITY_TYPE_ID`;
- `SITE_DEFECT_ARCHIVE_BITRIX_ARCHIVE_CATEGORY_ID`;
- `SITE_DEFECT_ARCHIVE_BITRIX_ARCHIVE_STAGE_ID`;
- `SITE_DEFECT_ARCHIVE_BITRIX_ROOT_FOLDER_ID`;
- `SITE_DEFECT_ARCHIVE_BITRIX_FIELD_MAP`.

# Invariants

- Старые Cloud URL, cookies, токены и временные download-ссылки не сохраняются в
  DB payload, `metadata.json`, API или странице поиска.
- Один старый post импортируется в один архивный кейс.
- Полный текст хранится локально в `pricing-service`; Bitrix получает только
  настроенные поля и ссылку на папку Disk.
- Экспертиза остаётся отдельной сущностью; архивный кейс может только ссылаться
  на неё.

# Errors / Edge Cases

- Если в экспорте нет `comments-store-raw.json`, импорт завершается ошибкой до
  записи в базу.
- Если файл из CSV отсутствует локально, кейс всё равно импортируется, а файл
  остаётся как метаданные без загрузки в Disk.
- Если Bitrix-настройки неполные, `--apply-bitrix` останавливается с ошибкой.
- Если повторно запустить `--apply`, сообщения и файлы кейса пересобираются, а
  сам кейс не дублируется.
- В полном экспорте `sourcePosts=376`, но полноценных записей с
  `parent_message_id` в `threads/posts.csv` — 370. Импорт создаёт карточки
  только для этих 370 записей, а dry-run отдельно показывает `source_posts_total`
  и `importable_posts`.

# Implementation Checklist

- [x] Добавить модели и Alembic-миграцию.
- [x] Добавить парсер экспорта и локальный импорт.
- [x] Добавить поиск и API.
- [x] Добавить внутреннюю HTML-страницу.
- [x] Добавить CLI-импорт.
- [x] Добавить ensure-скрипт для smart-process, категорий, стадий и полей Bitrix.
- [x] Добавить unit/API тесты.
- [ ] Настроить smart-process и карту полей на Box-портале.
- [ ] Выполнить live dry-run/контрольный apply на тестовой категории.

# Review Notes / Risks

- Массовый `--apply-bitrix` создаёт сотни карточек и загружает сотни файлов, его
  нельзя запускать без подтверждения операционного владельца.
- Перед live запуском нужно проверить права Disk-папки и права редактирования
  архивных полей в smart-process.
- HTML-страница v1 использует внутренний токен; для постоянного менеджерского UX
  лучше встроить её в Bitrix с нормальной авторизацией.

# Tests

Автоматически:
- `tests/test_site_defect_archive.py` проверяет парсинг, dry-run, идемпотентный
  apply, поиск по номерам/фразе/имени файла и API.

Ручной smoke перед live:
- dry-run на 10 карточках;
- полный dry-run с ожидаемыми счётчиками;
- apply в локальную базу;
- `--apply --apply-bitrix` только на тестовой категории;
- ручная проверка 5 карточек, ссылок Disk и прав менеджеров.

# Rollout

1. Применить миграцию БД.
2. Скопировать экспорт `chat69465` в локально доступную папку сервера.
3. Запустить dry-run на 10 карточках.
4. Запустить полный dry-run.
5. Запустить `--apply` для локального индекса.
6. Проверить `/site-defects/archive` по контрольным запросам.
7. После настройки Bitrix smart-process и Disk прав выполнить
   `--apply --apply-bitrix`.

# Changelog

- 2026-06-11 — implemented v1 archive import and search.
