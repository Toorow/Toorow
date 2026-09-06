"""Source-agnostic, Project-scoped read model for the four Governance screens.

Governance owns business meaning, matching decisions, rule/control definitions,
approvals and reference evidence (``docs/product-architecture/governance.md``).
It is not a second writer and it has no store of its own: every object below is
composed from the owner that already holds it, through an adapter that must be
able to name a *stable* identifier.

Three rules this module exists to enforce:

1. **No fabricated identity.** A row without a stable owner ID is never promoted
   by minting one from its type, label or position. It stays unavailable until
   the owner that holds it supplies the identity.
2. **Unavailable is not empty.** An adapter whose store could not be read reports
   ``state="unavailable"`` with a typed reason and its owner link. Returning an
   empty list would claim "we looked, there is nothing" — a false statement.
   The converse is a rule too: an ``empty`` lens says WHY it is empty and names
   the gesture that fills it. It never names a release, a story or a deployment
   state — nobody reading a Governance screen can act on one.
3. **No fallback.** A missing object, a cross-scope object and a version that
   does not belong to its object all fail; none of them resolves to the current
   object, the collection, or a similarly named neighbour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Mapping

from core.business_identity_catalogue import (
    CLASSIFICATION_SOURCE,
    CLASSIFICATION_VERSION_SOURCE,
    CURRENT_VERSION_ID,
    DOMAIN_SOURCE,
    DOMAIN_VERSION_SOURCE,
)
from core.dq_governance import open_issue_predicate

#: Named for the module that owns the read, not for the app: a degraded lens
#: must say WHICH read failed. `logger.warning` was already called below and the
#: name was never defined -- so the one branch meant to soften an unreadable
#: shadow raised `NameError` instead, inside the `except` that exists to prevent
#: exactly that. Pre-existing, found by ruff F821 on 2026-08-16.
logger = logging.getLogger("core.governance_read_model")

SCHEMA_COLLECTION = "governance-collection.v1"
SCHEMA_OBJECT = "governance-object.v1"
SCHEMA_VERSION = "governance-version.v1"

#: Hard ceiling on any single collection response. Truncation is never silent:
#: it is reported in ``coverage`` and as a typed unavailable reason.
MAX_COLLECTION_ITEMS = 200
#: Hard ceiling on the bounded reference lists carried by one object.
MAX_FACET_REFS = 50


class GovernanceObjectNotFound(LookupError):
    """The object does not exist inside the authorized Project snapshot."""


class GovernanceUnknownRoute(ValueError):
    """The section, lens, object type or version shape is not registered."""


# ---------------------------------------------------------------------------
# The registered contracts. The CLIENT registry (ui/admin/src/shell/navigation.ts)
# stays the route-shape authority; this table exists so the server can refuse a
# type it cannot serve, and it is proven equal to the client by test parity.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObjectContract:
    object_type: str
    tabs: tuple[str, ...]
    default_tab: str

    @property
    def supports_versions(self) -> bool:
        return "versions" in self.tabs


@dataclass(frozen=True, slots=True)
class SectionContract:
    section: str
    label: str
    lenses: tuple[str, ...]
    objects: tuple[ObjectContract, ...]

    @property
    def default_lens(self) -> str:
        return self.lenses[0]

    def object_contract(self, object_type: str) -> ObjectContract | None:
        return next((o for o in self.objects if o.object_type == object_type), None)


_MASTER_DATA_TABS = ("overview", "hierarchy", "mappings-aliases", "used-by", "versions")

GOVERNANCE_SECTIONS: dict[str, SectionContract] = {
    "master-data": SectionContract(
        section="master-data",
        label="Master Data",
        # `competitor-registry` is CONDITIONAL, not permanent: the lens is listed
        # here because the section contract is static, and it answers
        # `unavailable` with a reason for every Project where the capability is
        # Disabled. A Project that never enabled Competitors therefore sees no
        # registry, no matrix and no identities -- which is the difference
        # between a lens and a navigation item (Story 48.5, AC1/AC9).
        lenses=(
            "business-domains",
            "classifications",
            "products",
            "activities",
            "registries",
            "competitor-registry",
        ),
        objects=(
            ObjectContract("business-domain", _MASTER_DATA_TABS, "overview"),
            ObjectContract("master-data-object", _MASTER_DATA_TABS, "overview"),
            ObjectContract("registry", _MASTER_DATA_TABS, "overview"),
            ObjectContract(
                "tracked-entity",
                ("overview", "representations", "coverage", "used-by", "versions"),
                "overview",
            ),
        ),
    ),
    "semantic-model": SectionContract(
        section="semantic-model",
        label="Semantic Model",
        # `value-tables` is the client's OWN vocabulary (Story 60.1), not a second
        # conformance authority. A lens carries ONE object type, so the three
        # sections of the `Transformations` tab (tables, calculated fields, row
        # filters) cannot be one lens — that tab belongs to story 60.4 and this
        # story does not name it.
        # `cleanup-rules` is story 60.3. It is its OWN lens rather than a section
        # of `value-tables` because a lens carries one object type, and a cleanup
        # rule is not a lookup table: it removes rows or strips a substring AT
        # READ, from a stored pattern. The `Transformations` tab that will group
        # the three families is story 60.4 and is not named here.
        # `canonical-fields` is lot A1 of issue #68. It is its OWN lens for the
        # same reason the two above are: a lens carries ONE object type, and a
        # canonical field is not a Concept -- a Concept is a governed, published
        # definition with a formula and a version history, a canonical field is
        # the raw vocabulary entry six modules validate bindings against. The
        # table held zero rows at both scopes and no screen anywhere listed it,
        # so the enumeration a Template closes over was closed on nothing.
        lenses=(
            "concepts",
            "semantic-views",
            "mapping-coverage",
            "value-tables",
            "cleanup-rules",
            "canonical-fields",
            # `metric-definitions` is the LOWER declaring store, added 2026-08-16.
            # `resolve_declared_additivity` reads it on every render -- it decides
            # whether a metric may be summed across two days -- and no lens listed
            # it. A person reading Governance believed they saw everything that
            # governs an aggregation. Its own lens, not a section of `concepts`,
            # because a lens carries ONE object type and a mutable row with no
            # version and no formula is not a Concept.
            #
            # ITS WRITERS WENT ON 2026-08-25 (story 49.3 AC1) AND THE LENS STAYED.
            # `metric_definition_upsert` (MCP) is unregistered and the two REST
            # doors answer 409; only the seed import still fills the PLATFORM rows.
            # A store nobody may author still DECIDES -- the cascade below is read
            # on every render -- so hiding it would put a person back exactly where
            # this lens found them: believing they saw everything that governs an
            # aggregation.
            "metric-definitions",
        ),
        objects=(
            ObjectContract(
                "semantic-concept",
                ("definition", "semantics", "source-bindings", "used-by", "versions"),
                "definition",
            ),
            ObjectContract(
                "semantic-view",
                ("definition", "metrics-dimensions", "source-bindings", "used-by", "versions"),
                "definition",
            ),
            # A `versions` tab, since story 60.5. It was withheld while 60.1 and
            # 60.3 shipped no ledger -- contracting a tab over a history nothing
            # writes promises an empty screen -- and migration 242 wrote both
            # ledgers, so the promise is now backed: `app.value_mapping_table_
            # versions` and `app.cleanup_rule_versions`, one immutable row per act,
            # written by `core/rule_versions.py` and by nothing else.
            ObjectContract(
                "value-mapping-table", ("overview", "used-by", "versions"), "overview"
            ),
            ObjectContract("cleanup-rule", ("overview", "used-by", "versions"), "overview"),
            # ONE TAB, AND THAT IS THE HONEST NUMBER. `app.mdm_canonical_fields`
            # is "mutable-with-audit" (migration 032 header) and carries no
            # version ledger, so a `versions` tab would promise a history nothing
            # writes -- the exact defect story 60.5 was written to repair. There
            # is no per-field used-by count either: the modules that validate
            # against a field id do not record which ones they used. Definition
            # is what this object can answer today, so Definition is what it
            # contracts.
            # TWO TABS SINCE 2026-08-17, and the second is honest for the same
            # reason the first one was alone. `used-by` stays absent because
            # nothing records which reader consumed a field id -- but the
            # opposite direction IS recorded: `core.dimension_lineage.get_fed_by`
            # composes, from the connector manifests and the project's plan
            # versions, which source columns FEED a canonical dimension. That
            # reader shipped with `/api/dimension-lineage/fed-by` and no screen
            # ever called it (audit of 2026-08-17, P2-7). `lineage` is that
            # reading, and it contracts a question an owner already answers.
            ObjectContract("canonical-field", ("definition", "lineage"), "definition"),
            # ONE TAB, for the same reason and measured the same way.
            # `app.metric_definitions` carries no version ledger (migration 049 is
            # an upsert store with an audit table beside it), and nothing records
            # which renders read a given definition. A `versions` or a `used-by`
            # tab would contract a history no owner writes -- the defect story
            # 60.5 exists to have repaired once.
            ObjectContract("metric-definition", ("definition",), "definition"),
        ),
    ),
    "controls-quality": SectionContract(
        section="controls-quality",
        label="Controls & Quality",
        lenses=("conflicts", "reconciliation", "data-quality", "rule-sets"),
        objects=(
            ObjectContract(
                "control-case",
                ("evidence", "candidate-change", "impact", "decision-history"),
                "evidence",
            ),
            ObjectContract("dq-monitor", ("overview", "coverage", "history", "issues"), "overview"),
            ObjectContract(
                "rule-set",
                ("overview", "rules", "effective-dates", "approvals-exceptions", "versions"),
                "overview",
            ),
        ),
    ),
    "evidence": SectionContract(
        section="evidence",
        label="Evidence",
        lenses=("lineage-provenance", "versions-approvals", "audit-activity"),
        objects=(
            ObjectContract("evidence-trace", ("overview", "lineage", "provenance"), "overview"),
            ObjectContract(
                "object-version", ("overview", "diff", "approvals", "used-by"), "overview"
            ),
            ObjectContract("audit-event", ("overview",), "overview"),
        ),
    ),
}

#: Which lens(es) can produce each object type. Order matters: the first source
#: that recognises the identifier owns it. `rule-set` genuinely has two owners —
#: a reconciliation group and a capability rule ladder — with disjoint ID spaces.
_OBJECT_SOURCES: dict[tuple[str, str], tuple[str, ...]] = {
    ("master-data", "business-domain"): ("business-domains",),
    # THREE SOURCES, ONE TYPE, AND THE ID SPACES ARE DISJOINT -- the same shape
    # `rule-set` uses above. Migration 143 settles which type a Product is: its
    # header states that "`registry` and `master-data-object` are distinct object
    # types" and that Products and Activities version PER OBJECT, one lifecycle
    # per identity (`version_scope='node'`). So the REGISTRY of Products is a
    # `registry` -- already listed by the `registries` lens -- and each Product is
    # a `master-data-object`, exactly like a classification. Minting a fourth type
    # would have split one workbench in two and made Hierarchy, Mappings & Aliases,
    # Used by and Versions answer twice, which is the argument
    # `_client_object_registry` already makes for the registry half.
    ("master-data", "master-data-object"): ("classifications", "products", "activities"),
    ("master-data", "registry"): ("registries",),
    ("master-data", "tracked-entity"): ("competitor-registry",),
    ("semantic-model", "semantic-concept"): ("concepts",),
    ("semantic-model", "semantic-view"): ("semantic-views",),
    ("semantic-model", "value-mapping-table"): ("value-tables",),
    ("semantic-model", "cleanup-rule"): ("cleanup-rules",),
    ("semantic-model", "canonical-field"): ("canonical-fields",),
    ("semantic-model", "metric-definition"): ("metric-definitions",),
    ("controls-quality", "control-case"): ("conflicts",),
    ("controls-quality", "dq-monitor"): ("data-quality",),
    ("controls-quality", "rule-set"): ("reconciliation", "rule-sets"),
    ("evidence", "evidence-trace"): ("lineage-provenance",),
    ("evidence", "object-version"): ("versions-approvals",),
    ("evidence", "audit-event"): ("audit-activity",),
}


# ---------------------------------------------------------------------------
# Lens results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LensResult:
    """One lens' contribution: real items, or an honest reason there are none."""

    state: str  # "available" | "empty" | "unavailable"
    items: tuple[dict[str, Any], ...] = ()
    reason: dict[str, Any] | None = None
    total: int | None = None
    #: Set only by an adapter that paginates server-side (the Evidence index).
    #: It carries the cursor, the per-adapter coverage and the evidence horizon,
    #: none of which a `total` alone can express.
    page: Any = None

    @property
    def truncated(self) -> bool:
        return self.total is not None and self.total > len(self.items)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _text(value: Any) -> str | None:
    """A row value as a string, or None -- never the empty string.

    A precondition a door sends back must be either an identity or the honest
    absence of one; `""` is neither, and it would compare equal to nothing.
    """

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fetch(conn: Any, query: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _owner_href(
    section: str,
    *,
    object_type: str | None = None,
    object_id: str | None = None,
    tab: str | None = None,
    version_id: str | None = None,
) -> dict[str, Any]:
    """Semantic owner reference. A browser URL is never stored or emitted: the
    client builds hrefs from its own registry, so a route rename cannot freeze
    a dead address into a pinned reference."""
    from core.project_overview import owner_reference  # noqa: PLC0415 -- shared contract

    return owner_reference(
        "governance",
        section,
        object_type=object_type,
        object_id=object_id,
        tab=tab,
        version_id=version_id,
    )


def _data_owner_href(section: str, object_type: str, object_id: str, tab: str) -> dict[str, Any]:
    from core.project_overview import owner_reference  # noqa: PLC0415 -- shared contract

    return owner_reference("data", section, object_type=object_type, object_id=object_id, tab=tab)


def _facet(
    state: str,
    refs: list[dict[str, Any]] | None = None,
    *,
    count: int | None = None,
    reason: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Used-by, Versions and Evidence are three DIFFERENT questions. Each answers
    for itself whether it is available, genuinely empty, or not delivered.

    ``reason`` is carried only by a facet that is NOT available: a screen must be
    able to say what is missing and name the gesture, not print a state word.
    """
    bounded = (refs or [])[:MAX_FACET_REFS]
    facet = {
        "state": state,
        "count": count if count is not None else len(refs or []),
        "refs": bounded,
        "truncated": len(refs or []) > len(bounded),
    }
    if reason is not None:
        facet["reason"] = reason
    return facet


#: What a facet says when it knows HOW MANY and cannot say WHICH. See
#: :func:`_counted_facet`.
UNLISTED_REFS_CODE = "facet_refs_not_composed"


def _counted_facet(
    count: Any,
    refs: list[dict[str, Any]] | None = None,
    *,
    unlisted_message: str | None = None,
) -> dict[str, Any]:
    """A facet whose state comes from a COUNT that may not have been readable.

    AI-239. Every used-by site in this module wrote the same shape:

        _facet("available" if int(row.get(...) or 0) else "empty", count=...)

    and `int(x or 0)` gives the same answer to two opposite facts. A count of
    zero means the store answered "nothing depends on this". A count that is
    absent -- a column the query did not return, a sub-read that failed -- means
    nobody asked, and it arrived on screen as "nothing depends on this" too. That
    is the reading a person acts on before moving or deleting an object.

    `NodeImpact` (`master_data.py:739-748`) already states the rule for the same
    question one workspace over: "an empty tuple means the store answered and
    there are none -- it never means the store could not be read". This is that
    rule, as the one helper the five sites derive from.

    ``None`` -> ``unavailable``. Anything countable -> ``empty`` or ``available``.

    THE SAME DISTINCTION, HELD ON THE REFS (49-2 verdict, 2026-09-01). A count of
    three with an empty ``refs`` list is not a smaller answer than three named
    consumers -- it is the SAME impossible state one level down, and the console
    read it as the empty one: ``GovernanceObjectWorkbench`` answered "Nothing
    depends on this object. Its owner answered, and the answer is none." for a
    Business Domain with three live links, in the tab a person opens before
    archiving it. So a positive count with nothing to list is no longer
    ``available``: it is ``unavailable`` WITH its count, which says the two true
    things at once -- how many there are, and that this owner cannot name them
    here. `available` from this helper always carries the refs it counted.
    """
    if count is None:
        return _facet("unavailable")
    number = int(count)
    if not number:
        return _facet("empty", count=0)
    if refs:
        return _facet("available", refs, count=number)
    return _facet(
        "unavailable",
        count=number,
        reason=_unavailable(
            UNLISTED_REFS_CODE,
            unlisted_message
            or (
                f"{number} recorded, and this owner cannot list them here. "
                "Open the owner to see which ones."
            ),
        ),
    )


def _unavailable(code: str, message: str, owner: dict[str, Any] | None = None) -> dict[str, Any]:
    reason: dict[str, Any] = {"code": code, "message": message}
    if owner is not None:
        reason["owner_reference"] = owner
    return reason


# `_pending_story` used to live here. It built, for the two lenses below, the
# sentence "... is owned by Story 49.2, which has not been delivered" -- a
# DEPLOYMENT STATE, on screen, to a person who has no story tracker and no way to
# act on one. The screen rule this repository works under is the opposite: an
# empty list says why it is empty and names the GESTURE that fills it. It had
# exactly two callers, both replaced below by living lenses, so the generator of
# that sentence is gone rather than reworded -- a dead helper that can still emit
# a forbidden sentence is a trap the next lens walks into.


# ---------------------------------------------------------------------------
# Owner queries. Every one is scoped by the authenticated Project or its
# organization; none of them reads a raw provider record.
# ---------------------------------------------------------------------------

#: The Business Domain lens, re-pointed at the authority (story 49.2, AC2/AC3).
#: `d` and `bv` are no longer the superseded tables: they are
#: `business_identity_catalogue`'s sources, which answer from
#: `app.master_data_nodes` / `app.master_data_object_versions` and fall back to
#: the legacy row only for an organization that has not converged. Every clause
#: below -- the four LATERALs, the `used_by` filter, the ORDER BY -- is unchanged,
#: because the sources carry the superseded store's own column names.
#:
#: The Versions facet is what this closes. `governance.md` § *Decision 2* names
#: the debt in its own words: the facet counted `mdm_business_domain_versions`,
#: "so a natively created identity reads `None` there until the lens is
#: re-pointed". It now counts published node versions, and `version_refs` is
#: built from them -- so an identity minted in the authority shows the history it
#: actually has instead of an empty facet that reads as "never revised".
_BUSINESS_DOMAINS = f"""
    SELECT d.id, d.slug, d.name, d.description, d.owner, d.status,
           d.created_at, d.updated_at, d.archived_at,
           v.version_count, v.latest_version, v.latest_changed_at,
           v.version_refs,
           {CURRENT_VERSION_ID.format(id_expression="d.id", org_expression="d.org_id")}
             AS current_version_id,
           c.classification_count, l.used_by_count
    FROM {DOMAIN_SOURCE} d
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS version_count,
               MAX(ledger.version_number) AS latest_version,
               MAX(ledger.changed_at) AS latest_changed_at,
               COALESCE(
                   jsonb_agg(
                       jsonb_build_object(
                           'object_type', 'business-domain-version',
                           'id', ledger.domain_id || ':' || ledger.version_number::TEXT,
                           'version', ledger.version_number,
                           'state', CASE
                               WHEN ledger.version_number = ledger.latest_version
                                   THEN 'active'
                               ELSE 'previous'
                           END,
                           'recorded_at', ledger.changed_at,
                           'recorded_by', ledger.changed_by
                       )
                       ORDER BY ledger.version_number DESC
                   ),
                   '[]'::jsonb
               ) AS version_refs
        FROM (
            SELECT bv.*,
                   MAX(bv.version_number) OVER () AS latest_version
            FROM {DOMAIN_VERSION_SOURCE} bv
            WHERE bv.domain_id = d.id AND bv.org_id = d.org_id
        ) ledger
    ) v ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS classification_count
        FROM {CLASSIFICATION_SOURCE} bc
        WHERE bc.domain_id = d.id AND bc.archived_at IS NULL
    ) c ON TRUE
    LEFT JOIN LATERAL (
        SELECT (
            SELECT COUNT(*)
            FROM app.mdm_business_links bl
            WHERE bl.taxonomy_type = 'business_domain'
              AND bl.taxonomy_id = d.id
              AND bl.project_id = %(project_id)s
              -- Migration 306: a withdrawn link is no longer a consumer. Counting
              -- it would block an archive on a dependency that has been released.
              AND bl.retired_at IS NULL
        ) + (
            -- THE SECOND CONSUMER STORE, AND IT WAS NOT COUNTED (49-2 AC7).
            -- A published Semantic Model version that names this domain lands in
            -- `app.master_data_used_by` (`core.semantic_model_used_by`), and so do
            -- Rule Set dependencies and observed-entity attachments. Counting only
            -- the business links reported "nothing depends on this" for an
            -- identity a published View resolves through.
            SELECT COUNT(*)
            FROM app.master_data_used_by u
            WHERE u.node_id = d.id
              AND u.project_id = %(project_id)s
              AND u.released_at IS NULL
        ) + (
            -- The Project reuse of an organization identity. AC7 names the
            -- associations among the consumers used-by must group.
            SELECT COUNT(*)
            FROM app.master_data_project_associations a
            WHERE a.node_id = d.id
              AND a.project_id = %(project_id)s
              AND a.retired_at IS NULL
        ) AS used_by_count
    ) l ON TRUE
    WHERE d.org_id = %(org_id)s
    ORDER BY d.name, d.id
"""

#: The classifications lens, re-pointed on the same day and for the same reason
#: as `_BUSINESS_DOMAINS` above. The parent Business Domain's NAME comes through
#: the authority too: a classification minted natively under a natively minted
#: domain used to show a blank parent, which reads as "belongs to nothing".
_CLASSIFICATIONS = f"""
    SELECT c.id, c.slug, c.name, c.description, c.owner, c.status, c.domain_id,
           c.parent_id, c.classification_type, c.created_at, c.updated_at, c.archived_at,
           d.name AS domain_name,
           v.version_count, v.latest_version, v.latest_changed_at,
           {CURRENT_VERSION_ID.format(id_expression="c.id", org_expression="c.org_id")}
             AS current_version_id,
           l.used_by_count
    FROM {CLASSIFICATION_SOURCE} c
    LEFT JOIN {DOMAIN_SOURCE} d ON d.id = c.domain_id AND d.org_id = c.org_id
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS version_count,
               MAX(cv.version_number) AS latest_version,
               MAX(cv.changed_at) AS latest_changed_at
        FROM {CLASSIFICATION_VERSION_SOURCE} cv
        WHERE cv.classification_id = c.id
    ) v ON TRUE
    LEFT JOIN LATERAL (
        -- Three stores, same reason as `_BUSINESS_DOMAINS` above.
        SELECT (
            SELECT COUNT(*)
            FROM app.mdm_business_links bl
            WHERE bl.taxonomy_type = 'business_classification'
              AND bl.taxonomy_id = c.id
              AND bl.project_id = %(project_id)s
              AND bl.retired_at IS NULL
        ) + (
            SELECT COUNT(*)
            FROM app.master_data_used_by u
            WHERE u.node_id = c.id
              AND u.project_id = %(project_id)s
              AND u.released_at IS NULL
        ) + (
            SELECT COUNT(*)
            FROM app.master_data_project_associations a
            WHERE a.node_id = c.id
              AND a.project_id = %(project_id)s
              AND a.retired_at IS NULL
        ) AS used_by_count
    ) l ON TRUE
    WHERE c.org_id = %(org_id)s
    ORDER BY c.name, c.id
