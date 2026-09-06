# Project Context: toorow

Last reviewed: 2026-07-24

## Product

toorow is a multi-tenant, modular MCP marketing-reporting platform. It connects
analytics, advertising, commerce, billing and context sources; schedules and
audits extraction; harmonizes data into a canonical semantic layer; and exposes
compact agent-facing answers plus rich MCP App widgets.

The repository name remains `connector`; user-facing product language uses
`toorow`. The original product brief is `info.md`; the current canonical product
contract is `_bmad-output/specs/spec-toorow/SPEC.md`.

The strategic target is broader than a connector catalog: toorow is a sovereign
control plane for trustworthy agentic analytics. It combines managed or external
ingestion, semantic governance, shared business context, DQ/provenance,
post-click attribution without a proprietary pixel and a measurable evaluation
loop. `docs/product-direction.md` is the concise direction statement.
`docs/product-architecture/README.md` is the concise control map of product
views, cross-project capabilities and pending contract corrections.

## Repository model

This is a private multi-part working monorepo with two virtual publication
zones. The shareable application projection is intended for publication in a
public GitHub repository:

- **Shareable application:** `server/`, `ui/`, `dbt/`, `infra/`, `.github/`,
  root build/configuration files and supporting scripts.
- **Private product/presence workspace:** `web/`, `studio/`, `docs/`, `doc/`,
  `_bmad/`, `_bmad-output/`, `reviews/`, `_screenshots/`, brand assets and local
  agent/design configuration.

The boundary is defined in `distribution/public-app.toml` and enforced by
`scripts/export_public_app.py`. It is an allow-list: an unknown new path stays
private until reviewed.

## Product language and administration viewport

- All visible application and administration copy is English. Dates and
  business numbers may follow the user's locale; technical timestamps remain
  explicit and timezone-qualified.
- The administration console is a desktop product. Supported device layout
  begins at 1280 CSS px and canonical compositions target 1440×900. There is no
  mobile administration information architecture.
- Accessibility zoom/reflow from a supported desktop viewport remains required
  and is not a mobile product variant. Essential wide data tables may use a
  named, keyboard-focusable internal horizontal scroller.
- Compatible MCP hosts provide the compact alternative interaction surface when
  the full desktop administration workspace is not appropriate.
- Public GitHub availability does not mean open source by itself. Do not claim
  an open-source license until one is selected and added.

## Architecture

The application follows a microkernel/plugin design:

- `server/core/` is the source-agnostic kernel: FastMCP, module discovery,
  authentication, project access, queue/scheduler, warehouse routing, report
  composition, governance APIs, cache and observability.
- `server/modules/<name>/` contains source-specific connectors, manifests,
  staging SQL, report definitions, fixtures and seed tooling.
- `dbt/` owns canonical marts and semantic calculations.
- `ui/` owns the administration console, shared shells/design tokens and
  single-file MCP App widgets/cards.
- `infra/` owns local services, deployment containers, migrations and cloud IaC.

Primary runtime: Python 3.12, FastMCP 3.4.x, Starlette, uvicorn, DuckDB and
PostgreSQL. Data transforms use dbt-core 1.11.x with DuckDB locally and BigQuery
in cloud targets. UI packages use React 19, TypeScript, Vite 8, MUI and pnpm 9.

## Non-negotiable architecture invariants

1. Data tools split output into a short LLM text channel and a full canonical
   `structuredContent` envelope for the widget.
2. Connector-specific knowledge stays in auto-discovered module folders; the
   core remains source-agnostic.
3. Nango owns provider OAuth/token lifecycle; extraction code does not persist
   provider tokens. OAuth 1.0a requests are signed inside the generic Nango proxy;
   modules receive provider responses without token secrets.
4. `fact_daily_kpi` is the canonical daily fact surface. Additive values are
   stored; ratios and non-additive metrics are calculated by semantic views.
