# Design QA: «Помощник заказов»

- Source visual truth: `docs/design-qa/order-assistant/reference-assistant-1486x1059.png`.
- Source pixels: `1486 × 1059`, RGB PNG.
- Implementation: `/bitrix/procurement-order-formation/assistant`, компонент `ui/src/components/ProcurementOrderAssistant.tsx`.
- Implementation screenshot: `docs/design-qa/order-assistant/implementation-1486x1059-final.png`.
- Combined comparison: `docs/design-qa/order-assistant/comparison-approved-vs-final.png`.
- Responsive evidence: `implementation-tablet-1024x900.png` и `implementation-mobile-390x844.png` в том же каталоге.
- Target viewport: `1486 × 1059` CSS px, `deviceScaleFactor=1`.
- Density normalization: не применялась; эталон и desktop-снимок имеют одинаковый размер и плотность.
- State: таблица помощника, быстрый фильтр `Все`, выбранные готовые строки, раскрытая карточка поставщика и меню пакета.

## Findings

- Открытых P0/P1/P2 замечаний нет. В desktop-состоянии полностью видны колонка
  `Решение`, правая панель и основная кнопка сборки; длинный быстрый фильтр не
  обрезается.
- Таблица не содержит колонку `Что мешает`; присутствуют фото, потребность,
  поставщик, цена, рентабельность, брак, срок и решение. Правая панель показывает
  классы A/B/C, исторические показатели и финансовые условия.
- Шрифты и типографика используют существующий стек продукта
  `Inter, Segoe UI, Arial`; плотность, переносы и иерархия сопоставлены с
  эталоном в общем изображении `1486 × 1059`.
- Ритм и компоновка сохраняют основное соотношение таблицы и правой панели.
  Первая карточка раскрыта, карточки B/C компактны, поэтому CTA остаётся в
  первом экране.
- Семантические зелёные, оранжевые и красные состояния читаются без потери
  контраста. Axe не нашёл critical/serious нарушений WCAG 2 A/AA и 2.1 A/AA.
- Качество изображений: интерфейс показывает реальную миниатюру и открывает
  исходный URL без повторного сжатия. Подмена товарных фото заглушками или
  нарисованными активами не используется.
- Тексты: русские подписи соответствуют утверждённому сценарию; отсутствие
  истории обозначается явно, показатели не выдумываются.

## Full-view and focused comparison evidence

Эталон и финальная реализация объединены в `comparison-approved-vs-final.png`
без масштабирования. Сравнение проверено по шапке, быстрым фильтрам, плотности
таблицы, фотографиям, правой панели, раскрытому меню пакета и доступности CTA.
Отдельные responsive-снимки подтверждают одноколоночную компоновку на мобильном
и отсутствие горизонтального переполнения всей страницы; широкая таблица и
быстрые фильтры прокручиваются внутри своих областей.

## Interaction and runtime checks

- Playwright проверил быстрый фильтр брака, поиск в расширенных фильтрах и сброс.
- Скачаны и проверены `Список + фото` и `Фото отдельно`; оба manifest-файла
  содержат ссылки на оригинальные изображения.
- Desktop `1486 × 1059`, tablet `1024 × 900` и mobile `390 × 844` отрисованы без
  горизонтального переполнения документа.
- Ошибок browser console и `pageerror` нет; critical/serious нарушений Axe нет.
- Component tests подтверждают загрузку данных, исходную ссылку фото, безопасную
  сборку полностью выбранного проекта и блокировку проекта без фото.

## Comparison history

1. Первый снимок выявил обрезанную колонку `Решение`, обрезку длинного фильтра и
   недоступный без прокрутки CTA из-за трёх раскрытых карточек.
2. Таблица и фильтры уплотнены, первая карточка оставлена раскрытой, B/C переведены
   в компактное состояние; в шапку добавлена дата.
3. Axe выявил недостаточный контраст мелкого текста и неверную структуру `dl`;
   оба нарушения исправлены.
4. Контрольный Playwright-прогон прошёл полностью.

final result: passed

---

# Архив: Design QA вкладки «Закупки»

