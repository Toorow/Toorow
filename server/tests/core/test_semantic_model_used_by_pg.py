"""The archive guard of the authority reads what the publisher wrote.

WHAT WAS OPEN. The cutover of 2026-08-25 moved every Business Domain write into
Master Data and wrote the hole it left into the *Incomplete if* of
``docs/product-architecture/governance.md``:

    a converged Business Domain can be archived while a published Semantic Model
    version still references it. The taxonomy writer refused that; the
    authority's impact guard reads ``app.master_data_used_by``, and no writer
    registers a Semantic Model version's Business Domain reference there yet.

WHY THIS FILE IS pg-GATED AND HAS NO MOCK TWIN. Every property below is a
property of the DATABASE and of the real publication path:

* an organization-scoped node has ``project_id IS NULL``, and until migration 311
  the used-by table's composite foreign keys made a row naming one IMPOSSIBLE --
  a mock cursor accepts any INSERT and would have proved nothing;
* the reference is written inside ``confirm_change_set``'s transaction, through
  ``create_change_set -> prepare_change_set -> confirm_change_set``, which is the
  walk a person makes;
* a supersession must release the version it replaced and NOT the one just
  declared, and the two versions name the same domain -- an ordering defect that
  only shows against real rows.

THE MUTATION. ``test_without_the_writer_the_domain_archives_under_the_view``
switches the writer off and walks the same path: the archive then SUCCEEDS while
a published view still names the domain. That is the defect the *Incomplete if*
describes, reproduced on purpose, so the green of the tests above it means the
writer and not the fixture.
"""

from __future__ import annotations

import pytest

# The pg gate, the id minter, the org/Project fixture and the Test-gate seeder
# are the ones story 60.2's walk already established. Imported rather than
# retyped: a second copy of `_record_test_gate` would be a second answer to
# "what evidence does Governance accept", which is the class of defect this
# repository keeps paying for.
from tests.core.test_semantic_model import (  # noqa: F401 -- fixture_project is a fixture
    _RATIO_CONCEPT,
    _publish_concept,
    _record_test_gate,
    _uid,
    fixture_project,
    pg_available,
)

ACTOR = "owner@example.com"
ARCHIVE_REASON = "The view is retired; its Business Domain reference goes with it."

#: The one dataset every View below declares. Its content is irrelevant to what
#: is measured here -- the compiler only asks that a member sit on a dataset the
#: View names -- and it is a single constant so the three versions of the
#: supersession test cannot drift apart on it.
DATASET = {"name": "used_by_facts", "source": "mirror.used_by_facts"}


@pytest.fixture
def project(fixture_project):  # noqa: F811 -- the imported fixture is this one's argument
    """`fixture_project`, plus the cleanup its own eraser cannot perform.

    `purge_org_tree` deletes `app.semantic_concepts` before
    `app.semantic_view_version_concepts`, whose foreign key carries no
    `ON DELETE CASCADE` -- so a Project where a View pins a Concept cannot be
    erased. That is a defect of the ERASER, already reported by
    `test_semantic_model._drop_holding_view` and not repaired here; removing the
    pin at teardown keeps the finding visible instead of turning every test in
    this file red for a reason that has nothing to do with its subject.
    """

    from core.db import get_connection

    yield fixture_project
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.semantic_view_version_concepts WHERE view_version_id IN "
                "(SELECT id FROM app.semantic_view_versions WHERE project_id = %s)",
                (fixture_project["project_id"],),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def _domain_ids(conn, org_id: str) -> list[str]:
    """The Business Domains migration 130 seeds on the organization trigger."""

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.mdm_business_domains WHERE org_id = %s ORDER BY id",
            (org_id,),
        )
        return [str(row[0]) for row in cur.fetchall()]


def _converge(conn, org_id: str, project_id: str) -> dict:
    from core.master_data_convergence import converge_org_taxonomy

    return converge_org_taxonomy(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor=ACTOR,
        idempotency_key=_uid("conv"),
        reason="the authority owns the identities",
    )