5. Every access resolves through the organization-rooted AD-5 resource graph; project and Datastream grants are explicit, and tenant isolation is a CI-enforced property.
6. Currency/timezone harmonization happens once during ingestion.
7. Pulls are immutable, identified by a core-minted `pull_id`, and replayable.
8. PostgreSQL owns transactional/governance entities; the analytical warehouse
   owns facts and marts. No entity has two writers.
9. Provenance, freshness and alerts are required in data envelopes.
10. Widgets mutate only through server tools. View tools are local/read-only.
11. Widgets build to one self-contained HTML file with no external URL assets.
12. Third-party API calls go through the shared queue and land before reads.
13. Tracing and feedback share one trace spine.
14. Inbound identity is distinct from provider OAuth and scopes every call.
15. Product configuration is owned by the standalone admin console.

AD-17 through AD-19 remain proposed. AD-20 (revocable frozen-snapshot sharing),
AD-21 (direct Google auth exception), AD-22 (ephemeral DuckDB read-through
cache), AD-23 (Universal Datastream), and AD-24 through AD-32 are ratified.
The latter bind peer compatible MCP Apps hosts, four capability profiles,
fail-closed organization-rooted access, default-none provider-account exposure,
server-verifiable confirmation, transactional audit/outbox, host/workspace
governance, one-time external bearer exchange, untrusted-data minimization and
anti-inference funnel telemetry. These are target contracts with explicit
brownfield production gates in the spine, not claims that current code already
enforces them.

## Current connector modules

- Google Analytics
- Google Search Console
- Meta Ads
- TikTok Ads
- LinkedIn Ads
- Shopify
- Stripe
- Klaviyo
- GitHub context
- Generic daily-grain source

Module manifests are schema-versioned. Tools mount under
`<module-name>_<tool_name>`; cross-source tools remain core-owned.

## Data and governance

- PostgreSQL schema migrations live in `infra/nango/migrations/` and currently
  run from `001` through `029`.
- Governance entities include projects, memberships, project modules,
  connection references/health, pull jobs/verifications, preferences,
  datastreams/mappings/target fields, reports/overrides, context events, alerts,
  feedback, notebooks/runs, morning briefings, tenant-key audit and DQ baselines.
- Module staging views converge through central dbt marts including
  `fact_daily_kpi`, semantic ratio views, anomalies, customer journeys,
  cross-source conversions, attribution/dedup and reconciliation models.
- Local analytical development uses DuckDB. Production architecture targets
  BigQuery for analytics and PostgreSQL for governance.

## Entry points and interfaces

- MCP/HTTP server: `server/core/main.py`, default endpoint
  `http://localhost:8000/mcp`.
- Admin REST surface: `server/core/admin_api.py`, mounted by the server and
  organized around projects, connections, jobs, context, reports, modules,
  notebooks, datastreams, data model, quality and cache administration.
- Admin UI: `ui/admin/src/main.tsx`.
- Widget/card entry points: `ui/widgets/*/src/main.tsx` and
  `ui/cards/*/src/main.tsx`.
- dbt project: `dbt/dbt_project.yml`.
- Local platform: `infra/nango/docker-compose.yml`.
- CI: `.github/workflows/ci.yml` (checks only — it does NOT deploy).
- Deploy: `infra/scripts/deploy.sh`, run BY HAND. There is no automatic
  deployment; the GitHub deploy workflow was removed 2026-08-02 because it had
  never authenticated once.

## Development rules

- Use `uv` and the root `uv.lock`; do not introduce pip/Poetry workflows.
- Use pnpm for the `ui/` workspace; do not use npm/yarn there.
- A new connector needs conformance fixtures and tests, its own manifest and
  staging model, plus central semantic integration where required.
- Core code must not contain connector-specific vocabulary.
- No secrets, live profiles, Terraform state/variables, tenant keys or local
  databases may be committed.
- Infrastructure apply/deploy and destructive Docker volume deletion remain
  human-gated.
