"""The measurement grain store, against a real database (story 71.1).

Four properties cannot be proven anywhere else, and each is the mirror of one the
common key proves:

  * the immutability TRIGGER fires on a stored version, and the RGPD hatch still
    opens it -- migration 209's doctrine on the table added after it;
  * a grain of ZERO dimensions is accepted and stored (the members CHECK is
    `BETWEEN 0 AND 16`), where the common key's is `BETWEEN 1 AND 8`;
  * adding a dimension is a NEW version with a different content_hash, and version
    N is left untouched;
  * the head pointer FK cannot cross grains or projects.

It runs as the ordinary `connector` role. `live_postgres` rolls back on teardown.
"""

from __future__ import annotations

import json

import pytest

psycopg = pytest.importorskip("psycopg")

from core import metric_dimensions as grains  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    hash64,
    make_canonical_field,
    make_datastream,
    make_project,
    uid,
)


def _pin_grain_in_a_view(
    conn, project_id, grain_version_id, *, status="published", name="revenue_view"
):
    """A Semantic View version whose `master_data_refs` pins a grain VERSION.

    The referenceable surface `grain_used_by` reads today: the array where a
    semantic version records the MDM/master-data versions it is built on. A
    consumer pins a grain by its version id, so that is what lands here. `status`
    is a parameter because a `superseded` or `archived` version is history that
    must NOT block an archive, and only a parameter can prove both.
    """
    view_id, version_id = uid("sv"), uid("svv")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
            "VALUES (%s,%s,%s,'tester')",
            (view_id, project_id, name),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, master_data_refs, created_by)
            VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s::jsonb,'tester')
            """,
            (
                version_id,
                view_id,
                project_id,
                status,
                name,
                "A view built on the grain",
                hash64(),
                hash64(),
                json.dumps([{"version_id": grain_version_id}]),
            ),
        )
    return version_id


@pytest.fixture()
def project(live_postgres):
    return make_project(live_postgres, label="Epic 71")


@pytest.fixture()
def vocabulary(live_postgres, project):
    """One measure and two dimensions, minted (the table holds zero rows fresh)."""
    _org_id, project_id = project
    return {
        "spend": make_canonical_field(
            live_postgres, project_id, "spend", kind="metric", value_type="money"
        ),
        "clicks": make_canonical_field(
            live_postgres, project_id, "clicks", kind="metric", value_type="integer"
        ),
        "day": make_canonical_field(live_postgres, project_id, "day", value_type="date"),
        "campaign": make_canonical_field(live_postgres, project_id, "campaign_id"),
    }


# ---------------------------------------------------------------------------
# Declaration and versioning
# ---------------------------------------------------------------------------


def test_creating_a_grain_freezes_version_one_and_points_the_head_at_it(
    live_postgres, project, vocabulary
):
    _org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend by day and campaign",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"], vocabulary["campaign"]],
        actor="tester",
    )

    assert created["current_version"]["version_number"] == 1
    assert created["current_version"]["head"]["canonical_name"] == "spend"
    assert [m["canonical_name"] for m in created["current_version"]["members"]] == [
        "day",
        "campaign_id",
    ]
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT current_version_id, status FROM app.mdm_metric_dimensions WHERE id = %s",
            (created["id"],),
        )
        head = cur.fetchone()
    assert head[0] == created["current_version"]["id"]
    assert head[1] == "active"


def test_a_grain_of_zero_dimensions_is_stored_as_a_total(live_postgres, project, vocabulary):
    """The divergence, proven against the real CHECK (`BETWEEN 0 AND 16`)."""
    _org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Total spend",
        head_field_id=vocabulary["spend"],
        member_field_ids=[],
        actor="tester",
    )
    assert created["current_version"]["members"] == []
    read = grains.read_measurement_grain(
        live_postgres, project_id=project_id, measurement_grain_id=created["id"]
    )
    assert read["current_version"]["members"] == []
    assert read["current_version"]["head"]["canonical_name"] == "spend"


def test_appending_a_version_moves_the_head_and_leaves_the_first_alone(
    live_postgres, project, vocabulary
):
    _org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    first_hash = created["current_version"]["content_hash"]
    appended = grains.append_version(
        live_postgres,
        project_id=project_id,
        measurement_grain_id=created["id"],
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"], vocabulary["campaign"]],
        actor="tester",
    )

    assert appended["current_version"]["version_number"] == 2
    assert appended["current_version"]["content_hash"] != first_hash
    read = grains.read_measurement_grain(
        live_postgres, project_id=project_id, measurement_grain_id=created["id"]
    )
    assert [v["version_number"] for v in read["versions"]] == [2, 1]
    assert len(read["versions"][1]["members"]) == 1  # version 1 untouched


def test_a_version_that_changes_nothing_is_refused_by_name(live_postgres, project, vocabulary):
    _org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend by day",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    with pytest.raises(grains.MeasurementGrainRefused) as excinfo:
        grains.append_version(
            live_postgres,
            project_id=project_id,
            measurement_grain_id=created["id"],
            head_field_id=vocabulary["spend"],
            member_field_ids=[vocabulary["day"]],
            actor="tester",
        )
    assert excinfo.value.code == "measurement_grain_unchanged"


def test_two_active_grains_cannot_share_a_name(live_postgres, project, vocabulary):
    _org_id, project_id = project
    grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    with pytest.raises(grains.MeasurementGrainRefused) as excinfo:
        grains.create_measurement_grain(
            live_postgres,
            project_id=project_id,
            name="  spend  ",
            head_field_id=vocabulary["clicks"],
            member_field_ids=[vocabulary["day"]],
            actor="tester",
        )
    assert excinfo.value.code == "measurement_grain_name_taken"


# ---------------------------------------------------------------------------
# The refusals that need the real registry
# ---------------------------------------------------------------------------


def test_a_dimension_head_is_refused_against_the_real_registry(live_postgres, project, vocabulary):
    _org_id, project_id = project
    with pytest.raises(grains.MeasurementGrainRefused) as excinfo:
        grains.create_measurement_grain(
            live_postgres,
            project_id=project_id,
            name="Bad head",
            head_field_id=vocabulary["day"],
            member_field_ids=[],
            actor="tester",
        )
    assert excinfo.value.code == "head_is_dimension"


def test_a_metric_member_is_refused_against_the_real_registry(live_postgres, project, vocabulary):
    _org_id, project_id = project
    with pytest.raises(grains.MeasurementGrainRefused) as excinfo:
        grains.create_measurement_grain(
            live_postgres,
            project_id=project_id,
            name="Bad member",
            head_field_id=vocabulary["spend"],
            member_field_ids=[vocabulary["clicks"]],
            actor="tester",
        )
    assert excinfo.value.code == "member_is_metric"


def test_an_archived_member_is_refused(live_postgres, project, vocabulary):
    """An archived field leaves the visible catalog, so it arrives as absence."""
    _org_id, project_id = project
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.mdm_canonical_fields SET status = 'archived' WHERE id = %s",
            (vocabulary["campaign"],),
        )
    with pytest.raises(grains.MeasurementGrainRefused) as excinfo:
        grains.create_measurement_grain(
            live_postgres,
            project_id=project_id,
            name="Spend by campaign",
            head_field_id=vocabulary["spend"],
            member_field_ids=[vocabulary["campaign"]],
            actor="tester",
        )
    assert excinfo.value.code == "member_not_found"


def test_a_member_of_another_project_is_refused(live_postgres, vocabulary):
    """A field of another Project is invisible here, so it is absence too."""
    other_org, other_project = make_project(live_postgres, label="Epic 71 other")
    foreign_dim = make_canonical_field(live_postgres, other_project, "market")
    # Declare the grain in the FIRST project's world.
    with live_postgres.cursor() as cur:
        cur.execute("SELECT project_id FROM app.mdm_canonical_fields WHERE id = %s",
                    (vocabulary["spend"],))
        project_id = cur.fetchone()[0]
    with pytest.raises(grains.MeasurementGrainRefused) as excinfo:
        grains.create_measurement_grain(
            live_postgres,
            project_id=project_id,
            name="Cross-project",
            head_field_id=vocabulary["spend"],
            member_field_ids=[foreign_dim],
            actor="tester",
        )
    assert excinfo.value.code == "member_not_found"


def test_a_duplicated_member_is_refused(live_postgres, project, vocabulary):
    _org_id, project_id = project
    with pytest.raises(grains.MeasurementGrainRefused) as excinfo:
        grains.create_measurement_grain(
            live_postgres,
            project_id=project_id,
            name="Doubled",
            head_field_id=vocabulary["spend"],
            member_field_ids=[vocabulary["day"], vocabulary["day"]],
            actor="tester",
        )
    assert excinfo.value.code == "member_duplicated"


def test_members_typed_differently_across_sources_are_refused(live_postgres, project, vocabulary):
    """The physical-type disagreement refusal, against real published mappings."""
    org_id, project_id = project
    make_datastream(
        live_postgres, org_id, project_id, "Google Ads",
        bindings={vocabulary["campaign"]: ("campaign", "confirmed", "string")},
    )
    make_datastream(
        live_postgres, org_id, project_id, "Meta Ads",
        bindings={vocabulary["campaign"]: ("campaign", "resolved", "bigint")},
    )
    with pytest.raises(grains.MeasurementGrainRefused) as excinfo:
        grains.create_measurement_grain(
            live_postgres,
            project_id=project_id,
            name="Spend by campaign",
            head_field_id=vocabulary["spend"],
            member_field_ids=[vocabulary["campaign"]],
            actor="tester",
        )
    assert excinfo.value.code == "member_physical_types_disagree"


# ---------------------------------------------------------------------------
# Immutability, and the hatch that must stay open
# ---------------------------------------------------------------------------


def test_a_stored_version_cannot_be_updated_or_deleted(live_postgres, project, vocabulary):
    _org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Frozen",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    version_id = created["current_version"]["id"]

    for statement, params in (
        ("UPDATE app.mdm_metric_dimension_versions SET content_hash = %s WHERE id = %s",
         ("f" * 64, version_id)),
        ("DELETE FROM app.mdm_metric_dimension_versions WHERE id = %s", (version_id,)),
    ):
        with live_postgres.cursor() as cur:
            cur.execute("SAVEPOINT immutable_probe")
            with pytest.raises(psycopg.errors.IntegrityConstraintViolation) as excinfo:
                cur.execute(statement, params)
            assert excinfo.value.sqlstate == "23000"
            cur.execute("ROLLBACK TO SAVEPOINT immutable_probe")


def test_the_rgpd_hatch_still_opens_the_append_only_guard(live_postgres, project, vocabulary):
    """Migration 209's doctrine, proven on the table added after it."""
    _org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Erasable",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    with live_postgres.cursor() as cur:
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        cur.execute(
            "UPDATE app.mdm_metric_dimensions SET current_version_id = NULL WHERE id = %s",
            (created["id"],),
        )
        cur.execute(
            "DELETE FROM app.mdm_metric_dimension_versions WHERE metric_dimensions_id = %s",
            (created["id"],),
        )
        assert cur.rowcount == 1
        cur.execute("SET LOCAL app.rgpd_erasure = 'off'")