def _a_metric(conn, fixture) -> dict:
    """One published metric, because a View that measures nothing is refused.

    `no_metrics_selected` -- *"publishing a View nobody can measure anything with
    would be publishing an empty promise"*. The subject here is the Business
    Domain reference, so the metric is the smallest one the contract accepts,
    published through the same walk rather than typed into the table.
    """

    concept_id, version_id = _publish_concept(conn, fixture, _RATIO_CONCEPT)
    return {
        "concept_id": concept_id,
        "concept_version_id": version_id,
        "role": "metric",
        # The View declares where its member lives; `DATASET` below declares it
        # to the compiler. A member sitting on a dataset the View does not name
        # is `unknown_dataset`, and rightly so.
        "dataset": DATASET["name"],
    }


def _publish_view(
    conn,
    fixture,
    *,
    name: str,
    domain_refs: list[str],
    member: dict,
    object_id: str | None = None,
    base_version_id: str | None = None,
) -> str:
    from core.semantic_model import (
        confirm_change_set,
        create_change_set,
        prepare_change_set,
    )

    org_id, project_id = fixture["org_id"], fixture["project_id"]
    change_set = create_change_set(
        conn,
        project_id,
        actor=ACTOR,
        object_type="semantic-view",
        object_id=object_id,
        base_version_id=base_version_id,
        intent={
            "action": "create_view" if object_id is None else "edit_view",
            "view": {
                "name": name,
                "label": name,
                "business_domain_refs": domain_refs,
                "concepts": [member],
                "datasets": [DATASET],
            },
        },
        idempotency_key=_uid("idem"),
    )
    conn.commit()
    _record_test_gate(
        conn, org_id, project_id, change_set.id, object_type="semantic-view"
    )
    prepared = prepare_change_set(conn, project_id, change_set.id, actor=ACTOR)
    conn.commit()
    assert prepared["refusals"] == [], prepared["refusals"]
    result = confirm_change_set(
        conn,
        project_id,
        change_set.id,
        actor=ACTOR,
        confirmation_token=prepared["confirmation_token"],
        org_id=org_id,
    )
    conn.commit()
    return str(result["result_version_id"])


def _view_id_of(conn, version_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT view_id FROM app.semantic_view_versions WHERE id = %s", (version_id,)
        )
        return str(cur.fetchone()[0])


def _retire_view(conn, fixture, view_id: str, version_id: str) -> None:
    from core.semantic_model import (
        confirm_change_set,
        create_change_set,
        prepare_change_set,
    )

    project_id = fixture["project_id"]
    change_set = create_change_set(
        conn,
        project_id,
        actor=ACTOR,
        object_type="semantic-view",
        object_id=view_id,
        base_version_id=version_id,
        intent={"action": "archive_object"},
        idempotency_key=_uid("idem"),
    )
    conn.commit()
    prepared = prepare_change_set(
        conn,
        project_id,
        change_set.id,
        actor=ACTOR,
        allow_test_override={"reason": ARCHIVE_REASON},
    )
    conn.commit()
    assert prepared["validation"]["publishable"] is True, prepared["validation"]
    confirm_change_set(
        conn,
        project_id,
        change_set.id,
        actor=ACTOR,
        confirmation_token=prepared["confirmation_token"],
        org_id=fixture["org_id"],
    )
    conn.commit()


def _used_by_rows(conn, node_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT consumer_kind, consumer_id, consumer_version_id, consumer_label, "
            "released_at FROM app.master_data_used_by WHERE node_id = %s "
            "ORDER BY consumer_version_id",
            (node_id,),
        )
        return list(cur.fetchall())


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


