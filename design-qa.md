# Design QA: помощник заказов, вариант «Решение сначала»

- Source visual truth: `/root/.codex/generated_images/019fbd4c-7110-7461-9a9f-e21a5eac5cac/exec-ec454811-7318-479e-b0d0-b7744eb7fbb7.png`.
- Implementation screenshot: `/opt/MM/.worktrees/pricing-order-assistant-panel-option2-20260802/.local/design-qa/pricing-ui/test-results/procurement-order-assistan-c9fae--CSV-and-accessibility-work-chromium/implementation-pending-1440x1024.png`.
- Combined comparison: `/opt/MM/.worktrees/pricing-order-assistant-panel-option2-20260802/.local/design-qa/pricing-ui/procurement-order-assistant-comparison.png`.
- Focused panel comparison: `/opt/MM/.worktrees/pricing-order-assistant-panel-option2-20260802/.local/design-qa/pricing-ui/procurement-order-assistant-panel-comparison.png`.
- Focused table comparison: `/opt/MM/.worktrees/pricing-order-assistant-panel-option2-20260802/.local/design-qa/pricing-ui/procurement-order-assistant-table-comparison.png`.
- Production-data evidence: `/opt/MM/.worktrees/pricing-order-assistant-panel-option2-20260802/.local/design-qa/pricing-ui/test-results/procurement-order-assistan-0b148-panel-layout-without-writes-chromium/production-realdata-1440x1024.png`.
- Viewport: `1440×1024` CSS px, device scale factor `1`.
- Source pixels: `1487×1058`; normalized to `1440×1024` for comparison.
- Implementation pixels: `1440×1024`, without density conversion.
- State: supplier panel open, one ready row selected, one unavailable row, classification proposal pending.

## Findings

No actionable P0/P1/P2 findings remain.

- Fonts and typography: the implementation keeps the existing `Inter / Segoe UI / Arial` stack and matches the target hierarchy. The dense table uses smaller operational text while headings, decisions and monetary values remain prominent.
- Spacing and layout rhythm: the right panel occupies `560 px` at the reference viewport. The compact table, selected supplier card and assembly action now remain in the first viewport, matching the target task hierarchy.
- Colors and tokens: the existing blue-white `pricing-service` palette is preserved. Ready, warning and unavailable states use both color and explicit text. All small panel labels were darkened after the first Axe pass.
- Image quality and assets: product thumbnails use the API-provided media URLs; the original WebP URL remains a separate link and is not downloaded or recompressed. Interface icons use Heroicons rather than CSS or text-glyph approximations.
- Copy and content: the panel leads with the working decision, then classification, lead time, official 1C terms and defect evidence. Missing facts remain explicitly labelled instead of being invented.
- Responsiveness: at widths up to `820 px` the supplier panel becomes a right-side overlay; the underlying assistant stays usable after closing it.
- Accessibility: Playwright + Axe WCAG 2.0/2.1 A/AA reports zero violations. Close, decision, filter, selection and package controls have accessible names and keyboard focus styles.

## Full-view and focused comparison evidence

The combined full-view comparison was inspected for overall hierarchy, panel proportion, above-the-fold task flow, table density and assembly-action visibility. The panel-focused comparison was used for typography, classification controls, terms, lead-time equation and quality sections. The table-focused comparison was required because the first implementation kept an oversized sticky decision column that could not be judged reliably from the full view alone.

## Interaction and runtime checks

- Open and close supplier panel; reopen it from the supplier name and class badge.
- Toggle individual ready rows and select all fully ready projects.
- Block unavailable rows with a concrete reason.
- Apply quick filters, advanced filters, search and reset.
- Require a rejection reason and call the versioned rejection API.
- Prepare both supplier CSV variants and preserve card/original-photo URLs.
- Assemble only fully selected ready projects; no automatic 1C submission.
- Mocked end-to-end flow: console/page/request errors `0`, Axe violations `0`.
- Read-only current production-data flow: API errors `0`, page errors `0`, Axe violations `0`; no write endpoint was called.

## Comparison history

1. First browser pass found a P1 accessibility issue: `#748196` labels at `9 px` did not meet WCAG AA contrast, and the lead-time equation used invalid children inside a definition list.
2. Fixed by using darker label tokens and semantically separate definition lists. Post-fix Axe result: zero violations.
3. Second visual pass found a P2 density issue: the old `260 px` sticky decision column hid risk columns, forced row wrapping and moved assembly below the first viewport.
4. Fixed by moving classification decisions into the supplier panel, sizing the table columns for the new responsibility split, compacting row media and reducing non-essential vertical spacing.
5. Post-fix evidence shows all requested columns, the selected supplier summary and the primary assembly action in the `1440×1024` viewport.

## Follow-up polish

- P3: when a dedicated supplier-card route is added, the truthful `Открыть проект` action in the panel header can be replaced with a direct supplier-card link.

final result: passed
