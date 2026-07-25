# Product Direction

Last reconciled: 2026-07-24

## Vision

toorow is a sovereign control plane for trustworthy agentic analytics. It is not
just a connector catalog and not another dashboard. It gives AI agents governed,
explainable access to business data while giving operators control over
identity, ingestion, semantics, quality, context and provenance.

## Target users

- Marketing and growth teams that need cross-channel answers without trusting
  each advertising platform's self-attribution.
- Agencies and multi-brand operators that require strict project isolation.
- Data teams that want to keep their warehouse and existing ETL investments.
- Organizations that need auditable AI analysis rather than opaque narrative.

## Product promise

An operator can connect or reference a data source, map it to a shared semantic
model, verify its quality and make it safely queryable by an agent. The agent
loads only the relevant business context, cites provenance and serves both a
compact explanation and an interactive visual result.

## Product interface and distribution

- Visible application and administration copy is English.
- The data administration console is desktop-only from 1280 CSS px; MCP hosts
  provide the compact alternative surface rather than a separate mobile admin
  information architecture.
- Accessibility zoom from a supported desktop viewport remains operable and
  does not create a mobile product variant.
- The shareable application is intended for a public GitHub repository through
  the reviewed allow-list projection. Private product, planning and content
  workspaces remain excluded.
- GitHub availability is not an open-source license. The product must not be
  described as open source until a license is selected and added.

## Strategic pillars

### 1. Flexible data onboarding

Support three governed Datastream paths through one source-first experience:

- **Connector report:** provider API -> identity service -> shared queue ->
  immutable raw landing -> full-grain dataset -> safe semantic projection.
  The user selects compatible metrics, dimensions, filters, grain, history and
  a source-supported cadence, including hourly where allowed.
- **Existing BigQuery:** declared third-party writer -> read-only table/view ->
  virtual pull/provenance -> versioned semantic mapping. Toorow never writes,
  alters or truncates the external object.
- **Managed feed:** CSV, Excel or Google Sheets -> toorow-owned import ledger ->
  isolated candidate landing in BigQuery -> validation -> atomic publication.

All three paths converge on versioned extraction and mapping plans, field
classification, Apache Ossie interchange metadata, MDM bindings, full-grain
preservation, DQ, provenance and recoverable publication operations. BigQuery
owns analytical data; PostgreSQL owns intent, governance, versions and audit.
### 2. One governed semantic truth

The data dictionary, mappings, aggregation rules, currency/timezone policy and
verification-source preference must be explicit and operator-controlled.
Advertising claims are reconciled against sources of truth such as GA4,
Shopify or another declared commerce/lead source.

### 3. Context-engineered analysis

Business topics, definitions and diagnostic procedures are durable shared data,
not private prompt fragments. The agent discovers context progressively and the
server measures whether definitions were consulted before analytical queries.

The target architecture stores operational knowledge in PostgreSQL, versions it
there and mirrors it read-only when analytics needs it. Git documentation may
explain the system, but it is not the runtime knowledge store.

### 4. Quality and provenance before narrative

Freshness, completeness, lineage, pull identity and DQ status are part of every
analytical answer. A successful API request that lands incomplete data is not a
successful analytical result.

### 5. Measured agent reliability

Context and semantic changes ultimately need a Golden Questions evaluation
harness covering SQL correctness, provenance citations and pre-query adherence.
Once that harness exists, regressions should block context changes in CI.

### 6. Broad connector reach without core sprawl

The long-term catalog target is 50+ sources across paid media, commerce, CRM,
email, product analytics, warehouses, files and webhooks. Catalog growth must
reuse the module contract; it must not add source-specific branches to the core.

## Prioritized horizons

1. **Make the foundation dependable:** keep installation, module conformance,
   dbt registration, publication boundaries and operator documentation accurate.
2. **Make onboarding universal with real data:** ship connector report selection,
   existing BigQuery, CSV, Excel and recurring Google Sheets through the same
   mapping, DQ, full-grain and atomic publication contracts.
3. **Make answers context-aware:** shared topics/procedures/graph, context search,
   procedures and measurable pre-query behavior.
4. **Make governance operable:** editable MDM, conflict resolution, complete DQ
   supervision and agent-visible quality reports.
5. **Make reliability measurable:** Golden Questions, regression reports and CI
   gates for context/semantic changes.
6. **Scale the catalog deliberately:** prioritize connectors by real customer
   journeys and verification value, not by raw connector count.
## Explicit non-goals

- No proprietary clickstream pixel in the core; use GA4/session exports and
  commerce transaction reconciliation for post-click journeys.
- No synchronous third-party API result served directly to an agent.
- No second writer for an entity or analytical mart.
- No source-specific logic in `server/core/`.
- No claim that a connector is production-ready without live API evidence.
- No claim that the code is open source until a license is selected and added.

## Decision filter

A proposed feature should improve at least one of: trustworthy data onboarding,
semantic consistency, operator control, context quality, measurable reliability
or safe connector reach. It must preserve project isolation, provenance,
single-writer ownership and the dual-channel MCP response contract.