@pg_available
def test_a_published_view_registers_the_domain_it_names_and_the_archive_refuses(
    project,
):
    from core.db import get_connection
    from core.master_data import (
        MasterDataConflict,
        archive_org_node_guarded,
        assess_org_node_impact,
    )

    org_id = project["org_id"]
    with get_connection() as conn:
        _converge(conn, org_id, project["project_id"])
        domain_id = _domain_ids(conn, org_id)[0]

        # Before the view exists, nothing depends on the identity and the
        # authority says so by letting it be archived. This is the counter-pin:
        # without it, a refusal below could come from anywhere.
        assert assess_org_node_impact(conn, org_id=org_id, node_id=domain_id).is_clear

        member = _a_metric(conn, project)
        version_id = _publish_view(
            conn,
            project,
            name=f"used_by_walk_{_uid('v')[-8:]}",
            domain_refs=[domain_id],
            member=member,
        )

        rows = _used_by_rows(conn, domain_id)
        assert len(rows) == 1
        kind, consumer_id, consumer_version, label, released_at = rows[0]
        assert kind == "semantic-view"
        assert consumer_version == version_id
        assert released_at is None
        # The label is the GOVERNED node's, so the refusal names the identity the
        # operator sees rather than an id.
        assert label

        impact = assess_org_node_impact(conn, org_id=org_id, node_id=domain_id)
        assert impact.describe() == "semantic-view (1)"

        with pytest.raises(MasterDataConflict) as excinfo:
            archive_org_node_guarded(conn, org_id=org_id, node_id=domain_id)
        conn.rollback()

    message = str(excinfo.value)
    assert "semantic-view (1)" in message
    assert domain_id in message


@pg_available
def test_retiring_the_view_releases_the_reference_and_the_archive_then_passes(
    project,
):
    """The repair path the refusal promises actually exists.

    A guard that refuses and names no way through is a wall. Retiring the view is
    that way: the row is RELEASED, not deleted -- the history of who depended on
    the identity survives the release -- and the next archive attempt passes.
    """

    from core.db import get_connection
    from core.master_data import archive_org_node_guarded, assess_org_node_impact

    org_id = project["org_id"]
    with get_connection() as conn:
        _converge(conn, org_id, project["project_id"])
        domain_id = _domain_ids(conn, org_id)[0]
        member = _a_metric(conn, project)
        version_id = _publish_view(
            conn,
            project,
            name=f"used_by_retire_{_uid('v')[-8:]}",
            domain_refs=[domain_id],
            member=member,
        )
        view_id = _view_id_of(conn, version_id)
        assert not assess_org_node_impact(conn, org_id=org_id, node_id=domain_id).is_clear

        _retire_view(conn, project, view_id, version_id)

        rows = _used_by_rows(conn, domain_id)
        # Released, never deleted: the row is still there and now says when it
        # stopped holding.
        assert len(rows) == 1
        assert rows[0][4] is not None

        assert assess_org_node_impact(conn, org_id=org_id, node_id=domain_id).is_clear
        node = archive_org_node_guarded(conn, org_id=org_id, node_id=domain_id)
        assert node["archived_at"] is not None
        conn.rollback()


@pg_available
def test_a_new_version_that_drops_the_reference_releases_only_the_one_it_replaced(
    project,
):
    """Supersession follows, and the order it follows in is the load-bearing part.

    The replaced version and the replacing one routinely name the SAME domain, so
    a release scoped by consumer alone -- or performed before the registration --
    would mark the dependency that was just declared as gone. Two edits prove the
    two directions: version 2 keeps the domain (one live row, the new version's),
    version 3 drops it (no live row at all).
    """

    from core.db import get_connection
    from core.master_data import assess_org_node_impact

    org_id = project["org_id"]
    with get_connection() as conn:
        _converge(conn, org_id, project["project_id"])
        domain_id = _domain_ids(conn, org_id)[0]
        name = f"used_by_super_{_uid('v')[-8:]}"
        member = _a_metric(conn, project)

        v1 = _publish_view(
            conn, project, name=name, domain_refs=[domain_id], member=member
        )
        view_id = _view_id_of(conn, v1)

        v2 = _publish_view(
            conn,
            project,
            name=name,
            domain_refs=[domain_id],
            member=member,
            object_id=view_id,
            base_version_id=v1,
        )
        live = [row for row in _used_by_rows(conn, domain_id) if row[4] is None]
        assert [row[2] for row in live] == [v2], "the version just published holds"
        assert not assess_org_node_impact(conn, org_id=org_id, node_id=domain_id).is_clear

        _publish_view(
            conn,
            project,
            name=name,
            domain_refs=[],
            member=member,
            object_id=view_id,
            base_version_id=v2,
        )
        live = [row for row in _used_by_rows(conn, domain_id) if row[4] is None]
        assert live == [], "the reference was dropped, so nothing holds any more"
        assert assess_org_node_impact(conn, org_id=org_id, node_id=domain_id).is_clear
        conn.rollback()