"""

# ---------------------------------------------------------------------------
# Story 49.4: the three governed Controls & Quality collections.
#
# Each of these replaced a projection that had no object underneath it:
#   conflicts       projected app.mapping_proposals AS a case. A proposal is a
#                   candidate; it carries no evidence episodes, no impact and no
#                   decision history, so "acknowledge" and "recurrence" had
#                   nowhere to live.
#   reconciliation  projected mutable app.overlap_groups, addressed by connector
#                   label and resolved through a hidden scope cascade.
#   data-quality    projected nothing at all -- it answered with a pending-owner
#                   reason, because the computed DQ types had no stable identity
#                   to list.
# ---------------------------------------------------------------------------

_GOVERNED_CONTROL_CASES = """
    SELECT c.id, c.case_type, c.subject_kind, c.subject_id, c.severity, c.status,
           c.owner, c.first_observed_at, c.last_observed_at, c.evidence_horizon_at,
           c.supersedes_case_id,
           o.occurrence_count, o.latest_observed_at,
           d.decision_count, d.latest_decision_kind, d.latest_owner_outcome,
           k.candidate_count,
           u.dependent_issue_count
    FROM app.control_cases c
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS occurrence_count, MAX(observed_at) AS latest_observed_at
        FROM app.control_case_occurrences WHERE case_id = c.id
    ) o ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS decision_count,
               (ARRAY_AGG(decision_kind ORDER BY decided_at DESC))[1] AS latest_decision_kind,
               (ARRAY_AGG(owner_outcome ORDER BY decided_at DESC))[1] AS latest_owner_outcome
        FROM app.control_case_decisions WHERE case_id = c.id
    ) d ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS candidate_count
        FROM app.control_case_candidates WHERE case_id = c.id
    ) k ON TRUE
    -- Used-by, COUNTED rather than declared empty. A case exists because
    -- something escalated to it; an object that says "nothing uses me" while a
    -- DQ issue points straight at it is the exact failure the Governance
    -- contract calls "a governed object lacks used-by".
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS dependent_issue_count
        FROM app.dq_issues WHERE control_case_id = c.id
    ) u ON TRUE
    WHERE c.project_id = %(project_id)s
    ORDER BY CASE c.severity WHEN 'blocking' THEN 0 WHEN 'degrading' THEN 1 ELSE 2 END,
             c.last_observed_at DESC, c.id
"""

_GOVERNED_RULE_SETS = """
    SELECT s.id, s.family, s.name, s.label, s.lifecycle_status,
           s.current_version_id, s.pending_version_id, s.last_known_good_version_id,
           s.created_at, s.updated_at,
           v.version_number, v.profile, v.effective_from, v.effective_to,
           v.content_hash, v.ordered_rules,
           n.version_count, a.approval_count, x.exception_count,
           u.dependent_monitor_count
    FROM app.governance_rule_sets s
    LEFT JOIN app.governance_rule_set_versions v ON v.id = s.current_version_id
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS version_count
        FROM app.governance_rule_set_versions WHERE rule_set_id = s.id
    ) n ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS approval_count
        FROM app.rule_set_approvals WHERE rule_set_id = s.id
    ) a ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS exception_count
        FROM app.governance_rule_set_exceptions
        WHERE rule_set_id = s.id
          AND (expires_at IS NULL OR expires_at >= CURRENT_DATE)
    ) x ON TRUE
    -- Monitors whose PUBLISHED version pins this Rule Set. Counted from the
    -- version and not from the head, because "the latest policy" is not a
    -- policy: a monitor depends on the exact version it was published against.
    LEFT JOIN LATERAL (
        SELECT COUNT(DISTINCT m.id) AS dependent_monitor_count
        FROM app.dq_monitors m
        JOIN app.dq_monitor_versions mv ON mv.id = m.current_version_id
        WHERE mv.rule_set_id = s.id AND m.lifecycle_status <> 'archived'
    ) u ON TRUE
    WHERE s.project_id = %(project_id)s AND s.lifecycle_status <> 'archived'
    ORDER BY s.family, s.name
"""

_GOVERNED_DQ_MONITORS = f"""
    SELECT m.id, m.name, m.label, m.target_kind, m.target_id, m.lifecycle_status,
           m.runtime_state, m.current_version_id, m.pending_version_id,
           m.created_at, m.updated_at,
           v.version_number, v.check_profile, v.severity, v.window_days, v.content_hash,
           e.outcome, e.evaluated_at, e.total_eligible, e.evaluated_count,
           e.passed_count, e.failed_count, e.unavailable_count,
           i.open_issue_count,
           u.dependent_case_count
    FROM app.dq_monitors m
    LEFT JOIN app.dq_monitor_versions v ON v.id = m.current_version_id
    LEFT JOIN LATERAL (
        SELECT outcome, evaluated_at, total_eligible, evaluated_count,
               passed_count, failed_count, unavailable_count
        FROM app.dq_evaluations
        WHERE monitor_id = m.id AND project_id = m.project_id
        ORDER BY evaluated_at DESC LIMIT 1
    ) e ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS open_issue_count
        FROM app.dq_issues
        -- "Still open" is dq_governance's ONE sentence (AI-235): spelled here
        -- without its suppression half, a live-suppressed anomaly counted as
        -- open on this card while `dq_governance.open_issues` excluded it.
        WHERE monitor_id = m.id AND {open_issue_predicate()}
    ) i ON TRUE
    -- The Control Cases this monitor's issues escalated to. This is the link
    -- that makes "can I retire this monitor?" answerable from the object rather
    -- than by reading four tables.
    LEFT JOIN LATERAL (
        SELECT COUNT(DISTINCT control_case_id) AS dependent_case_count
        FROM app.dq_issues
        WHERE monitor_id = m.id AND control_case_id IS NOT NULL
    ) u ON TRUE
    WHERE m.project_id = %(project_id)s AND m.lifecycle_status <> 'archived'
    ORDER BY m.name
"""  # noqa: S608 -- the interpolation is a module-owned literal

_CONTROL_CASES = """
    SELECT mp.id, mp.datastream_id, mp.mapping_version_id, mp.mode, mp.state,
           mp.checks, mp.diff, mp.policy_version, mp.content_hash,
           mp.created_at, mp.updated_at,
           d.name AS datastream_name,
           pc.id AS confirmation_id, pc.state AS confirmation_state,
           pc.created_at AS confirmed_at
    FROM app.mapping_proposals mp
    LEFT JOIN app.datastreams d
      ON d.id = mp.datastream_id AND d.project_id = mp.project_id
    LEFT JOIN LATERAL (
        SELECT c.id, c.state, c.created_at
        FROM app.publication_confirmations c
        WHERE c.proposal_id = mp.id AND c.project_id = mp.project_id
        ORDER BY c.created_at DESC
        LIMIT 1
    ) pc ON TRUE
    WHERE mp.project_id = %(project_id)s
    ORDER BY mp.created_at DESC, mp.id
"""

# `_RECONCILIATION_RULE_SETS` lived here and read `app.overlap_groups`. Story
# 49.4 cut the reconciliation lens over to `_GOVERNED_RULE_SETS` and left the
# query behind: measured 2026-08-16, its only remaining reader was a test
# asserting its TEXT. A tenant-isolation guard on a query nobody runs reads as
# proof that reconciliation is scoped, while the path actually served carries
# none of it -- so the query is gone and the guard now reads the live one.


# The registries a CLIENT declares (Story 64.1/64.13, AI-232).
#
# WHY THIS QUERY EXISTS AND WHY THE LENS IS WRONG WITHOUT IT. `_registries_lens`
# read `_CAPABILITY_OWNER_OBJECTS` and nothing else, so a registry was listed only
# when a project CAPABILITY pinned it -- Country, Competitors. A client object kind
# is pinned by no capability: it is declared for one Project against one feeding
# Datastream. It was therefore absent from the lens AND unresolvable at Level 3,
# because `_load_object` resolves an object by scanning its own lens. A whole
# governed object could be declared over MCP and no screen could open it.
#
# `live_source_count` is selected, not derived on the client: zero live sources is
# a state a reader must be able to see (the kind is declared and no longer fed),
# and it is not the same sentence as "no such kind".
_CLIENT_OBJECT_REGISTRIES = """
    SELECT r.id, r.object_kind, r.label, r.lifecycle_state, r.version_scope,
           r.scope, r.created_by, r.created_at, r.updated_at, r.current_version_id,
           (SELECT COUNT(*) FROM app.master_data_nodes n
             WHERE n.registry_id = r.id AND n.archived_at IS NULL) AS node_count,
           (SELECT COUNT(*) FROM app.master_data_source_bindings b
             WHERE b.registry_id = r.id AND b.released_at IS NULL) AS live_source_count,
           (SELECT COUNT(*) FROM app.master_data_used_by u
             WHERE u.registry_id = r.id AND u.released_at IS NULL) AS used_by_count,
           (SELECT COUNT(*) FROM app.master_data_object_versions v
             WHERE v.registry_id = r.id) AS version_count
    FROM app.master_data_registries r
    WHERE r.project_id = %(project_id)s
    ORDER BY r.label, r.id
"""

# The INSTANCES of one client-declared Master Data collection (Story 49.2).
#
# WHY IT IS A LEFT JOIN AND NOT A FILTER ON NODES. Four states have to be told
# apart, and three of them return no instance:
#
#   no row at all      -- this Project never declared the object kind;
#   a row, node NULL,  -- the kind is declared, its source binding was released;
#     live_source 0
#   a row, node NULL   -- declared and fed, and no identity has been resolved yet;
#   rows with a node   -- the instances, which are what the lens lists.
#
# A query that started `FROM app.master_data_nodes` would collapse the first three
# into one empty list, and the screen would send an operator to declare a kind
# that already exists -- the exact confusion `object_kind_registry.
# describe_object_kind` names when it returns `no_live_source_binding`.
#
# The version columns are read twice, per NODE and per REGISTRY, because
# `version_scope` decides which of the two is this object's history (migration
# 143). Counting only the node's would tell a reader that a registry-scoped object
# has no version, when its history is simply kept one level up.
_CLIENT_OBJECT_INSTANCES = """
    SELECT r.id AS registry_id,
           r.object_kind AS declared_object_kind,
           r.label AS registry_label,
           r.lifecycle_state AS registry_state,
           r.version_scope,
           r.scope AS registry_scope,
           (SELECT COUNT(*) FROM app.master_data_source_bindings b
             WHERE b.project_id = r.project_id AND b.registry_id = r.id
               AND b.released_at IS NULL) AS live_source_count,
           (SELECT COUNT(*) FROM app.master_data_object_versions rv
             WHERE rv.project_id = r.project_id AND rv.registry_id = r.id
               AND rv.node_id IS NULL) AS registry_version_count,
           n.id AS node_id,
           n.label AS node_label,
           n.node_kind,
           n.created_by AS node_created_by,
           n.updated_at AS node_updated_at,
           -- AN ARCHIVED INSTANCE KEEPS ITS ADDRESS, and this column is what
           -- carries its state up (2026-08-31). See the JOIN below.
           n.archived_at AS node_archived_at,
           cur.id AS current_version_id,
           cur.version_number AS current_version_number,
           cur.published_at AS current_published_at,
           latest.status AS latest_version_status,
           (SELECT COUNT(*) FROM app.master_data_object_versions v
             WHERE v.project_id = n.project_id AND v.node_id = n.id) AS node_version_count,
           -- THREE CONSUMER STORES, NOT ONE (49-2 AC7). A Product is named by a
           -- Rule Set or a Semantic Model version through `master_data_used_by`,
           -- by a Context topic or a Datastream field through a business link,
           -- and by a Project through an association. Counting one of the three
           -- made the other two invisible to the person about to archive it.
           ((SELECT COUNT(*) FROM app.master_data_used_by u
              WHERE u.project_id = n.project_id AND u.node_id = n.id
                AND u.released_at IS NULL)
            + (SELECT COUNT(*) FROM app.mdm_business_links bl
                WHERE bl.taxonomy_id = n.id AND bl.project_id = r.project_id
                  AND bl.retired_at IS NULL)
            + (SELECT COUNT(*) FROM app.master_data_project_associations a
                WHERE a.node_id = n.id AND a.project_id = r.project_id
                  AND a.retired_at IS NULL)) AS used_by_count,
           (SELECT COUNT(*) FROM app.master_data_aliases a
             WHERE a.node_id = n.id AND a.retired_at IS NULL) AS alias_count
    FROM app.master_data_registries r
    -- NO `archived_at IS NULL` HERE, and its absence is the repair of 2026-08-31.
    -- The filter used to live on this JOIN, so an archived Product or Activity
    -- had NO ADDRESS AT ALL: `_load_object` resolves an instance by scanning this
    -- lens, so the workbench answered "not found" and the Restore command the
    -- Overview draws for an archived identity was unreachable -- the object could
    -- be retired and never brought back. A business domain has always been served
    -- archived (`_BUSINESS_DOMAINS` filters nothing and reports `status`), and
    -- this is the same answer for the same question: the row is served with its
    -- lifecycle state, and the screen says Archived and offers Restore.
    LEFT JOIN app.master_data_nodes n
      ON n.project_id = r.project_id AND n.registry_id = r.id
    LEFT JOIN LATERAL (
        SELECT v.id, v.version_number, v.published_at
        FROM app.master_data_object_versions v
        WHERE v.project_id = n.project_id AND v.node_id = n.id AND v.status = 'current'
        LIMIT 1
    ) cur ON TRUE
    LEFT JOIN LATERAL (
        SELECT v.status
        FROM app.master_data_object_versions v
        WHERE v.project_id = n.project_id AND v.node_id = n.id
        ORDER BY v.version_number DESC
        LIMIT 1
    ) latest ON TRUE
    WHERE r.project_id = %(project_id)s
      AND r.object_kind = %(object_kind)s
    -- Live first, archived after, exactly as the Master Data node tree orders
    -- them. An archived instance is listed and says so; it is not hidden and it
    -- is not mixed in as though nothing had happened to it.
    ORDER BY (n.archived_at IS NOT NULL), n.label, n.id
"""

# Epic 48 pinned Governance owner references. This is the only place a capability
# object (a registry, a rule ladder) has a stable id AND an exact version id.
_CAPABILITY_OWNER_OBJECTS = """
    SELECT r.object_id, r.object_type, r.version_id, r.capability_key,
           r.owner_reference, r.evidence_hash, r.configuration_version_id,
           r.carried_forward, r.created_at,
           pc.state AS capability_state, pc.availability,
           pc.active_version_id
    FROM app.project_configuration_owner_references r
    LEFT JOIN app.project_capabilities pc
      ON pc.project_id = r.project_id AND pc.capability_key = r.capability_key
    WHERE r.project_id = %(project_id)s
      AND r.owner_kind = 'governance'
      AND r.object_type = %(object_type)s
    ORDER BY r.created_at DESC, r.object_id
