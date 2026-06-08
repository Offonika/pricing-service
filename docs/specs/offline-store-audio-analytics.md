---
spec_id: "offline-store-audio-analytics"
title: "Offline Store Audio Analytics"
doc_type: spec
domain: speech_analytics
status: draft
owner: product
source_of_truth: true
related_code:
  - infra/cron/offline_audio_ingest.py
  - infra/cron/weekly_asr_transcription.py
  - infra/cron/asr_runtime.py
  - infra/cron/calls_unified_projection.py
  - infra/cron/meeting_action_digest.py
related_tests:
  - tests/test_offline_audio_ingest.py
  - tests/test_weekly_asr_transcription.py
contracts:
  - docs/Onepage.OfflineStoreAudioAnalytics.md
  - docs/imports/openclaw-b-offline-dialog-recording-onepage.md
  - docs/TechDesign.ManagementControlTower.md
depends_on:
  - docs/TechDesign.ManagementControlTower.md
supersedes: []
rollout_required: true
updated_at: "2026-05-14"
---

# Назначение

Организовать стабильную офлайн-запись живых диалогов менеджер-клиент у стойки
продаж в магазинах и подключить эти записи к существующему контуру
транскрибации, PostgreSQL-метаданных и управленческих отчетов по аналогии со
звонками.

Главная цель первой волны - не речевая аналитика как продукт, а надежный
контур получения качественных записей с одной переносной точки пилота.

# Scope / Out of Scope

Входит:

- переносной пилотный комплект для одного магазина за раз;
- аппаратный recorder-кандидат для пилота, если он надежнее Windows-recorder-а;
- запись локальных WAV-файлов на Windows-ПК у стойки;
- edge-buffer на Windows с локальным хранением `3-7` дней;
- автоматическая доставка готовых файлов на центральный сервер;
- central landing-zone и ingest job;
- хранение raw audio файлами, отдельно от PostgreSQL;
- PostgreSQL-карточка диалога, статусы, transcript, summary, quality flags;
- подключение ASR worker по паттерну существующего `weekly-asr-transcription`;
- минимальные проверки качества и алерты;
- software-ready подготовка до выезда на пилот;
- план масштабирования без зависимости от EOL-микрофонов.

Не входит:

- закупка оборудования сразу на все магазины;
- использование `Snooper` как долгосрочного стандарта;
- центральный публичный FTP;
- хранение аудио в PostgreSQL;
- полноценная diarization-модель как обязательное условие первой волны;
- замена существующего контура звонков Bitrix24.

# Source of Truth

Для пилота source of truth делится по слоям:

- Windows-ПК у стойки - временный edge-buffer и источник raw recording events;
- центральный сервер - system of record для карточек диалогов, статусов,
  transcript и analysis;
- файловое storage - system of record для raw audio до истечения retention;
- PostgreSQL - system of record для метаданных и статусов обработки;
- `1С`/существующие справочники - источник истины по магазину/сотруднику, если
  для пилота потребуется обогащение;
- `Bitrix24`/Telegram - только UX и delivery layer для отчетов и алертов.

Для реализации в текущем workspace базовый владелец контура - `pricing-service`,
потому что существующие ASR/management jobs и `call_analytics` уже находятся в
этом проекте. Корень `/opt/MM` хранит только registry/orchestration.

Исходный план из `Openclaw B` сохранен локально как
`docs/imports/openclaw-b-offline-dialog-recording-onepage.md`
(`sha256: 9f09269f3a55120177bf07b010288ef17767edd1b8e05821d8560530b2c27716`).

# Data Flow

Fallback-поток для Windows-recorder-а:

```text
Recorder/Snooper
  -> D:\MM-Audio\spool\incoming
  -> local manifest + finalized .wav
  -> Windows uploader
  -> SFTP/HTTPS central server
  -> /var/lib/mm-offline-audio/landing/manual/<device_id>/incoming
  -> ingest job
  -> PostgreSQL metadata
  -> ASR worker
  -> transcript + analysis
  -> daily reports / alerts
```

Пилотный поток v1 с аппаратным recorder-ом:

```text
SpRecord MIC4 + STELBERRY M-1105HD
  -> internal recorder storage
  -> FTPS upload
  -> /var/lib/mm-offline-audio/landing/sprecord/<device_id>/incoming
  -> ingest job
  -> PostgreSQL metadata
  -> ASR worker
  -> transcript + analysis
  -> daily reports / alerts
```

Базовый rollout-поток, если mixed-запись пилота достаточна:

```text
1 mixed microphone per workplace
  -> hardware recorder or Windows recorder/upload agent
  -> local/device buffer
  -> central landing-zone
  -> offline_dialog table + raw storage path
  -> ASR worker
  -> transcript + quality-aware analysis
```

Upgrade-поток для раздельных ролей, если mixed-запись не вытягивает качество:

```text
current non-EOL 2-channel kit or alternate vendor
  -> 2 channels per workplace
  -> hardware recorder or Windows recorder/upload agent
  -> local ready/uploading/sent/error spool
  -> central landing-zone
  -> offline_dialog table + raw storage path
  -> ASR worker
  -> transcript + channel-aware analysis
```

На Windows-ПК:

- рекордер пишет во временный файл `.part` или во временную директорию;
- после завершения запись атомарно переименовывается в `.wav`;
- рядом создается manifest с магазином, ПК, микрофоном, каналами, временем,
  длительностью, форматом и версией агента;
- uploader забирает только завершенные файлы;
- локальный буфер хранит `3-7` дней;
- при сбоях сети uploader ретраит доставку и не удаляет raw audio до
  подтверждения приема.

Рекомендуемая структура spool на Windows:

- `incoming` - recorder пишет временные файлы;
- `ready` - завершенные `.wav` и manifest готовы к отправке;
- `uploading` - файл забран uploader'ом в текущую попытку отправки;
- `sent` - принятые сервером файлы до истечения локального retention;
- `error` - файлы, которые не прошли локальную проверку или исчерпали retries.

На центральном сервере:

- входящие файлы попадают в landing-zone;
- ingest job валидирует manifest и аудио;
- raw audio перемещается в storage с детерминированным ключом;
- PostgreSQL получает карточку диалога и статусы;
- ASR worker берет только валидные готовые записи;
- analysis worker строит summary, flags, business tags и отчетные витрины.

Протокол:

- для v1 аппаратный `SpRecord MIC4` отправляет записи по `FTPS` напрямую в
  защищенную central landing-zone текущего сервера;
- `FTPS` включать только в закрытой сети/VPN/allowlist, с отдельными учетными
  данными, chroot/landing-zone и TLS;
- `SFTP` остается предпочтительным вариантом для Windows-uploader fallback, если
  используется программный recorder вместо аппаратного `SpRecord`;
- `FTP` допустим только локально на Windows как адаптер для recorder'а, если он
  не умеет писать в папку или `SFTP`;
- для собственного Windows-agent в rollout целевой вариант - `HTTPS API` с
  bearer token, idempotency key, checksum и явным приемочным ответом сервера.

# Pre-Pilot Software Scope

До выезда на настройку микрофона нужно подготовить программный контур так, чтобы
ручная работа на месте сводилась к монтажу, регулировке чувствительности и
проверке тестовых записей.

Минимальный backlog:

- central landing-zone под `FTPS` от аппаратного recorder-а и ручную загрузку
  fallback-файлов;
- ingest scanner для новых файлов в `/var/lib/mm-offline-audio/incoming`;
- вычисление `sha256`, длительности, sample rate, channel count и базовых
  quality flags;
- manifest parser и fallback-метаданные из имени файла/папки, если recorder не
  отдает JSON;
- PostgreSQL draft migration для `offline_dialog`, `offline_dialog_transcript`,
  `offline_dialog_analysis`;
- ASR candidate query по `ingest_status=stored`;
- dry-run режим без side effects на тестовых WAV/MP3;
- daily status/quality report: received, stored, asr_done, ingest_error,
  asr_error, silent/short/clipped candidates;
- pilot runbook для выезда: расположение микрофона, уровень чувствительности,
  тестовые фразы, 5-10 пробных записей, финальный контроль качества.

# API / Data Contracts

## Versioning V1

- code/pipeline version: `OFFLINE_AUDIO_PIPELINE_VERSION=0.1.0-pilot.1`;
- release tag после коммита: `offline-audio-v0.1.0-pilot.1`;
- manifest contract: `manifest_schema_version=1`, в v1 допускаются только
  additive changes;
- hardware/profile: `hardware_profile_version=sprecord-mic4-m1105hd-v1`;
- ASR profile: `asr_profile_version=offline-asr-ssh-v1`;
- storage layout: `storage_layout_version=raw-v1`;
- ingest обязан писать эти версии в `offline_dialog` и normalized manifest.

## Manifest V1

Минимальный JSON рядом с аудио:

```json
{
  "version": 1,
  "dialog_id": "store-001-pc-01-20260514T102030-abcdef",
  "source": "offline_store",
  "store_id": "store-001",
  "store_name": "Магазин 1",
  "pc_id": "pc-01",
  "recorder": "sprecord_mic4",
  "recorder_model": "SpRecord MIC4",
  "recorder_serial": null,
  "record_id": null,
  "upload_protocol": "ftps",
  "agent_version": "pilot-manual",
  "microphone_model": "STELBERRY M-1105HD",
  "started_at": "2026-05-14T10:20:30+03:00",
  "ended_at": "2026-05-14T10:24:10+03:00",
  "duration_sec": 220,
  "audio_format": "wav_pcm_s16le_16000",
  "sample_rate_hz": 16000,
  "codec": "pcm_s16le",
  "bitrate_kbps": null,
  "channels": [
    {"index": 0, "role": "mixed"}
  ],
  "sha256": "..."
}
```

