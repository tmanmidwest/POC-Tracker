# Questlog — brand brief

App formerly known as **POC Tracker**. New name: **Questlog**.
A pre-sales POC tracking tool for sales engineers. The concept: every POC is a
quest — with objectives, a status, and a reward (the deal). The UI can lean into
that framing lightly (never so hard it stops being a serious daily-use tool).

## Naming
- Product name: **Questlog** (one word).
- Wordmark is always **lowercase**: `questlog`. Tight letter-spacing (~-0.02em),
  font-weight 500. Lowercase keeps it in dev-tool territory (grep, sudo, kubectl)
  rather than corporate SaaS.
- If migrating from the old name, find-and-replace `POC Tracker` / `POC-Tracker`
  with `Questlog` across UI strings, titles, package name, README, etc. Keep the
  underlying `poc` domain language in the data model (POCs are still POCs) — only
  the product brand changes.

## The mark
A stylized quest-giver exclamation marker — the golden "!" that floats over an
NPC with something for you to do. Reads instantly to any RPG player; stripped
down it's just a clean glyph that also means "this needs attention." Geometry:
a tapered vertical bar (trapezoid) over a diamond dot, both in the accent color,
on a rounded-square tile (rx = 22 on a 96×96 tile ≈ 23% radius).

Files provided:
- `icon/questlog-icon-midnight.svg` — primary app icon (dark tile, gold mark)
- `icon/questlog-icon-gold.svg` — loud variant (gold tile, dark mark)
- `icon/questlog-icon-teal.svg` — security-flavored variant (teal tile, cream mark)
- `icon/questlog-mark-mono.svg` — glyph only, `fill="currentColor"` for inline
  use in nav/buttons/loading states (inherits surrounding text color)
- `QuestlogLogo.tsx` — React component (icon + optional wordmark)

## Palette (hex, mode-stable — do not invert these in dark mode)
| Token                | Hex       | Use                                  |
|----------------------|-----------|--------------------------------------|
| midnight tile        | `#1c1b18` | primary icon background              |
| midnight border      | `#3a3833` | 0.5px hairline on dark tile if needed|
| quest gold (accent)  | `#EF9F27` | the mark; primary brand accent       |
| gold deep (on-gold)  | `#412402` | text/mark on a gold surface          |
| identity teal        | `#0F6E56` | alt tile; secondary brand accent     |
| teal cream (on-teal) | `#E1F5EE` | text/mark on a teal surface          |

Suggested CSS custom properties:
```css
:root {
  --ql-ink:        #1c1b18;
  --ql-accent:     #EF9F27; /* quest gold */
  --ql-accent-ink: #412402;
  --ql-teal:       #0F6E56;
  --ql-teal-ink:   #E1F5EE;
}
```
Accent gold (`#EF9F27`) pairs with dark ink text (`#412402`) — never white text
on gold (fails contrast). On the midnight tile, gold-on-dark is the correct pair.

## Tagline options
- `Every POC is a quest — track it, win it.` (balanced default)
- `Grind the eval. Claim the win.` (leans gamer)
- `Your pre-sales quest log.` (understated)
- `Six campaigns. One log. Zero dropped quests.` (the SE juggling act)

## Optional theme language (use sparingly)
If you want the RPG framing to carry into the UI, map existing concepts rather
than inventing new ones:
- a POC → a "quest" (or "campaign")
- POC use cases / requirements → "objectives"
- technical win → "boss cleared" / "objective complete"
- an active empty state → "no active quests" instead of "no records"
Keep status labels, dates, and data plain and professional. The theme is a wink
in copy and iconography, not a reskin of every field.

## Do / don't
- Do use lowercase `questlog` for the wordmark everywhere.
- Do use the mono mark (currentColor) for small inline spots so it adapts to
  light/dark automatically.
- Don't recolor the icon tiles per theme — they're fixed brand colors.
- Don't put white text on the gold surface.
- Don't over-theme: it's a tool SEs live in daily, not a game.
