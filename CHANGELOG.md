# Changelog

All notable changes to Questlog are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). See
[docs/RELEASING.md](docs/RELEASING.md) for how releases are cut.

## [Unreleased]

### Added

- **Customer logos.** Each customer can now have a logo, shown on its projects, the
  external portal, and reports. Upload an image, or use **Fetch logo from website** (on the
  customer's detail and edit pages) to pull one automatically — auto-attempted on create too
  when a website is set. With a **Logo.dev** publishable token configured in **Settings →
  System** (or the `POCT_LOGODEV_TOKEN` env var) you get real brand logos; without one it
  falls back to the site's favicon. Each fetch writes an Activity event recording which
  source produced the logo and the full trail of what was tried.

## [1.2.1] — 2026-07-24

### Fixed

- **Dynamic RBAC enable toggle.** 1.2.0 shipped the role builder and its
  `rbac_dynamic_enabled` master switch, but without a UI control to flip it —
  so the feature couldn't actually be turned on. Added the **"Enforce
  admin-defined roles"** toggle to Settings → System (off by default, next to the
  region-enforcement switch), with an audit event on change. No behavior change
  until you enable it.

## [1.2.0] — 2026-07-24

A backward-compatible feature release. The headline is a **dynamic RBAC role
builder** — admin-defined roles assembled from fine-grained capabilities, meant
to replace the four hardcoded roles. Like region RBAC, it ships **disabled by
default**: a master switch keeps the existing role behavior in force until you
turn it on, so upgrading changes nothing until you choose to flip it.

### Added

- **Dynamic RBAC role builder.**
  - A **capability catalog** of 44 fine-grained, per-action permissions
    (e.g. `project.edit`, `note.view_internal`, `settings.manage`), grouped by area.
  - **Admin-defined roles** built in Settings → Roles: create, edit, and delete
    roles from a capability matrix (with per-area "select all"), and assign
    **multiple roles per user** (effective permissions are the union).
  - Four **seeded system roles** (Admin, Manager, SE, External Viewer) that
    reproduce today's behavior exactly, so existing users are unchanged on upgrade.
  - A single enforcement helper, **`user.can("capability")`**, behind a master
    **`rbac_dynamic_enabled` switch (off by default)**. While off, authorization
    resolves through the legacy admin/internal/external gates — identical to 1.1.0.
  - Guard rails: no self-role-change, the seeded admin always keeps a superuser
    role, the system can never be left with zero superusers, system roles can't be
    deleted, and a non-superuser can't grant capabilities they don't hold.

### Changed

- Authorization now funnels through the capability layer at its core choke points
  (project edit/grant, plus the library, lookups, and feedback surfaces). With the
  new switch off, behavior is unchanged; the remaining call sites are migrated in
  later waves.

## [1.1.0] — 2026-07-24

A large, fully backward-compatible feature release. Region-based access control
is the headline, and it ships **disabled by default** — existing deployments
behave exactly as they did on 1.0.0 until enforcement is turned on.

### Added

- **Region-based access control (RBAC).**
  - Regions are now a first-class concept; users are assigned to one or more regions.
  - The **SE** role is scoped to its own region; a new **Manager** role spans several
    assigned regions; admins continue to see everything.
  - A master **enforcement switch** in Settings → System, **off by default**, so
    region data is stored but not enforced until you enable it.
  - **Bulk region assignment** with CSV import, and a **backfill** tool that derives
    each project's region from its assigned SE.
  - **Manager reporting** — Win/Loss analytics broken down by region, and a
    **region column + filter** in the project list.
- **Feedback** — in-app feedback submission with an admin management board.
- **Win/Loss statistics** reporting.
- **Milestone reporting.**
- **Full POC use-case view.**
- **Project Type** lookup for projects.
- **Collapsible navigation** sidebar (per-user, persists).
- **AWS deployment** support and scripts.

### Changed

- **Redesigned the project report page** — exports are laid out as labeled cards
  with short descriptions, and each format's options sit on the card they control.
- **Relabeled the "Standard" user role to "SE"** across the UI. Display-only —
  the internal role key is unchanged, so no data migration and no RBAC behavior change.
- Dashboard layout refresh; task create/edit now returns to the project page.

### Fixed

- Deployment fix and assorted UI fixes.

## [1.0.0] — 2026-07-10

Initial stable release — the app is marked stable/released at this version.
Established GitHub-native documentation, automated Docker image publishing to
GHCR, and general cleanup.

[1.2.1]: https://github.com/tmanmidwest/POC-Tracker/releases/tag/v1.2.1
[1.2.0]: https://github.com/tmanmidwest/POC-Tracker/releases/tag/v1.2.0
[1.1.0]: https://github.com/tmanmidwest/POC-Tracker/releases/tag/v1.1.0
[1.0.0]: https://github.com/tmanmidwest/POC-Tracker/releases/tag/v1.0.0
