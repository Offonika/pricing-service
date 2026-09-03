# Design QA — ProcurementProductInsights

- Date: 2026-09-04
- Source visual truth: `/root/.codex/generated_images/01a06854-89aa-77c1-bf1d-846c82d58980/exec-593290e6-2ae2-435d-a00c-9ad4a25ff660.png`
- Implementation screenshot: `/tmp/mm-product-insights-qa.CHjQlu/desktop.png`
- Interaction screenshot: `/tmp/mm-product-insights-qa.CHjQlu/desktop-saved.png`
- Mobile screenshot: `/tmp/mm-product-insights-qa.CHjQlu/mobile.png`
- Combined comparison: `/tmp/mm-product-insights-qa.CHjQlu/comparison.png`
- Browser path: Browser plugin not available; Playwright Chromium CLI fallback explicitly approved by the user.

## Capture normalization

- Source image: `1487 × 1058` px, app-content crop approximately `1200 × 880` px.
- Desktop implementation: `1440 × 1181` px full-page screenshot; CSS viewport `1440 × 1000`, device scale factor `1`; app width `1220` CSS px.
- Mobile implementation: `390 × 2458` px full-page screenshot; CSS viewport `390 × 844`, device scale factor `1`; app `scrollWidth = clientWidth = 390`.
- Comparison image: equal `1200 × 880` app-content crops placed side by side. Bitrix24 shell chrome was excluded because it is not owned by this app surface.
- State compared: order `#14`, line `2`, quantity `25`, purchase price `1 RUB`, one hard display-family blocker.

## Evidence

The full-view comparison checks the product header, contextual order band, decision hierarchy, demand visualization, and inventory surface in the same initial state. Focused visual inspection was performed on the header/product image, order inputs and primary CTA, blocker action, profitability/defect signals, and the inventory rows. A separate post-interaction screenshot proves the successful save state.

Primary interaction tested:

1. Open the product insights route with `productId=1646&orderId=14&lineId=10`.
2. Change quantity from `25` to `30` and purchase price from `1` to `1.2`.
3. Press `Подтвердить строку`.
4. Verify `Строка подтверждена и сохранена` while remaining on the same product card.

Runtime evidence:

- page identity and title matched the intended product;
- meaningful body content rendered (`1131` characters in the initial state);
- framework overlay count: `0`;
- console errors and warnings: `0`;
- desktop save interaction: passed;
- mobile horizontal overflow: none.

## Required fidelity surfaces

- Fonts and typography: Inter/Segoe UI fallback, display heading weight, compact field labels, metric hierarchy, line height, and wrapping match the reference intent. Long product and blocker names wrap without collision.
- Spacing and layout rhythm: header, blue order band, four-part decision row, and two-column analysis area preserve the reference hierarchy. Desktop content is denser than the Bitrix shell mock because shell chrome is outside this component; mobile intentionally becomes a single column.
- Colors and tokens: white surfaces, pale-blue order context, Bitrix-like primary blue, red blocker state, and green positive signals match the source. No decorative gradients or custom CSS illustrations are used.
- Image quality and assets: the product image is rendered from `identity.photo_url`; the QA fixture uses the exact product crop from the selected source. Missing production photos use a Heroicons fallback rather than a fabricated image.
- Copy and content: labels and actions are operational, Russian, and source-backed. The blocker action opens the related order instead of presenting a non-functional control.
- Icons: standard Heroicons only, with consistent outline weight and accessible hidden/visible labels.
- Responsiveness and accessibility: semantic headings, labels, alt text, focus styling, disabled state, minimum-size inputs, and no horizontal overflow at `390` px.

## Intentional constraints

- The source mock draws a daily line chart. The current API provides only verified aggregate rates for 30/90/180 days, so the implementation uses three honest comparative bars and does not invent daily history. This is an intentional product-data constraint, not an unresolved visual defect.
- The exact Bitrix24 sidebar/header is not recreated because the component runs inside Bitrix24 and owns only the embedded app content.

## Comparison history

### Iteration 1

- P2: the main blocker matched visually but lacked the source action `Проверить распределение`.
- Fix: added a real action using the first resolution label and linked it to the related order with the affected line context.
- Post-fix evidence: `/tmp/mm-product-insights-qa.CHjQlu/comparison.png`; the action is visible beneath the blocker description and no new layout collision appears.

### Iteration 2

- No actionable P0/P1/P2 differences remained.
- Desktop and mobile captures passed page identity, content, overlay, console, interaction, and overflow checks.

## Follow-up polish

- P3: replace aggregate bars with the source-style daily line chart only after a trustworthy dated sales-series contract is added to the API.

final result: passed
