# Design QA: вкладка «Закупки»

- Source visual truth: `/root/.codex/attachments/f8384511-c900-456a-8309-46ba18bbdb46/codex-clipboard-ddb0514d-fabc-4ff1-82cd-b16b19a61930.png`
- Implementation screenshot: `/root/.codex/visualizations/2026/07/14/019f6131-b844-7ad3-a664-a78086d760cd/procurement-design-implementation/desktop-1366.png`
- Combined comparison: `/root/.codex/visualizations/2026/07/14/019f6131-b844-7ad3-a664-a78086d760cd/procurement-design-implementation/comparison-desktop.png`
- Additional evidence: `tablet-1024.png`, `zoom-200-equivalent-683.png`, `mobile-390.png`, `mobile-390-full.png` in the same directory.
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

The mobile full-page screenshot was used as the focused comparison for filters, order-card conversion, distribution blocks and disclosure controls. No separate crop was required because the original-resolution full-page capture keeps these elements readable.

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

final result: passed

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

`final result: blocked`

Причина блокировки только визуальная: требуется открыть развернутую сборку в Bitrix24 и сделать браузерные снимки витрины и очереди переходов. Функциональная реализация и статические проверки не заблокированы.
