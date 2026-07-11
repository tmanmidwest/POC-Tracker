# Executive readout deck (.pptx)

A one-click **PowerPoint readout** of a single POC — the deck you present at the
exec wrap-up. It's built from the same data as the on-screen report, so the
numbers always match. An optional AI pass writes slide-ready bullets, and admins
can drop in a branded template so every deck matches the company theme.

- [Overview](#overview)
- [What's in the deck](#whats-in-the-deck)
- [Downloading a deck](#downloading-a-deck)
- [AI-written narrative](#ai-written-narrative)
- [Branded template](#branded-template)
- [Brand logo](#brand-logo)
- [Speaker notes](#speaker-notes)
- [How it's built](#how-its-built)
- [Access & visibility](#access--visibility)

---

## Overview

The readout deck is a **presentation** view of a POC (not the full report). It's
generated on the fly with [python-pptx](https://python-pptx.readthedocs.io/) —
pure Python, no system libraries — and downloaded straight from the report page.
Every figure comes from the project's own data, so a deck can be produced with
**no AI provider configured**. When a provider *is* configured, an opt-in AI pass
enriches the summary and next-steps slides; when an admin uploads a template, the
generated slides sit on top of its theme.

## What's in the deck

| Slide | Contents |
|---|---|
| **Title** | Brand name, POC name, customer · status, generated date |
| **Executive summary** | The project's saved executive summary, or AI-written bullets when the AI deck is requested. Omitted if neither exists. |
| **Scorecard** | A **doughnut progress ring** (use cases validated / total, with the %), the `N of M validated` caption, and a color-coded **by-status** breakdown |
| **Results by category** | One slide per use-case category. Each use case shows a **`✓ PASS`** (green) or **`◔ <status>`** (amber) chip, its reference number, name, and comment. Long categories continue on additional slides. |
| **Proof** | One slide per screenshot (auto-fit, centered, captioned), capped at 12 |
| **Timeline & next steps** | Start → end dates, the list of open (incomplete) use cases, and — in the AI deck — a **Recommended next steps** column |

Pass/fail coloring keys off a use-case status's **"complete"** flag (the same
flag the dashboard and reports use), so it always tracks how you've configured
your use-case statuses under **Settings → Lookups**.

## Downloading a deck

Open a project's report (**project page → Report**, or **Reports → a POC**) and
use the buttons in the report toolbar:

- **⬇ Download deck** — the deterministic deck. Fast, free, always available.
- **⬇ Deck (AI)** — only shown when an AI provider is configured; see below.

Each download is freshly generated and never browser-cached (filenames carry a
date + random tag, same as the PDF/Word exports).

## AI-written narrative

When an AI provider is set up under **Settings → AI Assistant**, the report page
also offers **Deck (AI)** (the `?ai=1` variant of the download). It asks the
configured provider (Anthropic Claude or Google Gemini) for **slide-ready
bullets**: 3–4 executive-summary points and 2–4 recommended next steps, grounded
strictly in the project's data.

- The model returns **strict JSON**; the deck drops the bullets onto the summary
  and next-steps slides.
- It's an **opt-in per download** — the plain "Download deck" never makes a
  network call or spends tokens.
- It **degrades gracefully**: no provider, a bad key, or an unparseable response
  all fall back silently to the deterministic deck, so a download is always
  produced (never a 500).

The AI writes *narrative only* — every number and status on the deck is
deterministic, so the model can't invent results.

## Branded template

Admins can upload a company-branded template under **Settings → Branding →
Readout deck template**. The template's **slide master, theme, and fonts** sit
underneath the generated slides, so every readout matches the corporate look.

- Accepts **`.pptx`** *and* **`.potx`** (PowerPoint template files). A `.potx` is
  structurally a `.pptx` but carries a template content-type that python-pptx
  won't open — on upload it's normalized and stored as a `.pptx`. Max **25 MB**;
  invalid/corrupt files are rejected by actually trying to open them.
- **The deck follows the template's slide size.** The layout is authored on a
  base 16:9 design but every coordinate is scaled to the template's *actual*
  dimensions, so a **4:3** template produces a 4:3 deck (not a stretched 16:9 one)
  — a logo or footer on the master lands where the template intends.
- There's only ever one template (stored at
  `<data_dir>/readout_template.pptx`). Uploading replaces it; **Remove template**
  reverts to the built-in 16:9 layout.
- If the stored template is ever unreadable at generation time, the deck falls
  back to the built-in layout rather than failing.

## Brand logo

Independently of the template, admins can upload a **logo** (Settings → Branding →
Readout deck logo) that's stamped in the **top-right corner of every slide**.

- Accepts PNG/JPG/GIF (max **5 MB**); the image is validated and normalized to
  PNG (transparency preserved). Stored at `<data_dir>/readout_logo.png`.
- Works with or without a template, and scales with the slide size. A bad image
  at generation time is skipped rather than failing the export.

### Accent color — app vs. output

The **app's** Branding accent (Settings → Branding → Accent color) is UI chrome
only; it does **not** brand the deck when a template is in play. The deck's accent
(progress ring, section kickers, headers) is chosen like this:

- **A template is uploaded** → the deck uses the **template's own theme accent**
  (its `accent1`). Whatever the template declares is honored — if a template puts
  an unexpected color there, fix it in the template. (Falls back to a neutral slate
  if the theme has no accent.)
- **No template** → the deck uses the **app's** Branding accent, since that's the
  only branding signal available.

Pass/fail chips are always green/amber (they carry meaning, not brand).

Likewise, the app's **brand name** is stamped on the title slide only for the
built-in deck. When a template is uploaded it's **omitted** — the template's own
master/logo does the branding. (The POC's own data — customer, project, dates,
use cases — is always included; that's the report content, not app branding.)

## Speaker notes

Every slide has its **speaker-notes pane** filled with talking points for whoever
presents (typically the AE) — how to open, the headline number to land, what to
call out per category, and how to close on next steps. They're written from the
project's data, so they stay specific to the POC.

## How it's built

- `app/services/report_pptx.py` — the deck builder. Consumes the **same report
  context** (`branding`, `progress`, `use_case_groups`, …) as the PDF and Word
  exports, so all three stay in sync.
- `app/services/ai/readout.py` — the AI narrative generator (JSON → bullets),
  reusing the shared provider registry.
- `app/services/report_template.py` — stores/validates the uploaded template
  (incl. `.potx` normalization) and the logo. `Canvas` in `report_pptx.py` scales
  the design to the template's slide size.
- Routes live in `app/ui/report_routes.py`
  (`GET /ui/reports/projects/{id}/readout.pptx`, add `?ai=1` for the AI deck) and
  the template upload in `app/ui/settings_routes.py`.

> **Dependency note.** The deck uses `python-pptx` (declared in
> `pyproject.toml`). The runtime Docker image installs only `.[mcp]`, so after
> pulling this change rebuild the image (`docker compose build`) before the
> download routes work in a deployed instance.

## Access & visibility

The deck route uses the same access checks as the rest of a project's report: an
**external viewer** can only download decks for projects shared with them, and the
deck honors the report's **audience toggle** (`?audience=client|internal`) like the
other exports. In practice the deck's content is the same either way — it surfaces
customer, use cases, statuses, and screenshots, none of which carry the
**internal-only** flag (that flag lives on journal notes and tasks, which the deck
doesn't include), so internal-only content never reaches a deck regardless of
audience. Treat it like any other shareable POC report.
