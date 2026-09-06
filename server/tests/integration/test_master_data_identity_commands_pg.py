"""Creating, renaming and archiving a business identity IN the authority.

WHY THIS FILE EXISTS. The cutover ratified on 2026-08-25 closed the four legacy
identity writers with one sentence -- *converge this organization, then create,
rename or archive it in Master Data* -- and the second half of that sentence
pointed at nothing: `master_data_commands.SUPPORTED_ACTIONS` was `archive` and
`restore`, so a CONVERGED organization could no longer declare a Business Domain
anywhere at all. This suite walks the gesture that closes it.

WHY IT IS pg-GATED. Every property under test is a property of the DATABASE:

* the created node lands in the SAME registry the convergence targets, which is
  a row `uq_master_data_registries_org_kind` allows exactly one of;
* the minted id satisfies `master_data_nodes_id_check` (migration 307), so the
  projection row and the node share one identity;
* the membership edge belongs to the CHILD's version, enforced by
  `uq_master_data_membership_open_node_any_scope`;
* the archive guard reads three stores, one of which is a trigger-protected link
  table (migration 306).

A cursor that answers whatever a fixture tells it to proves none of them.
"""

from __future__ import annotations

import uuid

import pytest

from tests.integration.taxonomy_fixtures import insert_domain_fixture

pytestmark = pytest.mark.pg_owner

ACTOR = "owner@example.com"


@pytest.fixture()
def scope(live_postgres):
    """One organization and one Project, with the six domains the trigger seeds."""
    suffix = uuid.uuid4().hex[:10]
    org_id = f"org_{suffix}"
    project_id = f"proj_{suffix}"
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
    live_postgres.commit()
    return {"suffix": suffix, "org_id": org_id, "project_id": project_id}


@pytest.fixture()
def converged(live_postgres, scope):
    """The same organization, after the ratified operator gesture has run."""
    from core.master_data_convergence import converge_org_taxonomy

    converge_org_taxonomy(
        live_postgres,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        actor=ACTOR,
        idempotency_key=f"conv_{uuid.uuid4().hex}",
        reason="AC1: one authority",
    )
    live_postgres.commit()
    return scope


def _create(conn, scope, **kwargs):
    from core.master_data_commands import create_business_identity

    payload = {
        "kind": "business_domain",
        "name": "Retail Media",
        "actor": ACTOR,
        "idempotency_key": f"mdc_{uuid.uuid4().hex}",
        "reason": "the organization opened a new line",
    }
    payload.update(kwargs)
    return create_business_identity(
        conn, project_id=scope["project_id"], org_id=scope["org_id"], **payload
    )


def _base(conn, scope, node_id: str):
    """The version identity a reader of this object sees right now.

    Exactly what the console's two rename doors send back: the id of the
    authority's current published revision (`governance.md`, 2026-08-30).
    """
    from core.master_data import current_node_version_id

    return current_node_version_id(conn, org_id=scope["org_id"], node_id=node_id)


