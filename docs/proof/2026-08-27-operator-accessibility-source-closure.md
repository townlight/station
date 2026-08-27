# S20 operator accessibility source-closure evidence

Date: 2026-08-27
Scope: `web/operator` source, focused on the routed operator shell and shared controls.
Standard: WCAG 2.1 AA source review criteria.

## Result

This bounded source slice is complete. The review found and corrected three inherited source issues:

| Finding | WCAG relationship | Severity | Closure |
| --- | --- | --- | --- |
| The fixed mobile navigation placed the primary route set and CG moderation entry into a six-column grid. At narrow widths, controls could be cramped and extend beyond the usable navigation surface. | WCAG 2.1 AA: 2.1.1 Keyboard, 2.4.3 Focus order, 1.4.10 Reflow. Enhanced target objective: 2.5.5 Target Size (AAA). | Major | The mobile navigation is a horizontally scrollable strip. Each exposed route remains a real button with `aria-current`, a 72px layout slot, and a 56px minimum height. |
| Shared text-action buttons used 8px padding with no minimum dimensions, and a later CG rule reduced compact Edit/Delete controls to 30px high. | Enhanced 44×44 design target corresponding to 2.5.5 Target Size (AAA), not a WCAG 2.1 AA conformance claim. | Moderate | `.text-button` and the effective compact CG action overrides now enforce 44px minimum width and height while preserving the existing visible focus outline. |
| Form helper text used `#84938f`, which is not a reliable 4.5:1 normal-text foreground on the white form surfaces. | 1.4.3 Contrast | Major | `label small` now uses the audited `#5d746b` foreground. |

The source audit also checked the routed shell, authentication, media, channels, schedule, live, automation, publishing, metadata, recordings, reporting, health, commissioning, and CG surfaces for semantic landmarks, nested form labels, keyboard controls, focus indicators, status/error regions, table semantics, meaningful image alternatives, and reduced-motion handling. Existing source protections remain in place: the shell has a skip link and `main`/`nav` landmarks, route changes move focus to the workspace, CG tabs implement roving keyboard focus, confirmation actions restore focus, reporting tables have captions and column scopes, the lifecycle thumbnail has meaningful alt text, and reduced motion disables transitions/animations.

Decorative CG empty-state glyphs are now explicitly hidden from assistive technology. The shared empty-state action is explicitly `type="button"` so it cannot accidentally submit a containing form.

## Verification

- `npm test`: 99/99 passed, including a rule-order test that inspects every explicit text-action minimum and fails on a later sub-44px override.
- `npm run build`: passed with TypeScript and Vite 7.3.6; Vite emitted only the existing large-chunk warning (`operator.js` is approximately 509 kB minified).
- `git diff --check`: passed.

The build was run from this isolated source clone with the normal machine filesystem because the restricted command sandbox could not let Vite's esbuild child resolve the isolated config path. The exact resulting tracked `web/operator/dist/index.html`, `operator.css`, and `operator.js` files are intentionally included because the Rust station binary embeds those production assets.

## Explicit limitations

This evidence does not claim a manual keyboard-only run, NVDA or another screen-reader run, measured browser contrast across every composited/gradient state, 200% browser zoom inspection, installed-machine behavior, or field usability. Those remain S20 capability gaps.