# ---------------------------------------------------------------------------
# Archive and list
# ---------------------------------------------------------------------------


def test_archiving_frees_the_name_and_lists_the_grain_as_archived(
    live_postgres, project, vocabulary
):
    _org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    grains.archive_measurement_grain(
        live_postgres, project_id=project_id, measurement_grain_id=created["id"], actor="tester"
    )
    # The name is free again: a new active grain can take it.
    again = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend",
        head_field_id=vocabulary["clicks"],
        member_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    listed = {g["id"]: g["status"] for g in grains.list_measurement_grains(
        live_postgres, project_id=project_id
    )}
    assert listed[created["id"]] == "archived"
    assert listed[again["id"]] == "active"


# ---------------------------------------------------------------------------
# Derived coverage against the real mapping store (story 71.2)
# ---------------------------------------------------------------------------


def test_coverage_names_full_partial_and_unknown_against_the_real_schema(
    live_postgres, project, vocabulary
):
    """The heart of 71.2: a metric bound without a dimension is PARTIAL, named."""
    org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend by day and campaign",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"], vocabulary["campaign"]],
        actor="tester",
    )
    # Full: binds the head and BOTH dimensions.
    make_datastream(
        live_postgres, org_id, project_id, "Google Ads",
        bindings={
            vocabulary["spend"]: ("cost", "resolved", "number"),
            vocabulary["day"]: ("date", "confirmed", "date"),
            vocabulary["campaign"]: ("campaign", "resolved", "string"),
        },
    )
    # Partial: binds the head and day, but NOT campaign.
    make_datastream(
        live_postgres, org_id, project_id, "Analytics",
        bindings={
            vocabulary["spend"]: ("revenue", "resolved", "number"),
            vocabulary["day"]: ("day", "confirmed", "date"),
        },
    )
    # Unknown: a Datastream that has published no mapping version.
    make_datastream(live_postgres, org_id, project_id, "Bare")

    read = grains.read_measurement_grain(
        live_postgres, project_id=project_id, measurement_grain_id=created["id"]
    )
    coverage = read["coverage"]
    assert coverage["state"] == "available"
    assert coverage["counts"]["full"] == 1
    assert coverage["counts"]["partial"] == 1
    assert coverage["counts"]["unknown"] == 1

    by_name = {ds["datastream_name"]: ds for ds in coverage["datastreams"]}
    assert by_name["Google Ads"]["coverage"] == "full"
    partial = by_name["Analytics"]
    assert partial["coverage"] == "partial"
    assert [m["canonical_name"] for m in partial["missing_dimensions"]] == ["campaign_id"]
    assert {u["name"] for u in coverage["unknown_datastreams"]} == {"Bare"}