- Existing local changes are user work and must be preserved.
- Follow `docs/working-method.md`: orient, frame, implement, verify, document,
  create explicit atomic commits, then hand off with evidence and open risks.
- Treat `doc/` as research/proposal input. SPEC, ratified architecture, accepted
  stories and executable contracts determine what is binding or already shipped.

## Definition of Done — per story

A story is not done because its tests pass. It is done when all of the following
hold. Each line is checkable by the person reviewing, in one command or one look.

**1. The target was read first.** The story names the architecture document of the
surface it touches (`docs/product-architecture/*.md`) and quotes the requirement
it implements. If no document covers the surface, the story says so explicitly
before any code is written — it never invents a design decision and presents it
as an implementation choice.

**2. Acceptance is the document's own `Incomplete if` list.** The story states
which of those criteria it turns from true to false, and re-evaluates them at the
end. A criterion still true is stated as still true, not omitted.

```bash
python scripts/finished_work_audit.py <surface>   # must be run; exits 1 while open
python scripts/finished_work_audit.py --plan      # the delivery plan, generated
```

The audit runs on its own: `.claude/settings.json` fires `--gate` on every turn
end, and blocks on any finding not already in `known-debt.json` — a component
mounted nowhere, a contracted tab swallowed by a `default:`, non-English copy, a
duplicated selector.

**A server route with no door is REPORTED, not blocked, and only since
2026-08-01.** Until that date the scanner read `server/core/admin_api.py` alone,
which merely *mounts* routes while 32 of the 48 `server/core/*_api.py` modules
*declare* them: it saw 137 of the 393 mounted `/api/` routes and was blind to 256.
Two Epic 52 routes shipped with no caller at all under a green gate. The scanner
now reads the declaring modules and sees 358 — which takes the doorless count from
22 to 59. Those 37 are not new debt; they were invisible. Making them block at once
would redden the gate for every session simultaneously, so they are listed by
`--plan` and triaged by hand into `known-debt.json` before the switch
(`_ROUTE_FINDINGS_BLOCK`) is flipped. **Until then, `--gate → 0` is not evidence
that a route has a door** — the evidence is a caller, named. Accepting one requires writing
it into that file, deliberately, where it stays visible in git history. The
perimeter is enumerated from the inventory, never from the diff: eleven explicit
requests to audit produced seven audits and zero detections precisely because
the perimeter was chosen each time from the code just touched.

Clearing a criterion means recording it in `docs/product-architecture/completeness-ledger.json`
with the command, route or screen that produced the verdict. An unrecorded
criterion counts as still true, so forgetting to evaluate one can never read as
progress. The audit also runs live structural checks that no verdict can silence.

**2b. Say `rebuild` when it is a rebuild.** When the shipped object is not the
contracted object, the honest answer is "this view has to be rebuilt", or "to
repair it, here is everything required" — with the list. A partial repair
presented as a fix is worse than no repair: it spends the person's trust and
they discover the gap by clicking. Announcing a rebuild is never the wrong
answer; announcing a fix that isn't one always is.

**3. The whole class is treated.** A defect or a change applies to every instance
of its class: all screens, all 37 connectors, all routes of the family — never
only the one that was reported. The story names the class and the count.

**4. The user's path was executed end to end.** Not a mocked unit test: the real
sequence a person performs, from the first click to the screen that shows the
result. A harness that stops at the first link is not evidence.

**5. Nothing new was invented in the shared layers.** Three standing rules, each
now measured by the audit rather than merely written down:

- **All visible copy is English.** Not identifiers, not directory names — what
  reaches the screen. `Extraits` and `Réglages` shipped as tab labels for months.
- **One stylesheet, primitives first.** No new base class, no hardcoded colour,
  no literal spacing value. Use an `ui/admin/src/app.css` primitive or change the
  foundation deliberately.
