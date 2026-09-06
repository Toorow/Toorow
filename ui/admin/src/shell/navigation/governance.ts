/**
 * The `governance` workspace: its sections, their lenses, their object contracts.
 *
 * AD-42, 2026-08-12 -- declared here rather than inside the 313-line array it
 * shared with five other workspaces. A change to governance no longer opens the file
 * the others are written in.
 *
 * ORDER IS PART OF THE CONTRACT and it stays in `navigation.ts`: the subnav
 * renders `WORKSPACES` as declared. This file owns the CONTENT of one entry,
 * never its position.
 */

import {
  CONTROLS_QUALITY_QUERY,
  EVIDENCE_QUERY,
  MASTER_DATA_QUERY,
  MASTER_DATA_TABS,
  SEMANTIC_MODEL_QUERY,
  section,
} from "./vocabulary";

export const governance = {
  key: "governance",
  slug: "governance",
  label: "Governance",
  question: "How are the data defined, matched and verified?",
  subnav: [
    section("master-data", "Master Data", [
      { type: "business-domain", label: "Business Domain", tabs: MASTER_DATA_TABS, defaultTab: "overview" },
      { type: "master-data-object", label: "Master Data Object", tabs: MASTER_DATA_TABS, defaultTab: "overview" },
      { type: "registry", label: "Registry", tabs: MASTER_DATA_TABS, defaultTab: "overview" },
      {
        // The wire token stays `tracked-entity` -- route slug, envelope
        // `object_type`, `owner.kind`, stored references. Renaming it is a
        // migration with a backfill and is deferred with `context_topic` and
        // `skill` (`alignment-register.md:110-123` item 4).
        //
        // What a PERSON reads is `Competitor`, and it is declared once, here:
        // `README.md:103` names the object Competitor, `glossary.md` defines the
        // entity as "the governance half of a Competitor", and the lens below is
        // called Competitor Registry. Only the workbench read the token back.
        // Ratifié Jean 2026-09-01 (`governance.md`, *Amendment, 2026-09-01*).
        type: "tracked-entity",
        label: "Competitor",
        // Not `hierarchy`/`mappings-aliases`: a Competitor is org-scoped with a
        // per-Project role and no parent, and its aliases are on the Overview,
        // so those two would be permanently empty. These two carry the questions
        // the object actually answers -- what each source calls it, and where it
        // is collected. Ratified in `governance.md`, same amendment.
        tabs: ["overview", "representations", "coverage", "used-by", "versions"],
        defaultTab: "overview",
      },
    ], [], [
      { slug: "business-domains", label: "Business Domains" },
      { slug: "classifications", label: "Classifications" },
      { slug: "products", label: "Products" },
      { slug: "activities", label: "Activities" },
      { slug: "registries", label: "Registries" },
      // Conditional, not permanent: the server answers `unavailable` with a
      // reason for every Project where Competitors is Disabled, and the lens
      // renders that reason instead of an empty registry (Story 48.5).
      { slug: "competitor-registry", label: "Competitor Registry" },
    ], MASTER_DATA_QUERY),
    section("semantic-model", "Semantic Model", [
      {
        type: "semantic-concept",
        label: "Semantic Concept",
        tabs: ["definition", "semantics", "source-bindings", "used-by", "versions"],
        defaultTab: "definition",
      },
      {
        type: "semantic-view",
        label: "Semantic View",
        tabs: ["definition", "metrics-dimensions", "source-bindings", "used-by", "versions"],
        defaultTab: "definition",
      },
      // Story 60.5 opened the `versions` tab on both types. It was withheld
      // while 60.1 and 60.3 shipped no ledger — a contracted tab with no
      // writer promises a history that does not exist — and migration 242
      // wrote both: `app.value_mapping_table_versions` and
      // `app.cleanup_rule_versions`, one immutable row per act.
      {
        type: "value-mapping-table",
        label: "Value Mapping Table",
        tabs: ["overview", "used-by", "versions"],
        defaultTab: "overview",
      },
      {
        type: "cleanup-rule",
        label: "Cleanup Rule",
        tabs: ["overview", "used-by", "versions"],
        defaultTab: "overview",
      },
      // Lot A1 (issue #68). `app.mdm_canonical_fields` is validated against in
      // six places on the server and NO screen anywhere listed it, so no link
      // could ever point at a canonical field. This contract is what makes one
      // addressable.
      //
      // ONE TAB, AND THAT IS THE HONEST NUMBER — the rule the `datastream`
      // contract above states in the other direction. A tab absent from this
      // list is unreachable even when a screen can draw it; a tab present that
      // no screen can draw is the same defect mirrored. This table is
      // "mutable-with-audit" (migration 032) and carries no version ledger, so
      // a `versions` tab would promise a history nothing writes; and nothing
      // records which reader used a given field id, so there is no `used-by`
      // to count. Definition is what the object can answer, and the server
      // contract in `governance_read_model.py` declares exactly the same one.
      // `lineage` added 2026-08-17 (audit P2-7). `used-by` is still absent for
      // the reason written above -- nothing records which reader consumed a
      // field id -- but the opposite direction IS recorded, and it had shipped
      // with a route no screen ever called: `GET /api/dimension-lineage/fed-by`
      // composes which source columns FEED a canonical dimension, from the
      // connector manifests and the project's plan versions. The tab contracts
      // a question an owner already answers.
      {
        type: "canonical-field",
        label: "Canonical Field",
        tabs: ["definition", "lineage"],
        defaultTab: "definition",
      },
      // Added 2026-08-16. `app.metric_definitions` is an upsert store with an
      // audit table beside it — no version ledger — and nothing records which
      // renders read a given definition. One tab, for exactly the reason the
      // canonical field above has one.
      {
        type: "metric-definition",
        label: "Metric Definition",
        tabs: ["definition"],
        defaultTab: "definition",
      },
    ], [], [
      { slug: "concepts", label: "Concepts" },
      { slug: "semantic-views", label: "Semantic Views" },
      { slug: "mapping-coverage", label: "Mapping Coverage" },
      // Story 60.1: the client's OWN vocabulary, one lens for one object type.
      // NOT called `Transformations`: that tab groups three object families and
      // belongs to story 60.4, which this story does not name.
      { slug: "value-tables", label: "Value Tables" },
      // Story 60.3: the cleanup rules, applied AT READ. Its own lens because a
      // lens carries one object type — a cleanup rule removes rows or strips a
      // substring, which is not what a lookup table does. The word is NOT
      // `Filter`: `query_specs.ts`/`query_specs.py` hold it for a read filter
      // and `datastream_intents.py` for a collection filter.
      { slug: "cleanup-rules", label: "Cleanup Rules" },
      // Lot A1 (issue #68): the MDM canonical vocabulary, at both scopes. Its
      // own lens because a lens carries one object type — a canonical field is
      // not a Concept. A Concept is a governed, published definition with a
      // formula and a history; a canonical field is the raw vocabulary entry
      // every mdm-bound binding is validated against.
      { slug: "canonical-fields", label: "Canonical Fields" },
      // The LOWER declaring store, visible at last (2026-08-16).
      // `metric_definition_upsert` (MCP) writes it and
      // `resolve_declared_additivity` reads it on every render — it decides
      // whether a metric may be summed across two days — and no lens listed it.
      // Someone reading Governance believed they saw everything that governs an
      // aggregation. Its own lens because a lens carries ONE object type: a
      // mutable row with no version and no formula is not a Concept.
      { slug: "metric-definitions", label: "Metric Definitions" },
    ], SEMANTIC_MODEL_QUERY),
    section("controls-quality", "Controls & Quality", [
      {
        type: "control-case",
        label: "Control Case",
        tabs: ["evidence", "candidate-change", "impact", "decision-history"],
        defaultTab: "evidence",
      },
      {
        type: "dq-monitor",
        label: "DQ Monitor",
        tabs: ["overview", "coverage", "history", "issues"],
        defaultTab: "overview",
      },
      {
        type: "rule-set",
        label: "Rule Set",
        tabs: ["overview", "rules", "effective-dates", "approvals-exceptions", "versions"],
        defaultTab: "overview",
      },
    ], [], [
      { slug: "conflicts", label: "Conflicts" },
      { slug: "reconciliation", label: "Reconciliation" },
      { slug: "data-quality", label: "Data Quality" },
      { slug: "rule-sets", label: "Rule Sets" },
    ], CONTROLS_QUALITY_QUERY),
    section("evidence", "Evidence", [
      { type: "evidence-trace", label: "Evidence Trace", tabs: ["overview", "lineage", "provenance"], defaultTab: "overview" },
      { type: "object-version", label: "Object Version", tabs: ["overview", "diff", "approvals", "used-by"], defaultTab: "overview" },
      { type: "audit-event", label: "Audit Event", tabs: ["overview"], defaultTab: "overview" },
    ], [], [
      { slug: "lineage-provenance", label: "Lineage & Provenance" },
      { slug: "versions-approvals", label: "Versions & Approvals" },
      { slug: "audit-activity", label: "Audit Activity" },
    ], EVIDENCE_QUERY),
  ],
} as const;
