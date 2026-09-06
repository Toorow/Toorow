"""Every declared Governance object kind, opened, against a real database.

WHY THIS FILE EXISTS. `docs/product-architecture/governance.md` carries the
criterion "a governed object lacks stable identity, version, owner, used-by or
evidence". It is a claim about EVERY governed object, and until this file nothing
in the repository walked more than one of them: `test_governance_read_model.py`
holds sixty tests over a `MagicMock` cursor fed hand-written row fixtures, so it
proves what the projection functions do with rows a test invented -- never that a
row the product's own writers produce reaches the workbench with the five fields
answered. `completeness-ledger.json` names the gap in those words: "a traversal
that opens one object of every declared kind and shows the five fields answered
or honestly absent on each -- not one object, and not a unit test over the read
model".

THREE PROPERTIES MAKE IT A TRAVERSAL RATHER THAN SIXTEEN TESTS.

1. The list of kinds is READ from `GOVERNANCE_SECTIONS`, never retyped. A
   seventeenth object type registered tomorrow fails this file until somebody
   seeds it, which is the only way a completeness claim can survive the next
   story.
2. Every object is seeded THROUGH THE PRODUCT'S OWN WRITERS wherever one exists
   -- `master_data.create_registry`, `control_cases.observe`,
   `dq_governance.publish_version`, `evidence_index.register_reference`. A
   fixture that INSERTs its own shape proves the projection can read that shape,
   which is a fact about the fixture. Four stores have no writer at all
   (`app.semantic_concepts`, `app.semantic_views`, `app.mdm_canonical_fields`
   and the classification vocabulary reach the database only through a change
   set or a migration); those are inserted directly and the comment says so.
3. Each object is opened at LEVEL 3 -- `compose_governance_object`, the function
   the object route calls -- and not read out of the collection it was found in.
   A collection item and an opened object are composed by different code paths
   in this module, and the workbench shows the second one.

WHAT "HONESTLY ABSENT" MEANS HERE, because the criterion allows it and a test
that demanded a count everywhere would force the exact defect AI-239 repaired.
A facet may say `available`, `empty` or `unavailable`. `empty` and `available`
are measurements -- the owner answered. `unavailable` means no owner answers the
question, and it is legitimate: `app.mdm_canonical_fields` has no version ledger
and nothing records which reader consumed a field id. What is refused is the
fourth state nobody may produce: a facet that is missing, `None`, or carries a
count while claiming to be unreadable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.integration.taxonomy_fixtures import (
    insert_classification_fixture,
    insert_domain_fixture,
)

pytestmark = pytest.mark.pg_owner

ACTOR = "owner@example.com"

#: The three facets that must each be answered or honestly absent, plus the two
#: scalar fields. Read from the envelope `_object_envelope` builds, so a facet
#: renamed on the server renames here too rather than silently stopping being
#: checked.
FACETS = ("used_by", "versions", "evidence")
FACET_STATES = {"available", "empty", "unavailable"}


def _mint(prefix: str) -> str:
    from ulid import ULID

    return f"{prefix}_{ULID()}"


@pytest.fixture()
def scope(live_postgres):
    """One organization, one Project, one Datastream -- the floor every kind needs."""
    suffix = uuid.uuid4().hex[:10]
    org_id = f"org_{suffix}"
    project_id = f"proj_{suffix}"
    datastream_id = f"ds_{suffix}"
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, f"Org {suffix}", f"org-{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, f"Project {suffix}", f"project-{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.datastreams (id, project_id, org_id, name, module_name, enabled) "
            "VALUES (%s,%s,%s,%s,'example_connector',TRUE)",
            (datastream_id, project_id, org_id, "Probe stream"),
        )
        cur.execute(
            "INSERT INTO app.project_flux (project_id, flux_id, org_id) VALUES (%s,%s,%s)",
            (project_id, datastream_id, org_id),
        )
    return {
        "suffix": suffix,
        "org_id": org_id,
        "project_id": project_id,
        "datastream_id": datastream_id,
    }


def _seed_master_data(conn, scope: dict[str, str]) -> None:
    """`business-domain`, `master-data-object`, `registry`, `tracked-entity`."""
    from core import master_data, tracked_entities
    from core.project_capability_states import CAPABILITY_AVAILABILITY

    suffix = scope["suffix"]
    org_id = scope["org_id"]
    project_id = scope["project_id"]

    # FIXTURE ROWS since 2026-08-25: the two writers that produced them refuse,
    # and what this file measures is the READ -- that a Level 3 object opens
    # under the lens it belongs to. A superseded source is readable forever, so
    # a row that predates the cutover is exactly the case under test.
    domain = insert_domain_fixture(
        conn,
        org_id=org_id,
        name=f"Probe domain {suffix}",
        slug=f"probe-domain-{suffix}",
        actor=ACTOR,
    )
    insert_classification_fixture(
        conn,
        org_id=org_id,
        domain_id=str(domain["id"]),
        parent_id=None,
        classification_type="segment",
        slug=f"probe-classification-{suffix}",
        name=f"Probe classification {suffix}",
        actor=ACTOR,
    )

    registry = master_data.create_registry(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind="product",
        label=f"Probe products {suffix}",
        actor=ACTOR,
        version_scope="node",
    )
    master_data.create_node(
        conn,
        org_id=org_id,
        project_id=project_id,
        registry_id=str(registry["id"]),
        node_kind="product",
        label=f"Probe product {suffix}",
        actor=ACTOR,
    )

    # Competitors: the lens answers `unavailable` with a reason for a Project
    # that never enabled it, so the capability is enabled here on purpose. The
    # tracked entity is the ONE governed object whose absence would otherwise be
    # indistinguishable from a disabled capability.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.project_capabilities "
            "(project_id, capability_key, availability, state) "
            "VALUES (%s,'competitors',%s,%s) "
            "ON CONFLICT (project_id, capability_key) DO UPDATE SET state = EXCLUDED.state",
            (project_id, CAPABILITY_AVAILABILITY["competitors"], "ready"),
        )
    tracked_entities.ensure_registry(conn, org_id=org_id, actor=ACTOR)
    tracked_entities.ensure_entity_type(conn, org_id=org_id, actor=ACTOR)
    entity = tracked_entities.create_entity(
        conn,
        org_id=org_id,
        entity_kind=tracked_entities.KIND_BRAND,
        preferred_label=f"Probe competitor {suffix}",
        actor=ACTOR,
    )
    tracked_entities.set_project_role(
        conn,
        org_id=org_id,
        project_id=project_id,
        node_id=str(entity["entity"]["id"]),
        role=tracked_entities.ROLE_COMPETITOR,
        actor=ACTOR,
    )


def _seed_semantic_model(conn, scope: dict[str, str]) -> None:
    """`semantic-concept`, `semantic-view`, `value-mapping-table`, `cleanup-rule`,
    `canonical-field`, `metric-definition`."""
    from core import cleanup_rules, value_mapping_tables
    from core.canonical_field_registry import declare_project_field

    suffix = scope["suffix"]
    org_id = scope["org_id"]
    project_id = scope["project_id"]

    # A Concept and a View reach the database only through a change set, whose
    # preparation reads mapping versions this Project has none of. Inserted
    # directly, in the exact published shape the change set writes -- the two
    # `CHECK` families on `app.semantic_concept_versions` (a metric needs an
    # expression, an aggregation and an additivity class) make a decorative row
    # unstorable, which is why a direct insert is still an honest one here.
    concept_id = _mint("sc")
    concept_version_id = _mint("scv")
    view_id = _mint("sv")
    view_version_id = _mint("svv")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_concepts "
            "(id, project_id, kind, name, lifecycle_status, created_by) "
            "VALUES (%s,%s,'metric',%s,'published',%s)",
            (concept_id, project_id, f"probe_metric_{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.semantic_concept_versions "
            "(id, concept_id, project_id, version_number, status, kind, name, label, "
            " value_type, expression, aggregation, additivity_class, content_hash, created_by) "
            "VALUES (%s,%s,%s,1,'published','metric',%s,%s,'decimal',"
            " '{\"op\":\"source_measure\"}'::jsonb, '{\"function\":\"sum\"}'::jsonb,"
            " 'additive',%s,%s)",
            (
                concept_version_id,
                concept_id,
                project_id,
                f"probe_metric_{suffix}",
                f"Probe metric {suffix}",
                "e" * 64,
                ACTOR,
            ),
        )
        cur.execute(
            "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
            (concept_version_id, concept_id),
        )
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
            "VALUES (%s,%s,%s,%s)",
            (view_id, project_id, f"probe_view_{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.semantic_view_versions "
            "(id, view_id, project_id, version_number, status, name, label, "
            " dependency_fingerprint, content_hash, created_by) "
            "VALUES (%s,%s,%s,1,'published',%s,%s,%s,%s,%s)",
            (
                view_version_id,
                view_id,
                project_id,
                f"probe_view_{suffix}",
                f"Probe view {suffix}",
                "f" * 64,
                "a" * 64,
                ACTOR,
            ),
        )
        cur.execute(
            "UPDATE app.semantic_views SET current_version_id = %s WHERE id = %s",
            (view_version_id, view_id),
        )
        # `metric-definition` is the lower declaring store, mutable-with-audit.
        cur.execute(
            "INSERT INTO app.metric_definitions "
            "(id, org_id, project_id, scope_level, canonical_name, display_name, "
            " aggregation_type, additive, created_by) "
            "VALUES (%s,%s,%s,'PROJECT',%s,%s,'SUM',TRUE,%s)",
            (
                _mint("mdf"),
                org_id,
                project_id,
                f"probe_definition_{suffix}",
                f"Probe definition {suffix}",
                ACTOR,
            ),
        )

    declare_project_field(
        conn,
        project_id=project_id,
        concept_kind="dimension",
        canonical_name=f"probe_dimension_{suffix}",
        value_type="string",
        actor=ACTOR,
    )
    value_mapping_tables.create_table(
        conn,
        org_id=org_id,
        project_id=project_id,
        scope_level="PROJECT",
        name=f"probe_table_{suffix}",
        description="Governance object traversal",
        identity=ACTOR,
    )
    cleanup_rules.create_rule(
        conn,
        org_id=org_id,
        project_id=project_id,
        datastream_id=None,
        name=f"probe_rule_{suffix}",
        source_field="campaign_name",
        rule_kind="strip_match",
        pattern="TEST_",
        identity=ACTOR,
        dry_run_state="not_attempted",
    )


def _seed_controls_quality(conn, scope: dict[str, str]) -> None:
    """`control-case`, `rule-set`, `dq-monitor` -- each through its own writer."""
    from core import control_cases, controls_quality, dq_governance, governance_rule_sets

    suffix = scope["suffix"]
    org_id = scope["org_id"]
    project_id = scope["project_id"]

    control_cases.observe(
        conn,
        org_id=org_id,
        project_id=project_id,
        case_type="mapping_conflict",
        subject_kind="datastream",
        subject_id=scope["datastream_id"],
        severity="degrading",
        observation=control_cases.CaseObservation(
            detected_by="governance-object-traversal",
            observation={"reason": "one case of every declared kind"},
        ),
        actor=ACTOR,
    )

    # The `dq_policy` family rather than `metric_reconciliation`: both produce a
    # `rule-set`, and this one's profile needs no Concept reference, so the
    # traversal does not smuggle a semantic dependency into a controls fixture.
    governance_rule_sets.load_profiles()
    head = governance_rule_sets.ensure_rule_set(
        conn,
        org_id=org_id,
        project_id=project_id,
        family=controls_quality.FAMILY_DQ,
        name=f"probe_rules_{suffix}",
        label=f"Probe rules {suffix}",
        actor=ACTOR,
    )
    draft = governance_rule_sets.draft_version(
        conn,
        project_id=project_id,
        rule_set_id=str(head["id"]),
        profile=controls_quality.PROFILE_DQ,
        payload={"check": "null_rate", "severity": "degrading", "window_days": 1},
        label=f"Probe rules {suffix} v1",
        actor=ACTOR,
    )
    governance_rule_sets.publish_version(
        conn,
        project_id=project_id,
        rule_set_id=str(head["id"]),
        version_id=str(draft["id"]),
        actor=ACTOR,
    )

    monitor = dq_governance.ensure_monitor(
        conn,
        org_id=org_id,
        project_id=project_id,
        name=f"probe_monitor_{suffix}",
        label=f"Probe monitor {suffix}",
        target_kind="datastream",
        target_id=scope["datastream_id"],
        actor=ACTOR,
    )
    dq_governance.publish_version(
        conn,
        project_id=project_id,
        monitor_id=str(monitor["id"]),
        check_profile="null_rate",
        severity="degrading",
        actor=ACTOR,
        parameters={"thresholds": {"null_rate": 0.0}},
        window_days=1,
    )


def _seed_evidence(conn, scope: dict[str, str]) -> None:
    """`evidence-trace`, `object-version`, `audit-event` -- one record per kind."""
    from core.evidence_index import RECORD_KINDS, ReferenceEvent, register_reference

    suffix = scope["suffix"]
    # Derived from the producer's own tuple, so a fourth record kind declared in
    # `evidence_index` is seeded here without this file being edited.
    for kind in RECORD_KINDS:
        register_reference(
            conn,
            org_id=scope["org_id"],
            project_id=scope["project_id"],
            event=ReferenceEvent(
                producer="governance_object_traversal",
                record_kind=kind,
                owner_workspace="governance",
                owner_object_type="datastream",
                owner_object_id=scope["datastream_id"],
                occurred_at=datetime.now(UTC),
                source_identity_key=f"traversal-{kind}-{suffix}",
                # `lineage-provenance` lists ANCHORS only -- "a trace IS its
                # anchor", `evidence_index._lens_where`. A non-anchor trace is
                # indexed and invisible to the lens, so `evidence-trace` would be
                # declared and unopenable from a fixture that looked complete.
                is_anchor=(kind == "evidence_trace"),
            ),
        )


@pytest.fixture()
def seeded(live_postgres, scope):
    """One governed object of every declared kind, in one Project."""
    _seed_master_data(live_postgres, scope)
    _seed_semantic_model(live_postgres, scope)
    _seed_controls_quality(live_postgres, scope)
    _seed_evidence(live_postgres, scope)
    return scope


def _declared_kinds() -> list[tuple[str, str]]:
    from core.governance_read_model import GOVERNANCE_SECTIONS

    return [
        (section, contract.object_type)
        for section, spec in GOVERNANCE_SECTIONS.items()
        for contract in spec.objects
    ]


def _walk_collections(conn, project_id: str, org_id: str) -> dict[str, tuple[str, str]]:
    """Every lens of every section, once. Returns the first object of each kind."""
    from core.governance_read_model import GOVERNANCE_SECTIONS, compose_governance_collection

    found: dict[str, tuple[str, str]] = {}
    for section, spec in GOVERNANCE_SECTIONS.items():
        for lens in spec.lenses:
            envelope = compose_governance_collection(
                project_id, section, conn, lens=lens, org_id=org_id
            )
            for item in envelope.get("items") or ():
                ref = item["object_ref"]
                found.setdefault(str(ref["type"]), (section, str(ref["id"])))
    return found


def _assert_facet(where: str, name: str, facet: Any) -> None:
    assert isinstance(facet, dict), f"{where}: `{name}` is {facet!r}, not a facet"
    state = facet.get("state")
    assert state in FACET_STATES, (
        f"{where}: `{name}` answered {state!r}. A governed facet says `available`, "
        f"`empty` or `unavailable`; anything else reaches a person as a fact "
        f"nobody measured."
    )
    if state == "unavailable":
        # AI-239's rule, asserted rather than trusted: "nobody could look" never
        # carries a number, because a number on screen reads as a measurement.
        #
        # ONE EXCEPTION, AND IT IS THE OPPOSITE DEFECT (49-2, 2026-09-01). A facet
        # that COUNTED its references and could not list them is `unavailable`
        # with its count on purpose: three consumers and no list is not "nobody
        # could look", and serving it as `available` with an empty list is what
        # made the workbench answer "nothing depends on this object". Such a facet
        # says so by name, and only that reason licenses a number here.
        reason = facet.get("reason") or {}
        counted_but_unlisted = reason.get("code") == "facet_refs_not_composed"
        assert counted_but_unlisted or facet.get("count") in (None, 0), (
            f"{where}: `{name}` is `unavailable` and still carries "
            f"count={facet.get('count')!r} -- an unread store rendering as a count"
        )
        if counted_but_unlisted:
            assert facet.get("refs") == [], (
                f"{where}: `{name}` claims its references were not composed and "
                f"carries some anyway"
            )


@pytest.mark.integration
def test_every_declared_object_kind_is_reachable_from_its_own_lens(live_postgres, seeded):
    """A kind no lens produces cannot be opened at all -- `_load_object` scans lenses.

    This is the half a per-type test cannot state: the failure names the kinds
    that are declared and unreachable, which is exactly what "a governed object
    lacks stable identity" looks like from the console.
    """
    found = _walk_collections(live_postgres, seeded["project_id"], seeded["org_id"])
    missing = sorted(
        f"{section}/{object_type}"
        for section, object_type in _declared_kinds()
        if object_type not in found
    )
    assert not missing, (
        "declared in GOVERNANCE_SECTIONS and produced by no lens: "
        + ", ".join(missing)
    )


@pytest.mark.integration
def test_every_declared_object_kind_answers_the_five_fields(live_postgres, seeded):
    """The criterion, walked: identity, version, owner, used-by and evidence.

    Opened one by one through `compose_governance_object` -- the function the
    object route calls -- so the assertion is about the workbench's own
    composition and not about the row the collection happened to carry.
    """
    from core.governance_read_model import compose_governance_object

    found = _walk_collections(live_postgres, seeded["project_id"], seeded["org_id"])
    walked: list[str] = []
    for section, object_type in _declared_kinds():
        located = found.get(object_type)
        assert located is not None, f"{section}/{object_type} was not produced by any lens"
        _, object_id = located
        envelope = compose_governance_object(
            seeded["project_id"],
            section,
            object_type,
            object_id,
            live_postgres,
            org_id=seeded["org_id"],
        )
        where = f"{section}/{object_type}"
        assert envelope["state"] == "available", f"{where}: opened as {envelope['state']!r}"
        detail = envelope["object"]

        # 1. Stable identity -- the id the route was called with, never a label
        #    and never a position in a list.
        ref = detail["object_ref"]
        assert str(ref["id"]) == object_id, f"{where}: opened id {ref['id']!r}, asked {object_id!r}"
        assert str(ref["type"]) == object_type
        assert str(ref.get("label") or "").strip(), f"{where}: no label"
        assert ref.get("owner_href"), f"{where}: no route back to the owner"

        # 2. Version -- an active version reference, or a `versions` facet that
        #    says why there is none. A type with no version ledger is honest;
        #    a type with one and no pointer is the defect.
        assert "active_version_ref" in detail, f"{where}: `active_version_ref` absent"

        # 3. Owner -- who answers for this object.
        owner = detail.get("owner")
        assert isinstance(owner, dict) and owner, f"{where}: no owner"
        assert str(detail.get("lifecycle_status") or "").strip(), f"{where}: no lifecycle status"

        # 4/5. Used-by and evidence, plus versions, each answered or honestly absent.
        for facet in FACETS:
            _assert_facet(where, facet, detail.get(facet))

        walked.append(where)

    assert sorted(walked) == sorted(
        f"{section}/{object_type}" for section, object_type in _declared_kinds()
    )


@pytest.mark.integration
def test_the_traversal_covers_every_kind_the_route_registry_declares(live_postgres, seeded):
    """The list is READ, never retyped, and it is not empty.

    Two failures this closes at once: a traversal that silently walks nothing
    (green over an empty registry), and one that walks a hand-written list which
    stops matching the registry the day a lens is added.
    """
    from core.governance_read_model import GOVERNANCE_SECTIONS

    declared = _declared_kinds()
    assert declared, "GOVERNANCE_SECTIONS declares no object type"
    assert len(declared) == sum(len(spec.objects) for spec in GOVERNANCE_SECTIONS.values())
    found = _walk_collections(live_postgres, seeded["project_id"], seeded["org_id"])
    assert len(found) >= len(declared)


# ---------------------------------------------------------------------------
# An archived client-object instance keeps its address (2026-08-31).
# ---------------------------------------------------------------------------


def _products(conn, seeded: dict[str, str]) -> list[str]:
    """The ids the `products` lens lists, in the order it lists them.

    The lens by NAME, not `_walk_collections`: three lenses of this section
    compose a `master-data-object`, and the first of them is the classification
    vocabulary -- an organization identity with no node, which is a different
    object under the same type word.
    """
    from core.governance_read_model import compose_governance_collection

    listed = compose_governance_collection(
        seeded["project_id"], "master-data", conn, lens="products", org_id=seeded["org_id"]
    )
    return [str(item["object_ref"]["id"]) for item in (listed.get("items") or ())]


def _product_instance(conn, seeded: dict[str, str]) -> str:
    ids = _products(conn, seeded)
    assert ids, "the products lens produced no instance to archive"
    return ids[0]


def _open(conn, seeded: dict[str, str], object_id: str) -> dict[str, Any]:
    from core.governance_read_model import compose_governance_object

    return compose_governance_object(
        seeded["project_id"],
        "master-data",
        "master-data-object",
        object_id,
        conn,
        org_id=seeded["org_id"],
    )


@pytest.mark.integration
def test_an_archived_instance_keeps_its_address_and_can_be_restored(live_postgres, seeded):
    """ARCHIVE -> STILL ADDRESSABLE -> RESTORE, on the route the console calls.

    MEASURED 2026-08-31: `_CLIENT_OBJECT_INSTANCES` joined `AND n.archived_at IS
    NULL`, and `_load_object` resolves an instance by scanning its own lens. So
    archiving a Product removed its ADDRESS: the workbench answered "not found",
    and the Restore control the Overview draws for an archived identity could
    never be reached from anywhere. Retiring an instance was one way.

    MUTATION: put the `archived_at IS NULL` back on that join -> the second
    `_open` below answers `not_found` and this is red.
    """
    from core.master_data_commands import run_node_command

    conn = live_postgres
    object_id = _product_instance(conn, seeded)
    assert _open(conn, seeded, object_id)["object"]["lifecycle_status"] != "archived"

    run_node_command(
        conn,
        project_id=seeded["project_id"],
        org_id=seeded["org_id"],
        node_id=object_id,
        action="archive",
        actor=ACTOR,
        idempotency_key=f"arch_{uuid.uuid4().hex}",
    )
    conn.commit()

    archived = _open(conn, seeded, object_id)
    assert archived["state"] == "available", (
        "an archived instance answered "
        f"{archived['state']!r}: it has no address, so Restore is unreachable"
    )
    # It says WHAT it is, in the one word every other Master Data kind uses, so
    # `MasterDataIdentityOverview` draws Restore instead of rename and archive.
    assert archived["object"]["lifecycle_status"] == "archived"
    assert archived["object"]["summary"]["archived_at"]

    # And it is still LISTED, because an object hidden from its own collection is
    # findable only by someone who kept the id.
    assert object_id in _products(conn, seeded)

    run_node_command(
        conn,
        project_id=seeded["project_id"],
        org_id=seeded["org_id"],
        node_id=object_id,
        action="restore",
        actor=ACTOR,
        idempotency_key=f"rest_{uuid.uuid4().hex}",
    )
    conn.commit()

    assert _open(conn, seeded, object_id)["object"]["lifecycle_status"] != "archived"