def _command(conn, scope, node_id: str, action: str, **kwargs):
    from core.master_data_commands import run_node_command

    return run_node_command(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        node_id=node_id,
        action=action,
        actor=ACTOR,
        idempotency_key=kwargs.pop("idempotency_key", f"mdn_{uuid.uuid4().hex}"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Creating.
# ---------------------------------------------------------------------------


def test_a_created_domain_lands_where_the_convergence_lands(live_postgres, converged):
    """One home per identity kind, whichever door the identity came through.

    A creation that minted its own registry would give an organization two
    Business Domain collections -- the exact second authority story 49.2 exists
    to remove -- and the Governance lens would list one of them.
    """
    from core import master_data

    conn = live_postgres
    scope = converged
    registry_before = master_data.require_org_registry(
        conn, org_id=scope["org_id"], object_kind="business_domain"
    )

    out = _create(conn, scope, name="Retail Media", slug=f"retail-media-{scope['suffix']}")
    node_id = out["result"]["node_id"]

    assert out["result"]["registry_id"] == str(registry_before["id"])
    node = master_data.fetch_org_node(conn, org_id=scope["org_id"], node_id=node_id)
    assert node is not None
    assert node["node_kind"] == "business_domain"
    assert node["project_id"] is None, "a Business Domain is an organization identity"

    versions = master_data.list_node_versions(
        conn, org_id=scope["org_id"], node_id=node_id
    )
    assert len(versions) == 1
    assert versions[0]["status"] == "current", "an unpublished identity is unreadable"
    assert versions[0]["payload"]["name"] == "Retail Media"
    assert versions[0]["payload"]["lifecycle_state"] == "active"


def test_a_created_domain_is_visible_to_every_reader_of_the_superseded_store(
    live_postgres, converged
):
    """MUTATION: drop `write_superseded_projection` from the command -> red.

    Fourteen non-test readers resolve a Business Domain through
    `app.mdm_business_domains` -- both Governance lenses, the Context Hub,
    `golden_questions`, `project_settings` applicability, the Datastream
    workbench, `semantic_model`'s reference guard. An identity minted only in
    `app.master_data_nodes` would be invisible to all of them: a creation
    gesture producing an object no screen can show.

    The row is born SUPERSEDED, naming the node that holds its authority and the
    operation that minted it -- character for character the state a converged row
    is left in, which is what makes the two indistinguishable to a reader.
    """
    from core import business_taxonomy

    conn = live_postgres
    scope = converged
    out = _create(conn, scope, name="Retail Media", slug=f"retail-media-{scope['suffix']}")
    node_id = out["result"]["node_id"]

    taxonomy = business_taxonomy.list_taxonomy(conn, org_id=scope["org_id"])
    assert node_id in {str(row["id"]) for row in taxonomy["domains"]}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT slug, name, status, superseded_at, superseded_by_node_id, "
            "       superseded_by_operation_id "
            "FROM app.mdm_business_domains WHERE id = %s",
            (node_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[2] == "active"
    assert row[3] is not None, "born superseded: the authority is the node"
    assert row[4] == node_id
    assert row[5] == out["operation_id"]


def test_a_created_domain_can_receive_a_business_link(live_postgres, converged):
    """The gesture that follows creation has to work, or the identity is inert.

    `business_taxonomy.create_link` resolves its taxonomy row through
    `get_domain`, so a domain the superseded store does not carry cannot be
    linked to anything -- and linking is what a Business Domain is FOR.
    """
    from core import business_taxonomy

    conn = live_postgres
    scope = converged
    out = _create(conn, scope, name="Retail Media", slug=f"retail-media-{scope['suffix']}")
    node_id = out["result"]["node_id"]

    with conn.cursor() as cur:
        cur.execute("SELECT name FROM app.target_fields WHERE status <> 'deleted' LIMIT 1")
        target = cur.fetchone()[0]

    link = business_taxonomy.create_link(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        taxonomy_type="business_domain",
        taxonomy_id=node_id,
        target_type="target_field",
        target_id=target,
        relation_type="explains",
        actor=ACTOR,
        reason="the new line explains this field",
    )
    assert link["taxonomy_id"] == node_id


def test_an_unconverged_organization_is_refused_and_writes_nothing(live_postgres, scope):
    """The ratified order, at the threshold and not only on the screen."""
    from core.master_data_commands import CONVERGE_FIRST_CODE, MasterDataCommandRefused

    conn = live_postgres
    with pytest.raises(MasterDataCommandRefused) as refused:
        _create(conn, scope, name="Retail Media", slug=f"retail-media-{scope['suffix']}")
    assert refused.value.as_dict()["code"] == CONVERGE_FIRST_CODE

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.master_data_nodes WHERE org_id = %s", (scope["org_id"],)
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM app.operations WHERE effective_org_id = %s",
            (scope["org_id"],),
        )
        assert cur.fetchone()[0] == 0, "a refusal is not an act that happened"


@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        ("rename", {"label": "Renamed here", "expected_version": None}),
        ("archive", {}),
        ("restore", {}),
    ],
)
def test_a_command_on_an_unconverged_organization_names_the_convergence(
    live_postgres, scope, action, kwargs
):
    """THE SIX SEEDED DOMAINS ARE ADDRESSABLE, AND THE ANSWER IS NOT A 404.

    MEASURED 2026-08-31 before this test existed: `_require_converged` had ONE
    call site -- the create door -- so a rename, an archive or a restore aimed at
    one of the six Business Domains the Governance lens LISTS for an unconverged
    organization found no node and raised `MasterDataNotFound`, which the surface
    answers as **404 "Governance object not found"**. The screen showed the
    object; the command denied it existed; and the refusal named no gesture.

    MUTATION: remove the `_require_converged` call before the `MasterDataNotFound`
    in `run_node_command` -> `MasterDataNotFound` here and this test is red.
    """
    from core.master_data_commands import CONVERGE_FIRST_CODE, MasterDataCommandRefused

    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.mdm_business_domains WHERE org_id = %s ORDER BY id LIMIT 1",
            (scope["org_id"],),
        )
        row = cur.fetchone()
    assert row is not None, "migration 130 seeds this organization's six Business Domains"
    listed_node_id = str(row[0])

    with pytest.raises(MasterDataCommandRefused) as refused:
        _command(conn, scope, listed_node_id, action, **kwargs)

    answer = refused.value.as_dict()
    assert answer["code"] == CONVERGE_FIRST_CODE
    # The sentence names the two gestures, in order, and no store.
    assert "convergence" in answer["message"].lower()
    # The plan travels with the refusal, so the screen can say HOW MUCH is left.
    assert answer["impact"]["pending"]["pending_domains"] >= 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.operations WHERE effective_org_id = %s",
            (scope["org_id"],),
        )
        assert cur.fetchone()[0] == 0, "a refusal is not an act that happened"


def test_an_organization_that_falls_back_out_of_convergence_refuses_a_rename(
    live_postgres, converged
):
    """ONE AUTHORITY OR NONE, and the gate is the same one the create door holds.

    A legacy row written after the convergence puts the organization back in the
    state the ratified order forbids writing in. The rename is then refused with
    the convergence rather than performed against half a taxonomy.
    """
    from core.master_data_commands import CONVERGE_FIRST_CODE, MasterDataCommandRefused

    conn = live_postgres
    scope = converged
    created = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")
    node_id = created["result"]["node_id"]
    base = _base(conn, scope, node_id)

    insert_domain_fixture(
        conn,
        org_id=scope["org_id"],
        slug=f"late-{scope['suffix']}",
        name="Arrived after the convergence",
        actor=ACTOR,
    )
    conn.commit()

    with pytest.raises(MasterDataCommandRefused) as refused:
        _command(conn, scope, node_id, "rename", label="Anything", expected_version=base)
    assert refused.value.as_dict()["code"] == CONVERGE_FIRST_CODE


def test_a_short_code_already_in_use_is_refused(live_postgres, converged):
    from core.master_data_commands import DUPLICATE_SLUG_CODE, MasterDataCommandRefused

    conn = live_postgres
    scope = converged
    slug = f"retail-media-{scope['suffix']}"
    _create(conn, scope, name="Retail Media", slug=slug)

    with pytest.raises(MasterDataCommandRefused) as refused:
        _create(conn, scope, name="Retail Media Again", slug=slug)
    assert refused.value.as_dict()["code"] == DUPLICATE_SLUG_CODE


