# Аудит ночного контура прайсов и сопоставления конкурентов

Дата аудита: 2026-05-02.

> Исторический срез: факты и рекомендации до раздела обновлений относятся только к
> состоянию на 2026-05-02 и не описывают текущий production. С 2026-07-15 транспорт
> работает по HTTPS, а актуальный item-level contract находится в
> `docs/specs/competitor-matching-ui-v1.md`.

## Что проверено

- Источник FTP-прайсов конкурентов `moba` и `liberti`.
- Legacy-контур `competitor_ftp_record -> competitor_price/product_match`.
- Каталог ручного UI `competitor_item -> competitor_item_snapshot`.
- Парсинг `item_type` и embedding-match pipeline для `CompetitorItemMatch`.
- Ночные расписания cron/systemd на сервере.

## Фактическое состояние до обновления

- Последние FTP-записи и `competitor_item` были от 2026-03-04.
- Embeddings были от 2026-04-27.
- Для конкурентов не было активного nightly cron/systemd.
- В конфиге реальный рабочий источник сейчас FTP: FTP включён и настроен.
- `COMPETITOR_SOURCE_MODE=zenno`, но ZenLogs URL/источники не настроены, поэтому ZenLogs фактически не участвует.

## Что выполнено вручную

1. `./.venv/bin/python -m tasks.import_competitor_ftp`
   - обработано 4 файла:
     - `moba-2026.05.02.xlsx`;
     - `moba-2026.05.01.xlsx`;
     - `liberti-1-2026.05.02.xlsx`;
     - `liberti-1-2026.05.01.xlsx`;
   - 84 893 валидные строки, ошибок нет.

2. `./.venv/bin/python -m tasks.match_competitor_ftp --days-back 3 --disable-category-llm --skip-display-attrs`
   - обновлён legacy-контур цен и каталог `competitor_item`;
   - создано 1 725 новых `competitor_item`;
   - обновлено 83 168 `competitor_item`;
   - добавлено 84 893 snapshots;
   - создано 1 175 `competitor_price`;
   - создано 167 legacy `product_match`.

3. `./.venv/bin/python -m tasks.normalize_competitor_item_type --missing-only`
   - обработано 20 386 строк без `item_type`;
   - обновлено 2 464 строки правилами, без LLM.

4. `./.venv/bin/python -m tasks.compute_embeddings --target both --only-changed --embed-model text-embedding-3-small`
   - обновлены индексы embeddings;
   - `competitor_items_index.json` теперь содержит 46 303 позиции;
   - модель и размерность сохранены: `text-embedding-3-small`, 1536.

5. `./.venv/bin/python -m tasks.match_competitor_items_embeddings --only-null --first-seen-after 2026-05-01 ...`
   - обработано 1 725 новых позиций;
   - создано/обновлено 484 item-level matches;
   - 62 ушли в `needs_review`;
   - 935 ушли в `ambiguous`;
   - 271 auto-accepted как уникальные уверенные пары.

## Состояние после обновления

- `competitor_ftp_record.max(file_date) = 2026-05-02`.
- `competitor_item.max(scraped_at) = 2026-05-02 02:14:55+03:00`.
- `competitor_item_snapshot.max(scraped_at) = 2026-05-02 02:14:55+03:00`.
- `competitor_price.max(collected_at) = 2026-05-02 02:14:55+03:00`.
- `competitor_item_match` всего: 2 881.
- Статусы item-level:
  - `accepted`: 1 096;
  - `suggested`: 483;
  - `needs_review`: 367;
  - `ambiguous`: 935.

## Добавлен nightly job

Файлы:

- `infra/cron/competitor_matching_nightly.sh`
- `infra/cron/competitor_matching_nightly.cron`

Расписание установлено в:

- `/etc/cron.d/pricing-service-competitors`

Время запуска:

- ежедневно в 04:10 MSK.

Шаги nightly:

1. FTP import.
2. Legacy FTP matching и обновление `competitor_item`.
3. Rule-based нормализация `item_type`.
4. Incremental embeddings.
5. Embedding matching только для новых `competitor_item` по `first_seen_at`.

Логи:

- `/var/log/pricing/competitor_matching_nightly.log`
- `/var/log/pricing/competitor_matching_nightly.cron.log`
- отчёты matcher: `build/logs/match_competitor_items_embeddings_*.json/csv`

## Найденные проблемы

