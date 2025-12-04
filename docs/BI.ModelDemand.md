<!-- File: docs/BI.ModelDemand.md -->

# BI-витрины по спросу на запчасти (модели телефонов)

Назначение: дать продукт-менеджерам и закупке доступ к спросу по моделям телефонов (дисплеи/экраны) через готовые представления в БД и, при необходимости, через HTTP API.

---

## 1. Представления в БД

### 1.1. `vw_bi_model_demand_daily`
- Источник: `phone_models`, `keywords` (category = 'display', is_active), `keyword_demands`.
- Поля:
  - `date` — дата среза.
  - `brand`, `model_name`, `variant`, `device_model_id`.
  - `region`.
  - `impressions`, `clicks` — суммы по ключам дисплеев за день.
  - `keywords_count` — сколько ключей учтено.
  - `last_updated_at` — max(received_at) по этому срезу.

### 1.2. `vw_bi_model_demand_30d`
- Агрегат за последние 30 дней (фильтр `kd.date >= current_date - interval '30 days'`).
- Поля:
  - `brand`, `model_name`, `variant`, `device_model_id`, `region`.
  - `impressions_30d`, `clicks_30d`, `avg_impressions_per_day_30d`.
  - `keyword_demands_count`, `last_date`, `trend_flag` (пока NULL, оставлен для будущих трендов).

Представления создаются миграцией `1a4fb0e69e78_add_bi_views_for_model_demand.py`.

---

## 2. Подключение Power BI

- Рекомендуемый способ: прямое подключение к БД (PostgreSQL), DirectQuery или Import — по объёму данных.
- Пользователь: `bi_reader` (пример). Права: `SELECT` на `vw_bi_model_demand_daily`, `vw_bi_model_demand_30d` (и при необходимости `phone_models` как справочник).
- Таблицы/представления для подключения:
  - `vw_bi_model_demand_daily` — детальная хронология.
  - `vw_bi_model_demand_30d` — агрегаты за 30 дней.
  - `phone_models` (опционально) — доп. атрибуты моделей.
- Настройка прав: создаётся read-only роль в БД, привязывается к пользователю, выданы права только на перечисленные представления (создание ролей/пользователей — вне репозитория).

Примеры отчётов:
- Таблица/матрица: Brand, Model, Variant, Region, Impressions_30d, Clicks_30d.
- График: X = date, Y = impressions (daily view), Legend = brand или конкретная модель.

Методы подключения Metabase/Redash аналогичны: обычное SQL-подключение к тем же view.

---

## 3. HTTP API (для простых дашбордов/админки)

- `GET /api/analytics/model-demand/top`  
  Параметры: `days` (по умолчанию 30), `limit` (по умолчанию 50), `brand?`, `region?`.  
  Возвращает топ моделей по `vw_bi_model_demand_30d`.

- `GET /api/analytics/model-demand/{device_model_id}/timeseries`  
  Параметры: `from`, `to` (даты, по умолчанию последние 30 дней), `region?`.  
  Возвращает временной ряд из `vw_bi_model_demand_daily` по одной модели.

---

## 4. Дальнейшая работа (Frontend/UX)

- Добавить простую страницу «Model Demand» в админке: таблица топ-моделей (impressions_30d) и линия тренда по выбранной модели (daily view).
- Добавить фильтры: brand, region, период.
- Поддерживать те же API-эндпоинты для фронта; Power BI продолжает ходить напрямую в БД.