- Source visual truth: `docs/design-qa/procurement-dashboard/reference-dashboard-1366x768.png`
- Implementation screenshot: `docs/design-qa/procurement-dashboard/implementation-desktop-1366x768.png`
- Combined comparison: `docs/design-qa/procurement-dashboard/comparison-desktop-1366x768.png`
- Additional evidence: раздельные снимки tablet 1024×900, эффективной ширины 200% (683×768) и mobile 390×844 в этот архив не сохранены; ниже описаны только фактически приложенные desktop-доказательства.
- Viewports: 1366×768, 1024×900, 683×768 as the 200% effective-width check, 390×844.
- State: procurement snapshot v2, 124 open orders, 43 risk actions, full-access amounts.

## Findings

No actionable P0/P1/P2 findings remain.

- Typography follows the existing dashboard family, weights and hierarchy. KPI values remain the strongest level; labels, hints and table copy retain readable contrast.
- Layout now follows the intended operational hierarchy: compact status, KPI, action queue, analytical breakdowns. The first action row starts at y=621.6 and is visible at 1366×768.
- Existing color tokens remain in use. Warning and critical states also have visible Russian labels and badges, so meaning does not depend on color alone.
- The screen contains no raster product imagery or custom graphic assets. The proportional strips are native quantitative UI, not substituted artwork.
- Copy is Russian and operational. The raw ISO timestamp and `read-only snapshot` wording are no longer exposed on the first screen.
- At 390 px the table becomes readable order cards without horizontal page overflow; filters stack to one column. At the 683 px effective-width check the first order card width is 629 px and remains within the viewport.

## Full-view and focused comparison evidence

The combined desktop image was inspected for overall hierarchy, density, typography, spacing, colors and above-the-fold content. The original screen spends the first viewport on duplicate status and analytical lists; the new screen intentionally exposes two action rows in the same area while preserving the established visual language.

The mobile full-page state was reviewed live during the run for filters, order-card conversion, distribution blocks and disclosure controls; that capture was not retained in this archive, so only the desktop comparison above is attached as a saved artifact.

## Interaction and runtime checks

- Default queue contains five rows; «Показать все 43» expands it to 43.
- «Об источнике» opens and exposes the human-readable source timestamp.
- Desktop, tablet, effective 200% width and mobile states rendered with zero browser console errors.
- Keyboard behavior is covered by the existing Enter/Space handler and visible `:focus-visible` styles.

## Comparison history

1. Initial responsive capture found a P2: the mobile table inherited the common 980 px minimum width.
2. Fixed with a procurement-specific compound selector overriding the shared table minimum width.
3. Post-fix evidence: the 390 px order card is 336 px wide; the full mobile screenshot has no horizontal table overflow.

## Follow-up polish

- P3: a future dashboard-wide pass may further compact the common header and long tab navigation, but changing other tabs is outside this release.

archived result: passed

---

## Архив предыдущей проверки: «Формирование заказа»

Дата: `2026-07-10`.

### Источники

- Основной макет: `reports/assortment_lifecycle/2026-07-10/order-formation-app-final-showcase-2026-07-10.png`.
- Очередь переходов: `reports/assortment_lifecycle/2026-07-10/order-formation-lifecycle-transition-queue-2026-07-10.png`.
- Реализация: `ui/src/components/ProcurementOrderFormationWorkspace.tsx`.
- Стили: `ui/src/App.css`.

### Проверено по коду и сборке

- Тёмная шапка приложения и четыре вкладки.
- Жизненный порядок карточек и отдельный action-бейдж.
- `Рабочий → Review`, вложенный `ДН`, ручные статусы и таблица внимания.
- Полноэкранная очередь без автоматического выбора строк.
- Выбор только готовых строк, лимит `100`, одиночное и пакетное утверждение.
- Реестр заказов, карточка заказа, очередь свойств и журнал истории.
- Адаптивные состояния для узкого экрана.
- `npm run lint` проходит без новых ошибок; `npm run build` успешен.

### Не выполнено

- Нет браузерного снимка реализованного интерфейса в OAuth-контексте Bitrix24.
- Поэтому не выполнено обязательное попиксельное сравнение реализации с двумя эталонными PNG при одинаковом viewport и состоянии.

### Итог предыдущей проверки

`archived result: blocked`

Причина блокировки только визуальная: требуется открыть развернутую сборку в Bitrix24 и сделать браузерные снимки витрины и очереди переходов. Функциональная реализация и статические проверки не заблокированы.
