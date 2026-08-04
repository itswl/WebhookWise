# Design Language

The dashboard is an operations console: a screen someone glances at for hours,
where the only thing that should ever grab the eye is a severity. Every rule
below serves that sentence. Most rules are enforced by contract tests in
`tests/runtime/test_dashboard_static_contracts.py` — where a rule has an
enforcing test, it is named; treat a red contract as the design system talking.

## Principles

1. **Dark is the base state, structurally.** `:root` in
   `templates/static/css/dashboard.css` IS the dark palette; `.theme-light` is
   the override. An unstyled first paint must land on the intended design, and
   any new rule inherits the dark value by default.
   *(test_dark_is_the_base_theme_not_an_override)*

2. **Colour carries meaning, never decoration.** One interactive accent
   (`--primary`, "you can act here"). One hue per severity
   (`--danger/--warning/--success/--info`), separated by hue, not brightness,
   because severities are scanned peripherally. Anything coloured that is
   neither interactive nor a state is a bug.

3. **Colour lives in points, not surfaces.** Status badges are neutral
   surfaces (transparent, hairline border, secondary text); the status is one
   point of colour — the badge's icon, or a 6px `.ww-dot` when it has none.
   Left-border accents exist only for STATES (failing delivery, active
   silence, queue backlog, action severity) and are 3px; a 4px accent or an
   inline badge background is decoration and is banned.
   *(test_colour_stays_in_points_not_surfaces)*

4. **All colour flows through tokens.** No hex literals in JS renderers —
   inline literals freeze mid-redesign and ignore theming forever. Use
   `var(--…)` in generated style strings, or better, a class.
   *(test_js_renderers_carry_no_hardcoded_colors)*

5. **One icon voice.** The self-hosted sprite in `dashboard.html` (stroke,
   24×24, `currentColor`) rendered via `wwIcon(name)`. Emoji are banned as
   iconography: they ship their own palette, render differently per OS, and
   vary in weight. Icons are decorative (`aria-hidden`); meaning must always
   be in adjacent text. Never pass `wwIcon()` output through `escapeHtml()`.
   *(test_dashboard_uses_the_icon_system_not_emoji,
   test_every_icon_reference_resolves_to_a_sprite_symbol)*

6. **Five type sizes, one job each.** `--fs-xs` badges/eyebrows/meta,
   `--fs-sm` secondary/dense cells, `--fs-md` body, `--fs-lg` panel titles,
   `--fs-num` stat figures. A size outside the scale is a design bug; the
   ratchet only tightens. Every figure an operator compares down a column
   gets `font-variant-numeric: tabular-nums`.
   *(test_font_sizes_stay_on_the_scale_ratchet)*

7. **Elevation is background steps and borders, not shadows.** Dark UIs read
   depth from `--bg-base → --bg-surface → --bg-elevated → --bg-subtle`.
   Shadow is reserved for layers that genuinely float: the palette, modals,
   the mobile drawer. When everything casts a shadow, nothing reads as
   elevated. Cards nested in cards dissolve into divided regions.

8. **One motion voice, one focus treatment.** 0.15s transitions; anything
   slower reads as decoration. `:focus-visible` ring only — mouse clicks
   don't flash rings, keyboard users always get one.

9. **Spacing sits on a 4px grid** (`--sp-1…6`). Repeated inline patterns get
   semantic classes (`.ww-empty`, `.ww-error`, `.ww-eyebrow`, `.ww-meta`,
   `.ww-muted`, `.ww-mono`, `.ww-num`, `.ww-dot*`) instead of copy-pasted
   style strings.

10. **Navigation has one model.** Destinations live in `CommandPalette._groups`
    (the single source); the sidebar renders from it, `navigateTo(slug)` is
    the only way to jump, every destination has a `#/slug` URL, and
    `recordDestination` — called by the setter that actually entered the view
    — is the sole writer of URL/breadcrumb/highlight state.
    *(test_sidebar_renders_the_same_map_as_the_palette,
    test_every_destination_is_reachable_by_url,
    test_one_click_navigation_invariants)*

11. **Text degrades, never leaks.** An untranslated label falls back to its
    slug, never to a raw `nav.dest.*` key; a failed refresh says so, because
    silence must never look like "nothing new".
    *(test_sidebar_never_shows_raw_i18n_keys)*

## When adding UI

- Colour: does it mean interactive or a state? If neither, remove it.
- New icon → add a `<symbol>` to the sprite, reference by name; check
  `test_every_icon_reference_resolves_to_a_sprite_symbol` passes.
- New destination → add to `CommandPalette._groups` only; sidebar, palette,
  URL, and contracts follow automatically.
- New repeated style → a semantic class in `components.css`, not an inline
  string.
- Numbers people compare → tabular numerals.
- Feishu/DingTalk card emoji in `services/` are product output, not UI — this
  document does not apply there. The lite/ edition keeps its own single-file
  style on purpose.

## Known debt (honest ledger)

- ~600 inline `style="…"` strings remain in JS renderers (the semi-unique
  long tail). Migrate opportunistically when touching a view; never add new
  ones for patterns a class already covers.
- 17 raw font-size values remain in CSS/HTML under the ratchet (bound: 24,
  lower it as they migrate).
- The light theme passed a screenshot review (overview/alerts/rules, 2026-08-04)
  after the restraint pass: no known visual gaps. Dark remains the default and
  the reference; light gets re-checked when tokens change, not per-PR.
- Spacing tokens exist but are not yet contract-enforced.
