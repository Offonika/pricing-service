<!-- File: docs/TechDesign.AgentsMarketDemand.md -->

# Tech Design — Агентный контур (новые модели + спрос через Яндекс.Директ)

Документ детализирует техническую архитектуру подсистем:
- Market Research / Device Models (новые смартфоны из пресс-релизов).
- Keyword Generation (фразы под запчасти).
- Yandex Demand (спрос через Яндекс.Директ).
- Agent Integration Layer (HTTP-инструменты для агентов).

Опирается на `docs/architecture.md` (§2.5) и `docs/agents-market-research.md`.

---

## 1. Слои и компоненты

1) **Agent Integration Layer (`/api/agents/*`)**  
   - Принимает нормализованные данные от агентов (пресс-релизы, готовые ключи).  
   - Валидация входа через Pydantic-схемы.  
   - Проксирует вызовы в доменные сервисы: `DeviceModelService`, `KeywordGenerationService`.

2) **Device Models**  
   - Сущность `PhoneModel`, сервис `DeviceModelService`.  
   - Правило upsert по `brand + model_name + variant`.

3) **Keyword Generation**  
   - Сущность `Keyword`, сервис `KeywordGenerationService`.  
   - Генерация по шаблонам дисплей/экран/тач/стекло <brand> <model> (+варианты купить/замена/оригинал).  
   - Поддержка `bulk_create_from_agent`, если агент приносит готовые фразы.

4) **Yandex Demand**  
   - Сущность `KeywordDemand`, сервис `DemandService`, клиент `YandexDirectClient`.  
   - Официальный API Яндекс.Директ, без скрейпинга Wordstat.  
   - История спроса хранится с привязкой к фразе/региону/дате.

5) **Pricing Engine**  
   - Читает `PhoneModel` / `Keyword` / `KeywordDemand` как витрины спроса и новинок, использует в стратегиях и приоритизации ассортимента.
   - Матчинг Product ↔ PhoneModel: эвристика по бренду/подстроке модели; при необходимости результаты логируются и могут быть сохранены в отдельную таблицу сопоставлений.

---

## 2. Модель данных (концептуально)

- **PhoneModel**
  - `brand`, `model_name`, `variant?`
  - `announce_date?`, `release_date?`
  - `screen_size_inch?`, `screen_technology?`, `screen_refresh_rate_hz?`
  - `is_active`, `created_at`, `updated_at`

- **Keyword**
  - `phrase`, `language?`, `category` (display/…)
  - `phone_model_id` (FK), `source` (agent/backend), `is_active`
  - `created_at`, `updated_at`

- **KeywordDemand**
  - `keyword_id` (FK), `date`, `region`
  - метрики: `impressions`, `clicks?`, `ctr?`, `bid_metrics?`
  - `source="yandex_direct"`, `received_at`

Связи: `PhoneModel 1—M Keyword 1—M KeywordDemand`.

---

## 3. API контракты для агентов (`/api/agents/*`)

### 3.1. POST `/api/agents/devices/models`
**Назначение:** принять новую/обновлённую модель телефона.  
**Request (пример):**
```json
{
  "brand": "Samsung",
  "model_name": "Galaxy S26",
  "variant": "Ultra",
  "announce_date": "2026-02-10",
  "release_date": "2026-03-01",
  "screen": {
    "size_inch": 6.8,
    "technology": "AMOLED",
    "refresh_rate_hz": 120
  }
}
```
**Response 202:**
```json
{
  "id": 101,
  "brand": "Samsung",
  "model_name": "Galaxy S26",
  "variant": "Ultra",
  "announce_date": "2026-02-10",
  "release_date": "2026-03-01",
  "screen": {
    "size_inch": 6.8,
    "technology": "AMOLED",
    "refresh_rate_hz": 120
  },
  "created_at": "2026-02-10T12:00:00Z",
  "updated_at": "2026-02-10T12:00:00Z"
}
```