# ---------------------------------------------------------------------------
# Used-by, and the archive it gates (story 71.2)
# ---------------------------------------------------------------------------


def test_the_read_lists_what_pins_a_version_of_the_grain(live_postgres, project, vocabulary):
    _org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    _pin_grain_in_a_view(
        live_postgres, project_id, created["current_version"]["id"], name="revenue_view"
    )
    read = grains.read_measurement_grain(
        live_postgres, project_id=project_id, measurement_grain_id=created["id"]
    )
    assert [u["name"] for u in read["used_by"]] == ["revenue_view"]
    assert read["used_by"][0]["still_holds"] is True


def test_a_grain_cannot_be_archived_while_a_live_read_cuts_by_it(
    live_postgres, project, vocabulary
):
    _org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    _pin_grain_in_a_view(
        live_postgres, project_id, created["current_version"]["id"], name="revenue_view"
    )
    with pytest.raises(grains.MeasurementGrainRefused) as excinfo:
        grains.archive_measurement_grain(
            live_postgres, project_id=project_id, measurement_grain_id=created["id"], actor="tester"
        )
    assert excinfo.value.code == "measurement_grain_in_use"
    assert "revenue_view" in excinfo.value.message
    # Still active: the refusal did not half-archive it.
    read = grains.read_measurement_grain(
        live_postgres, project_id=project_id, measurement_grain_id=created["id"]
    )
    assert read["status"] == "active"


