# Нормализация моделей при разборе прайсов конкурентов

## Парсер моделей
- Модуль: `app/services/competitor_matching.py`.
- Бренды с отдельными правилами: Apple (Pro/Max, A-коды, года), Samsung (S/A/M/Note/Z/Ultra/FE), Xiaomi/Redmi/Poco, Huawei/Honor (вкл. Watch/GT), Realme/Oppo/Vivo/OnePlus (reno/neo/gt/nord/x/k/r/t).
- Общие принципы: чистим стоп-слова/цвет/качество, выделяем модель/variant, считаем confidence. При conf < 0.7 или ambiguous — **не создаём** `PhoneModel`, попадает в `low_conf_samples`/`ambiguous_samples`.
- Статистика: `match_competitor_ftp_records` возвращает `low_conf_samples`, `ambiguous_samples`, `unmatched_samples` и логирует краткую сводку.

## Очистка мусорных моделей
- CLI: `./.venv/bin/python -m tasks.cleanup_phone_models --brand apple` (dry-run по умолчанию).
- Применить: `--no-dry-run`. Запустить матчинг после чистки: `--rerun-matching`.
- Что удаляется: слепленные строки >25 символов без пробелов, цепочки A-кодов (`(A\d{4,5}){2+}`), слишком длинные ключи.

## Тесты
- Парсер: `./.venv/bin/python -m pytest tests/test_model_parser.py -q`
- Матчинг: `./.venv/bin/python -m pytest tests/test_competitor_matching.py -q`

## MVP пайплайна «разбор → embeddings → матчинг»

- Ограничение: один `competitor_item` → максимум один `Product`. Авто/LLM создают `suggested`, `accepted` выставляется вручную/отдельной командой; `accepted/manual` не трогаем без `--force`.
- `item_type` (LLM + гардрейлы) фиксированный список: `display`, `battery`, `camera`, `flex`, `housing`, `connector`, `cable`, `board`, `other`.
- Пороги по умолчанию (env/CLI): `TOP_K=20`, `TOP_K_LLM=5`, `MIN_LLM_CONFIDENCE=0.60`, `MIN_EMBED_SCORE=0.40`, `MIN_GAP=0.02`.
- Хранилище эмбеддингов: без pgvector, файл `embeddings/our_catalog_{model}_{dim}.npy` + JSON-индекс `embeddings/our_catalog_index.json`; кеш в памяти, инкрементальный пересчёт только изменившихся SKU/competitor_item.
- Статусы `competitor_item_match`: `suggested`/`accepted`/`rejected`/`needs_review`/`ambiguous`; `method` `embedding_auto|llm_arbitrate|manual`. Уникальность по `competitor_item_id`.
- План джобов:
  - `./.venv/bin/python -m tasks.extract_competitor_attrs`: LLM отдаёт `{item_type, normalized_title, attrs, confidence, uncertain_fields}`, пишет `attrs_json/llm_confidence/llm_raw_json/parse_status`, умеет `--only-null/--only-bad/--overwrite` и перезапуск ошибок (invalid_json/timeout/low_confidence/conflict). Для отладки можно писать сэмплы в файл через `--samples-file`.
  - `./.venv/bin/python -m tasks.compute_embeddings`: считает эмбеддинги для наших SKU и новых/обновлённых `competitor_item` (normalized_title + attrs_string), сохраняет `.npy` + index.
  - `./.venv/bin/python -m tasks.match_competitor_items_embeddings`: brute-force cosine → top-K, гардрейлы (item_type/brand/variant/stop-слова, score_gap, + battery capacity / display type / connector type), при проходе порога опциональный LLM-арбитр на top-5, запись в `competitor_item_match` со счётами и `rationale_json`. Для рестарта по статусам: `--only-open` или `--include-status`, для отладки `--samples-file`, `--report-file`, `--report-csv` (CSV включает parsed/normalized поля и best-product метаданные).
  - Общие флаги: `--limit/--batch-size/--workers`, `--only-null/--overwrite/--force`, `--min-llm-confidence`, `--min-embed-score`, `--min-gap`, `--top-k`, `--top-k-llm`, `--dry-run`, фильтры для рестарта (`parse_status!=ok`, `llm_confidence<t`, рематч только `suggested/needs_review/ambiguous`).