"""

# The three Evidence lenses no longer read three unrelated owner tables. They
# read `app.evidence_records` through `core.evidence_index`, which is the only
# place a producer is registered and the only place an edge is persisted.
#
# What was here before, and why it is gone:
#
#   `_LINEAGE_TRACES`  selected one row of `datastream_mapping_publication_log`
#                      and called it an Evidence Trace. One mapping publication
#                      is one NODE. It is now indexed as exactly that, with its
#                      own workbench reporting `partial` coverage.
#   `_VERSION_APPROVALS` used a CONFIRMATION id as the Object Version identity.
#                      A confirmation approves a version; it is not one. The
#                      version is now the object, and the confirmation is an
#                      `approved_by` edge to the exact approval record.
#   `_AUDIT_EVENTS`    scoped audit rows through `metadata->>'project_id'`,
#                      which the normalized 060 spine does not write -- so every
#                      audited operation since that spine landed was invisible
#                      to the lens named after it. Scope now resolves from the
#                      authorized resource graph, with legacy metadata accepted
#                      only when it names a real Project.


# ---------------------------------------------------------------------------
# Projections. One function per owner; each returns the common object envelope.
# ---------------------------------------------------------------------------


def _object_envelope(
    *,
    section: str,
    object_type: str,
    object_id: str,
    label: str,
    scope: str,
    owner: dict[str, Any],
    lifecycle_status: str,
    active_version_ref: dict[str, Any] | None,
    used_by: dict[str, Any],
    versions: dict[str, Any],
    evidence: dict[str, Any],
    summary: dict[str, Any],
    evidence_as_of: str | None,
    allowed_actions: list[str] | None = None,
) -> dict[str, Any]:
    contract = GOVERNANCE_SECTIONS[section].object_contract(object_type)
    if contract is None:  # pragma: no cover -- guarded by the adapter registry
        raise GovernanceUnknownRoute(f"unregistered object type: {object_type}")
    return {
        "object_ref": {
            "type": object_type,
            "id": object_id,
            "label": label,
            "owner_href": _owner_href(section, object_type=object_type, object_id=object_id),
        },
        "scope": scope,
        "owner": owner,
        "lifecycle_status": lifecycle_status,
        "active_version_ref": active_version_ref,
        "selected_version_ref": None,
        "available_tabs": list(contract.tabs),
        "default_tab": contract.default_tab,
        "allowed_actions": allowed_actions or [],
        "used_by": used_by,
        "versions": versions,
        "evidence": evidence,
        "summary": summary,
        "evidence_as_of": evidence_as_of,
    }


def _business_domain(row: Mapping[str, Any], *, org_id: str) -> dict[str, Any]:
    object_id = str(row["id"])
    # AI-239: `or 0` here answered "nothing depends on this" for a column the
    # query did not return. The absence survives as None and reads `unavailable`.
    used_by_count = None if row.get("used_by_count") is None else int(row["used_by_count"])
    version_count = int(row.get("version_count") or 0)
    version_refs = row.get("version_refs")
    if not isinstance(version_refs, list):
        version_refs = []
    return _object_envelope(
        section="master-data",
        object_type="business-domain",
        object_id=object_id,
        label=str(row.get("name") or object_id),
        scope="organization",
        owner={
            "kind": "business-taxonomy",
            "organization_id": org_id,
            "steward": row.get("owner"),
        },
        lifecycle_status=str(row.get("status") or "unavailable"),
        active_version_ref=(
            {
                "object_type": "business-domain-version",
                "id": f"{object_id}:{row['latest_version']}",
                "version": int(row["latest_version"]),
                "state": "active",
            }
            if row.get("latest_version")
            else None
        ),
        used_by=_counted_facet(used_by_count),
        versions=_counted_facet(version_count, version_refs),
        evidence=_facet("empty"),
        summary={
            "slug": row.get("slug"),
            "description": row.get("description"),
            "classification_count": int(row.get("classification_count") or 0),
            # The base a rename is preconditioned on -- the authority's current
            # revision, by id. It is NOT the `version` of `active_version_ref`
            # above, which is the union counter this surface SHOWS; a door that
            # sent that number back would compare two ledgers that count
            # independently. None where the identity has no published revision.
            "current_version_id": _text(row.get("current_version_id")),
        },
        evidence_as_of=_iso(row.get("latest_changed_at") or row.get("updated_at")),
    )


def _classification(row: Mapping[str, Any], *, org_id: str) -> dict[str, Any]:
    object_id = str(row["id"])
    # AI-239: `or 0` here answered "nothing depends on this" for a column the
    # query did not return. The absence survives as None and reads `unavailable`.
    used_by_count = None if row.get("used_by_count") is None else int(row["used_by_count"])
    version_count = int(row.get("version_count") or 0)
    return _object_envelope(
        section="master-data",
        object_type="master-data-object",
        object_id=object_id,
        label=str(row.get("name") or object_id),
        scope="organization",
        owner={
            "kind": "business-taxonomy",
            "organization_id": org_id,
            "steward": row.get("owner"),
            "parent_href": _owner_href(
                "master-data",
                object_type="business-domain",
                object_id=str(row["domain_id"]),
            )
            if row.get("domain_id")
            else None,
        },
        lifecycle_status=str(row.get("status") or "unavailable"),
        active_version_ref=(
            {
                "object_type": "master-data-object-version",
                "id": f"{object_id}:{row['latest_version']}",
                "version": int(row["latest_version"]),
                "state": "active",
            }
            if row.get("latest_version")
            else None
        ),
        used_by=_counted_facet(used_by_count),
        # No refs from the LENS: a classification's history is a UNION of two
        # stores and composing it per row would run one more statement for every
        # line of the collection. `_enrich_master_data_object` composes it for the
        # object a person actually opens; until then the facet says how many
        # there are and that it cannot name them, which is the truth.
        versions=_counted_facet(version_count),
        evidence=_facet("empty"),
        summary={
            "slug": row.get("slug"),
            "classification_type": row.get("classification_type"),
            "business_domain": row.get("domain_name"),
            "parent_id": row.get("parent_id"),
            "description": row.get("description"),
            # Same base, same reason as `_business_domain` above.
            "current_version_id": _text(row.get("current_version_id")),
        },
        evidence_as_of=_iso(row.get("latest_changed_at") or row.get("updated_at")),
    )


def _client_object_registry(row: Mapping[str, Any], *, org_id: str) -> dict[str, Any]:
    """One registry a client declared for its own object kind (Story 64.1, AI-232).

    It is a `registry`, the same object type Country and Competitors use, because
    it IS one -- `app.master_data_registries` with a client-named `object_kind`.
    Giving it a type of its own would have split one workbench in two and made
    Hierarchy, Mappings & Aliases, Used by and Versions answer twice.

    `version_scope` travels in the summary because it is the difference between an
    object kind and a vocabulary: `node` means each instance carries its own
    payload of properties, `registry` means one payload for the whole kind. A
    reader who cannot see which one this is cannot tell what a version means here.
    """
    object_id = str(row["id"])
    used_by_count = None if row.get("used_by_count") is None else int(row["used_by_count"])
    version_count = int(row.get("version_count") or 0)
    live_sources = None if row.get("live_source_count") is None else int(row["live_source_count"])
    return _object_envelope(
        section="master-data",
        object_type="registry",
        object_id=object_id,
        label=str(row.get("label") or row.get("object_kind") or object_id),
        scope=str(row.get("scope") or "project"),
        owner={
            "kind": "master-data",
            "organization_id": org_id,
            "steward": row.get("created_by"),
        },
        lifecycle_status=str(row.get("lifecycle_state") or "unavailable"),
        active_version_ref=(
            {
                "object_type": "master-data-object-version",
                "id": str(row["current_version_id"]),
                "state": "active",
            }
            if row.get("current_version_id")
            else None
        ),
        used_by=_counted_facet(used_by_count),
        versions=_counted_facet(version_count),
        evidence=_facet("empty"),
        summary={
            "object_kind": row.get("object_kind"),
            "version_scope": row.get("version_scope"),
            "instance_count": int(row.get("node_count") or 0),
            "live_source_count": live_sources,
        },
        evidence_as_of=_iso(row.get("updated_at") or row.get("created_at")),
    )


def _client_object_instance(row: Mapping[str, Any], *, org_id: str) -> dict[str, Any]:
    """One instance of a client-declared Master Data collection -- one Product,
    one Activity (Story 49.2).

    IT IS A `master-data-object`, AND THAT IS THE MIGRATION'S OWN ANSWER.
    Migration 143's header states it twice: "`registry` and `master-data-object`
    are distinct object types in the Story 49.1 route registry", and "49.2 needs
    Business Domains, Products and Activities to version PER OBJECT. Publishing
    one Product must not mint a new version of every other Product". The registry
    is the `registry` the `registries` lens already lists; each identity inside it
    is the object a person opens, edits and publishes on its own. A classification
    and a Product are the same kind of thing to a reader -- a governed name with a
    hierarchy, aliases, dependents and a history -- and they answer the same five
    tabs, so they are the same object type rather than two workbenches.

    NOTHING IS INVENTED WHEN A COLUMN IS ABSENT. A used-by count that did not come
    back is `unavailable`, never zero. A node with no version of its own is
    `declared`, not `draft`: no draft exists, and saying one does would send a
    reader looking for it.
    """
    object_id = str(row["node_id"])
    # AN ARCHIVED INSTANCE IS SERVED, AND IT SAYS SO (2026-08-31). The lens used
    # to filter it out of the join, so it had no address and the Restore command
    # the Overview draws for `lifecycle_status == "archived"` could never be
    # reached. Reported the way a Business Domain has always reported it -- from
    # `archived_at` itself -- because one lifecycle word per object is what lets
    # one screen read all three kinds.
    archived_at = row.get("node_archived_at")
    used_by_count = None if row.get("used_by_count") is None else int(row["used_by_count"])
    # `version_scope` decides WHERE this object's history is kept, so it decides
    # which of the two counts is the answer. Reading the node's count under a
    # registry-scoped kind would report "no version" for an object whose history
    # is simply the grouping's.
    version_scope = str(row.get("version_scope") or "node")
    version_count = (
        row.get("node_version_count")
        if version_scope == "node"
        else row.get("registry_version_count")
    )
    current_version_id = row.get("current_version_id")
    alias_count = None if row.get("alias_count") is None else int(row["alias_count"])
    live_sources = None if row.get("live_source_count") is None else int(row["live_source_count"])
    return _object_envelope(
        section="master-data",
        object_type="master-data-object",
        object_id=object_id,
        label=str(row.get("node_label") or object_id),
        scope=str(row.get("registry_scope") or "project"),
        owner={
            "kind": "master-data",
            "organization_id": org_id,
            "steward": row.get("node_created_by"),
            # The registry is the parent a reader climbs to for the source that
            # feeds this object and for the kind it belongs to.
            "parent_href": _owner_href(
                "master-data", object_type="registry", object_id=str(row["registry_id"])
            ),
        },
        lifecycle_status=(
            "archived"
            if archived_at
            else (
                "active"
                if current_version_id
                else str(row.get("latest_version_status") or "declared")
            )
        ),
        active_version_ref=(
            {
                "object_type": "master-data-object-version",
                "id": str(current_version_id),
                "version": (
                    int(row["current_version_number"])
                    if row.get("current_version_number") is not None
                    else None
                ),
                "state": "active",
            }
            if current_version_id
            else None
        ),
        used_by=_counted_facet(used_by_count),
        versions=_counted_facet(version_count),
        evidence=_facet("empty"),
        summary={
            "object_kind": row.get("declared_object_kind"),
            "registry_id": row.get("registry_id"),
            "registry_label": row.get("registry_label"),
            "registry_state": row.get("registry_state"),
            "version_scope": version_scope,
            "node_kind": row.get("node_kind"),
            "alias_count": alias_count,
            "live_source_count": live_sources,
            # The base a rename must state (governance.md, 2026-08-30). The two
            # identity composers carried it from the first change; this one did
            # not, so every Product / Activity rename sent `null` and was refused
            # `version_conflict` with a reload that could never help (review of
            # d1fbdbb6). The read model GIVES the base -- for all three kinds.
            "current_version_id": (
                str(current_version_id) if current_version_id else None
            ),
            # When it was retired, so an archived instance says WHEN and not only
            # THAT. None on a live one, which is the honest absence.
            "archived_at": _iso(archived_at),
        },
        evidence_as_of=_iso(row.get("current_published_at") or row.get("node_updated_at")),
    )


def _version_refs(rows: list[Mapping[str, Any]], object_type: str, object_id: str) -> list[dict]:
    """Exact immutable history, newest first. `state` is the version's OWN
    lifecycle, not its position: a superseded version says superseded."""
    return [
        {
            "object_type": f"{object_type}-version",
            "id": str(row["id"]),
            "version": int(row["version_number"]),
            "state": str(row.get("status") or "unavailable"),
            "recorded_at": _iso(row.get("created_at")),
            "evidence_hash": row.get("content_hash"),
            "owner_href": _owner_href(
                "semantic-model",
                object_type=object_type,
                object_id=object_id,
                tab="versions",
                version_id=str(row["id"]),
            ),
        }
        for row in rows
    ]


def _semantic_model_actions(row: Mapping[str, Any], *, edit_action: str) -> list[str]:
    """The change-set gestures this object could honour RIGHT NOW.

    Declared from what is true of the row, never from what the screen would like
    to draw. Three facts decide it, and each is a refusal the applier would
    otherwise make after the person acted:

    * **An exact base version.** `create_change_set` refuses `missing_exact_base`
      for every `edit_*` and for `archive_object` without one
      (`semantic_model.py:541-548`), so an object with no `current_version_id`
      can honour neither.
    * **It is not already archived.** `_apply_archive` answers `already_archived`,
      and editing a retired object would publish a version onto it.
    * **It is this Project's.** A platform-scoped Concept (`project_id IS NULL`)
      is read by every Project; `archive_object` refuses to retire it from one.

    `explore-data` stays where it was: it is a handoff, not a change set, and it
    answers a different question (is there a compiled, queryable version).
    """
    if str(row.get("lifecycle_status") or "") == "archived":
        return []
    if not row.get("current_version_id"):
        return []
    actions = [edit_action]
    # `project_id` is NOT NULL on `app.semantic_views`, so this only ever
    # excludes a platform Concept.
    if row.get("project_id"):
        actions.append("archive_object")
    return actions


def _concept_name_by_version_id(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """`{version_id -> nom du Concept}` pour les Concepts LISIBLES dans ce projet.

    Un operande `concept_ref` porte une version, pas un nom, donc sans cette
    table la parite d une formule publiee serait illisible pour tout le monde
    sauf pour qui lance le script. Elle est composee UNE fois par lentille, a
    partir des lignes deja chargees -- aucune requete de plus.

    Elle ne connait que les versions COURANTES : un operande epingle sur une
    version anterieure rend `unreadable`, ce qui est l aveu honnete que la
    comparaison n a pas eu lieu -- jamais un `aligned` invente.
    """
    return {
        str(row["current_version_id"]): str(row.get("name") or "")
        for row in rows
        if row.get("current_version_id") and row.get("name")
    }


def _semantic_concept(
    row: Mapping[str, Any],
    *,
    project_id: str,
    versions: list[Mapping[str, Any]] | None = None,
    coverage: Mapping[str, Any] | None = None,
    operand_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    object_id = str(row["id"])
    # AI-239, same reason as above.
    used_by_views = (
        None if row.get("used_by_view_count") is None else int(row["used_by_view_count"])
    )
    version_count = int(row.get("version_count") or 0)
    provenance = row.get("provenance") or {}
    migrated = provenance.get("migrated_from") or {}
    expression = row.get("expression")
    #  LE VERDICT DE PARITE, PORTE JUSQU A L ECRAN. La formule d un ratio existe
    #  en trois copies et `scripts/check_metric_formula_parity.py` les confronte
    #  depuis 2026-08-16 -- mais son verdict n etait visible que de qui lance le
    #  script. La REGLE est importee, jamais recopiee : une seconde comparaison
    #  ecrite ici serait exactement le defaut que la garde surveille.
    from core.platform_semantic_concepts import formula_parity  # noqa: PLC0415

    try:
        parity = formula_parity(
            name=row.get("name"),
            project_id=row.get("project_id"),
            expression=expression,
            names_by_version_id=operand_names,
        )
    except Exception:  # noqa: BLE001 -- un catalogue illisible ne casse pas la lentille
        parity = None
    return _object_envelope(
        section="semantic-model",
        object_type="semantic-concept",
        object_id=object_id,
        label=str(row.get("label") or row.get("name") or object_id),
        scope="project" if row.get("project_id") else "platform",
        owner={
            "kind": "semantic-model",
            "project_id": row.get("project_id"),
            "steward": row.get("owner"),
            # The dictionary row this identity was reconciled from, when there
            # was one. Kept so an operator can trace an id back to what it was.
            "migrated_from": migrated or None,
        },
        lifecycle_status=str(row.get("lifecycle_status") or "unavailable"),
        active_version_ref=(
            {
                "object_type": "semantic-concept-version",
                "id": str(row["current_version_id"]),
                "version": int(row.get("version_number") or 1),
                "state": "published",
            }
            if row.get("current_version_id")
            else None
        ),
        used_by=_counted_facet(used_by_views),
        versions=_facet(
            "available" if version_count else "empty",
            _version_refs(list(versions or ()), "semantic-concept", object_id),
            count=version_count,
        ),
        evidence=_facet("empty"),
        summary={
            # THE MACHINE NAME, and it is not decoration. Every published version
            # writes `payload["name"]` (`_apply_concept` :1688), so a console that
            # composes an edit MUST send it -- and `label` above is composed from
            # `label or name`, which means an object with a display label used to
            # arrive with its identity nowhere on the wire. The workbench dialog
            # then had to refuse the edit rather than reconstruct it.
            "name": row.get("name"),
            "concept_kind": row.get("kind"),
            "value_type": row.get("value_type"),
            "unit": row.get("unit"),
            "format": row.get("format"),
            "currency_scope": (row.get("currency_behavior") or {}).get("scope"),
            "aggregation": (row.get("aggregation") or {}).get("function"),
            "additivity_class": row.get("additivity_class"),
            "non_additive_dimensions": list(row.get("non_additive_dimensions") or ()),
            "semantic_type": row.get("semantic_type"),
            "conformance": row.get("conformance"),
            "description": row.get("definition"),
            "business_domain_refs": row.get("business_domain_refs") or [],
            "has_formula": bool(expression),
            "expression": expression,
            #  `None` quand il n y a RIEN a confronter -- c est le cas de presque
            #  tous les Concepts. Une pastille << rien a signaler >> sur chacun
            #  d eux apprendrait a ne plus la lire.
            "formula_parity": parity,
            "pending_version_id": row.get("pending_version_id"),
            "last_known_good_version_id": row.get("last_known_good_version_id"),
            # Coverage is answered by the Data projection, once, server-side.
            # `None` means the owner could not be read — never zero.
            "bound_datastream_count": (coverage or {}).get("bound"),
            "eligible_datastream_count": (coverage or {}).get("eligible"),
            "coverage_state": (coverage or {}).get("state", "unavailable"),
            "unspecified": provenance.get("unspecified") or [],
        },
        evidence_as_of=_iso(row.get("version_created_at") or row.get("updated_at")),
        allowed_actions=_semantic_model_actions(row, edit_action="edit_concept"),
    )


def _semantic_view(
    row: Mapping[str, Any],
    *,
    project_id: str,
    versions: list[Mapping[str, Any]] | None = None,
    used_by_count: int | None = None,
) -> dict[str, Any]:
    object_id = str(row["id"])
    version_count = int(row.get("version_count") or 0)
    matrix = row.get("queryability_matrix") or {}
    summary = matrix.get("summary") or {}
    published = bool(row.get("current_version_id"))
    return _object_envelope(
        section="semantic-model",
        object_type="semantic-view",
        object_id=object_id,
        label=str(row.get("label") or row.get("name") or object_id),
        scope="project",
        owner={
            "kind": "semantic-model",
            "project_id": project_id,
            "business_scope": row.get("business_scope"),
        },
        lifecycle_status=str(row.get("lifecycle_status") or "unavailable"),
        active_version_ref=(
            {
                "object_type": "semantic-view-version",
                "id": str(row["current_version_id"]),
                "version": int(row.get("version_number") or 1),
                "state": "published",
            }
            if published
            else None
        ),
        # `None` means the question could not be asked and `unavailable` is the
        # true answer. A number -- including zero -- means it WAS asked: zero is
        # then "nothing depends on this view", which is a fact, not a gap.
        used_by=_counted_facet(used_by_count),
        versions=_facet(
            "available" if version_count else "empty",
            _version_refs(list(versions or ()), "semantic-view", object_id),
            count=version_count,
        ),
        evidence=_facet(
            "available" if row.get("evidence_refs") else "empty",
            list(row.get("evidence_refs") or []),
        ),
        summary={
            # The machine name, for the same reason it is carried on a Concept:
            # `_apply_view` writes `payload["name"]` into every version (:1797),
            # so an edit that could not read it could not compose one.
            "name": row.get("name"),
            "business_scope": row.get("business_scope"),
            "description": row.get("description"),
            "metric_count": int(row.get("metric_count") or 0),
            "dimension_count": int(row.get("dimension_count") or 0),
            "datastream_count": int(row.get("datastream_count") or 0),
            "queryable_pairs": summary.get("accepted"),
            "refused_pairs": summary.get("refused"),
            "compiler_version": row.get("compiler_version"),
            "ossie_spec_version": row.get("ossie_spec_version"),
            "business_domain_refs": row.get("business_domain_refs") or [],
            # CARRIED SO AN EDIT CAN PRESERVE THEM. `_apply_view` writes both
            # from the payload, so a new version composed without them retires
            # what the published one pinned -- silently, which is the worst way.
            # `evidence_refs` also feeds the Evidence facet above; it is the same
            # stored list, read once and shown in both places rather than twice.
            "master_data_refs": row.get("master_data_refs") or [],
            "evidence_refs": row.get("evidence_refs") or [],
            "pending_version_id": row.get("pending_version_id"),
            "last_known_good_version_id": row.get("last_known_good_version_id"),
            "query_policy": row.get("query_policy") or {},
            # The exact pair Explore needs. Absent unless a published, compiled
            # version exists: `Explore data` must never open on a promise.
            "explore_handoff": (
                {
                    "semantic_view_id": object_id,
                    "semantic_view_version_id": str(row["current_version_id"]),
                }
                if published and summary.get("accepted")
                else None
            ),
        },
        evidence_as_of=_iso(row.get("version_created_at") or row.get("updated_at")),
        allowed_actions=(
            (["explore-data"] if published and summary.get("accepted") else [])
            + _semantic_model_actions(row, edit_action="edit_view")
        ),
    )


def _governed_control_case(row, *, project_id: str) -> dict[str, Any]:
    """One Control Case, with the four episode streams counted rather than copied."""
    object_id = str(row["id"])
    decisions = int(row.get("decision_count") or 0)
    # A decision whose owner command FAILED is not a resolution. Surfacing it as
    # the case's headline is what keeps a failed handoff from reading as done.
    stalled = str(row.get("latest_owner_outcome") or "") == "failed"
    return _object_envelope(
        section="controls-quality",
        object_type="control-case",
        object_id=object_id,
        label=f"{row['case_type']} on {row['subject_kind']} {row['subject_id']}",
        scope="project",
        owner={
            "kind": "control-case",
            "project_id": project_id,
            "subject_kind": str(row["subject_kind"]),
            "subject_id": str(row["subject_id"]),
        },
        lifecycle_status=str(row.get("status") or "open"),
        active_version_ref=None,
        # COUNTED, not declared empty. A case exists because something escalated
        # to it, and an object that reports "nothing uses me" while a DQ issue
        # points straight at it is exactly what the Governance contract means by
        # "lacks used-by". AI-239: and a count nobody could read says so.
        used_by=_counted_facet(row.get("dependent_issue_count")),
        versions=_facet("available" if decisions else "empty"),
        evidence=_facet("available" if int(row.get("occurrence_count") or 0) else "empty"),
        summary={
            "severity": str(row.get("severity") or "informational"),
            "case_type": str(row["case_type"]),
            "occurrences": int(row.get("occurrence_count") or 0),
            "candidates": int(row.get("candidate_count") or 0),
            "decisions": decisions,
            "latest_decision_kind": row.get("latest_decision_kind"),
            "owner_handoff_failed": stalled,
            "first_observed_at": _iso(row.get("first_observed_at")),
            "last_observed_at": _iso(row.get("last_observed_at")),
            "evidence_horizon_at": _iso(row.get("evidence_horizon_at")),
            "supersedes_case_id": row.get("supersedes_case_id"),
            "owner": row.get("owner"),
        },
        evidence_as_of=_iso(row.get("latest_observed_at") or row.get("last_observed_at")),
    )


def _governed_rule_set(row, *, project_id: str) -> dict[str, Any]:
    """One Rule Set head with its published version, coverage and exceptions."""
    current = row.get("current_version_id")
    rules = row.get("ordered_rules") if isinstance(row.get("ordered_rules"), list) else []
    return _object_envelope(
        section="controls-quality",
        object_type="rule-set",
        object_id=str(row["id"]),
        label=str(row.get("label") or row["name"]),
        scope="project",
        owner={"kind": "rule-set", "project_id": project_id, "family": str(row["family"])},
        lifecycle_status=str(row.get("lifecycle_status") or "draft"),
        active_version_ref=(
            {
                "object_type": "governance-rule-set-version",
                "id": str(current),
                "state": "published",
                "version_number": row.get("version_number"),
            }
            if current
            else None
        ),
        used_by=_counted_facet(row.get("dependent_monitor_count")),
        versions=_facet("available" if int(row.get("version_count") or 0) else "empty"),
        evidence=_facet("available" if int(row.get("approval_count") or 0) else "empty"),
        summary={
            "family": str(row["family"]),
            "profile": row.get("profile"),
            "rule_count": len(rules),
            "version_count": int(row.get("version_count") or 0),
            "approvals": int(row.get("approval_count") or 0),
            # Exceptions still in force. An expired one is not an exception.
            "active_exceptions": int(row.get("exception_count") or 0),
            "effective_from": _iso(row.get("effective_from")),
            "effective_to": _iso(row.get("effective_to")),
            "has_pending_version": bool(row.get("pending_version_id")),
            # Distinct from `current`: a rollback reads this, and collapsing the
            # two makes a failed publication invisible.
            "last_known_good_version_id": row.get("last_known_good_version_id"),
            "content_hash": row.get("content_hash"),
        },
        evidence_as_of=_iso(row.get("updated_at")),
    )


def _governed_dq_monitor(row, *, project_id: str) -> dict[str, Any]:
    """One DQ Monitor, with its LAST evaluation rather than an inference.

    `runtime_state` is reported separately from `lifecycle_status` on purpose: a
    published monitor that has never run is `unavailable`, not `healthy`, and the
    denominator below is what makes "passed over nothing" unrenderable.
    """
    outcome = row.get("outcome")
    total = row.get("total_eligible")
    return _object_envelope(
        section="controls-quality",
        object_type="dq-monitor",
        object_id=str(row["id"]),
        label=str(row.get("label") or row["name"]),
        scope="project",
        owner={
            "kind": "dq-monitor",
            "project_id": project_id,
            "target_kind": str(row["target_kind"]),
            "target_id": str(row["target_id"]),
        },
        lifecycle_status=str(row.get("lifecycle_status") or "draft"),
        active_version_ref=(
            {
                "object_type": "dq-monitor-version",
                "id": str(row["current_version_id"]),
                "state": "published",
                "version_number": row.get("version_number"),
            }
            if row.get("current_version_id")
            else None
        ),
        used_by=_counted_facet(row.get("dependent_case_count")),
        versions=_facet("available" if row.get("current_version_id") else "empty"),
        # No evaluation is `unavailable`, never `empty`: "never measured" and
        # "measured and found nothing" are different answers.
        evidence=_facet("available" if outcome else "unavailable"),
        summary={
            "runtime_state": str(row.get("runtime_state") or "unavailable"),
            "check_profile": row.get("check_profile"),
            "severity": row.get("severity"),
            "window_days": row.get("window_days"),
            "last_outcome": outcome,
            "last_evaluated_at": _iso(row.get("evaluated_at")),
            "coverage": {
                "total_eligible": total,
                "evaluated": row.get("evaluated_count"),
                "passed": row.get("passed_count"),
                "failed": row.get("failed_count"),
                "unavailable": row.get("unavailable_count"),
            },
            "open_issues": int(row.get("open_issue_count") or 0),
            "has_pending_version": bool(row.get("pending_version_id")),
        },
        evidence_as_of=_iso(row.get("evaluated_at")),
    )


def _control_case(row: Mapping[str, Any], *, project_id: str) -> dict[str, Any]:
    object_id = str(row["id"])
    datastream_id = str(row["datastream_id"])
    checks = row.get("checks") if isinstance(row.get("checks"), dict) else {}
    failed = [name for name, verdict in checks.items() if verdict is False]
    return _object_envelope(
        section="controls-quality",
        object_type="control-case",
        object_id=object_id,
        label=f"{row.get('datastream_name') or datastream_id} mapping proposal",
        scope="project",
        owner={
            "kind": "mapping-proposal",
            "project_id": project_id,
            "datastream_href": _data_owner_href(
                "datastreams", "datastream", datastream_id, "mapping"
            ),
        },
        lifecycle_status=str(row.get("state") or "unavailable"),
        active_version_ref={
            "object_type": "datastream-mapping-version",
            "id": str(row["mapping_version_id"]),
            "state": "candidate",
        },
        used_by=_facet("empty"),
        # A control case is not a versioned object; its decision history is.
        versions=_facet("unavailable"),
        evidence=_facet(
            "available" if row.get("confirmation_id") else "empty",
            [
                {
                    "evidence_id": str(row["confirmation_id"]),
                    "kind": "publication-confirmation",
                    "state": str(row.get("confirmation_state") or "unavailable"),
                    "recorded_at": _iso(row.get("confirmed_at")),
                    "owner_href": _owner_href(
                        "evidence",
                        object_type="object-version",
                        object_id=str(row["confirmation_id"]),
                    ),
                }
            ]
            if row.get("confirmation_id")
            else [],
        ),
        summary={
            "mode": row.get("mode"),
            "failed_checks": failed,
            "check_count": len(checks),
            "content_hash": row.get("content_hash"),
            "policy_version": row.get("policy_version"),
        },
        evidence_as_of=_iso(row.get("updated_at") or row.get("created_at")),
    )


def _reconciliation_rule_set(row: Mapping[str, Any], *, org_id: str) -> dict[str, Any]:
    object_id = str(row["id"])
    # AI-239: a member count nobody could read is not a group with no members.
    members = None if row.get("member_count") is None else int(row["member_count"])
    return _object_envelope(
        section="controls-quality",
        object_type="rule-set",
        object_id=object_id,
        label=str(row.get("name") or object_id),
        scope="project" if str(row.get("scope_level")) == "PROJECT" else "organization",
        owner={
            "kind": "reconciliation-group",
            "organization_id": org_id,
            "canonical_metric": row.get("canonical_name"),
        },
        lifecycle_status="configured" if row.get("rule_id") else "unconfigured",
        active_version_ref=None,
        used_by=_counted_facet(members),
        # Reconciliation rules are updated in place; there is no version ledger to
        # read, and inventing one from `updated_at` would fabricate a history.
        versions=_facet("unavailable"),
        evidence=_facet("empty"),
        summary={
            "method": row.get("method"),
            "join_key": row.get("join_key"),
            "truth_connector": row.get("truth_connector"),
            "priority_order": row.get("priority_order"),
            "target_mart": row.get("target_mart"),
            "member_count": members,
            "description": row.get("description"),
        },
        evidence_as_of=_iso(row.get("rule_updated_at") or row.get("updated_at")),
    )


def _capability_object(
    rows: list[Mapping[str, Any]],
    *,
    section: str,
    object_type: str,
    project_id: str,
    used_by_count: int | None = None,
) -> dict[str, Any]:
    """One Epic 48 governed capability object and its pinned version ledger.

    `rows` are every owner reference for one object id, newest first. The newest
    is the active version; the rest are its previous pinned versions.
    """
    head = rows[0]
    object_id = str(head["object_id"])
    capability_key = str(head.get("capability_key") or "")
    version_refs = [
        {
            "object_type": f"{object_type}-version",
            "id": str(entry["version_id"]),
            "state": "active" if index == 0 else "previous",
            "recorded_at": _iso(entry.get("created_at")),
            "evidence_hash": entry.get("evidence_hash"),
            "owner_href": _owner_href(
                section,
                object_type=object_type,
                object_id=object_id,
                tab="versions",
                version_id=str(entry["version_id"]),
            ),
        }
        for index, entry in enumerate(rows)
    ]
    return _object_envelope(
        section=section,
        object_type=object_type,
        object_id=object_id,
        label=f"{capability_key.replace('_', ' ').title()} {object_type.replace('-', ' ')}",
        scope="project",
        owner={
            "kind": "project-capability",
            "project_id": project_id,
            "capability_key": capability_key,
            "capability_state": head.get("capability_state"),
            "availability": head.get("availability"),
        },
        lifecycle_status=str(head.get("capability_state") or "unavailable"),
        active_version_ref=version_refs[0],
        # Counted, not declared unknowable. See `_capability_used_by`: a zero is
        # the store saying no Datastream carries this capability yet, and only a
        # failed read is `unavailable`.
        used_by=_counted_facet(used_by_count),
        versions=_facet("available", version_refs, count=len(version_refs)),
        evidence=_facet(
            "available",
            [
                {
                    "evidence_id": str(head["configuration_version_id"]),
                    "kind": "project-configuration-version",
                    "state": "recorded",
                    "recorded_at": _iso(head.get("created_at")),
                    "integrity_ref": head.get("evidence_hash"),
                    "owner_href": _owner_href(
                        "evidence",
                        object_type="object-version",
                        object_id=str(head["configuration_version_id"]),
                    ),
                }
            ]
            if head.get("configuration_version_id")
            else [],
        ),
        summary={
            "capability_key": capability_key,
            "carried_forward": bool(head.get("carried_forward")),
            "configuration_version_id": head.get("configuration_version_id"),
        },
        evidence_as_of=_iso(head.get("created_at")),
    )


def _evidence_label(record: Mapping[str, Any]) -> str:
    """A label built ONLY from indexed fields.

    Not from the owner. A cached owner name would be a copy of owner content
    that drifts the moment the owner renames, and the index contract says the
    display label is resolved through the owner or not at all. What is left --
    the owner object type and the exact identifier -- is the reference itself,
    and it is enough to recognise a record without borrowing anything.
    """
    owner_type = str(record["owner_object_type"]).replace("-", " ")
    version_id = record.get("owner_version_id")
    if version_id:
        return f"{owner_type} {version_id}"
    return f"{owner_type} {record['owner_object_id']}"


_EVIDENCE_OBJECT_TYPE = {
    "evidence_trace": "evidence-trace",
    "object_version": "object-version",
    "audit_event": "audit-event",
}


def _evidence_item(
    record: Mapping[str, Any],
    *,
    project_id: str,
    correlations: list[dict[str, str]],
    availability: dict[str, Any] | None,
    lens_summary: dict[str, Any],
) -> dict[str, Any]:
    """One indexed Evidence Record, in the shared Governance object envelope.

    `used_by`, `versions` and `evidence` stay three different questions. For an
    Evidence Record the answers are structural, not borrowed: it has no version
    ledger of its own (it IS a reference to one exact version), and its
    "evidence" facet is the reference itself.
    """
    from core.evidence_index import owner_route  # noqa: PLC0415 -- one route authority

    record_id = str(record["id"])
    record_kind = str(record["record_kind"])
    object_type = _EVIDENCE_OBJECT_TYPE[record_kind]
    state = (availability or {}).get("availability", "available")
    route = owner_route(
        str(record["owner_object_type"]),
        str(record["owner_object_id"]),
        record.get("owner_version_id"),
    )

    owner: dict[str, Any] = {
        "kind": str(record["producer"]),
        "workspace": str(record["owner_workspace"]),
        "object_type": str(record["owner_object_type"]),
        "object_id": str(record["owner_object_id"]),
        "project_id": project_id,
    }
    if route is not None:
        owner["owner_href"] = route

    return _object_envelope(
        section="evidence",
        object_type=object_type,
        object_id=record_id,
        label=_evidence_label(record),
        scope="project",
        owner=owner,
        lifecycle_status=state,
        active_version_ref=(
            {
                "object_type": str(record["owner_object_type"]),
                "id": str(record["owner_version_id"]),
                "state": "indexed",
                "evidence_hash": record.get("integrity_hash"),
            }
            if record.get("owner_version_id")
            else None
        ),
        used_by=_facet("empty"),
        # An Evidence Record has no version history: it references ONE exact
        # immutable version, which is why the route registry gives it no nested
        # `/versions/{id}` address either.
        versions=_facet("unavailable"),
        evidence=_facet(
            "available",
            [
                {
                    "evidence_id": record_id,
                    "kind": record_kind,
                    "state": state,
                    "recorded_at": _iso(record.get("occurred_at")),
                    "integrity_ref": record.get("integrity_hash"),
                    "correlation_ref": next(
                        (item["id"] for item in correlations if item["kind"] == "w3c_trace"),
                        None,
                    ),
                    **({"owner_href": route} if route is not None else {}),
                }
            ],
        ),
        summary={
            "record_kind": record_kind,
            "producer": str(record["producer"]),
            "owner_workspace": str(record["owner_workspace"]),
            "owner_object_type": str(record["owner_object_type"]),
            "owner_object_id": str(record["owner_object_id"]),
            "owner_version_id": record.get("owner_version_id"),
            "is_anchor": bool(record["is_anchor"]),
            "occurred_at": _iso(record.get("occurred_at")),
            "observed_at": _iso(record.get("observed_at")),
            "indexed_at": _iso(record.get("indexed_at")),
            "integrity_hash": record.get("integrity_hash"),
            "availability": state,
            "availability_reason_code": (availability or {}).get("reason_code"),
            "correlations": correlations,
            **lens_summary,
        },
        evidence_as_of=_iso(record.get("occurred_at")),
    )


_TRACE_SHAPE = """
    SELECT r.id AS anchor_id,
           COUNT(*) FILTER (WHERE l.to_record_id IS NOT NULL) AS linked_records,
           COUNT(*) FILTER (WHERE l.to_record_id IS NULL) AS owner_references,
           MIN(n.occurred_at) AS horizon_start,
           MAX(n.occurred_at) AS horizon_end
      FROM app.evidence_records r
      LEFT JOIN app.evidence_links l
             ON l.project_id = r.project_id
            AND (l.from_record_id = r.id OR l.to_record_id = r.id)
      LEFT JOIN app.evidence_records n
             ON n.project_id = r.project_id
            AND n.id IN (l.from_record_id, l.to_record_id)
     WHERE r.project_id = %(project_id)s AND r.id = ANY(%(ids)s)
     GROUP BY r.id