1. Документация противоречит фактическому контуру: `architecture.md` говорит, что ZenLogs/catalog удалён, но Bitrix UI и текущий manual matching завязаны на `competitor_item`.
2. `COMPETITOR_SOURCE_MODE=zenno` не соответствует реальному источнику, потому что ZenLogs sources не настроены, а FTP работает.
3. До этой доработки не было ночного расписания конкурентов.
4. `item_type` остаётся пустым у 17 922 строк. Правила закрывают часть каталога, но для хвоста нужен LLM или улучшение rule-based словаря.
5. Live-search после свежего импорта стал показывать больше кандидатов, но для iPhone 17 видны соседние варианты Pro/Pro Max/Air. Нужен более строгий фильтр совместимости по модели/варианту.
6. `tasks.compute_embeddings` по умолчанию берёт `EMBEDDINGS_MODEL` из настроек, а существующий индекс был построен на `text-embedding-3-small`. В nightly модель зафиксирована явно, чтобы не смешивать размерности.
7. Embedding matcher до доработки не имел фильтра по свежим competitor items и мог перерабатывать почти весь unmatched-каталог. Добавлены фильтры `--source`, `--first-seen-after`, `--last-seen-after`.

## Рекомендации

1. Привести конфиг к факту: либо переключить `COMPETITOR_SOURCE_MODE=ftp`, либо явно задокументировать, что UI-каталог питается от FTP через `match_competitor_ftp`.
2. Добавить алерт по свежести данных: если `max(competitor_ftp_record.file_date) < current_date - 1`, отправлять Telegram/Bitrix уведомление.
3. Добавить алерт по результату nightly: processed files, rows, new items, matched, needs_review, ambiguous, errors.
4. Усилить live-search и embedding guardrails для моделей Apple:
   - base `iPhone 17` не должен смешиваться с `17 Pro`, `17 Pro Max`, `17 Air`;
   - `17 Pro` не должен получать `17 Pro Max`;
   - учитывать `ProductPhoneModel` / `CompetitorItemCompatibility` как основной фильтр.
5. Расширить rule-based `item_type` словарь и добавить отдельный отчёт по top-N строкам с пустым `item_type`.
6. Разделить nightly на быстрый обязательный слой и тяжёлый quality layer:
   - обязательный: FTP, catalog, item_type rules, embeddings, matcher;
   - тяжёлый: LLM attrs, rerun `only_bad`, LLM arbiter, отчёты качества.
7. Согласовать статусную модель главной таблицы: сейчас `status` отражает сохранённые `CompetitorItemMatch`, а `live_candidate_count` отражает живой поиск. Это правильно, но в UI стоит явно различать “сохранённые кандидаты” и “есть live-кандидаты”.

## Hotfix постоянного runtime-состояния 2026-07-14

- Индексы `embeddings` подключаются к immutable release как внешний runtime-каталог,
  поэтому переключение release больше не вызывает полную повторную генерацию индексов.
- Nightly загружает `.env` до вычисления feature flags; nightly и watchdog используют
  одно значение `COMPETITOR_MATCHING_EMBEDDINGS_ENABLED`.
- После импорта отдельная read-only проверка контролирует максимальную дату файла.
  Устаревший источник завершает pipeline со статусом `degraded_source_stale`, а не
  маскируется статусом `success`; техническая ошибка проверки по-прежнему завершает job
  ошибкой.

## Переход на HTTPS 2026-07-15

- Активный источник MOBA и Liberti переключён на прямые HTTPS XLSX.
- Scheduled-run `04:10` обработал файлы за `2026-07-15` без ошибок и брака;
  watchdog `05:20` подтвердил свежесть обоих источников.
- FTP task, worker, runtime settings и секреты удалены. Исторические разделы выше
  сохранены как аудит состояния на дату `2026-05-02`.
- Повторный запуск в 04:45 пропускает уже завершённый за день pipeline как для
  `success`, так и для `degraded_source_stale`.

## Recovery nightly 2026-07-31

- HTTP worker переведён с отсутствующего runtime-пакета `requests` на зафиксированный
  в lock-файле `httpx`.
- Release preflight импортирует все nightly-модули до переключения active release.
- При ошибке импорта или устаревшем источнике pipeline завершается fail-closed и не
  запускает compatibility, embeddings, matcher, auto-accept и live-cache refresh.
- Имена `competitor_ftp_*` и поле watchdog `ftp` остаются совместимыми внутренними
  именами и не означают наличие сетевого FTP-транспорта.