- **No second copy for convenience.** Duplicating is cheaper than reading once,
  and more expensive than reading every time after — and it hides which copy is
  live. Consolidating 46 stylesheets into one while leaving 434 duplicated
  selectors inside is not consolidation.
- **A recurring thing is a component, never a local class.** Before writing any
  markup, read `ui/admin/src/ui/index.ts` and use what is there; if the concept
  is missing, add it there. A class carrying a screen prefix and a vocabulary
  word — `dso-btn`, `imports-dialog`, `kg-drawer`, `render-field` — is a
  component re-implemented locally, and the audit now reports it. The console
  holds **170** of them, including three separate button scales, because the
  written rule to re-read first has never once prevented one.

**6. Server capability and UI door ship together.** A route without a screen and a
screen without a route are both incomplete. A module whose functions no route
exposes is not delivered — the most common failure in this repository.

**7. Nothing was written to the production database.** Test artefacts belong in a
test database. Some rows here are immutable by trigger and cannot be removed.

**8. Every number reported carries the command that produced it.** No claim of
completeness without the measurement that supports it.

**9. Traces of missing work are preserved, not cleaned up.** A disabled tab, an
unrendered stylesheet block or an unwired capability is the inventory of what is
left to build. Deleting it destroys the only evidence that absence leaves.

## What a story must never do

- Ship a screen that renders another screen's component under a different route.
- Apply a request from conversation that contradicts a ratified document, without
  saying so and asking whether it is an amendment.
- Report progress at the boundary of what was touched rather than what the person
  can now do.
- Replace a measurement with an audit when a correction is possible.

## Verification baseline

The complete CI matrix covers Python lint/tests, widgets/cards/admin builds and
tests, external-resource bundle gates, dbt seed/build/tests, connector
conformance, live PostgreSQL tenant isolation and Terraform format/validation.
Focused local commands are documented in the root `README.md` and
`CONTRIBUTING.md`.

## Universal Datastream contract

- Epic 12 is the next real-data foundation and has 15 approved, development-sized
  stories in epic-12-ingestion-directe-bq.md.
- One source-first flow supports connector reports, existing read-only BigQuery
  tables/views, and managed CSV/Excel/Google Sheets feeds written to BigQuery.
- Connector users select compatible metrics, dimensions, grain, history and a
  capability-supported cadence; hourly is allowed when quota/freshness permits.
- Existing BigQuery objects keep one declared external writer. Managed feeds use
  a dedicated raw landing written only by toorow. dbt remains the sole mart writer.
- Extraction, parsing, mapping, schedule and publication plans are immutable and
  versioned. Preview and execution reference the same versions.
- Full selected joint grain is preserved in a governed dataset. fact_daily_kpi
  receives only explicitly safe additive projections; no dimension is silently
  discarded or forced into one breakdown slot.
- Apache Ossie 0.1.1 is the semantic interchange boundary. Ingestion, scheduling,
  MDM, privacy, DQ and publication stay in toorow under a namespaced TOOROW extension.
- Candidate data publishes atomically. Synchronize, Reload, Reprocess, Replace,
  safe Append and Rollback are distinct audited operations; failed/partial/empty
  candidates do not replace the current version.
- Epic 13 owns dictionary approval and cross-stream conflict resolution. Story
  15.6 remains the delivered read-only Google Sheets adapter/objectives preset;
  Epic 12 consumes it for arbitrary recurring managed feeds.

## Known publication risks

- The shareable application is intended for public GitHub publication. There is
  no license file, so the public repository must not be described as open source
  until Jean chooses and adds a license.
- Tracked or untracked scratch databases/data exist under `server/`; the public
  exporter blocks them explicitly.
- `.env.example` is intentionally public, but its placeholder values must be
  reviewed again before the first external push.
- The working tree contains extensive in-progress user changes; a release/export
  should be created from a deliberately reviewed commit, not an arbitrary dirty
  snapshot.