Для rollout `channels` должен содержать:

- `{"index": 0, "role": "manager"}`;
- `{"index": 1, "role": "client"}`.

## PostgreSQL Draft Tables

Минимальная карточка:

- `offline_dialog.id`;
- `dialog_id`;
- `source`;
- `store_id`, `store_name`;
- `pc_id`;
- `started_at`, `ended_at`, `duration_sec`;
- `audio_storage_path`;
- `manifest_storage_path`;
- `audio_sha256`;
- `format`, `channel_count`;
- `ingest_status`;
- `asr_status`;
- `analysis_status`;
- `quality_flags`;
- `created_at`, `updated_at`.

Минимальные статусы:

- `ingest_status`: `received`, `validated`, `stored`, `error`;
- `asr_status`: `pending`, `processing`, `done`, `error`, `skipped`;
- `analysis_status`: `pending`, `processing`, `done`, `error`, `skipped`;
- `upload_status` на стороне agent: `ready`, `uploading`, `sent`, `error`.

Транскрипт:

- `offline_dialog_transcript.dialog_id`;
- `language`;
- `model`;
- `transcript_text`;
- `segments_json`;
- `channel_roles_json`;
- `created_at`.

Аналитика:

- `offline_dialog_analysis.dialog_id`;
- `summary`;
- `sentiment`;
- `outcome`;
- `business_flags_json`;
- `quality_flags_json`;
- `created_at`.

DDL зафиксирован в Alembic revision `d1e2f3a4b5c6`; изменения после пилота
оформляются отдельными миграциями.

# Invariants

- FTP/SFTP на Windows может быть только техническим адаптером под recorder, если
  ПО не умеет писать в обычную папку; публичный центральный FTP не открываем.
- FTPS для аппаратного recorder-а не равен SFTP и не считается целевой
  архитектурой rollout; это допустимый pilot-adapter при защищенной сетевой
  настройке.
- Raw audio не пишется в PostgreSQL.
- PostgreSQL хранит только карточку диалога, пути, статусы, transcript и
  analytics.
- Uploader отправляет только завершенные `.wav`, не `.part`.
- Файл считается принятым только после проверки размера, checksum и manifest.
- Пилотный `Snooper` не становится rollout-стандартом без отдельного решения.
- Локальная Windows-папка не считается единственным хранилищем.
- Для пилота `STELBERRY M-1105HD`/`M-1100HD` считается mixed-микрофоном: он
  подходит для записи диалога, но не разделяет роли на отдельные каналы.
- У Stelberry не закладываем в план актуальный "миниатюрный однонаправленный
  одноканальный" закупочный стандарт: поставщик не подтвердил такую текущую
  модель под нашу задачу.
- EOL-модели, включая `STELBERRY M-1200` и `M-1300`, не использовать как основу
  rollout-плана, даже если их страницы описывают подходящие сценарии.
- Базовый rollout после успешного пилота: `1` mixed-канал на рабочее место.
- Upgrade rollout для раздельных ролей допускается только после пилота и выбора
  актуального не-EOL комплекта; ориентир по цене считать отдельно, текущая
  гипотеза - `x1.5+` к базовой mixed-схеме.
- Для собственного recorder/upload agent целевой формат: `WAV PCM 16 bit, 16 kHz`.
- Если аппаратный recorder выгружает `8` или `11.025` kHz, ingest должен явно
  фиксировать sample rate и ресемплить для ASR; `GSM 6.10` не использовать как
  baseline для аналитики без отдельного acceptance-теста качества.
- Для upgrade rollout с раздельными каналами роли каналов должны быть явно
  зафиксированы в manifest: менеджер и клиент.
- Доступ к raw audio и полным transcript ограничивается сервисными ролями и
  ответственными пользователями; управленческие отчеты получают summary/flags.
- Retention raw audio на старте `14-30` дней, локальный Windows-buffer `3-7`
  дней; удаление выполняется только после успешной обработки или по явной
  политике хранения.

# Errors / Edge Cases

Ожидаемые ошибки:

- пустой файл;
- слишком короткая или слишком длинная запись;
- битый WAV/header;
- manifest отсутствует или не совпадает с audio checksum;
- один канал постоянно молчит;
- нет новых записей более `2` часов в рабочее время;
- локальная очередь `ready/uploading/error` растет;
- FTPS/SFTP/HTTPS недоступен;
- ASR worker не укладывается в SLA;
- transcript пустой или слишком низкого качества.

Поведение:

