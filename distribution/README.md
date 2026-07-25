# Repository publication boundary

This private monorepo contains two virtual products:

1. the shareable application (`server/`, `ui/`, `dbt/`, `infra/`, CI and
   supporting root files);
2. the private presence and product workspace (`web/`, `studio/`, `docs/`,
   internal planning, reviews, screenshots, brand assets and agent tooling).

`public-app.toml` is the source of truth. It is intentionally allow-list based:
new top-level paths remain private until explicitly reviewed and added.

## Audit the boundary

From the repository root:

```bash
python scripts/export_public_app.py
python scripts/export_public_app.py --list
```

The first command performs a read-only audit. The second prints the exact public
projection. Files ignored by Git are never selected, and an additional deny list
blocks local databases, environment files, credentials, build output and scratch
data below public directories.

## Create a public working tree

Choose a new or empty directory outside this repository:

```bash
python scripts/export_public_app.py --output ../connector-app-public
cd ../connector-app-public
git init
git add .
git status
```

Review the complete staged diff and run a dedicated secret scanner before the
first push. The exporter never initializes a repository, creates a remote,
commits or pushes.

## Two-remote model

toorow is developed as a single private monorepo and published as an
application-only projection:

| Remote | Repository | Contents | How it is updated |
| --- | --- | --- | --- |
| Private (`origin`) | `github.com/jlalbany/toorow` | The full monorepo (app **and** website, studio, planning, docs). | Every normal commit; `git push origin`. |
| Public | `github.com/Toorow/Toorow` | Only the allow-listed application projection. | The export → review → push flow below (never a direct `git push` of the monorepo). |

Day-to-day work commits to the private remote. The public remote is refreshed
deliberately, on a cadence you choose, from a clean export — so private history,
marketing content and internal planning never leak into public Git history.

### Refresh the public application

```bash
python scripts/export_public_app.py --output ../toorow-public-export
# review ../toorow-public-export and run a secret scanner, then sync it into a
# checkout of github.com/Toorow/Toorow, commit and push.
```

Because the public repo is an export (not a live remote of this monorepo), a
force/replace push is expected and safe: it always reflects the latest reviewed
projection, not a shared branch other people commit to.

## Boundary changes

- Add application paths only to `root_files` or `public_directories`.
- Add private paths to `private_paths` for human-readable policy coverage.
- Add defense-in-depth rules to `exclude_globs`.
- Keep exceptions narrow in `allow_globs`; templates such as `.env.example` are
  the expected use case.
- Run the audit after any change.

No license is currently included in the source repository. Do not describe the
export as open source until the project owner selects and adds a license.
