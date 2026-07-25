# twoHelixes design system

The product's promise is *legibility* — it shows its working. The visual
language has to earn that: calm, precise, generous with space, and never
decorative at the expense of the data. Museum, not dashboard-vendor.

Everything here is implemented in `interp/twohelixes/routes/pages.py` (`_css()`)
and `web/src/app.css`. If a value appears in a component, it comes from a token
below; nothing is hand-picked at the call site.

---

## 1. Colour

### The rule that overrides taste

**Chart colours are validated, not chosen.** The eight categorical hues, the
sequential ramp and the diverging pair in `charts/palette.py` passed a
six-check validator — lightness band, chroma floor, colour-blind separation,
normal-vision floor, contrast against surface — in *both* light and dark mode.

Do not edit them to match a mood board. If the brand needs different hues, run
the validator on the candidates and take only a passing set. The ordering is
the colour-blind-safety mechanism, not a preference.

| Slot | Hue | Light | Dark |
|---|---|---|---|
| 1 | blue | `#2a78d6` | `#3987e5` |
| 2 | orange | `#eb6834` | `#d95926` |
| 3 | aqua | `#1baf7a` | `#199e70` |
| 4 | yellow | `#eda100` | `#c98500` |
| 5 | magenta | `#e87ba4` | `#d55181` |
| 6 | green | `#008300` | `#008300` |
| 7 | violet | `#4a3aa7` | `#9085e9` |
| 8 | red | `#e34948` | `#e66767` |

Slot 1 doubles as the **brand accent** (`--accent`). That is deliberate: the
interface and the charts share one blue, so a link and a first series read as
the same system.

### UI chrome — free to tune

Chrome is *not* the chart palette and can change without revalidation.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--surface-1` | `#fcfcfb` | `#1a1a19` | page background |
| `--panel` | `#ffffff` | `#1f1f1e` | cards, figures, sheets |
| `--panel-2` | `#f7f7f5` | `#252523` | inset areas, table hover |
| `--border` | `#e6e5e1` | `#302f2d` | default hairline |
| `--border-strong` | `#d5d4cf` | `#3d3c39` | hover, inputs |
| `--text-primary` | `#0b0b0b` | `#ffffff` | headings, values |
| `--text-secondary` | `#52514e` | `#c3c2b7` | body |
| `--text-muted` | `#7a7973` | `#8f8e85` | captions, axis labels |

The neutrals are **warm** (`#fcfcfb`, not `#ffffff`; `#1a1a19`, not `#000`).
A pure-grey UI next to saturated chart colours reads clinical; a bone-white
paper tone reads considered, and it is easier on the eye over a long session.

### Two rules people get wrong

1. **Text never wears a series colour.** Values, labels and legends stay in ink
   (`--text-*`). A coloured swatch *beside* the text carries identity.
2. **Status colours are reserved.** good/warning/serious/critical never double
   as "series 4", and always ship with an icon or label, never colour alone.

---

## 2. Typography

One family: **Inter**, with the system stack behind it.

| Role | Size | Weight | Tracking |
|---|---|---|---|
| Hero h1 | `clamp(2.15rem, 5.2vw, 3.6rem)` | 700 | `-0.032em` |
| Section h2 | `clamp(1.45rem, 3vw, 2rem)` | 670 | `-0.02em` |
| Card h3 | `1rem` | 640 | `-0.01em` |
| Body | `0.93–1rem` | 400 | normal |
| Caption | `0.82rem` | 400 | normal |
| Eyebrow / label | `0.7–0.78rem` | 650 | `+0.05em`, uppercase |

**Tracking tightens as size grows.** Large text at default tracking looks
loose and amateur; the negative values above are the single highest-leverage
typographic fix on the page.

**Measure is capped.** Hero lede `46ch`, body `58ch`. Past ~70 characters the
eye loses the line return.

Numbers in stats and tables use `font-variant-numeric: tabular-nums` so digits
align in a column and do not shimmer when they update.

---

## 3. Space

One scale, `--s1` … `--s8`: **4, 8, 12, 16, 24, 32, 48, 64px**. No arbitrary
values. If something needs 20px, it wants 16 or 24 — pick one.

Section rhythm is `clamp(2rem, 4vw, 3.25rem)` vertical. Earlier drafts used
double this and the page read as empty rather than airy: **space is earned by
content density, not applied uniformly.** Once the sections carried real
charts, the padding came down and the page got better.

Radii: `--r-sm 8px` (controls, chips), `--r-md 12px` (stats, code), `--r-lg
16px` (cards, figures, sheets), `--r-full` (pills). Never mix more than two on
one component.

---

