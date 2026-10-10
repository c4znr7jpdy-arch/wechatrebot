# Settings and chat configuration visual QA

Target: 0.9.9, manager incremental update. Source preview only; no installed robot was started or updated.

## Evidence

Local evidence directory: `C:/Users/Administrator/.codex/visualizations/2026/10/10/01a12503-0585-73f1-9dac-1c02f8985d86/`.

- Approved visual truth: `settings-concept-1.png`, `settings-chat-avatar-layout.png`.
- Browser-rendered implementation: `settings-core-implementation.png`, `settings-implementation.png`, `chat-implementation.png`, `private-small-dark.png`.
- Full comparisons: `settings-comparison.png`, `chat-comparison.png` (approved and implemented views in the same image).
- Focused right-panel comparison: `chat-focused-comparison.png`.
- Preview URL during review: `http://127.0.0.1:52157/?preview`.
- Desktop viewport and source images: 1440 × 1024. Implementation capture: 1440 × 1024 pixels, 1 pixel per CSS pixel. Full comparisons preserve each image's dimensions; the focused comparison normalizes the corresponding right-panel regions to equal panels for alignment review, not pixel-difference scoring.
- Narrow viewport: 1080 × 680, light and dark themes. Additional 800-pixel browser observation confirmed the chat panels stack instead of forcing the desktop columns.

The approved images contain a concept-title strip and a simplified application shell. The implementation retains the existing product navigation, status header, directory feedback and resize behavior. Comparisons prioritize the setting hierarchy and right-panel control alignment. Mock conversations differ from preview fixtures. Preview avatars use the existing fallback when no image URL is supplied; production still uses the original avatar renderer and image URLs. Private avatar images from the concept are not copied into product assets.

## Findings and comparison history

1. Initial rendered inspection found a startup error in the newly extracted profile controller and missing registration of its delivered asset routes. Fixed the function signature and registered both new assets. Startup and asset-route regressions pass.
2. Initial desktop comparison found inherited horizontal-tab underline styling and excess space around the chat header. Removed that underline for the vertical settings navigation, aligned the header actions, and normalized row spacing. Re-captured desktop implementation and compared both full views and the right-panel crop.
3. Narrow dark inspection (`chat-small-dark.png`) found low-contrast chat identifiers. Private-chat inspection also exposed an unstyled native profile selector. Applied existing semantic foreground/surface tokens and shared select dimensions; rechecked in `private-small-dark.png`. Low-frequency private rules and feature lists now collapse consistently with group rules.
4. Post-fix comparison found no remaining actionable P0/P1/P2 layout differences within the approved scope. Existing admin identity, schema-backed fields, revision protection and template override controls remain available. The mock's abbreviated, non-schema fields were not invented as new backend settings. These intentional content differences make the real settings page longer than the concept.

## Fidelity surfaces

- Typography: retains the product's existing bundled fonts and hierarchy; names truncate in the left list, identifiers wrap in the right panel. No substitute font or logo introduced.
- Spacing: vertical settings navigation, aligned label/control tracks, one profile selector, consistent toggle alignment and one policy save action. Automated geometry assertions verify equal select edges at both desktop widths.
- Colors: existing warm surface/green accent tokens in light mode, existing dark tokens in dark mode. Identifier contrast corrected.
- Images: existing group/contact avatar rendering retained, displayed alongside group name and member count. Unknown counts remain loading/failure states rather than fabricated zeroes.
- Copy: four direct setting categories; memory/emotion parameter forms removed; data maintenance retains memory management. A profile switch clearly confirms and verifies its separate application.

## Interaction and regression evidence

Ran:

```text
PYTHONPATH=src python -m pytest tests/test_settings_webui.py tests/test_manager_startup.py tests/test_core_settings.py tests/test_app_shell.py tests/test_feature_scope_ui.py tests/test_plugin_center.py -q
200 passed
git diff --check
```

The focused browser tests cover navigation, unsaved core draft cancellation, search draft preservation, unique schema fields, group member counts, template-management entry and control alignment. Profile tests cover confirmed application, cancellation, changed account/chat, concurrent binding change, failed readback, exact private origins, absent/ambiguous origins and offline failure. Startup tests exercise delivered resource routes and missing-controller handling. Browser console inspection found only the earlier fixed syntax error; current reloads and page-error assertions are clean.

Remaining acceptance boundary: installed-runtime behavior and real WeChat data are not claimed from preview fixtures. No configuration migration or memory/emotion reset was performed.

final result: passed