def test_a_created_classification_hangs_off_its_domain_and_carries_its_type(
    live_postgres, converged
):
    """The edge is on the CHILD's version, and the word the organization chose
    is carried rather than read as a Product or an Activity."""
    from core import master_data

    conn = live_postgres
    scope = converged
    domain = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")
    domain_id = domain["result"]["node_id"]

    out = _create(
        conn,
        scope,
        kind="business_classification",
        name="Sponsored Products",
        slug=f"sponsored-{scope['suffix']}",
        classification_type="product_line",
        domain_node_id=domain_id,
    )
    node_id = out["result"]["node_id"]

    versions = master_data.list_node_versions(conn, org_id=scope["org_id"], node_id=node_id)
    assert versions[0]["payload"]["classification_type"] == "product_line"
    edges = master_data.fetch_org_memberships(conn, version_id=str(versions[0]["id"]))
    assert [(e.parent_node_id, e.child_node_id) for e in edges] == [(domain_id, node_id)]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT domain_id, classification_type, superseded_by_node_id "
            "FROM app.mdm_business_classifications WHERE id = %s",
            (node_id,),
        )
        assert cur.fetchone() == (domain_id, "product_line", node_id)


def test_the_same_idempotency_key_mints_one_identity(live_postgres, converged):
    conn = live_postgres
    scope = converged
    key = f"mdc_{uuid.uuid4().hex}"

    first = _create(
        conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}", idempotency_key=key
    )
    replay = _create(
        conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}", idempotency_key=key
    )

    assert replay["idempotent_replay"] is True
    assert replay["result"]["node_id"] == first["result"]["node_id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.master_data_nodes WHERE org_id = %s AND label = %s",
            (scope["org_id"], "Retail Media"),
        )
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Renaming.
# ---------------------------------------------------------------------------


def test_a_rename_moves_the_label_the_version_and_the_projection_together(
    live_postgres, converged
):
    """MUTATION: drop `update_superseded_projection` -> red.

    A rename that moved only the node would leave every reader of the superseded
    store serving the old name, and the Governance lens -- which reads that row
    -- would contradict the object it just renamed.
    """
    from core import business_taxonomy, master_data

    conn = live_postgres
    scope = converged
    created = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")
    node_id = created["result"]["node_id"]

    out = _command(
        conn,
        scope,
        node_id,
        "rename",
        label="Retail & Commerce Media",
        expected_version=_base(conn, scope, node_id),
    )

    assert out["result"]["label"] == "Retail & Commerce Media"
    versions = master_data.list_node_versions(conn, org_id=scope["org_id"], node_id=node_id)
    assert [v["status"] for v in versions] == ["current", "superseded"]
    assert versions[0]["payload"]["name"] == "Retail & Commerce Media"
    # The short code is the identity's stable handle and is NOT renamed with it.
    assert versions[0]["payload"]["slug"] == f"rm-{scope['suffix']}"

    taxonomy = business_taxonomy.list_taxonomy(conn, org_id=scope["org_id"])
    renamed = [row for row in taxonomy["domains"] if str(row["id"]) == node_id][0]
    assert renamed["name"] == "Retail & Commerce Media"


def test_a_renamed_classification_keeps_its_place_in_the_tree(live_postgres, converged):
    """MUTATION: stop carrying the edges into the new version -> red.

    A membership belongs to a VERSION. Publishing a new one without re-attaching
    the edges would detach the classification from its Business Domain: a rename
    that silently regroups.
    """
    from core import master_data

    conn = live_postgres
    scope = converged
    domain_id = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")[
        "result"
    ]["node_id"]
    node_id = _create(
        conn,
        scope,
        kind="business_classification",
        name="Sponsored Products",
        slug=f"sp-{scope['suffix']}",
        classification_type="product_line",
        domain_node_id=domain_id,
    )["result"]["node_id"]

    _command(
        conn,
        scope,
        node_id,
        "rename",
        label="Sponsored Listings",
        expected_version=_base(conn, scope, node_id),
    )

    current = master_data.current_node_versions(
        conn,
        org_id=scope["org_id"],
        registry_id=str(
            master_data.require_org_registry(
                conn, org_id=scope["org_id"], object_kind="business_classification"
            )["id"]
        ),
        node_ids=[node_id],
    )[node_id]
    edges = master_data.fetch_org_memberships(conn, version_id=str(current["id"]))
    assert [(e.parent_node_id, e.child_node_id) for e in edges] == [(domain_id, node_id)]
    assert current["payload"]["classification_type"] == "product_line"


# ---------------------------------------------------------------------------
# The rename's optimistic lock (governance.md, amendment of 2026-08-30).
#
# Recorded as open work on 2026-08-25 and closed here: the console sent no
# precondition, the authority asked for none, and two people editing one identity
# from two screens were arbitrated by whoever pressed Save last. None of these
# properties can be proved against a fake cursor -- the last one needs two real
# transactions contending for one row lock.
# ---------------------------------------------------------------------------


def test_a_rename_on_the_current_base_publishes_the_next_version(live_postgres, converged):
    """The precondition is a gate, not a wall: the honest save still goes through."""
    from core import master_data

    conn = live_postgres
    scope = converged
    node_id = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")[
        "result"
    ]["node_id"]
    conn.commit()

    base = _base(conn, scope, node_id)
    out = _command(
        conn, scope, node_id, "rename", label="Retail & Commerce", expected_version=base
    )

    assert out["result"]["version_id"] != base
    assert master_data.current_node_version_id(
        conn, org_id=scope["org_id"], node_id=node_id
    ) == out["result"]["version_id"]
    versions = master_data.list_node_versions(conn, org_id=scope["org_id"], node_id=node_id)
    assert [v["status"] for v in versions] == ["current", "superseded"]


