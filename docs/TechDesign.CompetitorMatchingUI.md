# TechDesign — UI для ручного сопоставления товаров с конкурентами

> Статус: `superseded` с 2026-07-31. Это исторический проектный черновик; актуальные
> контракты, инварианты и rollout описаны только в
> `docs/specs/competitor-matching-ui-v1.md`. Черновые endpoint и bulk-auto требования
> ниже не являются действующим production contract.

## Цель
Дать операторам удобный веб-интерфейс для сопоставления наших товаров с товарами конкурентов: просмотр списка наших товаров, выбор кандидатов конкурентов, принятие/снятие привязки, работа с большими объёмами (тысячи строк) без лагов.

## Стек и общие решения
- Веб SPA: **React + TypeScript + Vite**.
- Таблица: **AG Grid Community** (виртуализация строк, серверные сортировки/фильтры/пагинация, фиксированные колонки, hotkeys).
- Данные/запросы: **React Query** (кэш, refetch, optimistic updates), **Zustand** для локального UI-состояния (фильтры, выбранные строки).
- Стили: базовый CSS/SCSS или легковесный utility (Tailwind по желанию). Без обязательной коммерческой лицензии.

## API контракты (предлагаемые)
- `GET /api/products` — список наших товаров с серверными `page, page_size, sort, filters` (название, артикул, бренд, категория, статус сопоставления). Ответ: `{items: ProductRow[], total: int}`.
- `GET /api/products/{product_id}/candidates` — кандидаты конкурентов для выбранного товара. Параметры: `limit`, `offset`, опционально `source`. Ответ: `{items: Candidate[], total: int}`.
- `POST /api/product-matches/{product_id}` — привязать выбранного конкурента: body `{competitor_id, reason?, confidence?, source?}`.
- `DELETE /api/product-matches/{product_id}/{competitor_id}` — снять привязку.
- `GET /api/competitors/search` — поиск по товарам конкурента (строка, артикул, бренд) с пагинацией; используется в «ручном подборе».
- `POST /api/product-matches/{product_id}/reject` — пометить кандидата отклонённым: body `{competitor_id, reason?}` (чтобы не предлагать повторно).
- Логи/статус: `GET /api/product-matches/{product_id}/history` (опционально) — история действий по товару.

## Основные компоненты
- `ProductsGrid` (AG Grid):
  - Колонки: Наш товар (название), SKU, бренд, категория, статус сопоставления (нет/авто/ручное/множественное), маржа/цены (опционально), флаг «нужна проверка».
  - Фиксированные колонки слева: чекбокс выбора, SKU/название; справа — статус.
  - Серверные сортировка/фильтры/пагинация. Виртуализация.
  - Hotkeys: `↑/↓` — навигация по строкам; `Enter` — открыть панель кандидатов; `Backspace` — снять привязку (после подтверждения).
- `CandidatePanel` (right-side drawer):
  - Детали нашего товара (название, SKU, бренд, атрибуты, цена).
  - Список кандидатов конкурентов (таблица или список) с: название, SKU конкурента, бренд, цена, источник, метрика схожести, бейдж «авто-предложение».
  - Кнопки на строке: «Принять» (создать/обновить привязку), «Снять» (если текущий матч), «Отклонить» (скрыть/записать как отклонённого).
  - Быстрый поиск по конкурентам (live search, debounce).
- `BulkActionsBar`:
  - Массово принять авто-пары с порогом `>= threshold`.
  - Пометить выбранные товары как «нужна проверка».
  - Сбросить фильтры.
- `Progress/Stats`:
  - Счётчик: всего / сопоставлено / в ручной проверке / без пары.
  - Индикация фоновых загрузок.

## UX и поведение
- Загрузка кандидатов — lazy: только по выбранной строке.
- Сохранение фильтров/сортировок в URL (query params) для шаринга.
- Optimistic UI при «Принять/Снять», с откатом при ошибке.
- Debounce поиска по конкурентам (300–500 мс).
- Ограничение ререндеров: React.memo, `suppressColumnVirtualisation=false` в AG Grid.
- Тосты об ошибках/успехе, inline спиннеры на строках.

## Стейты и кэш
- React Query: ключи `["products", filters, sort, page]`, `["candidates", product_id, params]`.
- Zustand: выбранный `productId`, выбранные `productIds[]`, фильтры (чтобы использовать вне запроса, напр., в UI/URL), порог автопринятия.

## Минимальный роутинг
- SPA без сложного роутинга: `/matching` с query-параметрами для состояния (page, sort, filters, productId).

## Горячие клавиши
- `↑/↓` — переход по нашим товарам.
- `Enter` — принять выделенного кандидата (если фокус в списке кандидатов) или открыть панель.
- `Esc` — закрыть панель кандидатов.
- `Backspace` — снять привязку (с подтверждением).
- `Ctrl/Cmd + F` внутри панели — фокус на поле поиска по конкурентам.

