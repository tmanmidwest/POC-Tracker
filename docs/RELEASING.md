# Releasing & versioning

A short, practical guide to how versions work here and the exact steps to cut a
new one. Written for someone new to Git/GitLab.

- [The mental model](#the-mental-model)
- [What the numbers mean (SemVer)](#what-the-numbers-mean-semver)
- [Where the version lives](#where-the-version-lives)
- [Everyday workflow: branch → commit → MR → merge](#everyday-workflow-branch--commit--mr--merge)
- [Cutting a release (the checklist)](#cutting-a-release-the-checklist)
- [Glossary](#glossary)

---

## The mental model

**You do not bump the version on every commit.** Commits are your fine-grained
save-points; you make lots of them freely. A *version* is a milestone you stamp
deliberately, usually covering many commits.

```
commit  commit  commit  commit        commit  commit
  └────────── v0.2.0 ──────────┘         └──── (unreleased, heading to v0.3.0)
```

## What the numbers mean (SemVer)

The version is `MAJOR.MINOR.PATCH` (e.g. `0.2.0`). Increment based on *what
changed since the last version*:

| You did… | Bump | Example |
|---|---|---|
| A bug fix, nothing new, nothing breaks | **PATCH** — `0.2.0 → 0.2.1` | Fixed a miscounted scorecard |
| A new, backward-compatible feature | **MINOR** — `0.2.0 → 0.3.0` | Added the readout deck |
| A breaking change, or "it's officially ready" | **MAJOR** — `0.2.0 → 1.0.0` | Renamed a REST field others depend on |

Rules of thumb:

- Bumping a bigger number **resets** the smaller ones to 0 (`0.2.5` + a feature → `0.3.0`).
- A leading **`0.`** means "early development — anything may change." Bump to
  **`1.0.0`** when you consider the app stable/released.

## Where the version lives

There is **one** source of truth:

- **[`app/__init__.py`](../app/__init__.py)** → `__version__ = "x.y.z"`

Everything else follows it automatically, so you only ever edit that one line:

- `pyproject.toml` reads it dynamically (`[tool.setuptools.dynamic]`), so the
  installed package version matches.
- `app/config.py` (`app_version`) defaults to it — this is the `v…` shown in the
  app sidebar and returned by `/health`.

A test (`tests/test_version.py`) fails if these ever drift.

> You can still override the *displayed* version at runtime with the
> `POCT_APP_VERSION` env var (rarely needed) without touching the source.

## Everyday workflow: branch → commit → MR → merge

Day-to-day work doesn't touch the version at all. The version only changes when
you decide to *release* (next section).

```bash
# 1. Start a branch off main so work-in-progress never breaks the good copy.
git switch -c feature/readout-deck

# 2. Do the work; commit as often as you like.
git add -A
git commit -m "Add readout deck builder and export route"

# 3. Push the branch to GitLab.
git push -u origin feature/readout-deck

# 4. In the GitLab web UI, open a Merge Request (MR) from your branch into main,
#    review the diff, then click Merge. main now has your work.
```

Good commit messages are short and imperative: "Add expiry sweep", "Fix stale
badge". A common convention is a type prefix — `feat:`, `fix:`, `chore:`,
`docs:` — but plain clear English is fine.

> **GitHub vs GitLab:** this repo lives on GitLab, which calls a "Pull Request"
> a **Merge Request**. Same concept. Everything else (branch, commit, push, tag)
> is identical.

## Cutting a release (the checklist)

When you've merged a batch of work into `main` and want to stamp it as a version:

```bash
# 0. Be on an up-to-date main.
git switch main
git pull

# 1. Bump the ONE line. e.g. 0.2.0 -> 0.3.0 for a new feature.
#    Edit app/__init__.py:  __version__ = "0.3.0"

# 2. Sanity check + run the tests (in Docker — see docs/… / project convention).
#    The version test confirms nothing drifted.

# 3. Commit the bump.
git commit -am "chore: release v0.3.0"

# 4. Tag that exact commit. The tag is the real record of the version.
git tag -a v0.3.0 -m "Readout deck + external-user expiry"

# 5. Push the commit and the tag.
git push
git push origin v0.3.0
```

That's it. Optionally, in the GitLab UI go to **Deploy → Releases** (or
**Repository → Tags**) and create a Release from the `v0.3.0` tag with a few
bullet points of what changed — nice for a changelog, not required.

Rebuild the Docker image after a release so the running app reports the new
version (`docker compose build`).

## Glossary

- **Commit** — a save-point in history with a message.
- **Branch** — a parallel line of work; `main` is the trusted one.
- **Push** — upload your local commits to GitLab (`origin`).
- **Merge Request (MR)** — a proposal to merge a branch into `main`, reviewed in
  the web UI. (GitHub calls this a Pull Request.)
- **Tag** — a permanent label pinned to one commit, e.g. `v0.3.0`. This is how a
  version is actually recorded.
- **Release** — a GitLab/GitHub page built on a tag, with notes and downloads.
- **SemVer** — the `MAJOR.MINOR.PATCH` numbering scheme above.