### 3.2. POST `/api/agents/keywords/bulk`
**Назначение:** принять готовые фразы, если агент их генерирует.  
**Request (пример):**
```json
{
  "phone_model_id": 101,
  "phrases": [
    "дисплей galaxy s26 ultra",
    "экран galaxy s26 ultra купить",
    "замена дисплея galaxy s26 ultra"
  ],
  "language": "ru",
  "category": "display",
  "source": "agent"
}
```
**Response 202 (пример):**
```json
[
  {"id": 501, "phrase": "дисплей galaxy s26 ultra", "phone_model_id": 101, "language": "ru", "category": "display"},
  {"id": 502, "phrase": "экран galaxy s26 ultra купить", "phone_model_id": 101, "language": "ru", "category": "display"}
]
```

---

## 4. Последовательность действий (text)

1. **Агент пресс-релизов** по расписанию обходит источники → отправляет `POST /api/agents/devices/models`.  
2. **DeviceModelService** выполняет upsert `PhoneModel` и возвращает id.  
3. **KeywordGenerationService**:
   - либо запускается внутри backend и создаёт фразы по новым `PhoneModel`;
   - либо принимает `POST /api/agents/keywords/bulk`, если фразы пришли снаружи.  
   Результат сохраняется в `Keyword`.
4. **DemandService**:
   - получает список `Keyword`, вызывает `YandexDirectClient.get_stats(phrases, region)`;
   - сохраняет `KeywordDemand` (история спроса).
5. **Pricing-engine** читает витрины `PhoneModel` / `Keyword` / `KeywordDemand` и использует их в стратегиях и приоритизации закупок.

---

## 5. Реализация (итеративно)

- В коде добавлены модели/миграции, базовые сервисы upsert и генерации ключей, API `/api/agents/*`, заглушка клиента Яндекс.Директ (без реального запроса) и провайдер спроса для pricing (логирует при включённом фича-флаге).
- Следующие этапы:
  1) Подключить реальный `YandexDirectClient` (токен, ретраи, лимиты, маппинг ответов).  
  2) Добавить фоновые задачи/cron для генерации фраз и обновления спроса.  
  3) Расширить BI/отчёты витринами по новинкам и спросу и подключить сигнал спроса в стратегии.  
  4) Доработать поиск PhoneModel по brand/model_name (матчинг с нашим каталогом) для более точного использования спроса.

---

## 6. Конфигурация и фича-флаг
- `FEATURE_YANDEX_DEMAND_ENABLED` — включает использование спроса в воркерах/pricing.
- `YANDEX_DIRECT_API_TOKEN`, `YANDEX_DIRECT_API_BASE_URL`, `YANDEX_DIRECT_TIMEOUT`, `YANDEX_DIRECT_BATCH_SIZE`, `YANDEX_DEFAULT_REGION`, `YANDEX_DEMAND_DAYS_WINDOW` — параметры клиента и агрегации спроса.

---

## 7. Источники и критерии «официального анонса»
- Список источников (URL) и брендов задаётся в конфиге агента; требования:
  - домены официальных производителей (apple.com, samsung.com, mi.com, etc.) и их пресс-центры/блоги;
  - допускается whitelist новостных лент, если они публикуют пресс-релизы без слухов.
- Критерии анонса:
  - наличие даты публикации и явного упоминания модели;
  - наличие бренда и серии/модели в заголовке или первых абзацах;
  - при сомнении — пометка низкой уверенности и логирование.
- Поведение при ошибках: ретраи обхода источников, алерт при N неуспехах подряд, логирование URL.

---

## 8. Лимиты Яндекс.Директ и тестирование
- Клиент поддерживает batch size (`YANDEX_DIRECT_BATCH_SIZE`) и таймаут (`YANDEX_DIRECT_TIMEOUT`).
- RPS лимиты: при заданном RPS в настройках вводится задержка между запросами; при ошибках — логирование и возврат пустых статов (без падения воркера).
- Метрики: показы, клики, ctr, bid_metrics (расширяемо). При отсутствии ответа — пропуск сохранения.
- Мок-тесты: рекомендуется интеграционный тест с мок-клиентом, проверяющий батчинг и сохранение KeywordDemand (без реального токена).

---

## 9. Свежесть данных и фоновые задачи
- Фоновая задача `update_stale_keyword_demand` выбирает активные ключи без данных или со старыми данными старше `YANDEX_DEMAND_STALENESS_DAYS`.
- Ограничение числа ключей за прогон — `YANDEX_DEMAND_UPDATE_LIMIT`.
- Фича-флаг `FEATURE_YANDEX_DEMAND_ENABLED` отключает запросы к Яндекс.Директ.