def test_a_history_only_dependent_does_not_block_the_archive(live_postgres, project, vocabulary):
    """A superseded consumer is history nobody can revive -- it must not block."""
    _org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    _pin_grain_in_a_view(
        live_postgres, project_id, created["current_version"]["id"],
        status="superseded", name="old_view",
    )
    archived = grains.archive_measurement_grain(
        live_postgres, project_id=project_id, measurement_grain_id=created["id"], actor="tester"
    )
    assert archived["status"] == "archived"


def test_a_grain_nothing_depends_on_archives_cleanly(live_postgres, project, vocabulary):
    _org_id, project_id = project
    created = grains.create_measurement_grain(
        live_postgres,
        project_id=project_id,
        name="Spend",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    archived = grains.archive_measurement_grain(
        live_postgres, project_id=project_id, measurement_grain_id=created["id"], actor="tester"
    )
    assert archived["status"] == "archived"


def test_the_head_pointer_cannot_name_a_version_of_another_grain(
    live_postgres, project, vocabulary
):
    """The composite FK refuses a crossed head pointer."""
    _org_id, project_id = project
    a = grains.create_measurement_grain(
        live_postgres, project_id=project_id, name="A",
        head_field_id=vocabulary["spend"], member_field_ids=[vocabulary["day"]], actor="tester",
    )
    b = grains.create_measurement_grain(
        live_postgres, project_id=project_id, name="B",
        head_field_id=vocabulary["clicks"], member_field_ids=[vocabulary["day"]], actor="tester",
    )
    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT cross_probe")
        # The head-pointer FK is DEFERRABLE INITIALLY DEFERRED, so the crossed
        # pointer is caught when the constraint is checked, not on the UPDATE.
        # Force the check immediately rather than committing (which the rollback
        # fixture would swallow).
        cur.execute(
            "UPDATE app.mdm_metric_dimensions SET current_version_id = %s WHERE id = %s",
            (b["current_version"]["id"], a["id"]),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ROLLBACK TO SAVEPOINT cross_probe")