## 4. Elevation

Two-layer shadows only:

```
--sh-1: 0 1px 2px rgba(16,16,14,.05), 0 1px 3px rgba(16,16,14,.04)
--sh-2: 0 2px 4px rgba(16,16,14,.05), 0 6px 16px rgba(16,16,14,.06)
--sh-3: 0 8px 24px rgba(16,16,14,.10), 0 2px 6px rgba(16,16,14,.06)
```

A tight contact shadow plus a soft ambient one. A single large blur reads as a
smudge at these sizes. Dark mode uses higher opacity because shadows on dark
surfaces need more to register.

Levels map to meaning: `sh-1` resting, `sh-2` hover and figures, `sh-3`
overlays only.

---

## 5. Motion

Restrained and fast: `120–200ms`, `ease`. Buttons lift 1px on `:active`; cards
lift 1px and brighten their border on hover. Nothing moves on scroll, nothing
parallaxes.

`@media (prefers-reduced-motion: reduce)` disables every transition and
transform. The helix spinner slows to a quarter speed rather than stopping, so
it still reads as "working".

---

## 6. Texture — film grain

A very fine noise overlay sits above the page background at low opacity. It
does two things: it stops large flat fields from banding on cheap panels, and
it gives the bone-white surface a paper quality that flat CSS colour cannot.

Rules: **grain never sits over a chart, a control or body text.** It applies to
the page background and to art plates only, at `opacity ≤ 0.06`, and it is
`pointer-events: none`. If it is visible as *texture* rather than felt as
*material*, it is too strong.

---

## 7. Art direction

The generated plates are **Greek futurism**: classical marble geometry —
columns, amphorae, platonic solids — rendered as clean studio product
photography, with a polished-glass double helix as the recurring motif.

Constraints every plate obeys:

- palette pinned to the brand blues, teals and bone white; no foreign hues
- one soft key light, long calm shadow, matte marble
- generous negative space, subject anchored off-centre so text can overlay
- no text, no logos, no people
- exported **WebP q85** — the quality floor where marble gradients stay clean

Art is atmosphere, never information. It sits behind or beside content and
never competes with a chart. Any page can drop every plate and still work.

---

## 8. Charts in the page

Chart SVGs are rendered server-side by `charts/svg.py` and embedded inline,
which buys three things:

- **`theme_vars=True`** emits `var(--text-primary, #0b0b0b)` instead of literal
  hex. Inline SVG inherits the page's custom properties, so one rendering is
  correct in light *and* dark with no JavaScript. The literal stays as the
  fallback so a downloaded file still looks right.
- **`transparent=True`** omits the background rect so the figure frame supplies
  the surface. Without it a chart draws its own background and reads as a box
  inside a box.
- The marketing examples run through the *same* `defaults.apply` as customer
  charts and are audited. A regression in chart defaults visibly changes the
  homepage — which is the point.

---

## 9. Component rules worth stating

**Buttons.** Primary is accent-filled with `sh-1`, lifting to `sh-2` on hover.
Ghost is `--panel` with a `--border-strong` hairline. Never more than one
primary in a viewport region.

**Cards.** `--panel`, 1px `--border`, `--r-lg`, `sh-1`. On hover: border to
`--border-strong`, shadow to `sh-2`, `translateY(-1px)`.

**Overlays.** Sign-in and checkout are sheets over a blurred scrim, not pages.
A visitor deciding to buy on the pricing page stays on the pricing page.
Escape and backdrop-click both dismiss; focus returns to the trigger.

**Figures.** Chart in a `--panel` frame with `sh-2` and a muted caption. The
caption states what the chart demonstrates, not what it contains.

---

## 10. Accessibility floor

- `:focus-visible` is a 2px accent outline at 2px offset, everywhere, never
  removed.
- Body text clears 4.5:1; muted captions clear 4.5:1 against their surface.
- Three light-mode chart slots sit under 3:1 — charts using them **must** ship
  a legend or direct labels (the relief rule), enforced by `defaults.audit()`.
- Every overlay is `role="dialog" aria-modal="true"` with a labelled heading.
- Colour is never the only signal: series get legends or direct labels, status
  gets an icon and a word.
- Reduced motion is honoured.

---

## 11. Checklist before shipping a page

1. Does every spacing value come from the scale?
2. Do headings use the tracking for their size?
3. Is any text wearing a series colour?
4. Does it work in dark mode — *checked*, not assumed?
5. Does `visualbench` report zero horizontal overflow at 390px?
6. Do the charts pass `defaults.audit()`?
7. Is grain over any chart, control or paragraph? (It must not be.)
8. Does the page still read with every image removed?