"""


def _trace_summaries(conn: Any, project_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    """The shape of each listed trace, in ONE bounded query for the whole page.

    Not one traversal per row: a per-row graph walk on a list is the O(n) fan-out
    the collection contract forbids, and it would make the page cost grow with
    the size of the traces rather than the size of the page.
    """
    if not ids:
        return {}
    summaries: dict[str, dict[str, Any]] = {}
    for row in _fetch(conn, _TRACE_SHAPE, {"project_id": project_id, "ids": ids}):
        linked = int(row.get("linked_records") or 0)
        references = int(row.get("owner_references") or 0)
        summaries[str(row["anchor_id"])] = {
            "linked_record_count": linked,
            "owner_reference_count": references,
            "horizon_start": _iso(row.get("horizon_start")),
            "horizon_end": _iso(row.get("horizon_end")),
            # A one-record chain is not a proven end-to-end trace. Saying
            # `partial` here is what stops a single node from reading as one.
            "completeness": "partial" if linked == 0 else "linked",
        }
    return summaries


_VERSION_SHAPE = """
    SELECT r.id AS record_id,
           BOOL_OR(l.relation = 'approved_by') AS approved,
           BOOL_OR(l.relation = 'supersedes') AS has_predecessor
      FROM app.evidence_records r
      LEFT JOIN app.evidence_links l
             ON l.project_id = r.project_id AND l.from_record_id = r.id
     WHERE r.project_id = %(project_id)s AND r.id = ANY(%(ids)s)
     GROUP BY r.id
"""


def _version_summaries(conn: Any, project_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    return {
        str(row["record_id"]): {
            "approval_state": "approved" if row.get("approved") else "unapproved",
            "diff_available": bool(row.get("has_predecessor")),
        }
        for row in _fetch(conn, _VERSION_SHAPE, {"project_id": project_id, "ids": ids})
    }


# `provider_account`, `connection_ref` and raw `metadata` are NOT selected here.
# Not filtered downstream -- not selected, so widening this projection later
# cannot reintroduce them by accident.
_AUDIT_SHAPE = """
    SELECT a.id, a.identity, a.action, a.outcome, a.resource_path,
           a.trace_id, a.operation_id, a.created_at
      FROM app.audit_log a
     WHERE a.id = ANY(%(ids)s)
"""


def _audit_summaries(conn: Any, owner_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not owner_ids:
        return {}
    from core.evidence_index import is_w3c_trace_id  # noqa: PLC0415

    rows = _fetch(conn, _AUDIT_SHAPE, {"ids": owner_ids})
    summaries: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = row.get("resource_path")
        summaries[str(row["id"])] = {
            "actor": row.get("identity"),
            "action": row.get("action"),
            "outcome": row.get("outcome"),
            "governed_target": (list(path)[0] if isinstance(path, list) and path else None),
            "trace_id": row["trace_id"] if is_w3c_trace_id(row.get("trace_id")) else None,
            "operation_id": row.get("operation_id"),
        }
    return summaries


def _decorate_evidence_items(
    conn: Any, project_id: str, lens: str, records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Resolve the per-lens columns for ONE page, through the owner, server-side."""
    from core.evidence_index import load_availability, load_correlations  # noqa: PLC0415

    ids = [str(record["id"]) for record in records]
    correlations = load_correlations(conn, project_id=project_id, record_ids=ids)
    availability = load_availability(conn, project_id=project_id, record_ids=ids)

    lens_summaries: dict[str, dict[str, Any]] = {}
    if lens == "lineage-provenance":
        lens_summaries = _trace_summaries(conn, project_id, ids)
    elif lens == "versions-approvals":
        lens_summaries = _version_summaries(conn, project_id, ids)
    else:
        owner_ids = [str(record["owner_object_id"]) for record in records]
        by_owner = _audit_summaries(conn, owner_ids)
        lens_summaries = {
            str(record["id"]): by_owner.get(str(record["owner_object_id"]), {})
            for record in records
        }

    return [
        _evidence_item(
            record,
            project_id=project_id,
            correlations=correlations.get(str(record["id"]), []),
            availability=availability.get(str(record["id"])),
            lens_summary=lens_summaries.get(str(record["id"]), {}),
        )
        for record in records
    ]


# ---------------------------------------------------------------------------
# Lens adapters
# ---------------------------------------------------------------------------


def _bounded(items: list[dict[str, Any]], limit: int | None) -> LensResult:
    total = len(items)
    kept = items if limit is None else items[:limit]
    return LensResult(
        state="available" if kept else "empty",
        items=tuple(kept),
        total=total,
        reason=None,
    )


def _business_domains_lens(
    conn: Any, project_id: str, org_id: str, limit: int | None
) -> LensResult:
    rows = _fetch(conn, _BUSINESS_DOMAINS, {"project_id": project_id, "org_id": org_id})
    return _bounded([_business_domain(row, org_id=org_id) for row in rows], limit)


def _classifications_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    rows = _fetch(conn, _CLASSIFICATIONS, {"project_id": project_id, "org_id": org_id})
    return _bounded([_classification(row, org_id=org_id) for row in rows], limit)


def _concept_coverage(conn: Any, project_id: str) -> dict[str, dict[str, Any]]:
    """Coverage per Concept, computed ONCE from Data's active mapping versions.

    An unreadable Data owner yields `unavailable` for every Concept with `None`
    counts. It never yields zero: "we could not look" and "nothing is bound" are
    different answers and only one of them is a measurement.
    """
    from core.semantic_coverage import (  # noqa: PLC0415 -- shared projection
        CoverageUnavailable,
        compose_mapping_coverage,
    )
    from core.semantic_model import concept_names  # noqa: PLC0415

    try:
        names = concept_names(conn, project_id)
        report = compose_mapping_coverage(
            conn, project_id, concept_names=names, limit=MAX_COLLECTION_ITEMS
        )
    except CoverageUnavailable:
        return {}
    except Exception:  # noqa: BLE001 -- adapter failure, not a zero
        return {}
    per_concept: dict[str, dict[str, Any]] = {}
    for row in report.rows:
        if not row.concept_id:
            continue
        entry = per_concept.setdefault(
            row.concept_id, {"bound": 0, "eligible": 0, "state": "unbound"}
        )
        entry["eligible"] += 1
        if row.state in {"active", "confirmed"}:
            entry["bound"] += 1
            entry["state"] = "bound"
    return per_concept


def _concepts_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    from core.semantic_model import (  # noqa: PLC0415 -- one semantic authority
        load_concept_versions,
        load_concepts,
    )

    rows = load_concepts(conn, project_id)
    coverage = _concept_coverage(conn, project_id)
    operand_names = _concept_name_by_version_id(rows)
    items = [
        _semantic_concept(
            row,
            project_id=project_id,
            versions=load_concept_versions(conn, project_id, str(row["id"])),
            coverage=coverage.get(str(row["id"])),
            operand_names=operand_names,
        )
        for row in rows
    ]
    return _bounded(items, limit)


#: Who consumes a Semantic View. `README.md:116` names them: Query Specs,
#: Reports and Test. These are the three tables that actually carry the
#: reference — `app.query_specs` (migration 151), `app.golden_question_versions`
#: and `app.observed_cohorts` (migration 153).
#:
#: Counted DISTINCT on the consumer's own identity, not on the row: a Golden
#: Question with nine versions pinned to this view is ONE consumer, and counting
#: versions would report a dependency load that does not exist.
_SEMANTIC_VIEW_CONSUMERS = """
    WITH consumers AS (
        SELECT semantic_view_id, id AS consumer_id
          FROM app.query_specs WHERE project_id = %(project_id)s
        UNION ALL
        SELECT semantic_view_id, golden_question_id
          FROM app.golden_question_versions WHERE project_id = %(project_id)s
        UNION ALL
        SELECT semantic_view_id, id
          FROM app.observed_cohorts
         WHERE project_id = %(project_id)s AND semantic_view_id IS NOT NULL
    )
    SELECT semantic_view_id, COUNT(DISTINCT consumer_id) AS used_by_count
      FROM consumers
     GROUP BY semantic_view_id
"""


def _semantic_view_used_by(conn: Any, project_id: str) -> dict[str, int] | None:
    """Consumers per Semantic View, or None when the question cannot be asked.

    The distinction is the whole point. `used_by` was hardcoded to
    `_facet("unavailable")` for this one type while eight other governed objects
    in this file computed it, so the workbench said "No owner answers Used by —
    this is not a count of zero" about a question that has an owner and an
    answer. Declaring something unknowable when it is merely unasked is the same
    class of error as `README.md` invariant 8 forbids in the other direction:
    unknown must never read as healthy, and knowable must never read as unknown.

    None is returned only when the query itself fails — then `unavailable` is the
    true answer and the screen keeps saying so.
    """
    try:
        rows = _fetch(conn, _SEMANTIC_VIEW_CONSUMERS, {"project_id": project_id})
    except Exception as exc:  # noqa: BLE001 -- an unreadable fan-out is `unavailable`
        # The refusal is not swallowed: returning None makes the facet say
        # `unavailable` on screen, which is exactly what a failed read means and
        # is louder to the person looking at it than a server log. It is logged
        # too -- this comment used to say the module had no logger, which stopped
        # being true on 2026-08-16 when ruff F821 found one used and undefined.
        logger.warning(
            "governance_read_model: semantic view used-by unreadable project=%s: %s",
            project_id,
            exc,
        )
        return None
    return {str(row["semantic_view_id"]): int(row["used_by_count"] or 0) for row in rows}


_CAPABILITY_CONSUMERS = """
    SELECT capability_key, COUNT(DISTINCT datastream_id) AS used_by_count
      FROM app.datastream_capability_proposals
     WHERE project_id = %(project_id)s
     GROUP BY capability_key
"""


def _capability_used_by(conn: Any, project_id: str) -> dict[str, int] | None:
    """Datastreams reached per capability, or None when the question cannot be asked.

    The LAST instance of the class `_semantic_view_used_by` closed above. Its
    docstring names the defect exactly -- `used_by` hardcoded to
    `_facet("unavailable")` while eight other governed objects in this file
    computed it -- and the Epic 48 capability objects were the type it did not
    reach. They said "No owner answers Used by" about a question that has both an
    owner and an answer: `app.datastream_capability_proposals` records which
    Datastream a capability was compiled into, with its applicability, and it is
    project-scoped.

    A count of zero is `empty` and means the store answered "no Datastream
    carries this capability yet". `None` is `unavailable` and means the read
    failed -- the two must never arrive on screen as the same sentence, because
    the first is a fact about the Project and the second about us.
    """
    try:
        rows = _fetch(conn, _CAPABILITY_CONSUMERS, {"project_id": project_id})
    except Exception as exc:  # noqa: BLE001 -- an unreadable fan-out is `unavailable`
        logger.warning(
            "governance_read_model: capability used-by unreadable project=%s: %s",
            project_id,
            exc,
        )
        return None
    return {str(row["capability_key"]): int(row["used_by_count"] or 0) for row in rows}


def _semantic_views_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    from core.semantic_model import load_view_versions, load_views  # noqa: PLC0415

    rows = load_views(conn, project_id)
    used_by = _semantic_view_used_by(conn, project_id)
    items = [
        _semantic_view(
            row,
            project_id=project_id,
            versions=load_view_versions(conn, project_id, str(row["id"])),
            used_by_count=None if used_by is None else used_by.get(str(row["id"]), 0),
        )
        for row in rows
    ]
    return _bounded(items, limit)


def _mapping_coverage_lens(
    conn: Any, project_id: str, org_id: str, limit: int | None
) -> LensResult:
    """Concepts ordered so the gaps come first, each carrying the real coverage
    of Data's ACTIVE mapping versions. It is a projection, never a second store:
    nothing here can be edited, and no proposal counts toward it."""
    from core.semantic_coverage import (  # noqa: PLC0415
        CoverageUnavailable,
        compose_mapping_coverage,
    )
    from core.semantic_model import (  # noqa: PLC0415
        concept_names,
        load_concept_versions,
        load_concepts,
    )

    try:
        compose_mapping_coverage(conn, project_id, concept_names=concept_names(conn, project_id))
    except CoverageUnavailable as exc:
        # Unavailable, never an empty list: an empty success here would read as
        # "this Project has no bindings", which is not what was observed.
        return LensResult(
            state="unavailable",
            reason=_unavailable(
                "mapping_owner_unreadable",
                "The Data mapping owner could not be read, so coverage is unknown "
                f"for this Project. This is not a coverage of zero. ({type(exc).__name__})",
                _data_owner_href("datastreams", "datastream", "", "mapping"),
            ),
        )
    rows = load_concepts(conn, project_id)
    coverage = _concept_coverage(conn, project_id)
    operand_names = _concept_name_by_version_id(rows)
    items = [
        _semantic_concept(
            row,
            project_id=project_id,
            versions=load_concept_versions(conn, project_id, str(row["id"])),
            coverage=coverage.get(str(row["id"])),
            operand_names=operand_names,
        )
        for row in rows
    ]
    items.sort(
        key=lambda item: (
            item["summary"]["bound_datastream_count"] is not None,
            item["summary"]["bound_datastream_count"] or 0,
            item["object_ref"]["label"],
        )
    )
    return _bounded(items, limit)


