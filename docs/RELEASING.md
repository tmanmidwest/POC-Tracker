# Releasing & versioning

A short, practical guide to how versions work here and the exact steps to cut a
new one. Written for someone new to Git/GitHub.

- [The mental model](#the-mental-model)
- [What the numbers mean (SemVer)](#what-the-numbers-mean-semver)
- [Where the version lives](#where-the-version-lives)
- [Everyday workflow: branch → commit → PR → merge](#everyday-workflow-branch--commit--pr--merge)
- [Cutting a release (the checklist)](#cutting-a-release-the-checklist)
- [Publishing the Docker image](#publishing-the-docker-image)
- [Glossary](#glossary)

---

## The mental model

**You do not bump the version on every commit.** Commits are your fine-grained
save-points; you make lots of them freely. A *version* is a milestone you stamp
deliberately, usually covering many commits.

```
commit  commit  commit  commit        commit  commit
  └────────── v1.0.0 ──────────┘         └──── (unreleased, heading to v1.1.0)
```

## What the numbers mean (SemVer)

The version is `MAJOR.MINOR.PATCH` (e.g. `1.0.0`). Increment based on *what
changed since the last version*:

| You did… | Bump | Example |
|---|---|---|
| A bug fix, nothing new, nothing breaks | **PATCH** — `1.0.0 → 1.0.1` | Fixed a miscounted scorecard |
| A new, backward-compatible feature | **MINOR** — `1.0.0 → 1.1.0` | Added the readout deck |
| A breaking change | **MAJOR** — `1.0.0 → 2.0.0` | Renamed a REST field others depend on |

Rules of thumb:

- Bumping a bigger number **resets** the smaller ones to 0 (`1.2.5` + a feature → `1.3.0`).
- A leading **`0.`** means "early development — anything may change." We passed
  that at **`1.0.0`**, which stamped the app as stable/released.

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

## Everyday workflow: branch → commit → PR → merge

Day-to-day work doesn't touch the version at all. The version only changes when
you decide to *release* (next section).

```bash
# 1. Start a branch off main so work-in-progress never breaks the good copy.
git switch -c feature/readout-deck

# 2. Do the work; commit as often as you like.
git add -A
git commit -m "Add readout deck builder and export route"

# 3. Push the branch to GitHub.
git push -u origin feature/readout-deck

# 4. In the GitHub web UI (or `gh pr create`), open a Pull Request (PR) from your
#    branch into main, review the diff, then click Merge. main now has your work.
```

Good commit messages are short and imperative: "Add expiry sweep", "Fix stale
badge". A common convention is a type prefix — `feat:`, `fix:`, `chore:`,
`docs:` — but plain clear English is fine.

## Cutting a release (the checklist)

When you've merged a batch of work into `main` and want to stamp it as a version:

```bash
# 0. Be on an up-to-date main.
git switch main
git pull

# 1. Bump the ONE line. e.g. 1.0.0 -> 1.1.0 for a new feature.
#    Edit app/__init__.py:  __version__ = "1.1.0"

# 2. Sanity check + run the tests (in Docker — see CONTRIBUTING.md).
#    The version test confirms nothing drifted.

# 3. Commit the bump.
git commit -am "chore: release v1.1.0"

# 4. Tag that exact commit. The tag is the real record of the version.
git tag -a v1.1.0 -m "Readout deck + external-user expiry"

# 5. Push the commit and the tag.
git push
git push origin v1.1.0
```

That's it. Optionally, in the GitHub UI go to **Releases → Draft a new release**,
pick the `v1.1.0` tag, and add a few bullet points of what changed — nice for a
changelog, not required.

Then publish the matching Docker image (next section) so anyone deploying gets
the exact code that tag represents.

## Publishing the Docker image

We publish images to the **GitHub Container Registry (GHCR)** at
`ghcr.io/tmanmidwest/poc-tracker`. The image **tag** mirrors the git version, so
`ghcr.io/tmanmidwest/poc-tracker:1.1.0` is always the code at git tag `v1.1.0`.
That parallel is your version control for images: a deployer can pin an exact
version, or track `latest` for "newest stable."

### Option A — Automated (recommended): let GitHub Actions build it

The workflow at [`.github/workflows/publish-image.yml`](../.github/workflows/publish-image.yml)
builds and pushes the image automatically **whenever you push a `v*` tag**. So
after step 5 above, you're done — GitHub builds `:1.1.0`, `:1.1`, `:1`, and
`:latest` and pushes them for you. Watch it run under the repo's **Actions** tab.

The tags mean:

| Tag | Points at | Use when a deployer wants… |
|---|---|---|
| `:1.1.0` | exactly that release, forever | to pin one immutable version |
| `:1.1` | newest `1.1.x` patch | patches but no new features |
| `:1` | newest `1.x.y` | any backward-compatible update |
| `:latest` | newest release overall | "just give me current stable" |

### Option B — Manual: build and push from your machine

You need Docker running and a one-time login. Create a GitHub **Personal Access
Token (classic)** with the `write:packages` scope (GitHub → Settings → Developer
settings → Personal access tokens), then:

```bash
# 1. Log in to GHCR (paste the token as the password when prompted).
echo $GHCR_TOKEN | docker login ghcr.io -u tmanmidwest --password-stdin

# 2. Build the image, tagging it with the version and `latest`.
docker build \
  -t ghcr.io/tmanmidwest/poc-tracker:1.1.0 \
  -t ghcr.io/tmanmidwest/poc-tracker:latest .

# 3. Push both tags.
docker push ghcr.io/tmanmidwest/poc-tracker:1.1.0
docker push ghcr.io/tmanmidwest/poc-tracker:latest
```

> **First publish only:** a new GHCR package starts **private**. To let others
> pull it, open the package page (repo → **Packages** → poc-tracker →
> **Package settings**) and set **Visibility → Public**, and link it to this
> repo under **Manage Actions access** so the workflow can push future versions.

### Deploying from the published image

Anyone with the image can run it without cloning the source. Point compose at the
published tag instead of building locally by setting `POCT_IMAGE`:

```bash
# Pull and run a specific version (no source checkout needed — you still need
# docker-compose.yml, or run `docker run` directly).
POCT_IMAGE=ghcr.io/tmanmidwest/poc-tracker:1.1.0 docker compose up -d
```

To upgrade later, change the tag and re-pull:

```bash
POCT_IMAGE=ghcr.io/tmanmidwest/poc-tracker:1.2.0 docker compose pull
POCT_IMAGE=ghcr.io/tmanmidwest/poc-tracker:1.2.0 docker compose up -d
```

The `poct-data` volume persists across upgrades, so data survives the swap.

## Glossary

- **Commit** — a save-point in history with a message.
- **Branch** — a parallel line of work; `main` is the trusted one.
- **Push** — upload your local commits to GitHub (`origin`).
- **Pull Request (PR)** — a proposal to merge a branch into `main`, reviewed in
  the web UI. (GitLab calls this a Merge Request.)
- **Tag** — a permanent label pinned to one commit, e.g. `v1.1.0`. This is how a
  version is actually recorded.
- **Release** — a GitHub page built on a tag, with notes and downloads.
- **Registry / GHCR** — where built Docker images are stored and pulled from;
  ours is `ghcr.io/tmanmidwest/poc-tracker`.
- **Image tag** — the `:1.1.0` part of an image name; the image equivalent of a
  git tag.
- **SemVer** — the `MAJOR.MINOR.PATCH` numbering scheme above.