## Производительность и лимиты
- Всегда серверные операции (сортировка/фильтр/пагинация).
- Пагинация 50–200 строк; виртуализация строк; не грузить весь каталог.
- Минимизировать ширину полезной нагрузки (не тянуть большие описания в грид, только краткие поля).

## Протоколы запросов (черновик)

### GET /api/products
- Query: `page, page_size, sort=field:asc|desc, filters[name|article|brand|category|status], search?`
- Ответ: `{items: ProductRow[], page, page_size, total}`.

### GET /api/products/{product_id}/candidates
- Query: `offset, limit, source?, include_rejected=false`.
- Ответ: `{items: Candidate[], total}`.

### POST /api/product-matches/{product_id}
- Body: `{competitor_id, reason?, confidence?, source?, mode?: "auto"|"manual"}`
- 200: `{ok: true, match: {...}}`
- 409: если уже есть ручной матч с другим конкурентом (надо подтверждение/overrides).

### DELETE /api/product-matches/{product_id}/{competitor_id}
- 200: `{ok: true}`
- 404: если связи нет.

### POST /api/product-matches/{product_id}/reject
- Body: `{competitor_id, reason?}`
- 200: `{ok: true}`

### GET /api/competitors/search
- Query: `q, brand?, source?, page, page_size`
- Ответ: `{items: Candidate[], total}`

## Ошибки/валидация (единый формат)
- `{error: "already_matched", message, details?}`
- `{error: "not_found", ...}`
- `{error: "validation_error", fields: {...}}`

## Статусы матчинга (для грида и бэка)
- `none` — нет пары.
- `auto` — автопредложение принято (confidence >= порога авто).
- `manual` — принято вручную.
- `ambiguous` — найдено несколько кандидатов, требуется ручное решение.
- `uncertain` — матч сомнительный (низкая confidence), требуется ручное решение.
- `multiple` — у товара несколько привязок (разные источники/вендоры).

## DTO (минимально)
- `ProductRow`: `{id, name, article, brand, category, status, margin?, price?, flags?: {needs_review?: bool, ambiguous?: bool}, current_match?: {competitor_id, source, sku, name, price?, confidence?, mode: "auto"|"manual"}}`
- `Candidate`: `{competitor_id, source, sku, name, brand, price?, stock?, confidence?, suggested: bool, rejected?: bool, reason?: string}`

## Отложенные решения
- Отклонённые кандидаты храним и не возвращаем в top-N, если `rejected=true`, кроме явного поиска.
- Историю действий показываем в панели (опционально).

## Вопросы/открытые решения
- Нужен ли лог «отклонённых» кандидатов (чтобы не предлагать повторно)? Если да — отдельный endpoint для mark-rejected.
- Какие статусы матчинга показываем в гриде (нет пары / авто / ручное / сомнительное / множественное)?
- Нужен ли экспорт Excel/CSV (если да — AG Grid Enterprise или собственный экспорт на backend).

## DoD (MVP)
- Грид с серверной пагинацией/фильтрами/сортировкой, hotkeys.
- Панель кандидатов с действиями «Принять/Снять», live search по конкурентам.
- Массовое применение автопар с порогом.
- Тосты/спиннеры, отображение статуса сопоставления.
- README-обновление: как запустить UI (dev), переменные окружения для API URL.

## Backend ToDo (чеклист)
- Эндпоинты, описанные выше, с серверной пагинацией/фильтрами и статусами (см. черновую реализацию `/api/matching/*` в `app/api/matching.py`).
- Фильтры: `status in (...)`, brand, category, search по name/sku.
- Авторизация — минимум Bearer token (или Basic), защищать `/api/matching/*`; CORS настроен через env.
- Сервис для «отклонённых» кандидатов, чтобы их не предлагать повторно.
- Логи/аудит: кто принял/снял матч, timestamp, reason.
- Ограничение RPS (rate limit) на поиск по конкурентам.
- Фича-флаги: включение UI/эндпоинтов, порог автопринятия.
- Статусная логика: `ambiguous` если нет матча, но >1 кандидата (по ценам) — реализовано упрощённо; `multiple` если >1 матча; `uncertain` — низкая confidence.

## Frontend ToDo (чеклист)
- Инициализация Vite + React + TS, AG Grid Community, React Query, Zustand.
- Components: `ProductsGrid`, `CandidatePanel`, `BulkActionsBar`, `ProgressBar/Stats`.
- Сервис API-обёртки (axios/fetch) с типами DTO.
- Состояние фильтров и пагинации в URL.
- Hotkeys и optimistic updates для accept/revoke.
- Дебаунс поиска кандидатов.
- Базовые тесты: рендер грида, загрузка кандидатов, accept/revoke mock API.
- Каркас реализован в `ui/` (грид + панель кандидатов, загрузка через `/api/matching/*`), нужны фильтры/пагинация/hotkeys/тосты.