def _rule_version_rows(
    conn: Any, kind: str, object_ids: list[str]
) -> dict[str, list[Mapping[str, Any]]] | None:
    """Every recorded version of a set of rules, in ONE query. `None` on failure.

    One query and not one per row, because a lens is a list: the per-object read
    the Concepts lens performs is affordable for a handful of Concepts and is not
    for a library. `None` means the ledger could not be read at all, and the
    caller renders `unavailable` -- never an empty history, which would say the
    rule has never been edited.
    """
    from core.rule_versions import ledger_for  # noqa: PLC0415 -- one ledger authority

    ledger = ledger_for(kind)
    if not object_ids:
        return {}
    try:
        rows = _fetch(
            conn,
            f"SELECT id, {ledger.object_column} AS object_id, version_number, status, "  # noqa: S608 -- identifiers come from LEDGERS, never from a caller
            f"content_hash, created_at, created_by "
            f"FROM app.{ledger.table} WHERE {ledger.object_column} = ANY(%(object_ids)s) "
            f"ORDER BY version_number DESC",
            {"object_ids": object_ids},
        )
    except Exception:  # noqa: BLE001 -- an unreadable ledger is not an empty one
        # No logger here, deliberately: this module is a pure composer and every
        # other failure path in it answers through the envelope. `None` IS the
        # answer, and the facet renders "the version history could not be read".
        return None
    grouped: dict[str, list[Mapping[str, Any]]] = {object_id: [] for object_id in object_ids}
    for row in rows:
        grouped.setdefault(str(row["object_id"]), []).append(row)
    return grouped


def _rule_version_refs(
    rows: list[Mapping[str, Any]], object_type: str, object_id: str, current_id: str | None
) -> list[dict[str, Any]]:
    """The history of one rule, newest first, with the CURRENT one named.

    `state` is read from the pointer and not from the position, because those two
    are not the same fact: a rule edited back to a body it already carried points
    at an OLDER version, and calling the highest number active would name a body
    the rule does not have.
    """
    return [
        {
            "object_type": f"{object_type}-version",
            "id": str(row["id"]),
            "version": int(row["version_number"]),
            "state": "active" if str(row["id"]) == (current_id or "") else "previous",
            "recorded_at": _iso(row.get("created_at")),
            "recorded_by": row.get("created_by"),
            "evidence_hash": row.get("content_hash"),
            "owner_href": _owner_href(
                "semantic-model",
                object_type=object_type,
                object_id=object_id,
                tab="versions",
                version_id=str(row["id"]),
            ),
        }
        for row in rows
    ]


def _versions_facet(
    rows: list[Mapping[str, Any]] | None,
    object_type: str,
    object_id: str,
    current_id: str | None,
) -> dict[str, Any]:
    """UNAVAILABLE, EMPTY and AVAILABLE are three different answers here.

    `None` is "the ledger could not be read"; `[]` is "no version has been
    recorded for this rule yet". The screen says two different sentences and this
    is where the difference is decided.
    """
    if rows is None:
        return _facet("unavailable")
    if not rows:
        return _facet("empty", count=0)
    return _facet(
        "available",
        _rule_version_refs(rows, object_type, object_id, current_id),
        count=len(rows),
    )