def test_a_rename_on_a_base_the_identity_has_left_mints_nothing_and_writes_no_audit(
    live_postgres, converged
):
    """MUTATION: drop the precondition -> the second save silently overwrites.

    This is the whole gesture named in `governance.md` as open work. Two readers
    open the same Business Domain; the first renames it; the second saves from the
    base it read. Without the precondition the second wins and the first change
    disappears with nobody told. With it, the second is refused BY NAME and the
    database is untouched: no version minted, no operation row, no audit line.
    """
    from core import master_data
    from core.master_data_commands import (
        MASTER_DATA_NODE_COMMAND,
        STALE_VERSION_CODE,
        MasterDataCommandRefused,
    )

    conn = live_postgres
    scope = converged
    node_id = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")[
        "result"
    ]["node_id"]
    conn.commit()

    # What BOTH readers saw when they opened the object.
    base = _base(conn, scope, node_id)
    _command(conn, scope, node_id, "rename", label="Retail & Commerce", expected_version=base)
    conn.commit()

    def counts():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT (SELECT count(*) FROM app.master_data_object_versions "
                "        WHERE node_id = %s),"
                "       (SELECT count(*) FROM app.operations "
                "        WHERE command_type = %s AND effective_org_id = %s),"
                "       (SELECT count(*) FROM app.audit_log "
                "        WHERE action = %s AND effective_org_id = %s)",
                (node_id, MASTER_DATA_NODE_COMMAND, scope["org_id"],
                 MASTER_DATA_NODE_COMMAND, scope["org_id"]),
            )
            return cur.fetchone()

    before = counts()

    with pytest.raises(MasterDataCommandRefused) as refused:
        _command(
            conn, scope, node_id, "rename", label="Retail Media EMEA", expected_version=base
        )
    conn.rollback()

    assert refused.value.code == STALE_VERSION_CODE
    assert counts() == before
    # And the name the first writer published is still the one every reader sees.
    current = master_data.current_node_versions(
        conn,
        org_id=scope["org_id"],
        registry_id=str(
            master_data.require_org_registry(
                conn, org_id=scope["org_id"], object_kind="business_domain"
            )["id"]
        ),
        node_ids=[node_id],
    )[node_id]
    assert current["payload"]["name"] == "Retail & Commerce"