@pg_available
def test_a_reference_published_before_the_convergence_enters_with_it(project):
    """The backfill, and it is the convergence rather than a script.

    Measured on production 2026-08-25: 12 Business Domains, 0 governed nodes. So
    the normal case is a reference published against a domain the authority does
    not own yet -- it is registered against nothing, counted, and logged. The
    moment the convergence mints the node is the moment it becomes registrable,
    and the pass runs there, in the same transaction.
    """

    from core.db import get_connection
    from core.master_data import assess_org_node_impact

    org_id = project["org_id"]
    with get_connection() as conn:
        domain_id = _domain_ids(conn, org_id)[0]

        member = _a_metric(conn, project)
        version_id = _publish_view(
            conn,
            project,
            name=f"used_by_backfill_{_uid('v')[-8:]}",
            domain_refs=[domain_id],
            member=member,
        )
        # Nothing governs the identity yet, so there is nothing to guard and
        # nothing was written. The publication was not refused over it.
        assert _used_by_rows(conn, domain_id) == []

        outcome = _converge(conn, org_id, project["project_id"])
        result = outcome["result"]
        assert result["registered_semantic_references"] == 1
        assert result["unregistered_semantic_references"] == 0

        rows = _used_by_rows(conn, domain_id)
        assert [row[2] for row in rows] == [version_id]
        assert not assess_org_node_impact(conn, org_id=org_id, node_id=domain_id).is_clear

        # Run it again: a replay registers the same row onto itself rather than
        # minting a second one. `register_used_by` upserts on
        # (project, node, consumer_kind, consumer_id, consumer_version_id), and
        # the scope trigger migration 311 installs fires on that UPDATE too.
        replay = _converge(conn, org_id, project["project_id"])["result"]
        assert replay["converged_domains"] == 0, "nothing left to converge"
        assert replay["registered_semantic_references"] == 1
        assert [row[2] for row in _used_by_rows(conn, domain_id)] == [version_id]
        conn.rollback()


@pg_available
def test_without_the_writer_the_domain_archives_under_the_view(
    project, monkeypatch
):
    """THE MUTATION. Switch the writer off and the *Incomplete if* comes back.

    ``semantic_model._record_business_domain_dependencies`` imports the writer at
    call time, so replacing it here replaces what the publication actually calls.
    The walk is identical to the first test; the only difference in the world is
    that nothing is registered -- and the archive, which refused there, succeeds
    here under a view that still names the domain.
    """

    from core.db import get_connection
    from core.master_data import archive_org_node_guarded

    monkeypatch.setattr(
        "core.semantic_model_used_by.register_published_version",
        lambda *args, **kwargs: {"registered": 0, "unregistered": 0, "released": 0},
    )

    org_id = project["org_id"]
    with get_connection() as conn:
        _converge(conn, org_id, project["project_id"])
        domain_id = _domain_ids(conn, org_id)[0]
        member = _a_metric(conn, project)
        version_id = _publish_view(
            conn,
            project,
            name=f"used_by_mutant_{_uid('v')[-8:]}",
            domain_refs=[domain_id],
            member=member,
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT business_domain_refs FROM app.semantic_view_versions WHERE id = %s",
                (version_id,),
            )
            # The version really does name the domain: the mutation removed the
            # REGISTRATION, not the reference.
            assert list(cur.fetchone()[0]) == [domain_id]

        assert _used_by_rows(conn, domain_id) == []
        node = archive_org_node_guarded(conn, org_id=org_id, node_id=domain_id)
        assert node["archived_at"] is not None, (
            "with no writer the authority archives a domain a published view names "
            "-- the defect governance.md carries in its Incomplete if"
        )
        conn.rollback()