- ingest не удаляет исходник из landing-zone до успешной валидации и перемещения;
- ошибки записываются в PostgreSQL и job-log;
- повторная доставка того же файла должна быть идемпотентной по `dialog_id` и
  `sha256`;
- Windows uploader не удаляет локальный файл до подтвержденного приема;
- ASR ошибки не должны блокировать ingest следующих файлов.

# Privacy / Security

- Перед живым пилотом нужно согласовать правила уведомления о записи разговоров
  с ответственными за безопасность, HR и юридический контур.
- Секреты uploader'а хранятся локально на Windows-ПК в конфиге/secret storage,
  не в коде и не в manifest.
- Центральный сервер не открывает публичный FTP.
- Landing-zone и raw storage должны быть доступны только сервисным пользователям
  ingest/ASR и ограниченному кругу ответственных.
- В Telegram/Bitrix отчеты не отправляют raw audio и полный transcript; допустимы
  агрегаты, summary, quality flags и ссылки с контролем доступа.
- Логи не должны печатать полные токены, персональные телефоны и длинные фрагменты
  transcript.

# Tests

Документация:

- `python scripts/validate_docs_manifest.py`;
- `python scripts/validate_specs.py`.

Будущие unit/integration tests:

- manifest parser принимает pilot mono/mixed и rollout stereo;
- fallback parser извлекает store/device/channel/time из имени файла/папки, если
  SpRecord не отдает manifest;
- ingest idempotent по `dialog_id` + `sha256`;
- `.part` файлы игнорируются;
- битый WAV уходит в `error`;
- пустой/короткий WAV получает quality flag;
- sample rate `8`/`11.025` kHz фиксируется и отправляется на server-side resample
  перед ASR;
- ASR candidate query выбирает только `ingest_status=stored` и валидный
  `audio_storage_path`;
- повторный ASR не дублирует transcript;
- retention job удаляет raw audio только по политике хранения.

Manual acceptance пилота:

- `30` тестовых диалогов суммарно;
- записи получены на центральном сервере без ручного копирования;
- для каждой записи есть manifest и карточка в PostgreSQL;
- ASR дал пригодный transcript хотя бы для грубой оценки качества;
- понятны quality gaps по магазину, микрофону и расстоянию.
- не менее `90%` тестовых записей успешно проходят путь
  `ready -> uploaded -> validated -> asr_done` без ручного вмешательства;
- для каждой тестовой записи известны длительность, checksum, магазин, ПК,
  микрофон и quality flags;
- для rollout-кандидата отдельно подтверждается, что в stereo-записи оба канала
  содержат полезный сигнал и не перепутаны ролями.

# Rollout

Этап 1. Переносной пилот:

- проверить `SpRecord MIC4 + STELBERRY M-1105HD` как основной аппаратный
  кандидат переносного комплекта;
- использовать `Snooper` только как fallback recorder;
- до выезда подготовить landing-zone, ingest, PostgreSQL draft tables, ASR
  candidate flow, dry-run и daily quality report;
- на выезде настроить расположение микрофона, чувствительность/громкость и
  сделать 5-10 пробных записей перед основным сбором;
- настроить аппаратную `FTPS`-выгрузку или ручную fallback-загрузку;
- протестировать магазины по очереди;
- собрать качество по `30` диалогам.

Этап 2. Транскрибация и отчеты:

- реализовать central ingest;
- добавить PostgreSQL metadata tables;
- подключить ASR worker;
- добавить daily report/status check по аналогии со звонками.
- добавить status/backlog check: свежесть записей, очередь upload, очередь ASR,
  доля ошибок ingest/ASR, молчащие каналы.

Этап 3. Rollout:

- если mixed-запись достаточна, масштабировать `1` канал на рабочее место;
- если mixed-запись недостаточна, выбрать актуальный не-EOL комплект для
  раздельных каналов и пересчитать бюджет;
- заменить `Snooper` на собственный Windows recorder/upload agent;
- подключить 12 магазинов;
- включить алерты по тишине, backlog, channel health и ASR SLA.

Rollback:

- остановить uploader/agent на Windows-ПК;
- сохранить локальный spool до ручной проверки;
- отключить ingest job;
- не удалять raw audio до разбора причины;
- оставить существующий контур звонков без изменений.

# Changelog

- 2026-05-14 — imported original Openclaw B OnePage and added hardware recorder
  pilot variant `SpRecord MIC4 + STELBERRY M-1105HD`.
- 2026-05-14 — implemented v1 software contour: Alembic tables, FTPS landing
  ingest, versioned raw storage, ASR candidate flow and read-only quality report.
- 2026-05-14 — corrected microphone plan after vendor check: no current compact
  one-direction one-channel Stelberry baseline; `M-1200`/`M-1300` are EOL; rollout
  starts from mixed channel per workplace unless pilot proves otherwise.
- 2026-05-14 — draft created from OnePage пилота офлайн-записи у стойки.
