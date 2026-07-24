# Changelog

All notable changes to Questlog are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). See
[docs/RELEASING.md](docs/RELEASING.md) for how releases are cut.

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

[1.1.0]: https://github.com/tmanmidwest/POC-Tracker/releases/tag/v1.1.0
[1.0.0]: https://github.com/tmanmidwest/POC-Tracker/releases/tag/v1.0.0