def test_two_simultaneous_renames_from_one_base_leave_exactly_one_winner(
    live_postgres, converged
):
    """MUTATION: drop `for_update` from the reading inside the mutation -> red.

    The reading taken BEFORE the operation cannot decide this: both transactions
    take it, both see the same base, and both pass. Only the locked reading inside
    the mutation makes the loser wait and then see the revision the winner
    published. Two real connections, released together, are the only way to prove
    it -- a fake cursor answers whatever the fixture tells it to, in one thread.
    """
    import os
    import threading

    from core.master_data_commands import (
        MASTER_DATA_NODE_COMMAND,
        STALE_VERSION_CODE,
        MasterDataCommandRefused,
    )

    conn = live_postgres
    scope = converged
    node_id = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")[
        "result"
    ]["node_id"]
    conn.commit()
    base = _base(conn, scope, node_id)

    def operation_rows() -> int:
        # The creation above runs under the same command type, so the property is
        # a DELTA: exactly one act was recorded, and it is the winner's.
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.operations "
                "WHERE command_type = %s AND effective_org_id = %s",
                (MASTER_DATA_NODE_COMMAND, scope["org_id"]),
            )
            return int(cur.fetchone()[0])

    operations_before = operation_rows()
    dsn = os.environ["TEST_POSTGRES_DSN"]
    barrier = threading.Barrier(2, timeout=30)
    outcomes: list[str | None] = [None, None]

    def rename(index: int, label: str) -> None:
        import psycopg

        worker = psycopg.connect(dsn, connect_timeout=10)
        try:
            with worker.cursor() as cur:
                # A bug that stopped releasing the row must surface as an error,
                # never as a suite that hangs.
                cur.execute("SET lock_timeout = '20s'")
            barrier.wait()
            try:
                _command(
                    worker, scope, node_id, "rename",
                    label=label, expected_version=base,
                )
                worker.commit()
                outcomes[index] = "renamed"
            except MasterDataCommandRefused as exc:
                worker.rollback()
                outcomes[index] = exc.code
        finally:
            worker.close()

    threads = [
        threading.Thread(target=rename, args=(0, "Retail & Commerce")),
        threading.Thread(target=rename, args=(1, "Retail Media EMEA")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a rename never returned -- the row lock was not released"

    assert sorted(outcomes) == sorted(["renamed", STALE_VERSION_CODE])
    with conn.cursor() as cur:
        # ONE new version, not two: the loser published nothing.
        cur.execute(
            "SELECT count(*) FROM app.master_data_object_versions WHERE node_id = %s",
            (node_id,),
        )
        assert cur.fetchone()[0] == 2
    # And ONE operation row added, not two. The loser's was inserted before its
    # mutation ran and rolled back with it: a refused command is not an act that
    # happened, even when the refusal is decided inside the mutation.
    assert operation_rows() == operations_before + 1


# ---------------------------------------------------------------------------
# Archiving -- the guard the cutover left open.
# ---------------------------------------------------------------------------


def _publish_view_naming(conn, scope, domain_id: str, *, status: str = "published") -> str:
    """A Semantic Model version that names this Business Domain, AND registers it.

    The version row is written as a fixture rather than through
    `semantic_model.create_change_set`: what is measured is the GUARD, and
    building a whole change set would put the thing under test behind four
    gestures that have their own suites. The REGISTRATION is not faked -- it goes
    through `semantic_model_used_by.register_published_version`, the one writer
    `confirm_change_set` calls, so this fixture cannot prove a guard the product
    would not arm.
    """
    from core.semantic_model_used_by import register_published_version
    from ulid import ULID

    view_id, version_id = f"sv_{ULID()}", f"svv_{ULID()}"
    digest = "0" * 64
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, lifecycle_status, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (view_id, scope["project_id"], f"view_{scope['suffix']}", status, ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 business_domain_refs, dependency_fingerprint, content_hash, created_by)
            VALUES (%s,%s,%s,1,%s,%s,%s,%s::jsonb,%s,%s,%s)
            """,
            (
                version_id,
                view_id,
                scope["project_id"],
                status,
                f"view_{scope['suffix']}",
                "Revenue by domain",
                f'["{domain_id}"]',
                digest,
                digest,
                ACTOR,
            ),
        )
    if status not in ("superseded", "archived"):
        register_published_version(
            conn,
            project_id=scope["project_id"],
            object_type="semantic-view",
            object_id=view_id,
            version_id=version_id,
            business_domain_refs=[domain_id],
            actor=ACTOR,
        )
    return version_id


def test_archiving_a_domain_a_published_view_still_names_is_refused(
    live_postgres, converged
):
    """MUTATION: read `assess_node_impact` (the Project twin) here -> red.

    THE HOLE THE CUTOVER NAMED, CLOSED. A published Semantic Model version is
    IMMUTABLE, so a reference that stops resolving can never be corrected in
    place -- which is why the legacy writer refused this, and why the cutover
    named its absence rather than leaving it to the first operator who archived
    a domain a published view names.

    Two things had to land for it: migration 311, because
    `master_data_used_by`'s composite foreign key pointed at
    `master_data_nodes(project_id, id)` and a converged Business Domain carries
    `project_id IS NULL` -- the store could not physically hold one row about it
    -- and the writer inside `confirm_change_set`. This command reads the store's
    own organization reader, and the Project-scoped one would answer a reassuring
    zero about every identity in the taxonomy.
    """
    from core.master_data_commands import MasterDataCommandRefused

    conn = live_postgres
    scope = converged
    node_id = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")[
        "result"
    ]["node_id"]
    version_id = _publish_view_naming(conn, scope, node_id)

    with pytest.raises(MasterDataCommandRefused) as refused:
        _command(conn, scope, node_id, "archive")

    payload = refused.value.as_dict()
    # The CONSUMER is the view; the version is what pins the moment it started
    # naming this domain, and both travel so an operator can open either.
    assert version_id in {c["consumer_version_id"] for c in payload["impact"]["consumers"]}
    assert "semantic-view" in payload["impact"]["summary"]

    from core import master_data

    node = master_data.fetch_org_node(conn, org_id=scope["org_id"], node_id=node_id)
    assert node["archived_at"] is None, "a refusal changes nothing"


def test_a_released_semantic_view_no_longer_blocks_the_archive(live_postgres, converged):
    """History is not a consumer, and the refusal names a path of repair.

    A guard that never released would make a domain that ever appeared in a view
    unarchivable for life, and the refusal would name a gesture nobody can do.
    The release goes through the product's own path -- `release_object`, what an
    archived Semantic Model object calls -- not through a DELETE this test
    invented.
    """
    from core.semantic_model_used_by import release_object

    conn = live_postgres
    scope = converged
    node_id = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")[
        "result"
    ]["node_id"]
    version_id = _publish_view_naming(conn, scope, node_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT view_id FROM app.semantic_view_versions WHERE id = %s", (version_id,)
        )
        view_id = cur.fetchone()[0]
    release_object(
        conn, project_id=scope["project_id"], object_type="semantic-view", object_id=view_id
    )

    out = _command(conn, scope, node_id, "archive")
    assert out["result"]["action"] == "archive"
    assert out["result"]["consumers_at_command_time"] == []


def test_a_live_business_link_blocks_the_archive_and_its_retirement_releases_it(
    live_postgres, converged
):
    """MUTATION: drop the `mdm_business_links` branch -> red.

    It is the same count the Governance lens prints in its `Used by` column, so
    a screen saying "Used by 1" beside an archive that succeeds silently would
    be the two halves of one object disagreeing.
    """
    from core import business_taxonomy
    from core.master_data_commands import MasterDataCommandRefused

    conn = live_postgres
    scope = converged
    node_id = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")[
        "result"
    ]["node_id"]
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM app.target_fields WHERE status <> 'deleted' LIMIT 1")
        target = cur.fetchone()[0]
    link = business_taxonomy.create_link(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        taxonomy_type="business_domain",
        taxonomy_id=node_id,
        target_type="target_field",
        target_id=target,
        relation_type="explains",
        actor=ACTOR,
        reason="it explains this field",
    )

    with pytest.raises(MasterDataCommandRefused):
        _command(conn, scope, node_id, "archive")

    business_taxonomy.retire_link(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        link_id=str(link["id"]),
        actor=ACTOR,
        reason="the line closed",
    )
    out = _command(conn, scope, node_id, "archive")
    assert out["result"]["consumers_at_command_time"] == []


def test_an_acknowledged_archive_records_what_it_went_ahead_despite_and_archives_both(
    live_postgres, converged
):
    from core import business_taxonomy, master_data

    conn = live_postgres
    scope = converged
    node_id = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")[
        "result"
    ]["node_id"]
    _publish_view_naming(conn, scope, node_id)

    out = _command(conn, scope, node_id, "archive", acknowledge_impact=True)

    assert out["result"]["impact_acknowledged"] is True
    assert len(out["result"]["consumers_at_command_time"]) == 1
    node = master_data.fetch_org_node(conn, org_id=scope["org_id"], node_id=node_id)
    assert node["archived_at"] is not None
    # BOTH halves, or the readers of the projection keep serving it as active.
    taxonomy = business_taxonomy.list_taxonomy(conn, org_id=scope["org_id"], status="archived")
    assert node_id in {str(row["id"]) for row in taxonomy["domains"]}
    versions = master_data.list_node_versions(conn, org_id=scope["org_id"], node_id=node_id)
    assert versions[0]["payload"]["lifecycle_state"] == "archived"


def test_restore_brings_back_the_identity_and_its_projection(live_postgres, converged):
    from core import business_taxonomy, master_data

    conn = live_postgres
    scope = converged
    node_id = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")[
        "result"
    ]["node_id"]
    _command(conn, scope, node_id, "archive")

    _command(conn, scope, node_id, "restore")

    node = master_data.fetch_org_node(conn, org_id=scope["org_id"], node_id=node_id)
    assert node["archived_at"] is None
    taxonomy = business_taxonomy.list_taxonomy(conn, org_id=scope["org_id"])
    restored = [row for row in taxonomy["domains"] if str(row["id"]) == node_id][0]
    assert restored["status"] == "active"
    versions = master_data.list_node_versions(conn, org_id=scope["org_id"], node_id=node_id)
    assert versions[0]["payload"]["lifecycle_state"] == "active"


def test_a_converged_domain_can_be_archived_at_all(live_postgres, converged):
    """MUTATION: call the Project-scoped `archive_node` unconditionally -> red.

    A converged Business Domain is an ORGANIZATION identity, and every clause of
    the Project-scoped writers reads `project_id = %s`. Before the scope was
    resolved, archiving one updated no row and answered "node not found" about
    an object the screen had just listed.
    """
    from core import master_data

    conn = live_postgres
    scope = converged
    seeded = [
        row
        for row in master_data.list_org_nodes(
            conn,
            org_id=scope["org_id"],
            registry_id=str(
                master_data.require_org_registry(
                    conn, org_id=scope["org_id"], object_kind="business_domain"
                )["id"]
            ),
        )
    ]
    assert seeded, "the convergence carried the six seeded domains"
    node_id = str(seeded[0]["id"])

    out = _command(conn, scope, node_id, "archive")
    assert out["result"]["action"] == "archive"
    assert (
        master_data.fetch_org_node(conn, org_id=scope["org_id"], node_id=node_id)["archived_at"]
        is not None
    )


def test_a_retried_archive_replays_instead_of_being_refused_by_a_new_consumer(
    live_postgres, converged
):
    """MUTATION: raise the refusal before asking whether the key already ran -> red.

    The guard reads the world, and the world moves. A client that timed out
    retries the archive it already ran; if a Semantic Model version has named the
    domain since, the pre-check would refuse a command that HAPPENED — telling an
    operator to release consumers of an identity that is already archived.
    """
    conn = live_postgres
    scope = converged
    node_id = _create(conn, scope, name="Retail Media", slug=f"rm-{scope['suffix']}")[
        "result"
    ]["node_id"]
    key = f"mdn_{uuid.uuid4().hex}"

    first = _command(conn, scope, node_id, "archive", idempotency_key=key)
    _publish_view_naming(conn, scope, node_id)

    replay = _command(conn, scope, node_id, "archive", idempotency_key=key)
    assert replay["idempotent_replay"] is True
    assert replay["operation_id"] == first["operation_id"]


def test_an_unconverged_taxonomy_row_is_answered_by_the_convergence_not_by_a_404(
    live_postgres, scope
):
    """A row that never converged is addressable, and the answer names a gesture.

    THIS TEST HELD THE DEFECT UNTIL 2026-08-31. It asserted `MasterDataNotFound`
    -- *"an unconverged row has no node"* -- which is true of the store and wrong
    as an ANSWER: the Governance lens lists that row, so the person acting on it
    is looking at an object the product showed them. `not found` closes the
    conversation; the convergence refusal opens the one gesture that reaches the
    identity. The store fact has not changed; what is said about it has.
    """
    from core.master_data_commands import CONVERGE_FIRST_CODE, MasterDataCommandRefused

    conn = live_postgres
    row = insert_domain_fixture(
        conn,
        org_id=scope["org_id"],
        slug=f"unconverged-{scope['suffix']}",
        name="Never converged",
        actor=ACTOR,
    )
    conn.commit()

    with pytest.raises(MasterDataCommandRefused) as refused:
        _command(conn, scope, str(row["id"]), "archive")
    assert refused.value.as_dict()["code"] == CONVERGE_FIRST_CODE


def test_an_unknown_id_on_a_converged_organization_is_still_not_found(
    live_postgres, converged
):
    """The 404 keeps the one meaning it should ever have had.

    Nothing above weakens it: on an organization with nothing left to converge,
    an id that names no identity is a caller looking at the wrong thing, and the
    convergence has no gesture to offer them.
    """
    from core.master_data import MasterDataNotFound

    conn = live_postgres
    scope = converged
    with pytest.raises(MasterDataNotFound):
        _command(conn, scope, f"bd_{uuid.uuid4().hex[:26]}", "archive")


def test_the_lock_still_holds_when_the_numbering_starts_above_the_legacy_maximum(
    live_postgres, scope
):
    """AI-324 renumbers the counter; the lock states an ID, so it does not move.

    `governance.md`, amendment of 2026-08-31: an identity converged out of a
    legacy ledger holding v1..v3 mints v4, not v1. If the precondition were ever
    re-expressed as a NUMBER -- the temptation every time a screen shows one --
    this is where it would break: the stale save below states a base that is
    still called "the current version" by number the whole time.
    """
    from core import master_data
    from core.business_taxonomy import list_taxonomy
    from core.master_data_commands import (
        STALE_VERSION_CODE,
        MasterDataCommandRefused,
    )
    from core.master_data_convergence import converge_org_taxonomy

    conn = live_postgres
    domain_id = str(list_taxonomy(conn, org_id=scope["org_id"])["domains"][0]["id"])
    with conn.cursor() as cur:
        for number in (2, 3):
            cur.execute(
                """
                INSERT INTO app.mdm_business_domain_versions
                    (domain_id, version_number, org_id, slug, name, description,
                     owner, status, created_by, created_at, updated_at,
                     archived_at, change_kind, changed_by)
                SELECT id, %s, org_id, slug, name, description, owner, status,
                       created_by, created_at, updated_at, archived_at,
                       'updated', %s
                  FROM app.mdm_business_domains
                 WHERE id = %s
                """,
                (number, ACTOR, domain_id),
            )
    converge_org_taxonomy(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        actor=ACTOR,
        idempotency_key=f"conv_{uuid.uuid4().hex}",
        reason="AI-324: the lock survives the renumbering",
    )
    conn.commit()

    base = _base(conn, scope, domain_id)
    current = master_data.list_node_versions(
        conn, org_id=scope["org_id"], node_id=domain_id
    )[0]
    assert current["version_number"] == 4, "minted above the legacy maximum"

    _command(conn, scope, domain_id, "rename", label="Sales EMEA", expected_version=base)
    conn.commit()

    moved = master_data.list_node_versions(
        conn, org_id=scope["org_id"], node_id=domain_id
    )
    assert [row["version_number"] for row in moved] == [5, 4]

    with pytest.raises(MasterDataCommandRefused) as refused:
        _command(
            conn, scope, domain_id, "rename", label="Sales APAC", expected_version=base
        )
    assert refused.value.code == STALE_VERSION_CODE
    assert [
        row["version_number"]
        for row in master_data.list_node_versions(
            conn, org_id=scope["org_id"], node_id=domain_id
        )
    ] == [5, 4], "a refused rename mints no version, renumbered or not"


# ---------------------------------------------------------------------------
# THE THIRD KIND: a Product and an Activity (AI-328).
#
# The Governance Overview reads all three Master Data kinds through one screen
# and draws the same Rename on each. The two business identities above have been
# holding the lock since 2026-08-30; the third kind was not holding it at all,
# and no suite here looked. What was measured on 2026-08-31, before a line of the
# repair was written, against a real base:
#
#   * a Product with a published revision: the Overview lens reported
#     `current_version_id: None` and `node_version_count: 0` for it, because
#     `create_node_version` filed the revision under `project_id NULL` while
#     `_CLIENT_OBJECT_INSTANCES` joins it on `v.project_id = n.project_id`. The
#     console sent the null it was given; the authority read the revision that
#     did exist; every rename was refused `version_conflict` with a reload that
#     could never help.
#   * an Activity minted by an observation, which carries no revision at all:
#     both renames from one base landed, one silently overwriting the other, and
#     `version_id` came back None while the screen promised a version.
#
# Neither is provable against a cursor: the first defect is a JOIN, the second is
# what two commits do to one row.
# ---------------------------------------------------------------------------


def _client_collection(conn, scope, *, object_kind: str, label: str, node_label: str):
    """One client-declared collection and one instance of it, project-scoped.

    The shape `entity_reference_import` and `observed_entities` land: a registry
    that versions PER IDENTITY (migration 143 names Products and Activities in
    that column's own comment) and a node inside this Project.
    """
    from core import master_data

    registry = master_data.create_registry(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        object_kind=object_kind,
        label=label,
        actor=ACTOR,
        version_scope=master_data.VERSION_SCOPE_NODE,
    )
    node = master_data.create_node(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        registry_id=registry["id"],
        node_kind=object_kind,
        label=node_label,
        actor=ACTOR,
    )
    return registry, node


def _overview_row(conn, scope, *, object_kind: str):
    """What the Governance Overview serves for this collection's one instance.

    Read through the lens's OWN query rather than a hand-written join: the base a
    door sends back is whatever this returns, so a test composing its own query
    would prove a base no screen ever holds.
    """
    from core.governance_read_model import _CLIENT_OBJECT_INSTANCES

    with conn.cursor() as cur:
        cur.execute(
            _CLIENT_OBJECT_INSTANCES,
            {"project_id": scope["project_id"], "object_kind": object_kind},
        )
        columns = [column.name for column in cur.description]
        return dict(zip(columns, cur.fetchone(), strict=False))


def test_a_products_revision_sits_in_the_project_its_node_sits_in(live_postgres, scope):
    """The Overview's base and the authority's base are ONE id, or neither works.

    MUTATION: write `project_id` as NULL in `create_node_version` again -> the
    lens reports no version and no base for an object that has one, so the door
    sends null, the authority compares it to the revision it can see, and every
    honest save is refused `version_conflict` forever.
    """
    from core import master_data

    conn = live_postgres
    registry, node = _client_collection(
        conn, scope, object_kind="product", label="Products", node_label="Blue Sneaker"
    )
    version = master_data.create_node_version(
        conn,
        org_id=scope["org_id"],
        registry_id=registry["id"],
        node_id=node["id"],
        payload={"attributes": {"sku": "SKU-1"}},
        actor=ACTOR,
    )
    master_data.publish_node_version(
        conn, org_id=scope["org_id"], version_id=version["id"], actor=ACTOR
    )
    conn.commit()

    row = _overview_row(conn, scope, object_kind="product")
    assert row["current_version_id"] == version["id"]
    assert row["node_version_count"] == 1
    assert row["version_scope"] == "node"
    # The two readings that must agree: what the screen shows, and what the lock
    # holds the caller to.
    assert master_data.current_node_version_id(
        conn, org_id=scope["org_id"], node_id=node["id"]
    ) == row["current_version_id"]


def test_a_product_rename_mints_a_version_and_the_next_stale_one_is_refused(
    live_postgres, scope
):
    """MUTATION: gate the republish on the two business kinds again -> the base
    never moves, the second rename from the same base lands, and the first
    writer's change disappears with nobody told."""
    from core import master_data
    from core.master_data_commands import STALE_VERSION_CODE, MasterDataCommandRefused

    conn = live_postgres
    registry, node = _client_collection(
        conn, scope, object_kind="product", label="Products", node_label="Blue Sneaker"
    )
    version = master_data.create_node_version(
        conn,
        org_id=scope["org_id"],
        registry_id=registry["id"],
        node_id=node["id"],
        payload={"attributes": {"sku": "SKU-1"}},
        actor=ACTOR,
    )
    master_data.publish_node_version(
        conn, org_id=scope["org_id"], version_id=version["id"], actor=ACTOR
    )
    conn.commit()

    # What BOTH readers saw when they opened it -- taken from the lens, which is
    # what the console reads.
    base = _overview_row(conn, scope, object_kind="product")["current_version_id"]
    out = _command(
        conn, scope, node["id"], "rename", label="Red Sneaker", expected_version=base
    )
    conn.commit()

    assert out["result"]["version_id"] not in (None, base)
    versions = master_data.list_node_versions(
        conn, org_id=scope["org_id"], node_id=node["id"]
    )
    assert [row["status"] for row in versions] == ["current", "superseded"]
    assert [row["version_number"] for row in versions] == [2, 1]
    # THE PAYLOAD IS CARRIED FORWARD UNCHANGED. A client object's payload is the
    # attributes bag its feed landed; writing a name beside it would be a key the
    # very next import drops, and the history would record a rename and then
    # silently un-record it. The label lives on the node.
    assert versions[0]["payload"] == {"attributes": {"sku": "SKU-1"}}
    with conn.cursor() as cur:
        cur.execute("SELECT label FROM app.master_data_nodes WHERE id = %s", (node["id"],))
        assert cur.fetchone()[0] == "Red Sneaker"

    with pytest.raises(MasterDataCommandRefused) as refused:
        _command(
            conn, scope, node["id"], "rename", label="Green Sneaker", expected_version=base
        )
    conn.rollback()

    assert refused.value.code == STALE_VERSION_CODE
    assert [
        row["version_number"]
        for row in master_data.list_node_versions(
            conn, org_id=scope["org_id"], node_id=node["id"]
        )
    ] == [2, 1], "a refused rename mints nothing"


def test_an_activity_that_never_had_a_revision_gets_one_and_the_lock_can_then_fire(
    live_postgres, scope
):
    """The case the lock could never fire in, for the whole life of the object.

    `observed_entities.attach_observed_entity` mints a node and no version, so the
    base was None, every rename stated None, and every rename matched. MUTATION:
    let `_republish` return None when there is nothing to carry forward -> both
    renames below land and the second silently overwrites the first.
    """
    from core import master_data
    from core.master_data_commands import STALE_VERSION_CODE, MasterDataCommandRefused

    conn = live_postgres
    _, node = _client_collection(
        conn,
        scope,
        object_kind="activity",
        label="Activities",
        node_label="Spring Campaign",
    )
    conn.commit()

    # An honest base: this object has no published revision, and the door states
    # exactly that rather than omitting the field.
    assert _overview_row(conn, scope, object_kind="activity")["current_version_id"] is None

    first = _command(
        conn, scope, node["id"], "rename", label="Summer Campaign", expected_version=None
    )
    conn.commit()

    assert first["result"]["version_id"] is not None
    minted = master_data.list_node_versions(conn, org_id=scope["org_id"], node_id=node["id"])
    assert [row["version_number"] for row in minted] == [1]
    assert minted[0]["status"] == "current"
    # And the Overview now says what it does: the object HAS a base, and it is
    # the one the authority holds a caller to.
    assert (
        _overview_row(conn, scope, object_kind="activity")["current_version_id"]
        == first["result"]["version_id"]
    )

    with pytest.raises(MasterDataCommandRefused) as refused:
        _command(
            conn, scope, node["id"], "rename", label="Autumn Campaign", expected_version=None
        )
    conn.rollback()

    assert refused.value.code == STALE_VERSION_CODE
    with conn.cursor() as cur:
        cur.execute("SELECT label FROM app.master_data_nodes WHERE id = %s", (node["id"],))
        assert cur.fetchone()[0] == "Summer Campaign"


def test_a_registry_scoped_grouping_renames_without_minting_a_node_version(
    live_postgres, scope
):
    """The registry-scoped shape is untouched, and that is not an accident.

    Country publishes the whole hierarchy as ONE version (migration 143's default
    scope). Minting a node version on a rename here would open a second history
    beside the registry's own, so the republish asks the registry's
    `version_scope` rather than the node's kind -- which is also what keeps this
    from being a list of object kinds nobody maintains.
    """
    from core import master_data

    conn = live_postgres
    registry = master_data.create_registry(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        object_kind="country",
        label="Countries",
        actor=ACTOR,
    )
    assert registry["version_scope"] == master_data.VERSION_SCOPE_REGISTRY
    node = master_data.create_node(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        registry_id=registry["id"],
        node_kind="country",
        label="France",
        actor=ACTOR,
    )
    conn.commit()

    out = _command(
        conn, scope, node["id"], "rename", label="Metropolitan France", expected_version=None
    )
    conn.commit()

    assert out["result"]["version_id"] is None
    assert (
        master_data.list_node_versions(conn, org_id=scope["org_id"], node_id=node["id"]) == []
    )
