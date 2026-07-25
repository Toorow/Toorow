# Working Method

This document defines how humans and agents work in this repository. Its goal is
to make each change understandable, reviewable and reversible while protecting
the architecture invariants and the user's in-progress work.

## Sources of truth

Use this order when documents disagree:

1. **Ratified product contract:** `_bmad-output/specs/spec-toorow/SPEC.md`.
2. **Ratified architecture:** the architecture spine and adopted ADs.
3. **Accepted delivery scope:** current epics, stories and acceptance criteria.
4. **Implementation reality:** code, migrations, manifests and executable tests.
5. **Project memory:** `docs/project-context.md` and this documentation index.
6. **Research and proposals:** `doc/` informs direction but is not automatically
   binding; every claim must be reconciled with the current implementation.
7. **Presence documentation:** Mintlify, Astro and Sanity content explain the
   product but do not override its technical contracts.

If implementation and a ratified contract disagree, record the discrepancy. Do
not silently redefine the contract from the code or rewrite code from a stale
research document.

## Standard change loop

All planned product work also follows the story gates in
[docs/bmad-delivery-procedure.md](./bmad-delivery-procedure.md): accepted epic,
Create Story, Dev Story, independent review, checkpoint and atomic commit.

### 1. Orient

- Read `docs/index.md`, `docs/project-context.md` and the relevant canonical spec.
- Inspect `git status` before touching files.
- Identify existing user changes and preserve them.
- Determine whether the change belongs to the shareable application or the
  private presence/product workspace.

### 2. Frame

- State the outcome, scope, acceptance criteria and explicit non-goals.
- Identify architecture decisions and schemas that bind the change.
- Resolve contradictions before implementation.
- Select the smallest relevant verification set.

### 3. Implement

- Change the smallest coherent surface.
- Reuse existing modules, components, schemas and scripts.
- Keep source-specific behavior out of the core.
- Update migrations/contracts before dependants when a persisted schema changes.
- Never mix unrelated cleanup with feature work.

### 4. Verify

- Run focused tests first, then broader gates proportional to risk.
- Validate seams, not only isolated helper functions.
- For external APIs, mocked tests do not replace a documented live integration
  pass.
- For analytical changes, prove grain stability, unchanged unrelated totals and
  absence of double counting.
- For UI, verify build, tests and the single-file/external-resource gate.

### 5. Document

- Update the canonical contract when scope or architecture changes.
- Update project memory when durable facts change.
- Update user-facing docs when setup or behavior changes.
- Mark proposed decisions as proposed; do not present them as shipped.

### 6. Commit

- Stage explicit paths; never use `git add .` in a dirty worktree.
- One commit must express one reviewable concern.
- Inspect `git diff --cached --stat` and `git diff --cached` before committing.
- Use an imperative message with a clear scope, for example
  `docs(project): define the working method`.
- Do not commit generated databases, credentials, local profiles, build output
  or another person's unrelated changes.

### 7. Handoff

- Lead with the achieved outcome.
- List files/commits and concrete verification results.
- State unverified paths and open risks explicitly.
- Do not push, deploy, provision or perform a destructive operation without the
  corresponding authorization or human gate.

## Common recipes

### Documentation page

1. Add `docs/<slug>.mdx` with `title` and `description` frontmatter.
2. Add the slug to the appropriate group in `docs/mint.json`.
3. Check commands and claims against the root `README.md` and current code.
4. Validate local links and the Mintlify configuration.

### Marketing or editorial page

1. Decide whether the page is static Astro content or Sanity-managed content.
2. Reuse an existing route/content type where possible.
3. For a new content type, add and register the Sanity schema, then add the GROQ
   query and Astro rendering route.
4. Verify both the Studio and website build.

### Connector

1. Use `.agents/skills/add-connector/SKILL.md` and
   `docs/adding-a-connector.md`.
2. Define name, landing kind, auth path, source grain, metrics/dimensions and
   quota before scaffolding.
3. Implement manifest, connector, staging, report and golden fixtures.
4. Register the module staging path in `dbt/dbt_project.yml` while model-paths
   remain explicit.
5. Run conformance by layer, dbt reconciliation, Ruff and the live-integration
   definition of done.

### Commit preparation in this monorepo

1. Partition changes by intent and public/private boundary.
2. Write the intended file list for each commit before staging.
3. Stage that list explicitly and inspect it.
4. Run the validations relevant to that commit.
5. Commit and confirm the remaining working tree still contains only expected
   user or later-commit changes.

## Definition of done

A change is done when its acceptance criteria are met, relevant tests are green,
seams and security boundaries are covered, required documentation is current,
the commit is coherent, and remaining risks are visible. Near-green or
documentation-only confidence is not a substitute for an unrun required check.