def _value_mapping_table(
    row: Mapping[str, Any],
    *,
    org_id: str,
    versions: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """One client-authored value mapping table (Story 60.1).

    `used_by` is the number of Datastreams the table is APPLIED to, counted by
    the same query that produced the row. `translates` is DERIVED from the
    assignments (`<source_field> -> <table name>`) rather than typed a second
    time: a client who renames the column would otherwise leave a caption behind
    that no longer names anything.

    A COUNT THAT CAME BACK NULL IS `unavailable`, NOT `empty`. The projection
    used to write `int(datastream_count or 0)`, which turned "the assignment
    count could not be read" into "nothing depends on this" -- the exact
    substitution the store below it refuses (`value_mapping_tables.py`,
    `assess_table_impact` raises rather than returning an empty tuple). A
    projection that reopens the hole its own store closed is worse than no
    projection, because it is the one a screen reads.
    """
    object_id = str(row["id"])
    datastream_count = row.get("datastream_count")
    field = row.get("sample_source_field")
    return _object_envelope(
        section="semantic-model",
        object_type="value-mapping-table",
        object_id=object_id,
        label=str(row.get("name") or object_id),
        scope="project" if row.get("project_id") else "organization",
        owner={
            "kind": "value-mapping-table",
            "organization_id": org_id,
            "project_id": row.get("project_id"),
        },
        # There is no lifecycle here and none is invented: an entry a client typed
        # is live at write time (migration 235 header), so the only honest status
        # is that the table exists.
        lifecycle_status="active",
        active_version_ref=(
            {
                "object_type": "value-mapping-table-version",
                "id": str(row["current_version_id"]),
                "version": next(
                    (
                        int(entry["version_number"])
                        for entry in (versions or ())
                        if str(entry["id"]) == str(row["current_version_id"])
                    ),
                    1,
                ),
                "state": "active",
            }
            if row.get("current_version_id")
            else None
        ),
        used_by=_counted_facet(datastream_count),
        # Story 60.5, migration 242. `unavailable` is now reserved for a ledger
        # that could not be READ; a table nobody has edited yet answers `empty`,
        # and the screen says so in its own sentence.
        versions=_versions_facet(
            versions, "value-mapping-table", object_id, row.get("current_version_id")
        ),
        evidence=_facet("unavailable"),
        summary={
            "scope_level": row.get("scope_level"),
            "description": row.get("description"),
            "entry_count": int(row["entry_count"]) if row.get("entry_count") is not None else None,
            "assignment_count": (
                int(row["assignment_count"]) if row.get("assignment_count") is not None else None
            ),
            "datastream_count": int(datastream_count) if datastream_count is not None else None,
            # Stated, so a reader never has to infer "did the count happen" from
            # the number beside it. The same word the API answers with.
            "impact_state": "known" if datastream_count is not None else "unknown",
            "translates": f"{field} -> {row.get('name')}" if field else None,
        },
        evidence_as_of=_iso(row.get("updated_at")),
    )


def _value_tables_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    """The client's own value mapping tables, with their real assignment count.

    UNAVAILABLE, never an empty success, when the store cannot be read: an empty
    list here would say "this Project has no value table", which is not what was
    observed. The same distinction the assignment count carries -- "impact
    unknown" is never rendered as a zero.
    """
    from core.value_mapping_tables import LIST_TABLES_SQL  # noqa: PLC0415 -- one store

    try:
        rows = _fetch(conn, LIST_TABLES_SQL, {"project_id": project_id, "org_id": org_id})
    except Exception as exc:  # noqa: BLE001 -- an unreadable store is not a zero
        return LensResult(
            state="unavailable",
            reason=_unavailable(
                "value_tables_store_unreadable",
                "The transformations library could not be read, so the value "
                "mapping tables of this Project are unknown. This is not a count "
                f"of zero. ({type(exc).__name__})",
                _owner_href("semantic-model"),
            ),
        )
    from core.rule_versions import KIND_VALUE_TABLE  # noqa: PLC0415 -- one ledger authority

    history = _rule_version_rows(conn, KIND_VALUE_TABLE, [str(row["id"]) for row in rows])
    return _bounded(
        [
            _value_mapping_table(
                row,
                org_id=org_id,
                versions=None if history is None else history.get(str(row["id"]), []),
            )
            for row in rows
        ],
        limit,
    )


def _cleanup_rule(
    row: Mapping[str, Any],
    *,
    org_id: str,
    versions: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """One cleanup rule (Story 60.3).

    `used_by` is how many Datastreams the rule REACHES, counted by the same query
    that produced the row -- one when it is bound to a Datastream, every
    Datastream of the Project when it is not. A count that came back NULL is
    `unavailable`, never `empty`: "the reach could not be read" and "it reaches
    nothing" are different facts, and the store below refuses to confuse them
    (`cleanup_rules.assess_rule_impact` raises rather than answering zero).

    THE EFFECT IS NOT ON THIS ENVELOPE, AND THAT IS THE POINT. How many rows a
    rule removes is a WAREHOUSE question, answered by
    `GET .../cleanup-rules/{id}/effect` with `effect_state` and a sentence when it
    could not be counted. Carrying a number here would mean either fabricating it
    or querying the warehouse once per row of a Governance list -- and a `0` in
    that column would tell a reader the rule removes nothing.
    """
    object_id = str(row["id"])
    datastream_count = row.get("datastream_count")
    return _object_envelope(
        section="semantic-model",
        object_type="cleanup-rule",
        object_id=object_id,
        label=str(row.get("name") or object_id),
        scope="project",
        owner={
            "kind": "cleanup-rule",
            "organization_id": org_id,
            "project_id": row.get("project_id"),
        },
        # `enabled` is the only lifecycle a cleanup rule has, and it is a real
        # column rather than an invented status.
        lifecycle_status="active" if row.get("enabled") else "disabled",
        active_version_ref=(
            {
                "object_type": "cleanup-rule-version",
                "id": str(row["current_version_id"]),
                "version": next(
                    (
                        int(entry["version_number"])
                        for entry in (versions or ())
                        if str(entry["id"]) == str(row["current_version_id"])
                    ),
                    1,
                ),
                "state": "active",
            }
            if row.get("current_version_id")
            else None
        ),
        used_by=_counted_facet(datastream_count),
        # Story 60.5, migration 242. A rule nobody has edited yet answers `empty`;
        # `unavailable` now means the ledger itself could not be read.
        versions=_versions_facet(
            versions, "cleanup-rule", object_id, row.get("current_version_id")
        ),
        evidence=_facet("unavailable"),
        summary={
            "source_field": row.get("source_field"),
            "rule_kind": row.get("rule_kind"),
            # The condition in words, so a reader who does not read regular
            # expressions still knows what survives.
            "condition": row.get("condition"),
            "enabled": bool(row.get("enabled")),
            "reach": "datastream" if row.get("datastream_id") else "project",
            "datastream_count": int(datastream_count) if datastream_count is not None else None,
            "impact_state": "known" if datastream_count is not None else "unknown",
            # What an engine said about this pattern before it was stored:
            # `passed`, or `not_attempted` when none could be reached.
            "dry_run_state": row.get("dry_run_state"),
        },
        evidence_as_of=_iso(row.get("updated_at")),
    )


def _cleanup_rules_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    """The Project's cleanup rules, with the real number of Datastreams they reach.

    UNAVAILABLE, never an empty success, when the store cannot be read: an empty
    list would say "this Project has no cleanup rule", which is not what was
    observed -- the rule every lens in this module is written to: `unavailable`
    is what a store that could not be read answers, and `empty` is a measurement.
    """
    from core.cleanup_rules import LIST_RULES_SQL, describe  # noqa: PLC0415 -- one store

    try:
        rows = _fetch(conn, LIST_RULES_SQL, {"project_id": project_id})
    except Exception as exc:  # noqa: BLE001 -- an unreadable store is not a zero
        return LensResult(
            state="unavailable",
            reason=_unavailable(
                "cleanup_rules_store_unreadable",
                "The transformation library could not be read, so the cleanup "
                "rules of this Project are unknown. This is not a count of zero. "
                f"({type(exc).__name__})",
                _owner_href("semantic-model"),
            ),
        )
    from core.rule_versions import KIND_CLEANUP_RULE  # noqa: PLC0415 -- one ledger authority

    history = _rule_version_rows(conn, KIND_CLEANUP_RULE, [str(row["id"]) for row in rows])
    items = []
    for row in rows:
        enriched = dict(row)
        # The sentence is composed by the STORE, from the same three columns, so
        # the Governance list and the editing surface cannot disagree about what
        # a rule does.
        enriched["condition"] = describe(
            row["rule_kind"], row["source_field"], row["pattern"]
        )
        items.append(
            _cleanup_rule(
                enriched,
                org_id=org_id,
                versions=None if history is None else history.get(str(row["id"]), []),
            )
        )
    return _bounded(items, limit)


def _canonical_field(row: Mapping[str, Any], *, org_id: str) -> dict[str, Any]:
    """One canonical field of the MDM vocabulary (lot A1, issue #68).

    THE SCOPE IS THE OBJECT'S MOST IMPORTANT PROPERTY HERE, and it is derived
    from `project_id IS NULL` rather than stored: `platform` is the governed
    vocabulary every Project of the instance aligns on, `project` is what this
    client declared for their own objects. `declare_project_field` refuses to
    mint a platform row from a project door, so the screen must never offer to
    edit one either -- and `allowed_actions` stays empty on both scopes because
    nothing in this lens writes yet.

    THREE FACETS, ALL `unavailable`, AND NONE OF THEM A ZERO. There is no version
    ledger for this table (it is "mutable-with-audit", migration 032 header), no
    per-field used-by count -- the six modules that validate a binding against a
    field id record nothing about which ones they used -- and no evidence
    adapter. `unavailable` says "no owner answers this"; `empty` would say "the
    answer is none", which was never observed.
    """
    object_id = str(row["id"])
    return _object_envelope(
        section="semantic-model",
        object_type="canonical-field",
        object_id=object_id,
        label=str(row.get("canonical_name") or object_id),
        scope=str(row["scope"]),
        owner={
            "kind": "canonical-field-registry",
            "organization_id": org_id,
            "project_id": row.get("project_id"),
        },
        # `status` is a real column with exactly two values, and the read filters
        # to `active`, so nothing here is an invented lifecycle.
        lifecycle_status=str(row.get("status") or "active"),
        active_version_ref=None,
        used_by=_facet("unavailable"),
        versions=_facet("unavailable"),
        evidence=_facet("unavailable"),
        summary={
            # The STABLE identifier, carried beside the label rather than
            # inferred from it. `lineage` asks `dimension_lineage.get_fed_by`
            # for a canonical DIMENSION, and a panel that passed the display
            # label would be keying a reading on a string the label is free to
            # stop being.
            "canonical_name": row.get("canonical_name"),
            "concept_kind": row.get("concept_kind"),
            "value_type": row.get("value_type"),
            "aggregation": row.get("aggregation"),
            "non_additive": bool(row.get("non_additive")),
            "unit": row.get("unit"),
            # Which object this field qualifies -- the duration OF THE VIDEO, not
            # a free-floating duration (migration 241). NULL on every platform
            # row by CHECK constraint, and that is the vocabulary being about the
            # fact rather than about an object.
            "object_kind": row.get("object_kind"),
            "description": row.get("description"),
            "dictionary_field_name": row.get("dictionary_field_name"),
        },
        evidence_as_of=None,
    )


def _canonical_fields_lens(
    conn: Any, project_id: str, org_id: str, limit: int | None
) -> LensResult:
    """The MDM canonical vocabulary, BOTH scopes, from the one reader that owns it.

    `canonical_field_registry.list_visible_canonical_fields` is also what
    `GET /api/projects/{id}/mdm/canonical-fields` reads, on purpose: a field
    listed on one surface and hidden on the other would make part of the
    vocabulary invisible depending on which door a person walked through.

    UNAVAILABLE, never an empty success, when the table cannot be read. Zero rows
    is what every Project has today, so the difference between "nobody has
    declared a field" and "I could not read the vocabulary" is the whole meaning
    of this screen: only the first one invites a person to start declaring.
    """
    from core.canonical_field_registry import (  # noqa: PLC0415 -- one reader
        list_visible_canonical_fields,
    )

    try:
        rows = list_visible_canonical_fields(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable vocabulary is not a zero
        return LensResult(
            state="unavailable",
            reason=_unavailable(
                "canonical_fields_unreadable",
                "The canonical vocabulary could not be read, so which fields this "
                "Project can bind to is unknown. This is not a count of zero. "
                f"({type(exc).__name__})",
                _owner_href("semantic-model"),
            ),
        )
    return _bounded([_canonical_field(row, org_id=org_id) for row in rows], limit)


def _metric_definition(row: dict[str, Any], *, org_id: str, shadowed: bool) -> dict[str, Any]:
    """One curated metric definition, said as what it is and not as a Concept.

    IT IS NOT A CONCEPT AND THE ENVELOPE MUST NOT SUGGEST OTHERWISE. A Concept is
    versioned, published through a change set and carries an expression. This row
    is mutable-with-audit, has no version ledger and no formula tree -- only a
    declared aggregation and additivity. Every version-shaped facet is therefore
    `unavailable`, which says "no owner answers this", never `empty`, which would
    say "the answer is none".
    """
    object_id = str(row["id"])
    return _object_envelope(
        section="semantic-model",
        object_type="metric-definition",
        object_id=object_id,
        label=str(row.get("display_name") or row.get("canonical_name") or object_id),
        scope=str(row.get("scope_level") or "PLATFORM").lower(),
        owner={
            "kind": "metric-definition-store",
            "organization_id": org_id,
            "project_id": row.get("project_id"),
        },
        lifecycle_status="active",
        active_version_ref=None,
        used_by=_facet("unavailable"),
        versions=_facet("unavailable"),
        evidence=_facet("unavailable"),
        summary={
            "canonical_name": row.get("canonical_name"),
            "aggregation_type": row.get("aggregation_type"),
            "additive": bool(row.get("additive")),
            "non_additive_dimensions": list(row.get("non_additive_dimensions") or ()),
            "ratio_numerator": row.get("ratio_numerator"),
            "ratio_denominator": row.get("ratio_denominator"),
            "unit": row.get("unit"),
            "certified": bool(row.get("certified")),
            # THE ONE FIGURE THIS LENS EXISTS FOR. `resolve_declared_additivity`
            # gives the Semantic Model precedence, so a definition whose metric a
            # published Concept already carries governs NOTHING -- it is read past.
            # Listing it without saying so would be the same half-truth as hiding
            # it: a person would think it still decides how the metric aggregates.
            "governs": not shadowed,
            "shadowed_by_concept": shadowed,
        },
        evidence_as_of=None,
    )


def _metric_definitions_lens(
    conn: Any, project_id: str, org_id: str, limit: int | None
) -> LensResult:
    """The lower declaring store, VISIBLE at last (2026-08-16).

    WHAT WAS WRONG. `metric_definition_upsert` was an MCP curation tool that wrote
    `app.metric_definitions` (retired 2026-08-25, story 49.3 AC1 -- along with the
    two REST doors; the seed import is the one writer left), and
    `metric_semantics.resolve_declared_additivity` reads that store on every
    render -- it decides whether a metric may be summed across two days. Nothing
    on the Governance surface listed it. A person reading Governance believed they
    saw everything that governs a metric's aggregation, and a definition curated
    through MCP governed a roll-up from outside their view. `governance.md` named
    this as remaining work; this is the half of it that does not require a
    projection.

    AND THE LENS OUTLIVES ITS WRITERS, deliberately. A store nobody may author any
    more still DECIDES, through the cascade below, for every metric no published
    Concept carries. Retiring the view with the doors would put a person back
    exactly where this lens found them.

    IT IS ITS OWN LENS, for the reason every other one here is: a lens carries ONE
    object type, and a metric definition is not a Concept. Merging the two into
    `concepts` would put a mutable row with no version and no formula beside
    published, expression-carrying definitions -- which is exactly the confusion
    the glossary arbitration of 2026-08-14 spent a day separating.

    THE CASCADE IS THE READER'S, NOT A SECOND ONE. `resolve_metric_definitions`
    applies PROJECT > ORG > PLATFORM and is what every render already goes
    through, so this lens shows what is IN FORCE rather than every stored row --
    listing a PLATFORM row a PROJECT row overrides would show a definition that
    decides nothing.

    UNAVAILABLE, never an empty success: "nobody curated a definition" and "I could
    not read the store" are opposite facts, and only the first is safe to act on.
    """
    from core.metric_semantics import (  # noqa: PLC0415 -- one reader, theirs
        _load_declared_additivity_rows,
        resolve_metric_definitions,
    )

    try:
        resolved = resolve_metric_definitions(project_id, conn=conn)
    except Exception as exc:  # noqa: BLE001 -- an unreadable store is not a zero
        return LensResult(
            state="unavailable",
            reason=_unavailable(
                "metric_definitions_unreadable",
                "The curated metric definitions could not be read, so what governs "
                "a metric this Project's Semantic Model does not carry is unknown. "
                f"This is not a count of zero. ({type(exc).__name__})",
                _owner_href("semantic-model"),
            ),
        )

    # Which metrics a published Concept already answers for. Read through the SAME
    # helper `resolve_declared_additivity` uses, so the shadow this lens draws and
    # the precedence a render applies can never disagree.
    try:
        governed_by_concept = {
            name for name, _ in _load_declared_additivity_rows(project_id, conn=conn)
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("governance_read_model: concept shadow unreadable: %s", exc)
        governed_by_concept = set()

    items = [
        _metric_definition(
            {**row, "canonical_name": row.get("canonical_name") or name},
            org_id=org_id,
            shadowed=name in governed_by_concept,
        )
        for name, row in sorted(resolved.items())
    ]
    return _bounded(items, limit)


def _conflicts_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    """Governed Control Cases (Story 49.4).

    This projected `app.mapping_proposals` AS cases. A proposal is a strong
    CANDIDATE artifact and not a case: it carries no evidence episodes, no impact
    and no decision history, so recurrence and acknowledgement had nowhere to
    live. A proposal is now REFERENCED by a case through
    `app.control_case_candidates`, and stays Data-owned.
    """
    rows = _fetch(conn, _GOVERNED_CONTROL_CASES, {"project_id": project_id})
    return _bounded([_governed_control_case(row, project_id=project_id) for row in rows], limit)


def _reconciliation_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    """The reconciliation family of the governed Rule Sets (Story 49.4).

    This read mutable `app.overlap_groups` across a PROJECT/ORG scope union --
    rows addressing their sources by connector label, resolved at runtime through
    a cascade nobody could see. The lens now shows one Project-owned head per
    family with its published version, so what is listed is what is in force.
    """
    rows = [
        row
        for row in _fetch(conn, _GOVERNED_RULE_SETS, {"project_id": project_id})
        if str(row.get("family")) == "metric_reconciliation"
    ]
    return _bounded([_governed_rule_set(row, project_id=project_id) for row in rows], limit)


def _capability_lens(
    conn: Any, project_id: str, limit: int | None, *, section: str, object_type: str
) -> LensResult:
    rows = _fetch(
        conn, _CAPABILITY_OWNER_OBJECTS, {"project_id": project_id, "object_type": object_type}
    )
    # ONE read for the whole lens, keyed by capability -- not one per object.
    used_by = _capability_used_by(conn, project_id)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        # A disabled or unknown capability fails closed: its objects are not
        # listed, and a direct route to one will not resolve either.
        if str(row.get("capability_state") or "disabled") == "disabled":
            continue
        grouped.setdefault(str(row["object_id"]), []).append(row)
    items = [
        _capability_object(
            entries,
            section=section,
            object_type=object_type,
            project_id=project_id,
            used_by_count=(
                None
                if used_by is None
                else used_by.get(str(entries[0].get("capability_key") or ""), 0)
            ),
        )
        for entries in grouped.values()
    ]
    items.sort(key=lambda item: item["object_ref"]["label"])
    return _bounded(items, limit)


def _registries_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    """Every registry this Project can open: capability-pinned AND client-declared.

    Two sources, and they do not overlap in what they answer -- the same shape
    `_rule_sets_lens` uses one function below, for the same reason. A capability
    registry (Country, Competitors) exists because a capability is enabled; a
    client object kind exists because somebody declared it against a feeding
    Datastream (Story 64.1). Reading only the first is how a governed object could
    be declared over MCP and then be openable by nobody: `_load_object` resolves an
    object by scanning its own lens, so absent from here means absent from Level 3.

    The capability row WINS on a collision: it is the one carrying the pinned
    configuration version, and a client-shaped copy of the same id would drop it.
    """
    capability = _capability_lens(
        conn, project_id, None, section="master-data", object_type="registry"
    )
    known = {item["object_ref"]["id"] for item in (capability.items or [])}
    client = [
        _client_object_registry(row, org_id=org_id)
        for row in _fetch(conn, _CLIENT_OBJECT_REGISTRIES, {"project_id": project_id})
        if str(row["id"]) not in known
    ]
    merged = list(capability.items or []) + client
    merged.sort(key=lambda item: item["object_ref"]["label"])
    if capability.state == "unavailable" and not client:
        return capability
    return _bounded(merged, limit)


def _rule_sets_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    """Every governed Rule Set family, plus the Epic 48 capability owner objects.

    Two sources on purpose, and they do not overlap: `governance_rule_sets` holds
    the heads this Project owns (money policy, FX ingestion, timezone, tax ladder,
    reconciliation, DQ policy), while `_capability_lens` still surfaces capability
    owner objects that are pinned by a Project configuration but live in another
    owner's table. Dropping the second would hide a rule ladder from the lens
    named after rules.
    """
    governed = [
        _governed_rule_set(row, project_id=project_id)
        for row in _fetch(conn, _GOVERNED_RULE_SETS, {"project_id": project_id})
    ]
    capability = _capability_lens(
        conn, project_id, None, section="controls-quality", object_type="rule-set"
    )
    known = {item["object_ref"]["id"] for item in governed}
    merged = governed + [
        item for item in (capability.items or []) if item["object_ref"]["id"] not in known
    ]
    merged.sort(key=lambda item: item["object_ref"]["label"])
    return _bounded(merged, limit)


def _evidence_lens(
    conn: Any,
    project_id: str,
    org_id: str,
    limit: int | None,
    *,
    lens: str,
    query: Mapping[str, Any] | None = None,
) -> LensResult:
    """One Evidence lens, read from the immutable reference index.

    The three lenses share this one adapter because they differ only by record
    kind — and that is exactly the property the contract requires: an event
    cannot be reclassified between lenses to fill missing data, because its kind
    is a column with a CHECK constraint, decided once at registration.
    """
    from core.evidence_index import DEFAULT_PAGE, list_records, normalize_filters  # noqa: PLC0415

    raw = dict(query or {})
    # `cursor` and `limit` position the page; they are not filters, and mixing
    # them into the filter fingerprint would invalidate every cursor the moment
    # the page size changed.
    cursor = str(raw.pop("cursor", "") or "").strip() or None
    requested = str(raw.pop("limit", "") or "").strip()
    filters = normalize_filters(lens, raw)
    page_size = int(requested) if requested.isdigit() else DEFAULT_PAGE
    if limit is not None:
        page_size = min(page_size, limit)
    page = list_records(
        conn,
        project_id=project_id,
        lens=lens,
        filters=filters,
        cursor=cursor,
        limit=page_size,
    )
    items = _decorate_evidence_items(conn, project_id, lens, list(page.rows))
    return LensResult(
        state="available"
        if items
        else ("empty" if page.coverage_state != "unavailable" else "unavailable"),
        items=tuple(items),
        total=page.total,
        reason=None,
        page=page,
    )


def _lineage_lens(
    conn: Any,
    project_id: str,
    org_id: str,
    limit: int | None,
    query: Mapping[str, Any] | None = None,
) -> LensResult:
    return _evidence_lens(conn, project_id, org_id, limit, lens="lineage-provenance", query=query)


def _versions_approvals_lens(
    conn: Any,
    project_id: str,
    org_id: str,
    limit: int | None,
    query: Mapping[str, Any] | None = None,
) -> LensResult:
    return _evidence_lens(conn, project_id, org_id, limit, lens="versions-approvals", query=query)


def _audit_lens(
    conn: Any,
    project_id: str,
    org_id: str,
    limit: int | None,
    query: Mapping[str, Any] | None = None,
) -> LensResult:
    return _evidence_lens(conn, project_id, org_id, limit, lens="audit-activity", query=query)


#: The object kind each of the two Master Data collections is declared under.
#:
#: `object_kind` is an OPAQUE client string everywhere below this module -- the
#: generic owner "does not know what a video, a venue or a product is"
#: (`master_data.py` and `object_kind_registry.py` both say so in their headers)
#: and never compares it to a literal. The two literals therefore live HERE, in
#: the lenses that are named after them, and nowhere lower.
#:
#: THEY ARE MATCHED EXACTLY, AND THAT IS THE POINT. Nothing here looks at a
#: label, a plural, a synonym or a classification type: migration 143:466-476
#: forbids guessing which existing classification is a Product ("never guess"),
#: and a fuzzy match on the kind would be that same guess wearing a WHERE clause.
#: A Project that spells its kind differently declares it, it is not detected.
PRODUCT_OBJECT_KIND = "product"
ACTIVITY_OBJECT_KIND = "activity"


def _client_object_kind_lens(
    conn: Any,
    project_id: str,
    org_id: str,
    limit: int | None,
    *,
    lens: str,
    object_kind: str,
    singular: str,
    plural: str,
) -> LensResult:
    """The instances of one client-declared Master Data collection.

    FOUR ANSWERS, NOT TWO. "This Project never said what a Product is", "it said
    so and nothing feeds it any more", "it is fed and no product has been
    identified yet" and "here they are" are four different facts, and only the
    last one is a list. The three empty ones each name the gesture that fills the
    list -- a Governance screen is read by someone who can act, and "not
    delivered" is not something anyone can act on.

    An unreadable store is `unavailable`, never one of those three: an empty list
    would claim we looked.
    """
    code = lens.replace("-", "_")
    try:
        rows = _fetch(
            conn, _CLIENT_OBJECT_INSTANCES, {"project_id": project_id, "object_kind": object_kind}
        )
    except Exception as exc:  # noqa: BLE001 -- an unreadable store is not a zero
        return LensResult(
            state="unavailable",
            reason=_unavailable(
                f"{code}_store_unreadable",
                f"The Master Data store could not be read, so the {plural} of this "
                f"Project are unknown. This is not a count of zero. "
                f"({type(exc).__name__})",
                _owner_href("master-data"),
            ),
        )

    if not rows:
        return LensResult(
            state="empty",
            total=0,
            reason=_unavailable(
                f"{code}_object_kind_not_declared",
                f"This Project has not said what a {singular} is, so there is none "
                f"to govern. Declare the object kind '{object_kind}' on the "
                f"Datastream whose mapping identifies your {plural}, and every one "
                "that mapping names is governed here.",
                _owner_href("master-data"),
            ),
        )

    instances = [row for row in rows if row.get("node_id")]
    if not instances:
        head = rows[0]
        label = str(head.get("registry_label") or singular)
        live_sources = head.get("live_source_count")
        # A count that did not come back must not become a claim about the
        # source: the second sentence below states only what is known either way.
        if live_sources is not None and int(live_sources) == 0:
            message = (
                f"{label} is declared in this Project and no source feeds it any "
                f"more. Bind the Datastream whose mapping identifies your {plural} "
                "to it, and every one it names is governed here."
            )
        else:
            message = (
                f"{label} is declared in this Project and no {singular} has been "
                "identified yet. Collect the Datastream bound to it: each row its "
                f"mapping identifies becomes a {singular} here, and none is "
                "invented in the meantime."
            )
        return LensResult(
            state="empty",
            total=0,
            reason=_unavailable(
                f"{code}_registry_holds_no_instance",
                message,
                _owner_href(
                    "master-data",
                    object_type="registry",
                    object_id=str(head.get("registry_id") or ""),
                ),
            ),
        )

    return _bounded([_client_object_instance(row, org_id=org_id) for row in instances], limit)


def _products_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    return _client_object_kind_lens(
        conn,
        project_id,
        org_id,
        limit,
        lens="products",
        object_kind=PRODUCT_OBJECT_KIND,
        singular="Product",
        plural="products",
    )


def _activities_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    return _client_object_kind_lens(
        conn,
        project_id,
        org_id,
        limit,
        lens="activities",
        object_kind=ACTIVITY_OBJECT_KIND,
        singular="Activity",
        plural="activities",
    )


def _data_quality_lens(conn: Any, project_id: str, org_id: str, limit: int | None) -> LensResult:
    """Governed DQ Monitors (Story 49.4).

    This answered with a pending-owner reason, and the reason was correct: the computed DQ
    types had no stable identity, so listing them would have meant minting an id
    from a type string. `app.dq_monitors` supplies the identity, and each row
    carries its LAST immutable evaluation with a denominator -- so a monitor that
    never ran reads `unavailable` rather than sharing a blank row with one that
    ran and passed.
    """
    rows = _fetch(conn, _GOVERNED_DQ_MONITORS, {"project_id": project_id})
    return _bounded([_governed_dq_monitor(row, project_id=project_id) for row in rows], limit)


_COMPETITORS_CAPABILITY_STATE = """
    SELECT state FROM app.project_capabilities
     WHERE project_id = %(project_id)s AND capability_key = 'competitors'
"""


def _competitor_registry_lens(
    conn: Any, project_id: str, org_id: str, limit: int | None
) -> LensResult:
    """The Project-wide entity x compatible-Datastream matrix (Story 48.5).

    Fails closed twice, for two different reasons.

    A Project that has not enabled Competitors gets `unavailable` with a reason,
    never an empty list: an empty success reads as "this Project tracks nobody",
    which is a claim, and the honest answer is "this Project has not decided".

    A Project that HAS enabled it sees only the identities IT associated. The
    organization master may hold many more; the query that would reveal them
    does not exist, so a sibling Project's roster cannot leak through a count, a
    truncation marker or a timing difference.

    THE SWITCH IS READ THROUGH ITS OWN VOCABULARY, since 2026-08-24. This lens
    compared the state with the literal `"enabled"`, which is not one of the five
    values migration 131's CHECK admits -- `disabled`, `draft`, `ready`,
    `degraded`, `blocked`. The comparison could therefore never be true: the lens
    answered `unavailable` for EVERY Project of every deployment, including one
    that had turned Competitors on, and `tracked-entity` -- a declared Governance
    object type -- could be opened by nobody, because `_load_object` resolves an
    object by scanning its own lens. `project_capability_states` was written on
    2026-08-07 and names this exact site in its header ("which is NOT one of the
    five -- so that lens can never be available to anybody"); the literal
    survived the diagnosis by seventeen days because nothing walked the object
    types. `capability_is_active` is the same reader `country`, `tax_fees` and
    `placement_mapping` already go through, so `degraded` shows the roster AND
    says it is degraded rather than hiding evidence already collected.
    """
    from core.project_capability_states import (  # noqa: PLC0415 -- one state authority
        CAPABILITY_STATE_UNSET,
        capability_is_active,
    )

    rows = _fetch(conn, _COMPETITORS_CAPABILITY_STATE, {"project_id": project_id})
    state = str(rows[0]["state"]) if rows else CAPABILITY_STATE_UNSET
    if not capability_is_active(state):
        return LensResult(
            state="unavailable",
            reason=_unavailable(
                "competitors_capability_disabled",
                "Competitors is not enabled for this Project, so no tracked identity, "
                "role or binding exists to show.",
                owner=_owner_href(
                    "capabilities", object_type="capability", object_id="competitors"
                ),
            ),
        )

    from core.entity_bindings import list_bindings  # noqa: PLC0415
    from core.tracked_entities import list_source_identities, project_registry  # noqa: PLC0415

    entities = project_registry(conn, org_id=org_id, project_id=project_id)
    if not entities:
        return _bounded([], limit)

    node_ids = [str(entity["entity_id"]) for entity in entities]
    identities: dict[str, list[Mapping[str, Any]]] = {}
    for identity in list_source_identities(conn, org_id=org_id, node_ids=node_ids):
        identities.setdefault(str(identity["node_id"]), []).append(identity)
    bindings: dict[str, list[Mapping[str, Any]]] = {}
    for binding in list_bindings(conn, project_id=project_id, node_ids=node_ids):
        bindings.setdefault(str(binding["node_id"]), []).append(binding)

    items = [
        _tracked_entity(
            entity,
            identities=identities.get(str(entity["entity_id"]), []),
            bindings=bindings.get(str(entity["entity_id"]), []),
            project_id=project_id,
            org_id=org_id,
        )
        for entity in entities
    ]
    items.sort(key=lambda item: (item["summary"]["project_role"], item["object_ref"]["label"]))
    return _bounded(items, limit)


def _tracked_entity(
    entity: Mapping[str, Any],
    *,
    identities: list[Mapping[str, Any]],
    bindings: list[Mapping[str, Any]],
    project_id: str,
    org_id: str,
) -> dict[str, Any]:
    """One row of the matrix: an identity, its role, and where it is bound.

    The per-Datastream cells carry the state VERBATIM -- `published`,
    `candidate`, `excluded` -- rather than a boolean. A candidate binding and a
    published one look identical through a checkbox, and they are the difference
    between an approved intention and collected data.
    """

    version = entity.get("current_version") or {}
    payload = version.get("payload") or {}
    object_id = str(entity["entity_id"])
    label = str(payload.get("preferred_label") or entity.get("label") or object_id)
    published = [item for item in bindings if item["application_state"] == "published"]
    return _object_envelope(
        section="master-data",
        object_type="tracked-entity",
        object_id=object_id,
        label=label,
        scope="organization",
        owner={
            "kind": "tracked-entity-registry",
            "organization_id": org_id,
            "project_role": str(entity.get("role") or "reference"),
        },
        lifecycle_status=str(payload.get("lifecycle_state") or "unavailable"),
        active_version_ref=(
            {
                "object_type": "object-version",
                "id": str(version["id"]),
                "version": int(version.get("version_number") or 1),
                "state": str(version.get("status") or "draft"),
            }
            if version
            else None
        ),
        used_by=_facet(
            "available" if bindings else "empty",
            [{"object_type": "datastream", "id": str(item["datastream_id"])} for item in bindings],
        ),
        versions=_facet("available" if version else "empty", count=1 if version else 0),
        evidence=_facet("available" if identities else "empty", count=len(identities)),
        summary={
            "entity_kind": entity.get("entity_kind"),
            "project_role": str(entity.get("role") or "reference"),
            "pinned_version_id": entity.get("pinned_version_id"),
            "aliases": [
                {"value": alias["raw_value"], "relation": alias["relation"]}
                for alias in (entity.get("aliases") or [])
            ],
            "representations": [
                {
                    "connector": identity["connector_name"],
                    "account_scope": identity["account_scope"],
                    "external_id": identity["external_id"],
                    "external_label": identity["external_label"],
                    "version": identity["version_number"],
                }
                for identity in identities
            ],
            # The matrix cell, per Datastream. `published_count` is stated
            # separately so a screen never has to derive "is this collected?"
            # from the length of a list that also holds candidates.
            "matrix": [
                {
                    "datastream_ref": {
                        "object_type": "datastream",
                        "id": str(item["datastream_id"]),
                    },
                    "state": item["application_state"],
                    "direction": item["direction"],
                    "report_id": item["report_id"],
                    "exception_reason_code": item["exception_reason_code"],
                    "exception_reason": item["exception_reason"],
                }
                for item in bindings
            ],
            "published_count": len(published),
            "bound_count": len(bindings),
        },
        evidence_as_of=_iso(version.get("published_at") or version.get("created_at")),
    )


LensAdapter = Callable[[Any, str, str, "int | None"], LensResult]

_LENS_ADAPTERS: dict[tuple[str, str], LensAdapter] = {
    ("master-data", "business-domains"): _business_domains_lens,
    ("master-data", "classifications"): _classifications_lens,
    ("master-data", "products"): _products_lens,
    ("master-data", "activities"): _activities_lens,
    ("master-data", "registries"): _registries_lens,
    ("master-data", "competitor-registry"): _competitor_registry_lens,
    ("semantic-model", "concepts"): _concepts_lens,
    ("semantic-model", "semantic-views"): _semantic_views_lens,
    ("semantic-model", "mapping-coverage"): _mapping_coverage_lens,
    ("semantic-model", "value-tables"): _value_tables_lens,
    ("semantic-model", "cleanup-rules"): _cleanup_rules_lens,
    ("semantic-model", "canonical-fields"): _canonical_fields_lens,
    ("semantic-model", "metric-definitions"): _metric_definitions_lens,
    ("controls-quality", "conflicts"): _conflicts_lens,
    ("controls-quality", "reconciliation"): _reconciliation_lens,
    ("controls-quality", "data-quality"): _data_quality_lens,
    ("controls-quality", "rule-sets"): _rule_sets_lens,
    ("evidence", "lineage-provenance"): _lineage_lens,
    ("evidence", "versions-approvals"): _versions_approvals_lens,
    ("evidence", "audit-activity"): _audit_lens,
}


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _evidence_filter_options(lens: str) -> dict[str, Any]:
    """The vocabulary the Evidence index VALIDATES, declared on the wire.

    THE THREE LISTS EXISTED IN THE BROWSER BEFORE THEY EXISTED IN AN ENVELOPE.
    `evidence_index.normalize_filters` refuses an `owner_workspace` outside
    `OWNER_WORKSPACES`, a `correlation_kind` outside `CORRELATION_KINDS`, and a
    `record_kind` that is not this lens's — and the Evidence filter bar, having
    no declaration to read, retyped all three (`EvidenceCollection.tsx:244`).
    A retyped vocabulary is a menu that offers 400s the day the server's list
    moves, and it moved twice already (`virtual_pull`, `evaluation_run`).

    DERIVED, NEVER RESTATED: every entry below is read out of the module that
    validates against it, so a value added there appears in the menu without a
    second edit, and a value removed there leaves it.

    `record_kinds` carries the ONE kind this lens indexes, because the index
    raises `EvidenceFilterInvalid` for the other two: a three-entry menu would
    be two refusals and one no-op.

    No `WORKSPACE`/`KIND` LABELS TRAVEL WITH IT. The wire carries slugs, which
    is what the filter takes; naming them in a person's words is the console's
    job and stays there — a server that shipped English labels would own half
    of a sentence whose other half it cannot see.
    """
    from core.evidence_index import (  # noqa: PLC0415 -- one index authority
        CORRELATION_KINDS,
        LENS_RECORD_KIND,
        OWNER_WORKSPACES,
    )

    kind = LENS_RECORD_KIND.get(lens)
    return {
        "owner_workspaces": list(OWNER_WORKSPACES),
        "correlation_kinds": list(CORRELATION_KINDS),
        "record_kinds": [kind] if kind else [],
    }


def _envelope_head(project_id: str, org_id: str, section: str) -> dict[str, Any]:
    return {
        "project_ref": {"object_type": "project", "id": project_id},
        "organization_ref": {"object_type": "organization", "id": org_id},
        "section": section,
        "generated_at": _now(),
    }


def resolve_section(section: str) -> SectionContract:
    contract = GOVERNANCE_SECTIONS.get(section)
    if contract is None:
        raise GovernanceUnknownRoute(f"unknown Governance section: {section!r}")
    return contract


def compose_governance_collection(
    project_id: str,
    section: str,
    conn: Any,
    *,
    lens: str | None = None,
    org_id: str,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One lens of one Governance collection, from one authorized snapshot.

    ``query`` carries the bounded, allowlisted cursor and filters the Evidence
    index needs. It is forwarded only to adapters that declare they accept it,
    so a filter aimed at a lens that does not paginate is refused rather than
    silently ignored — an ignored filter renders a page that does not match the
    address that produced it.
    """
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required")
    contract = resolve_section(section)
    selected = lens or contract.default_lens
    if selected not in contract.lenses:
        # No fallback to the default: an unknown lens stays unknown, because
        # silently landing on another lens makes a shared link lie.
        raise GovernanceUnknownRoute(f"unknown lens {selected!r} for section {section!r}")

    adapter = _LENS_ADAPTERS[(section, selected)]
    bounded = query or {}
    collection_next_cursor: str | None = None
    collection_filter_options: dict[str, Any] | None = None
    paginated_collection = False
    if section == "evidence":
        result = adapter(conn, project_id, org_id, MAX_COLLECTION_ITEMS, bounded)
    elif section == "master-data":
        paginated_collection = True
        allowed = {"cursor", "limit", "q", "scope"}
        unknown = set(bounded) - allowed
        if unknown:
            raise ValueError(f"unsupported Master Data filter: {sorted(unknown)[0]}")
        try:
            offset = int(str(bounded.get("cursor") or "0"))
            limit = int(str(bounded.get("limit") or MAX_COLLECTION_ITEMS))
        except ValueError as exc:
            raise ValueError("Master Data cursor and limit must be integers") from exc
        if offset < 0 or not 1 <= limit <= MAX_COLLECTION_ITEMS:
            raise ValueError(
                "Master Data cursor must be positive and limit within the collection bound"
            )
        complete = adapter(conn, project_id, org_id, None)
        all_items = list(complete.items)
        collection_filter_options = {
            "scopes": sorted({str(item.get("scope")) for item in all_items if item.get("scope")}),
        }
        q = str(bounded.get("q") or "").strip().casefold()
        scope = str(bounded.get("scope") or "").strip()
        if q:
            all_items = [
                item for item in all_items
                if q in " ".join(
                    str(value) for value in (
                        item.get("object_ref", {}).get("label"),
                        item.get("object_ref", {}).get("id"),
                        item.get("object_ref", {}).get("type"),
                        item.get("scope"),
                        item.get("lifecycle_status"),
                    ) if value
                ).casefold()
            ]
        if scope:
            all_items = [item for item in all_items if item.get("scope") == scope]
        total = len(all_items)
        if offset and offset >= total:
            raise ValueError("Master Data cursor does not reference an available page")
        page_items = all_items[offset : offset + limit]
        if offset + limit < total:
            collection_next_cursor = str(offset + limit)
        result = LensResult(
            state=(
                "unavailable"
                if complete.state == "unavailable"
                else "available" if page_items else "empty"
            ),
            items=tuple(page_items),
            reason=complete.reason,
            total=total,
        )
    else:
        if bounded:
            raise GovernanceUnknownRoute(f"{selected!r} accepts no query parameters")
        result = adapter(conn, project_id, org_id, MAX_COLLECTION_ITEMS)

    reasons: list[dict[str, Any]] = []
    if result.reason is not None:
        reasons.append(result.reason)
    if result.truncated and result.page is None and not paginated_collection:
        reasons.append(
            _unavailable(
                "collection_truncated",
                f"{result.total} objects match this lens and the first "
                f"{len(result.items)} are shown. The remainder is not counted as absent.",
            )
        )
    page = result.page
    if page is not None:
        reasons.extend(page.reasons)

    evidence_times = [item["evidence_as_of"] for item in result.items if item.get("evidence_as_of")]
    coverage: dict[str, Any] = {
        "state": result.state,
        "returned": len(result.items),
        "total": result.total,
        "bound": MAX_COLLECTION_ITEMS,
    }
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_COLLECTION,
        **_envelope_head(project_id, org_id, section),
        "lens": selected,
        "default_lens": contract.default_lens,
        "available_lenses": list(contract.lenses),
        "evidence_as_of": max(evidence_times) if evidence_times else None,
        "items": list(result.items),
        "coverage": coverage,
        "unavailable_reasons": reasons,
    }
    if section == "evidence":
        # A DECLARATION, NOT A MEASUREMENT — so it does not wait for the page.
        # An index that answered `unavailable` still accepts exactly these
        # filters, and the bar that offers them must be able to undo a narrowing
        # that produced the empty screen a person is looking at.
        envelope["filter_options"] = _evidence_filter_options(selected)
    if page is not None:
        # `state` here answers "did we look at everything?", which is NOT the
        # same question as "did we find anything?". Both are reported, because
        # a caller that cannot tell them apart reads a backfill in progress as
        # an empty Project.
        coverage["index_state"] = page.coverage_state
        coverage["adapters"] = list(page.adapters)
        coverage["evidence_horizon"] = page.horizon
        envelope["next_cursor"] = page.next_cursor
        envelope["applied_filters"] = {
            key: value for key, value in bounded.items() if key not in {"cursor", "limit"}
        }
    elif paginated_collection:
        envelope["next_cursor"] = collection_next_cursor
        envelope["applied_filters"] = {
            key: value for key, value in bounded.items() if key not in {"cursor", "limit"}
        }
        envelope["filter_options"] = collection_filter_options or {"scopes": []}
        coverage["bound"] = int(str(bounded.get("limit") or MAX_COLLECTION_ITEMS))
    return envelope


def _load_object(
    conn: Any, project_id: str, org_id: str, section: str, object_type: str, object_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (object, unavailable_reason). Exactly one of them is not None, or
    both are None when the object simply does not exist in this Project."""
    sources = _OBJECT_SOURCES.get((section, object_type))
    if sources is None:
        raise GovernanceUnknownRoute(f"unknown object type {object_type!r} for {section!r}")
    pending: dict[str, Any] | None = None
    for lens in sources:
        # Unbounded on purpose: the collection bound is a DISPLAY bound. Applying
        # it here would turn "the 201st object" into "no such object".
        result = _LENS_ADAPTERS[(section, lens)](conn, project_id, org_id, None)
        if result.state == "unavailable":
            pending = result.reason
            continue
        for item in result.items:
            if item["object_ref"]["id"] == object_id:
                return item, None
    return None, pending


def _enrich_controls_object(
    conn: Any, project_id: str, object_type: str, object_id: str, detail: dict[str, Any]
) -> None:
    """Add the per-object facets the three Controls & Quality workbenches render.

    Composed HERE and not in the collection, for the reason the Semantic Model
    gives one function above: these are per-object reads and would be O(n) work
    on a list. Composed SERVER-side and not by the browser, because a fan-out
    across four owner endpoints is authoritative-looking and wrong the moment one
    of them fails -- AC8 forbids it in as many words.

    Every facet answers for itself. `unavailable` is reserved for "no owner could
    answer"; an owner that answered "none" contributes an empty list, and the two
    are never merged.
    """

    facets: dict[str, Any] = {}
    try:
        if object_type == "control-case":
            from core.control_cases import case_history  # noqa: PLC0415

            streams = case_history(conn, project_id=project_id, case_id=object_id)
            facets = {
                "occurrences": streams["occurrences"],
                "candidates": streams["candidates"],
                "impacts": streams["impacts"],
                "decisions": streams["decisions"],
            }
        elif object_type == "dq-monitor":
            facets = {
                "evaluations": _dq_evaluation_history(conn, project_id, object_id),
                "issues": _dq_issue_list(conn, project_id, object_id),
            }
        elif object_type == "rule-set":
            facets = _rule_set_facets(conn, project_id, object_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable facet is not a zero
        # No logger here on purpose: this module is a pure composer, and every
        # other failure path in it answers through the envelope rather than a log
        # line. `facets_state` IS the answer -- a reader sees "could not be read",
        # never an empty list that would say "this case has no decisions".
        detail["summary"] = {
            **detail.get("summary", {}),
            "facets_state": "unavailable",
            "facets_unavailable_reason": type(exc).__name__,
        }
        return
    detail["summary"] = {**detail.get("summary", {}), **facets, "facets_state": "available"}


def _dq_evaluation_history(conn: Any, project_id: str, monitor_id: str) -> list[dict[str, Any]]:
    """The last evaluations, newest first. Bounded: a History tab is not a log."""
    columns = (
        "id",
        "monitor_version_id",
        "outcome",
        "window_start",
        "window_end",
        "total_eligible",
        "evaluated_count",
        "passed_count",
        "failed_count",
        "unavailable_count",
        "evaluated_at",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(columns)} FROM app.dq_evaluations "
            "WHERE project_id = %s AND monitor_id = %s "
            "ORDER BY evaluated_at DESC LIMIT 50",
            (project_id, monitor_id),
        )
        return [
            {
                key: _iso(value) if key.endswith(("_at", "_start", "_end")) else value
                for key, value in zip(columns, row)
            }
            for row in cur.fetchall()
        ]


def _dq_issue_list(conn: Any, project_id: str, monitor_id: str) -> list[dict[str, Any]]:
    """Issues with their workflow history counted. Closed ones are INCLUDED.

    A resolved issue that comes back is the thing worth seeing, and a list that
    hides it makes every recurrence look new.
    """
    columns = (
        "id",
        "status",
        "severity",
        "suppressed_until",
        "control_case_id",
        "first_seen_at",
        "last_seen_at",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(columns)}, "
            "(SELECT COUNT(*) FROM app.dq_issue_events e WHERE e.issue_id = i.id) AS event_count "
            "FROM app.dq_issues i "
            "WHERE i.project_id = %s AND i.monitor_id = %s "
            "ORDER BY i.last_seen_at DESC LIMIT 50",
            (project_id, monitor_id),
        )
        return [
            {
                **{
                    key: _iso(value) if key.endswith(("_at", "_until")) else value
                    for key, value in zip(columns, row[:-1])
                },
                "event_count": int(row[-1]),
            }
            for row in cur.fetchall()
        ]


def _rule_set_facets(conn: Any, project_id: str, rule_set_id: str) -> dict[str, Any]:
    """The ordered ladder, its effective window, approvals and exceptions."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.ordered_rules, v.payload, v.effective_from, v.effective_to, "
            "       v.profile, v.version_number "
            "FROM app.governance_rule_sets s "
            "JOIN app.governance_rule_set_versions v ON v.id = s.current_version_id "
            "WHERE s.id = %s AND s.project_id = %s",
            (rule_set_id, project_id),
        )
        current = cur.fetchone()

        cur.execute(
            "SELECT id, version_number, status, effective_from, effective_to, created_at "
            "FROM app.governance_rule_set_versions WHERE rule_set_id = %s AND project_id = %s "
            "ORDER BY version_number DESC LIMIT 50",
            (rule_set_id, project_id),
        )
        windows = [
            {
                "version_id": row[0],
                "version_number": row[1],
                "status": row[2],
                "effective_from": _iso(row[3]),
                "effective_to": _iso(row[4]),
                "created_at": _iso(row[5]),
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            "SELECT id, rule_set_version_id, decision, reason, decided_by, decided_at "
            "FROM app.rule_set_approvals WHERE rule_set_id = %s AND project_id = %s "
            "ORDER BY decided_at DESC LIMIT 50",
            (rule_set_id, project_id),
        )
        approvals = [
            {
                "id": row[0],
                "version_id": row[1],
                "decision": row[2],
                "reason": row[3],
                "decided_by": row[4],
                "decided_at": _iso(row[5]),
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            "SELECT id, subject_kind, subject_id, reason_code, reason, "
            "       effective_from, expires_at, decided_by "
            "FROM app.governance_rule_set_exceptions WHERE rule_set_id = %s AND project_id = %s "
            "ORDER BY effective_from DESC LIMIT 50",
            (rule_set_id, project_id),
        )
        exceptions = [
            {
                "id": row[0],
                "subject_kind": row[1],
                "subject_id": row[2],
                "reason_code": row[3],
                "reason": row[4],
                "effective_from": _iso(row[5]),
                "expires_at": _iso(row[6]),
                "decided_by": row[7],
                # Computed here so the screen never has to decide what "in force"
                # means, and two screens cannot decide it differently.
                "in_force": _in_force(row[5], row[6]),
            }
            for row in cur.fetchall()
        ]

    ordered_rules = (current[0] if current else []) or []
    return {
        "ordered_rules": ordered_rules,
        "policy": (current[1] if current else {}) or {},
        "effective_from": _iso(current[2]) if current else None,
        "effective_to": _iso(current[3]) if current else None,
        "profile": current[4] if current else None,
        "version_windows": windows,
        "approvals": approvals,
        "exceptions": exceptions,
        "preset_proposals": _tax_fee_proposals_for_empty_ladder(
            conn, project_id=project_id, rule_set_id=rule_set_id, rules=ordered_rules
        ),
    }


#: A workbench renders these; a payload that grew without bound would be the
#: "authoritative-looking and wrong" failure AC10 names. Twenty is far past what a
#: reader reviews in one sitting and far below anything that hurts.
_MAX_PRESET_PROPOSALS = 20


def _tax_fee_proposals_for_empty_ladder(
    conn: Any, *, project_id: str, rule_set_id: str, rules: list
) -> list[dict[str, Any]]:
    """What a `tax_fee` ladder could be proposed, when it is still empty.

    Completeness criterion [0] of tax-fees is "activation opens a blank rule editor
    WHEN A QUALIFIED PROPOSAL IS POSSIBLE". Two sessions met this defect from
    opposite sides on 2026-08-04. `TaxFeeLadderTabs.tsx` had just been written with
    an EmptyState whose text says, correctly for the tree it was written against:
    "The proposal path ... has no reachable caller in this build: its MCP tools are
    not registered and no REST route exposes them." Meanwhile `TaxFeesCompiler`
    gained one on the activation path. This is the read that lets the screen offer
    what the compiler already knows.

    THREE THINGS IT DELIBERATELY DOES NOT DO:

    * It does not run for a ladder that carries rules. Once a Project has decided,
      proposals are noise, and the preset scan is real work on the common path.
    * It does not run for another family. The Rule Set lifecycle is opaque to what a
      family MEANS, and a Money Policy head asking for tax presets would be exactly
      the leak the profile contract forbids.
    * It does not decide. Every proposal carries its unproven qualifications and its
      draft rule is inert; adopting one is an operator's act through a Change Set.
      A preset narrowed by a Project's own countries still proves nothing.

    Any failure returns `[]` rather than raising: an offer is an enrichment, and a
    ladder that cannot list its candidates must still render its own state. The
    caller's `facets_state` machinery is for facets a reader would otherwise
    misread as "none" -- this one is honestly empty far more often than not.
    """
    if rules:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.family, p.org_id FROM app.governance_rule_sets s "
            "JOIN app.projects p ON p.id = s.project_id "
            "WHERE s.id = %s AND s.project_id = %s",
            (rule_set_id, project_id),
        )
        row = cur.fetchone()
    if not row or str(row[0]) != "tax_fee":
        return []

    from core.capability_compilers import _tax_fee_preset_proposals  # noqa: PLC0415

    try:
        proposals = _tax_fee_preset_proposals(
            conn, project_id=project_id, org_id=str(row[1])
        )
    except Exception:  # noqa: BLE001 -- see the docstring: an offer is not a fact
        return []
    return proposals[:_MAX_PRESET_PROPOSALS]


def _in_force(effective_from: Any, expires_at: Any) -> bool:
    from datetime import date as _date  # noqa: PLC0415

    today = _date.today()
    if effective_from and effective_from > today:
        return False
    return not (expires_at and expires_at < today)


def _load_evidence_object(
    conn: Any, project_id: str, object_type: str, object_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """One exact Evidence Record, with the tab content its type contracts.

    The record kind is checked against the requested object type. An
    `object-version` id opened under `/evidence-trace/` is NOT found — it is not
    quietly re-labelled, because a shared address must keep meaning the same
    thing when someone else opens it.
    """
    from core.evidence_index import (  # noqa: PLC0415 -- one index authority
        load_audit_detail,
        load_availability,
        load_correlations,
        load_record,
        load_trace_graph,
        load_version_detail,
    )

    record = load_record(conn, project_id=project_id, record_id=object_id)
    if record is None:
        return None, None
    kind = str(record["record_kind"])
    if _EVIDENCE_OBJECT_TYPE.get(kind) != object_type:
        return None, None

    record_id = str(record["id"])
    correlations = load_correlations(conn, project_id=project_id, record_ids=[record_id])
    availability = load_availability(conn, project_id=project_id, record_ids=[record_id])

    lens_summary: dict[str, Any] = {}
    if kind == "evidence_trace":
        lens_summary["trace"] = (
            load_trace_graph(conn, project_id=project_id, anchor_id=record_id)
            if bool(record["is_anchor"])
            else {
                "coverage": "partial",
                "nodes": [],
                "edges": [],
                "owner_references": [],
                "roots": [],
                "terminals": [],
                "unavailable_reasons": [
                    {
                        "code": "not_a_trace_anchor",
                        "message": (
                            "This record is a step inside another trace, not the trace itself. "
                            "Open its anchor to see the chain it belongs to."
                        ),
                    }
                ],
            }
        )
    elif kind == "object_version":
        lens_summary["version_detail"] = load_version_detail(
            conn, project_id=project_id, record=record
        )
    else:
        lens_summary["audit_detail"] = load_audit_detail(conn, record=record)

    detail = _evidence_item(
        record,
        project_id=project_id,
        correlations=correlations.get(record_id, []),
        availability=availability.get(record_id),
        lens_summary=lens_summary,
    )
    return detail, None


def _enrich_semantic_object(
    conn: Any, project_id: str, object_type: str, object_id: str, detail: dict[str, Any]
) -> None:
    """Add the per-object facets the two Semantic Model workbenches render.

    Every facet answers for itself. An adapter that cannot be read reports
    ``unavailable`` with its reason; it never contributes an empty list, because
    an empty list here reads as "this Concept is bound nowhere".
    """
    from core.semantic_coverage import (  # noqa: PLC0415
        CoverageUnavailable,
        compose_mapping_coverage,
        unavailable_report,
    )
    from core.semantic_model import (  # noqa: PLC0415
        concept_names,
        load_view,
        load_view_members,
        load_view_relationships,
    )

    # A value mapping table (Story 60.1) binds no concept and has no source
    # bindings: attaching a mapping-coverage report to it would decorate it with
    # a measurement about something else.
    if object_type not in {"semantic-concept", "semantic-view"}:
        return

    summary = detail["summary"]
    try:
        names = concept_names(conn, project_id)
        if object_type == "semantic-concept":
            report = compose_mapping_coverage(
                conn, project_id, concept_ids=[object_id], concept_names=names
            )
        else:
            report = compose_mapping_coverage(conn, project_id, concept_names=names)
    except CoverageUnavailable as exc:
        report = unavailable_report(type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 -- adapter failure, never a zero
        report = unavailable_report(type(exc).__name__)
    summary["source_bindings"] = report.as_dict()

    if object_type != "semantic-view":
        return
    try:
        view = load_view(conn, project_id, object_id)
        version_id = view.get("current_version_id")
        summary["compiled_matrix"] = view.get("queryability_matrix") or {}
        summary["members"] = (
            [
                {
                    "concept_id": row["concept_id"],
                    "concept_version_id": row["concept_version_id"],
                    "role": row["role"],
                    "name": row["name"],
                    "label": row["label"],
                    # THE NUMBER A PERSON READS FOR THIS PIN. The editor listed
                    # members pinned to a superseded version as
                    # "<name> · version scv_01KZ..." because the id was the only
                    # thing the payload carried -- the same defect as a rail
                    # printing `mdm_01KZ...`. `semantic_concept_versions` has
                    # carried `version_number` since 142_semantic_model.sql, so
                    # the word exists and is served here, where the vocabulary
                    # lives, rather than composed in the browser.
                    "version_number": row.get("version_number"),
                    "value_type": row["value_type"],
                    "unit": row.get("unit"),
                    "semantic_type": row.get("semantic_type"),
                    "additivity_class": row.get("additivity_class"),
                }
                for row in load_view_members(conn, version_id)
            ]
            if version_id
            else []
        )
        summary["relationships"] = load_view_relationships(conn, version_id) if version_id else []
    except Exception:  # noqa: BLE001
        summary["compiled_matrix"] = None
        summary["members"] = None
        summary["relationships"] = None


def _md_node(kind: str, node_id: str, label: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "id": node_id, "label": label, **extra}


def _master_data_hierarchy(
    conn: Any, org_id: str, object_type: str, object_id: str
) -> dict[str, Any]:
    """Ancestors and children of one Master Data object.

    The three object types do not share a parent store, and pretending they do
    is how a second geographic (or taxonomic) mapping gets built. Each reads its
    own owner:

    * a Business Domain IS a root -- it has no ancestors, and saying so is an
      answer, not an absence. Its children are its top-level classifications;
    * a classification walks its parent edge up to its Domain;
    * a registry resolves through published memberships, which is the only place
      a registry hierarchy is versioned.

    Story 49.2: the taxonomy half reads `business_identity_catalogue`, so the
    Hierarchy facet of a NATIVELY created identity draws the same tree as a
    converged one. It used to walk the superseded store alone, where such an
    identity has no parent edge at all -- and a hierarchy that answers "no
    ancestors" for an object that plainly has one is worse than an outage,
    because it looks like a fact.
    """
    with conn.cursor() as cur:
        if object_type == "business-domain":
            cur.execute(
                f"""
                SELECT id, name, classification_type, status
                  FROM {CLASSIFICATION_SOURCE} child
                 WHERE org_id = %s AND domain_id = %s AND parent_id IS NULL
                   AND archived_at IS NULL
                 ORDER BY name
                """,
                (org_id, object_id),
            )
            children = [
                _md_node(
                    "master-data-object", str(r[0]), str(r[1]),
                    classification_type=r[2], status=r[3],
                )
                for r in cur.fetchall()
            ]
            return {
                "state": "available",
                "is_root": True,
                "ancestors": [],
                "children": children,
                "reason": None,
            }

        if object_type == "master-data-object":
            ancestors: list[dict[str, Any]] = []
            seen: set[str] = set()
            current: str | None = object_id
            domain_ref: dict[str, Any] | None = None
            while current and current not in seen:
                seen.add(current)
                cur.execute(
                    f"""
                    SELECT c.parent_id, c.domain_id, p.name, d.name
                      FROM {CLASSIFICATION_SOURCE} c
                      LEFT JOIN {CLASSIFICATION_SOURCE} p ON p.id = c.parent_id
                      LEFT JOIN {DOMAIN_SOURCE} d ON d.id = c.domain_id
                     WHERE c.org_id = %s AND c.id = %s
                    """,
                    (org_id, current),
                )
                row = cur.fetchone()
                if row is None:
                    break
                parent_id, domain_id, parent_name, domain_name = row
                if domain_id and domain_ref is None:
                    domain_ref = _md_node(
                        "business-domain", str(domain_id), str(domain_name or domain_id)
                    )
                if not parent_id:
                    break
                ancestors.append(
                    _md_node(
                        "master-data-object", str(parent_id), str(parent_name or parent_id)
                    )
                )
                current = str(parent_id)
            # The Domain closes the chain: it is the root every classification
            # resolves to, and omitting it would make the path look truncated.
            if domain_ref is not None:
                ancestors.append(domain_ref)

            cur.execute(
                f"""
                SELECT id, name, classification_type, status
                  FROM {CLASSIFICATION_SOURCE} child
                 WHERE org_id = %s AND parent_id = %s AND archived_at IS NULL
                 ORDER BY name
                """,
                (org_id, object_id),
            )
            children = [
                _md_node(
                    "master-data-object", str(r[0]), str(r[1]),
                    classification_type=r[2], status=r[3],
                )
                for r in cur.fetchall()
            ]
            return {
                "state": "available",
                "is_root": not ancestors,
                "ancestors": ancestors,
                "children": children,
                "reason": None,
            }

        # registry. Archived nodes are INCLUDED and flagged, not filtered out:
        # `restore` is a governed command, and a node the screen cannot see is a
        # node nobody can bring back. Hiding them would also make an archive look
        # like a deletion, which is exactly what archiving exists not to be.
        cur.execute(
            """
            SELECT n.id, n.label, n.node_kind, n.archived_at
              FROM app.master_data_nodes n
             WHERE n.registry_id = %s
             ORDER BY n.archived_at NULLS FIRST, n.label
            """,
            (object_id,),
        )
        nodes = [
            _md_node(
                "registry-node", str(r[0]), str(r[1]),
                node_kind=r[2],
                archived=r[3] is not None,
                # The action this node can take, decided here rather than in the
                # browser: the read model knows the state, and a screen that
                # guessed would offer Restore on a live node.
                command=("restore" if r[3] is not None else "archive"),
            )
            for r in cur.fetchall()
        ]
    return {
        "state": "available",
        "is_root": True,
        "ancestors": [],
        "children": nodes,
        "reason": None,
    }


# Only registry nodes carry aliases: `app.master_data_aliases.node_id` is a node
# reference, and no alias store exists for Business Domains or classifications.
# That is a real gap, so the tab reports it as one instead of rendering an empty
# table -- an empty table here reads as "this object has no aliases", which is
# not what is known.
_ALIAS_CAPABLE = frozenset({"registry"})


def _master_data_aliases(conn: Any, object_type: str, object_id: str) -> dict[str, Any]:
    """Aliases recorded against one Master Data object."""
    if object_type not in _ALIAS_CAPABLE:
        return {
            "state": "unavailable",
            "rows": [],
            "reason": _unavailable(
                "master_data_alias_store_absent",
                "No alias store exists for this object type: app.master_data_aliases "
                "keys on a registry node, and Business Domains and classifications have "
                "none. This is a missing store, not an object without aliases.",
            ),
        }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.namespace, a.locale, a.raw_value, a.normalized_value,
                   a.relation, a.confidence, a.provenance, a.conflict_state,
                   a.effective_from, a.effective_to, n.label
              FROM app.master_data_aliases a
              JOIN app.master_data_nodes n ON n.id = a.node_id
             WHERE n.registry_id = %s AND a.retired_at IS NULL
             ORDER BY n.label, a.namespace, a.raw_value
             LIMIT %s
            """,
            (object_id, MAX_FACET_REFS),
        )
        rows = [
            {
                "id": str(r[0]),
                "namespace": r[1],
                "locale": r[2],
                "raw_value": r[3],
                "normalized_value": r[4],
                "relation": r[5],
                "confidence": float(r[6]) if r[6] is not None else None,
                "provenance": r[7],
                "conflict_state": r[8],
                "effective_from": _iso(r[9]),
                "effective_to": _iso(r[10]),
                "node_label": r[11],
            }
            for r in cur.fetchall()
        ]
    return {"state": "available", "rows": rows, "reason": None}


def _client_object_sources(
    conn: Any, object_type: str, object_id: str, summary: Mapping[str, Any]
) -> dict[str, Any]:
    """The live sources feeding a client object kind (Story 64.1/64.13, AI-232).

    THE FACET THE EPIC OWED AND NOBODY COULD SEE. `object_kind_registry` composed
    exactly this answer -- which Datastream feeds the kind, in which namespace, and
    how a row of it becomes an object -- and it reached the MCP door only. A person
    could declare an object kind and then find no screen that says what feeds it.

    `unavailable` is reserved for an object type that HAS no such store: only a
    client-declared registry carries source bindings, and rendering an empty table
    for a Country registry would read as "this registry is fed by nothing", which
    is a claim about the wrong thing. A client kind whose bindings were all
    released answers `empty` instead, which is a different sentence.
    """
    kind = summary.get("object_kind")
    if object_type != "registry" or not kind:
        return {
            "state": "unavailable",
            "rows": [],
            "reason": _unavailable(
                "master_data_source_bindings_absent",
                "No source binding store exists for this object: bindings are "
                "declared for a client object kind, and a capability registry is "
                "fed by its capability. This is a missing store, not a registry "
                "without sources.",
            ),
        }

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.namespace, b.datastream_id, d.name, b.identity_mode,
                   b.label_field, b.mapping_version_id, m.mapping_payload
              FROM app.master_data_source_bindings b
              LEFT JOIN app.datastreams d ON d.id = b.datastream_id
              LEFT JOIN app.datastream_mapping_versions m
                ON m.id = b.mapping_version_id
             WHERE b.registry_id = %s AND b.released_at IS NULL
             ORDER BY b.namespace
            """,
            (object_id,),
        )
        rows = cur.fetchall()

    from core.object_kind_registry import identity_fields  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for namespace, ds_id, ds_name, mode, label_field, mapping_id, payload in rows:
        if isinstance(payload, str):  # pragma: no cover - driver-dependent
            import json  # noqa: PLC0415

            payload = json.loads(payload)
        out.append(
            {
                "namespace": namespace,
                "datastream_id": ds_id,
                # The Datastream is named, never just referenced: a person reading
                # Governance must not have to resolve an id in Data to know what
                # feeds this object.
                "datastream_label": ds_name,
                "identity_mode": mode,
                # A grain is only an identity in `source_key` mode. Showing one
                # beside a `governed_label` source would say the file carries a key.
                "identity_fields": list(identity_fields(payload)) if mode == "source_key" else [],
                "label_field": label_field,
                "mapping_version_id": mapping_id,
            }
        )
    return {"state": "available" if out else "empty", "rows": out, "reason": None}


# ---------------------------------------------------------------------------
# Used by, WITH ITS REFERENCES (Story 49.2 AC7 -- verdict of 2026-09-01).
#
# The count was composed and the list never was. `used_by=_counted_facet(...)`
# served `{"state": "available", "count": 3, "refs": []}` and the workbench read
# the empty list as the answer: "Nothing depends on this object. Its owner
# answered, and the answer is none." -- in the tab a person opens before
# archiving. AC7 states what a reference must carry, and nothing here is
# optional: *"owner workspace, stable consumer ID, pinned version when
# applicable, evidence time and an exact owner link"*.
#
# THREE STORES ANSWER, AND EACH ONE IS A DIFFERENT KIND OF DEPENDENCY:
#
#   app.mdm_business_links               a Context topic, a Datastream field, a
#                                        report that was LINKED to this identity
#   app.master_data_used_by              a published version that RESOLVES
#                                        through it (Semantic Model, Rule Sets,
#                                        observed-entity attachments)
#   app.master_data_project_associations a Project that REUSES an organization
#                                        identity under a role
#
# IT FAILS CLOSED, AND THAT IS THE WHOLE POINT. If one store cannot be read the
# facet is `unavailable` with the reason -- never the partial list, because a
# partial list is indistinguishable from a complete one on screen and it is the
# reading a person acts on before deleting something.
# ---------------------------------------------------------------------------

# The target is NAMED, not just referenced. A used-by list of opaque ids asks a
# person to resolve seven stores by hand before deciding whether an archive is
# safe -- which is the same as not answering. Every join is a LEFT join and the
# id is the fallback: a target whose owner store cannot name it is still a
# dependency, and dropping it would be the empty list this repair exists to end.
_BUSINESS_LINK_CONSUMERS = """
    SELECT bl.target_type, bl.target_id, bl.relation_type, bl.created_at,
           COALESCE(t.title, p.name, d.name, sv.name, sc.name, tf.display_name, tf.name)
      FROM app.mdm_business_links bl
      LEFT JOIN app.context_topics t
        ON bl.target_type = 'topic' AND t.id = bl.target_id
      LEFT JOIN app.procedures p
        ON bl.target_type = 'procedure' AND p.id = bl.target_id
      LEFT JOIN app.datastreams d
        ON bl.target_type = 'datastream' AND d.id = bl.target_id
      LEFT JOIN app.semantic_views sv
        ON bl.target_type = 'semantic_view' AND sv.id = bl.target_id
      LEFT JOIN app.semantic_concepts sc
        ON bl.target_type = 'semantic_concept' AND sc.id = bl.target_id
      -- A target field is addressed by its NAME, not by an id (migration 130's
      -- own validator reads `app.target_fields` on `name`).
      LEFT JOIN app.target_fields tf
        ON bl.target_type = 'target_field' AND tf.name = bl.target_id
     WHERE bl.taxonomy_id = %s AND bl.project_id = %s AND bl.retired_at IS NULL
     ORDER BY bl.target_type, bl.target_id
"""

_NODE_USED_BY_CONSUMERS = """
    SELECT u.consumer_kind, u.consumer_id, u.consumer_label, u.consumer_version_id,
           u.created_at
      FROM app.master_data_used_by u
     WHERE u.node_id = %s AND u.project_id = %s AND u.released_at IS NULL
     ORDER BY u.consumer_kind, u.consumer_id
"""

_REGISTRY_USED_BY_CONSUMERS = """
    SELECT u.consumer_kind, u.consumer_id, u.consumer_label, u.consumer_version_id,
           u.created_at
      FROM app.master_data_used_by u
     WHERE u.registry_id = %s AND u.project_id = %s AND u.released_at IS NULL
     ORDER BY u.consumer_kind, u.consumer_id
"""

_ASSOCIATION_CONSUMERS = """
    SELECT a.id, a.project_role, a.node_version_id, a.created_at
      FROM app.master_data_project_associations a
     WHERE a.node_id = %s AND a.project_id = %s AND a.retired_at IS NULL
     ORDER BY a.project_role, a.id
"""


def _consumer_ref(
    *,
    ref_id: str,
    kind: str,
    label: str | None,
    workspace: str | None,
    pinned_version_id: str | None,
    recorded_at: Any,
    owner_href: dict[str, Any] | None,
    source: str,
) -> dict[str, Any]:
    """One used-by reference, in the shape AC7 spells out.

    ``label`` falls back to the id rather than to a manufactured name: an id is
    what is known, and a prettier string that names nothing is worse.
    """
    from core.master_data_consumers import workspace_label  # noqa: PLC0415

    return {
        "id": ref_id,
        "kind": kind,
        "label": label or ref_id,
        "workspace": workspace,
        "workspace_label": workspace_label(workspace),
        "pinned_version_id": pinned_version_id,
        "recorded_at": _iso(recorded_at),
        "owner_href": owner_href,
        "source": source,
    }


def _master_data_used_by(
    conn: Any,
    *,
    project_id: str,
    object_type: str,
    object_id: str,
) -> dict[str, Any]:
    """Every live consumer of one Master Data object, named and linked."""

    from core.master_data_consumers import (  # noqa: PLC0415
        _ASSOCIATION_WORKSPACE,
        consumer_owner_href,
        consumer_workspace,
        link_target_owner_href,
        link_target_workspace,
    )

    refs: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        if object_type == "registry":
            # A registry's dependents are declared against the GROUPING, not
            # against one identity inside it. Business links and associations key
            # on a node, so they have no answer to give here -- and asking them
            # for one would return every node's dependents as the registry's.
            cur.execute(_REGISTRY_USED_BY_CONSUMERS, (object_id, project_id))
            store_rows = cur.fetchall()
            link_rows: list[Any] = []
            association_rows: list[Any] = []
        else:
            cur.execute(_NODE_USED_BY_CONSUMERS, (object_id, project_id))
            store_rows = cur.fetchall()
            cur.execute(_BUSINESS_LINK_CONSUMERS, (object_id, project_id))
            link_rows = cur.fetchall()
            cur.execute(_ASSOCIATION_CONSUMERS, (object_id, project_id))
            association_rows = cur.fetchall()

    for kind, consumer_id, label, consumer_version_id, created_at in store_rows:
        refs.append(
            _consumer_ref(
                ref_id=str(consumer_id),
                kind=str(kind),
                label=_text(label),
                workspace=consumer_workspace(str(kind)),
                pinned_version_id=_text(consumer_version_id),
                recorded_at=created_at,
                owner_href=consumer_owner_href(
                    str(kind), str(consumer_id), _text(consumer_version_id)
                ),
                source="master-data-used-by",
            )
        )

    for target_type, target_id, relation_type, created_at, target_label in link_rows:
        refs.append(
            _consumer_ref(
                ref_id=str(target_id),
                kind=str(target_type),
                # The relation is what the link MEANS ("explains", "measures").
                # It travels beside the label because two links to the same topic
                # under different relations are two different dependencies.
                label=_text(target_label),
                workspace=link_target_workspace(str(target_type)),
                pinned_version_id=None,
                recorded_at=created_at,
                owner_href=link_target_owner_href(str(target_type), str(target_id)),
                source="business-link",
            )
            | {"relation": _text(relation_type)}
        )

    for association_id, project_role, node_version_id, created_at in association_rows:
        refs.append(
            _consumer_ref(
                ref_id=str(association_id),
                kind="project-association",
                label=f"This Project, as {project_role}",
                workspace=_ASSOCIATION_WORKSPACE,
                pinned_version_id=_text(node_version_id),
                recorded_at=created_at,
                owner_href=None,
                source="project-association",
            )
        )

    return _counted_facet(len(refs), refs)


def _master_data_version_refs(
    conn: Any, org_id: str, object_type: str, object_id: str, summary: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The exact history of one Master Data object, newest first.

    Returns ``None`` for an object whose lens already composed its refs (a
    Business Domain does, in `_BUSINESS_DOMAINS`), so this never recomputes a
    facet that is already complete.
    """

    if object_type == "business-domain":
        return None

    if object_type == "registry":
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.version_number, v.status, v.content_hash,
                       COALESCE(v.published_at, v.created_at)
                  FROM app.master_data_object_versions v
                 WHERE v.registry_id = %s
                 ORDER BY v.version_number DESC
                """,
                (object_id,),
            )
            rows = cur.fetchall()
        return _counted_facet(len(rows), [_master_data_version_ref(object_id, r) for r in rows])

    # A `master-data-object` is one of two things, and they keep their history in
    # two different stores. The summary says which: a classification carries
    # `classification_type`, a client instance carries `registry_id`.
    if summary.get("registry_id"):
        scope = str(summary.get("version_scope") or "node")
        with conn.cursor() as cur:
            if scope == "node":
                cur.execute(
                    """
                    SELECT v.id, v.version_number, v.status, v.content_hash,
                           COALESCE(v.published_at, v.created_at)
                      FROM app.master_data_object_versions v
                     WHERE v.node_id = %s
                     ORDER BY v.version_number DESC
                    """,
                    (object_id,),
                )
            else:
                # Registry scope: this identity's history IS the grouping's, and
                # saying "no version" because the node holds none would be the
                # defect `_client_object_instance` already names.
                cur.execute(
                    """
                    SELECT v.id, v.version_number, v.status, v.content_hash,
                           COALESCE(v.published_at, v.created_at)
                      FROM app.master_data_object_versions v
                     WHERE v.registry_id = %s AND v.node_id IS NULL
                     ORDER BY v.version_number DESC
                    """,
                    (str(summary["registry_id"]),),
                )
            rows = cur.fetchall()
        return _counted_facet(len(rows), [_master_data_version_ref(object_id, r) for r in rows])

    # A classification. Its revisions are the same two-store union the lens
    # counts, and they have no id of their own on the superseded side -- the
    # address is `<object>:<number>`, exactly as a Business Domain's is.
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT v.version_number, v.changed_at, v.changed_by
              FROM {CLASSIFICATION_VERSION_SOURCE} v
             WHERE v.classification_id = %s AND v.org_id = %s
             ORDER BY v.version_number DESC
            """,
            (object_id, org_id),
        )
        rows = cur.fetchall()
    latest = max((int(r[0]) for r in rows), default=None)
    refs = [
        {
            "object_type": "master-data-object-version",
            "id": f"{object_id}:{int(number)}",
            "version": int(number),
            "state": "active" if number == latest else "previous",
            "recorded_at": _iso(changed_at),
            "recorded_by": changed_by,
        }
        for number, changed_at, changed_by in rows
    ]
    return _counted_facet(len(refs), refs)


def _master_data_version_ref(object_id: str, row: Any) -> dict[str, Any]:
    version_id, number, status, content_hash, recorded_at = row
    return {
        "object_type": "master-data-object-version",
        "id": str(version_id),
        "version": int(number),
        # The version's OWN lifecycle, never its position in the list.
        "state": str(status or "unavailable"),
        "recorded_at": _iso(recorded_at),
        "evidence_hash": content_hash,
        "owner_href": _owner_href(
            "master-data",
            object_type="master-data-object",
            object_id=object_id,
            tab="versions",
            version_id=str(version_id),
        ),
    }


def _enrich_master_data_object(
    conn: Any,
    org_id: str,
    object_type: str,
    object_id: str,
    detail: dict[str, Any],
    *,
    project_id: str | None = None,
) -> None:
    """Add the per-object facets the Master Data workbench renders (49.2).

    Composed here for the same reason as the Semantic Model facets: they are
    per-object and a browser fan-out that half-fails looks authoritative and is
    wrong. Each facet reports its own state, so one unreadable store never
    silences the other.

    ``used_by`` and ``versions`` are REPLACED here, not added: the collection
    lens can only afford a count, and the object a person opens is the one that
    owes the list. ``project_id`` is what scopes the consumer stores; without it
    the used-by facet stays as the lens left it rather than answering for a
    Project it was not told about.
    """
    summary = detail["summary"]
    # WHICH OBJECTS THESE STORES ANSWER FOR, AND WHICH THEY DO NOT. A Country or
    # Competitor registry is served by `_capability_object` from the Epic 48
    # pinned owner references: its used-by is "how many Datastreams carry this
    # capability" and its versions are configuration pins. Recomposing either
    # from `app.master_data_*` would answer a different question under the same
    # word -- so the enrichment is scoped by the composer that produced the row,
    # named through the owner kind it declared.
    owner_kind = str((detail.get("owner") or {}).get("kind") or "")
    master_data_owned = owner_kind in {"business-taxonomy", "master-data"}
    if project_id and master_data_owned:
        try:
            detail["used_by"] = _master_data_used_by(
                conn, project_id=project_id, object_type=object_type, object_id=object_id
            )
        except Exception as exc:  # noqa: BLE001 -- fail closed: never a bare empty list
            detail["used_by"] = _facet(
                "unavailable",
                count=int(detail.get("used_by", {}).get("count") or 0),
                reason=_unavailable(
                    "master_data_used_by_unreadable",
                    "A consumer store could not be read "
                    f"({type(exc).__name__}), so this list is incomplete and is not "
                    "shown. Reload; if it persists, retire the object from its "
                    "consumers first.",
                ),
            )
    try:
        versions = (
            _master_data_version_refs(conn, org_id, object_type, object_id, summary)
            if master_data_owned
            else None
        )
    except Exception as exc:  # noqa: BLE001
        versions = _facet(
            "unavailable",
            count=int(detail.get("versions", {}).get("count") or 0),
            reason=_unavailable(
                "master_data_versions_unreadable",
                f"The version ledger could not be read ({type(exc).__name__}). "
                "Reload to try again.",
            ),
        )
    if versions is not None and "versions" in detail:
        detail["versions"] = versions
    try:
        summary["hierarchy"] = _master_data_hierarchy(conn, org_id, object_type, object_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable store is a state, not a crash
        summary["hierarchy"] = {
            "state": "unavailable",
            "is_root": False,
            "ancestors": [],
            "children": [],
            "reason": _unavailable(
                "master_data_hierarchy_unreadable",
                f"The hierarchy store could not be read ({type(exc).__name__}).",
            ),
        }
    try:
        summary["aliases"] = _master_data_aliases(conn, object_type, object_id)
    except Exception as exc:  # noqa: BLE001
        summary["aliases"] = {
            "state": "unavailable",
            "rows": [],
            "reason": _unavailable(
                "master_data_aliases_unreadable",
                f"The alias store could not be read ({type(exc).__name__}).",
            ),
        }
    try:
        summary["sources"] = _client_object_sources(conn, object_type, object_id, summary)
    except Exception as exc:  # noqa: BLE001
        summary["sources"] = {
            "state": "unavailable",
            "rows": [],
            "reason": _unavailable(
                "master_data_source_bindings_unreadable",
                f"The source bindings could not be read ({type(exc).__name__}).",
            ),
        }


def compose_governance_object(
    project_id: str,
    section: str,
    object_type: str,
    object_id: str,
    conn: Any,
    *,
    org_id: str,
) -> dict[str, Any]:
    """One exact governed object. Never a neighbour, never a collection."""
    project_id = (project_id or "").strip()
    object_id = (object_id or "").strip()
    if not project_id or not object_id:
        raise ValueError("project_id and object_id are required")
    contract = resolve_section(section)
    if contract.object_contract(object_type) is None:
        raise GovernanceUnknownRoute(f"unknown object type {object_type!r} for {section!r}")

    if section == "evidence":
        # Looked up by exact identity, NOT by scanning the lens. The collection
        # bound is a display bound: the 201st Evidence Record must still open.
        detail, pending = _load_evidence_object(conn, project_id, object_type, object_id)
    else:
        detail, pending = _load_object(conn, project_id, org_id, section, object_type, object_id)
    if detail is None and pending is None:
        raise GovernanceObjectNotFound(object_id)
    if detail is not None and section == "controls-quality":
        _enrich_controls_object(conn, project_id, object_type, object_id, detail)
    if detail is not None and section == "master-data":
        # Story 49.2: Hierarchy and Mappings & Aliases. Same locus and the same
        # reason as the two enrichments around it -- per-object, and composed
        # server-side so a half-failed browser fan-out cannot look authoritative.
        _enrich_master_data_object(
            conn, org_id, object_type, object_id, detail, project_id=project_id
        )
    if detail is not None and section == "semantic-model":
        # Composed HERE and not in the collection: the binding rows and the
        # compatibility matrix are per-object and would be O(n) work on a list.
        # They are composed server-side rather than fetched by the workbench,
        # because a browser fan-out across Data, Test and Context is
        # authoritative-looking and wrong the moment one of them fails.
        _enrich_semantic_object(conn, project_id, object_type, object_id, detail)
    return {
        "schema_version": SCHEMA_OBJECT,
        **_envelope_head(project_id, org_id, section),
        "object_type": object_type,
        "state": "available" if detail else "unavailable",
        "evidence_as_of": detail["evidence_as_of"] if detail else None,
        "object": detail,
        "unavailable_reasons": [] if detail else [pending],
    }


def compose_governance_object_version(
    project_id: str,
    section: str,
    object_type: str,
    object_id: str,
    version_id: str,
    conn: Any,
    *,
    org_id: str,
) -> dict[str, Any]:
    """One exact version of one exact object.

    A version that does not belong to this object is not found. It is never
    replaced by the current version, the previous one, or a last-known-good.
    """
    version_id = (version_id or "").strip()
    if not version_id:
        raise ValueError("version_id is required")
    contract = resolve_section(section)
    object_contract = contract.object_contract(object_type)
    if object_contract is None or not object_contract.supports_versions:
        raise GovernanceUnknownRoute(f"{object_type!r} declares no versions tab")

    envelope = compose_governance_object(
        project_id, section, object_type, object_id, conn, org_id=org_id
    )
    detail = envelope["object"]
    if detail is None:
        envelope["schema_version"] = SCHEMA_VERSION
        envelope["requested_version_id"] = version_id
        return envelope

    facet = detail["versions"]
    if facet["state"] != "available":
        raise GovernanceObjectNotFound(version_id)
    selected = next((ref for ref in facet["refs"] if str(ref.get("id")) == version_id), None)
    if selected is None:
        raise GovernanceObjectNotFound(version_id)

    detail["selected_version_ref"] = selected
    envelope["schema_version"] = SCHEMA_VERSION
    envelope["requested_version_id"] = version_id
    # An exact previous version is authorized and readable, and it is NOT current.
    # Saying so is the whole point of pinning it.
    envelope["version_state"] = "current" if selected.get("state") == "active" else "stale"
    return envelope
