"""The common key store, against a real database (story 66.1).

Four properties cannot be proven anywhere else, and each one has already been the
shape of a real defect in this repository:

  * the immutability TRIGGER fires, and the RGPD hatch still opens it -- migration
    209 exists because two append-only tables shipped without the hatch and an
    organization erasure died on them;
  * Mapping Coverage is DERIVED from the published mapping payloads, so it needs
    real `datastream_mapping_versions` rows, real JSONB and the real
    `binding.mdm_target` shape;
  * a Datastream with no published mapping is `unknown` and NEVER counted as
    covered -- a mock would have counted whatever it was told;
  * the head pointer FK cannot cross keys or projects.

It runs as the ordinary `connector` role. `live_postgres` rolls back on teardown.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from core import mdm_common_keys as keys  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    make_canonical_field,
    make_datastream,
    make_project,
    make_semantic_view,
    uid,
)


@pytest.fixture()
def project(live_postgres):
    return make_project(live_postgres)


@pytest.fixture()
def vocabulary(live_postgres, project):
    """`day` and `campaign_id` as canonical dimensions, plus one measure.

    Minted rather than assumed: `mdm_canonical_fields_api.py:24` records that the
    table held ZERO rows at both scopes on 2026-08-08.
    """
    _org_id, project_id = project
    return {
        "day": make_canonical_field(live_postgres, project_id, "day", value_type="date"),
        "campaign": make_canonical_field(live_postgres, project_id, "campaign_id"),
        "spend": make_canonical_field(
            live_postgres, project_id, "spend", kind="metric", value_type="money"
        ),
    }


def _datastream(conn, org_id, project_id, name, *, bindings=None):
    return make_datastream(conn, org_id, project_id, name, bindings=bindings)


# ---------------------------------------------------------------------------
# Declaration and versioning
# ---------------------------------------------------------------------------


def test_creating_a_key_freezes_version_one_and_points_the_head_at_it(
    live_postgres, project, vocabulary
):
    _org_id, project_id = project
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day and Campaign",
        canonical_field_ids=[vocabulary["day"], vocabulary["campaign"]],
        actor="tester",
    )

    assert created["current_version"]["version_number"] == 1
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT current_version_id, status FROM app.mdm_common_keys WHERE id = %s",
            (created["id"],),
        )
        head = cur.fetchone()
    assert head[0] == created["current_version"]["id"]
    assert head[1] == "active"
    # The order the caller sent, preserved and numbered.
    assert [c["canonical_name"] for c in created["current_version"]["components"]] == [
        "day",
        "campaign_id",
    ]


def test_appending_a_version_moves_the_head_and_leaves_the_first_alone(
    live_postgres, project, vocabulary
):
    _org_id, project_id = project
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    appended = keys.append_version(
        live_postgres,
        project_id=project_id,
        common_key_id=created["id"],
        canonical_field_ids=[vocabulary["day"], vocabulary["campaign"]],
        actor="tester",
    )

    assert appended["current_version"]["version_number"] == 2
    read = keys.read_common_key(
        live_postgres, project_id=project_id, common_key_id=created["id"]
    )
    assert [v["version_number"] for v in read["versions"]] == [2, 1]
    assert len(read["versions"][1]["components"]) == 1  # version 1 untouched


def test_a_version_that_changes_nothing_is_refused_by_name(live_postgres, project, vocabulary):
    _org_id, project_id = project
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day only",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    with pytest.raises(keys.CommonKeyRefused) as excinfo:
        keys.append_version(
            live_postgres,
            project_id=project_id,
            common_key_id=created["id"],
            canonical_field_ids=[vocabulary["day"]],
            actor="tester",
        )
    assert excinfo.value.code == "common_key_unchanged"


def test_two_active_keys_cannot_share_a_name(live_postgres, project, vocabulary):
    _org_id, project_id = project
    keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    with pytest.raises(keys.CommonKeyRefused) as excinfo:
        keys.create_common_key(
            live_postgres,
            project_id=project_id,
            name="  day  ",
            canonical_field_ids=[vocabulary["campaign"]],
            actor="tester",
        )
    assert excinfo.value.code == "common_key_name_taken"


# ---------------------------------------------------------------------------
# Immutability, and the hatch that must stay open
# ---------------------------------------------------------------------------


def test_a_stored_version_cannot_be_updated_or_deleted(live_postgres, project, vocabulary):
    _org_id, project_id = project
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Frozen",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    version_id = created["current_version"]["id"]

    # The two halves are refused by two different mechanisms, and naming only the
    # trigger is what made this assertion stale. Migration 258 granted
    # SELECT, INSERT on this table, but 207's `ALTER DEFAULT PRIVILEGES` had
    # already handed `connector` UPDATE on every table created after it, so the
    # narrow grant was declarative until migration 316 revoked it
    # (`316_a_narrow_grant_is_declarative_until_the_revoke_lands.sql:77`, pinned
    # by `tests/conformance/test_the_erasure_hatch_is_a_privilege_too.py`).
    # PostgreSQL checks the privilege before the trigger, so the UPDATE is 42501
    # and never reaches the append-only guard. DELETE keeps its privilege -- the
    # RGPD erasure hatch needs it, and the test below opens it -- so there the
    # trigger is what speaks, in 23000.
    for statement, params, sqlstate, expected in (
        ("UPDATE app.mdm_common_key_versions SET content_hash = %s WHERE id = %s",
         ("f" * 64, version_id), "42501", psycopg.errors.InsufficientPrivilege),
        ("DELETE FROM app.mdm_common_key_versions WHERE id = %s", (version_id,),
         "23000", psycopg.errors.IntegrityConstraintViolation),
    ):
        with live_postgres.cursor() as cur:
            cur.execute("SAVEPOINT immutable_probe")
            with pytest.raises(expected) as excinfo:
                cur.execute(statement, params)
            assert excinfo.value.sqlstate == sqlstate
            cur.execute("ROLLBACK TO SAVEPOINT immutable_probe")


def test_the_rgpd_hatch_still_opens_the_append_only_guard(live_postgres, project, vocabulary):
    """Migration 209's doctrine, proven on the table that was added after it.

    An organization erasure sets `app.rgpd_erasure` for the length of one
    transaction. A new append-only table that forgot the hatch re-blocks the
    erasure -- which has already happened twice in this repository.
    """
    _org_id, project_id = project
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Erasable",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    with live_postgres.cursor() as cur:
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        cur.execute(
            "UPDATE app.mdm_common_keys SET current_version_id = NULL WHERE id = %s",
            (created["id"],),
        )
        cur.execute(
            "DELETE FROM app.mdm_common_key_versions WHERE common_key_id = %s",
            (created["id"],),
        )
        assert cur.rowcount == 1
        cur.execute("SET LOCAL app.rgpd_erasure = 'off'")


# ---------------------------------------------------------------------------
# Mapping Coverage: derived, and honest about what it could not read
# ---------------------------------------------------------------------------


def test_coverage_names_every_published_mapping_that_implements_a_component(
    live_postgres, project, vocabulary
):
    org_id, project_id = project
    _datastream(
        live_postgres, org_id, project_id, "Campaign spend",
        bindings={vocabulary["day"]: ("date", "confirmed"),
                  vocabulary["campaign"]: ("campaign", "confirmed")},
    )
    _datastream(
        live_postgres, org_id, project_id, "Conversions",
        bindings={vocabulary["day"]: ("event_date", "resolved")},
    )
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day and Campaign",
        canonical_field_ids=[vocabulary["day"], vocabulary["campaign"]],
        actor="tester",
    )

    coverage = keys.read_common_key(
        live_postgres, project_id=project_id, common_key_id=created["id"]
    )["mapping_coverage"]

    assert coverage["state"] == "available"
    by_name = {c["canonical_name"]: c for c in coverage["components"]}
    assert by_name["day"]["implementation_count"] == 2
    assert by_name["campaign_id"]["implementation_count"] == 1
    physical = {i["physical_field_id"] for i in by_name["day"]["implemented_by"]}
    assert physical == {"date", "event_date"}
    # Every implementation names the exact mapping version it was read from.
    assert all(i["mapping_version_id"] for i in by_name["day"]["implemented_by"])


def test_a_datastream_without_a_published_mapping_is_unknown_not_covered(
    live_postgres, project, vocabulary
):
    org_id, project_id = project
    _datastream(
        live_postgres, org_id, project_id, "Mapped",
        bindings={vocabulary["day"]: ("date", "confirmed")},
    )
    _datastream(live_postgres, org_id, project_id, "Never published")
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )

    coverage = keys.read_common_key(
        live_postgres, project_id=project_id, common_key_id=created["id"]
    )["mapping_coverage"]

    assert coverage["components"][0]["implementation_count"] == 1
    assert [d["name"] for d in coverage["unmapped_datastreams"]] == ["Never published"]


def test_a_suggested_binding_is_not_an_implementation(live_postgres, project, vocabulary):
    """A proposal is not a physical fact -- the reading `datastream_field_mapping.py:644` uses."""
    org_id, project_id = project
    _datastream(
        live_postgres, org_id, project_id, "Proposed only",
        bindings={vocabulary["day"]: ("date", "suggested")},
    )
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    coverage = keys.read_common_key(
        live_postgres, project_id=project_id, common_key_id=created["id"]
    )["mapping_coverage"]
    assert coverage["components"][0]["implementation_count"] == 0


# ---------------------------------------------------------------------------
# Used by, archiving and isolation
# ---------------------------------------------------------------------------


def test_archiving_is_refused_while_a_relationship_pins_a_version(
    live_postgres, project, vocabulary
):
    org_id, project_id = project
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Pinned",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    view_id, view_version_id = make_semantic_view(live_postgres, project_id)
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.semantic_view_version_relationships
                (view_version_id, ordinal, name, from_dataset, to_dataset, from_columns,
                 to_columns, cardinality_type, fan_out_policy, mdm_common_key_version_id)
            VALUES (%s, 0, 'spend_to_conversions', 'spend', 'conversions', ARRAY['date'],
                    ARRAY['event_date'], 'many_to_one', 'forbid', %s)
            """,
            (view_version_id, created["current_version"]["id"]),
        )

    pinned = keys.used_by(
        live_postgres, project_id=project_id, common_key_id=created["id"]
    )
    assert len(pinned) == 1
    assert pinned[0]["view_id"] == view_id
    assert pinned[0]["key_version_number"] == 1

    with pytest.raises(keys.CommonKeyRefused) as excinfo:
        keys.archive_common_key(
            live_postgres, project_id=project_id, common_key_id=created["id"], actor="tester"
        )
    assert excinfo.value.code == "common_key_in_use"
    assert "Pinned" in excinfo.value.message


def _pin(conn, view_version_id: str, key_version_id: str, *, name="spend_to_conversions"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.semantic_view_version_relationships
                (view_version_id, ordinal, name, from_dataset, to_dataset, from_columns,
                 to_columns, cardinality_type, fan_out_policy, mdm_common_key_version_id)
            VALUES (%s, 0, %s, 'spend', 'conversions', ARRAY['date'],
                    ARRAY['event_date'], 'many_to_one', 'forbid', %s)
            """,
            (view_version_id, name, key_version_id),
        )


def test_a_pin_from_a_superseded_view_version_never_blocks_archiving(
    live_postgres, project, vocabulary
):
    """MESURE DU 2026-08-16 : `used_by` ne filtrait aucun statut.

    Une relation vivant dans une version de vue `superseded` ou `archived` est de
    l HISTOIRE -- rien ne peut la ranimer -- et elle refusait l archivage POUR
    TOUJOURS. Le message envoyait alors chercher un geste qui n existe pas : on
    ne << retire >> pas une relation d une version figee.
    """
    _org_id, project_id = project
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Historic",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    _view_id, version_id = make_semantic_view(
        live_postgres, project_id, name="retired_view", status="superseded"
    )
    _pin(live_postgres, version_id, created["current_version"]["id"])

    # Le pin est TOUJOURS rendu -- l histoire ne disparait pas du releve...
    pinned = keys.used_by(live_postgres, project_id=project_id, common_key_id=created["id"])
    assert len(pinned) == 1
    assert pinned[0]["view_status"] == "superseded"
    # ...mais il ne tient plus rien, et c est ce mot-la qui decide.
    assert pinned[0]["still_holds"] is False

    keys.archive_common_key(
        live_postgres, project_id=project_id, common_key_id=created["id"], actor="tester"
    )
    listed = keys.list_common_keys(live_postgres, project_id=project_id)
    assert [k["status"] for k in listed if k["id"] == created["id"]] == ["archived"]


def test_a_draft_still_blocks_and_the_refusal_says_it_is_not_published_yet(
    live_postgres, project, vocabulary
):
    """Un brouillon bloque, et pas pour la meme raison qu une vue publiee.

    Il ne donne permission a rien aujourd hui -- c est la regle de
    `_executable_key_versions` et elle tient -- mais quelqu un peut le publier
    demain. Les compter ensemble sans les nommer envoyait la personne chercher
    dans les vues PUBLIEES une relation qui n y est pas.
    """
    _org_id, project_id = project
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Pending",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    _view_id, version_id = make_semantic_view(
        live_postgres, project_id, name="work_in_progress", status="draft"
    )
    _pin(live_postgres, version_id, created["current_version"]["id"])

    with pytest.raises(keys.CommonKeyRefused) as excinfo:
        keys.archive_common_key(
            live_postgres, project_id=project_id, common_key_id=created["id"], actor="tester"
        )
    message = excinfo.value.message
    assert "not yet published" in message
    # Et il NOMME ou chercher, ce qu un compte seul ne fait pas.
    assert "work_in_progress" in message and "draft" in message


def test_an_unpinned_key_archives(live_postgres, project, vocabulary):
    _org_id, project_id = project
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Retired",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    keys.archive_common_key(
        live_postgres, project_id=project_id, common_key_id=created["id"], actor="tester"
    )
    listed = keys.list_common_keys(live_postgres, project_id=project_id)
    assert [k["status"] for k in listed] == ["archived"]


def test_a_key_of_another_project_is_not_found_rather_than_forbidden(
    live_postgres, project, vocabulary
):
    _org_id, project_id = project
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Elsewhere",
        canonical_field_ids=[vocabulary["day"]],
        actor="tester",
    )
    other_org, other_project = uid("org"), uid("proj")
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (other_org, "Other", other_org.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) VALUES (%s,%s,%s,%s,%s)",
            (other_project, other_org, "Other", other_project.lower(), "tester"),
        )

    with pytest.raises(keys.CommonKeyNotFound):
        keys.read_common_key(
            live_postgres, project_id=other_project, common_key_id=created["id"]
        )
    assert keys.list_common_keys(live_postgres, project_id=other_project) == []


def test_a_relationship_cannot_pin_a_common_key_version_from_another_project(
    live_postgres, project, vocabulary
):
    _org_id, project_id = project
    _view_id, view_version_id = make_semantic_view(live_postgres, project_id)
    foreign_org, foreign_project = make_project(live_postgres, "Foreign key owner")
    foreign_day = make_canonical_field(
        live_postgres, foreign_project, "day", value_type="date"
    )
    foreign_key = keys.create_common_key(
        live_postgres,
        project_id=foreign_project,
        name="Foreign day",
        canonical_field_ids=[foreign_day],
        actor="tester",
    )

    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT foreign_common_key")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                """
                INSERT INTO app.semantic_view_version_relationships
                    (view_version_id, ordinal, name, from_dataset, to_dataset,
                     from_columns, to_columns, cardinality_type, fan_out_policy,
                     mdm_common_key_version_id)
                VALUES (%s, 0, 'foreign', 'left', 'right', ARRAY['day'], ARRAY['day'],
                        'many_to_one', 'forbid', %s)
                """,
                (view_version_id, foreign_key["current_version"]["id"]),
            )
        cur.execute("ROLLBACK TO SAVEPOINT foreign_common_key")

    assert foreign_org != _org_id


def test_a_measure_is_refused_against_the_real_registry(live_postgres, project, vocabulary):
    """The pure test proves the branch; this proves the registry row reaches it."""
    _org_id, project_id = project
    with pytest.raises(keys.CommonKeyRefused) as excinfo:
        keys.create_common_key(
            live_postgres,
            project_id=project_id,
            name="Spend as a key",
            canonical_field_ids=[vocabulary["spend"]],
            actor="tester",
        )
    assert excinfo.value.code == "component_is_metric"




def _audit_actions(conn, project_id: str) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT action, identity
            FROM app.audit_log
            WHERE metadata->>'project_id' = %s
              AND action LIKE 'mdm.common_key.%%'
            ORDER BY created_at, id
            """,
            (project_id,),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def test_declaring_versioning_and_archiving_leave_a_named_trace(
    live_postgres, project, vocabulary
):
    """AUDIT MDM & GOUVERNANCE, 2026-08-14 : publier une Vue laissait une trace,
    declarer l'IDENTITE qu'elle croise n'en laissait aucune.

    Ce que le journal apporte que la table n'a pas : l'ARCHIVAGE n'a aucun auteur
    en base -- `mdm_common_keys` ne porte que `created_by`. Sans cette ligne,
    « qui a retire cette identite, et quand » n'a pas de reponse.
    """
    _org_id, project_id = project
    created = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day",
        canonical_field_ids=[vocabulary["day"]],
        actor="alice@example.com",
    )
    keys.append_version(
        live_postgres,
        project_id=project_id,
        common_key_id=created["id"],
        canonical_field_ids=[vocabulary["day"], vocabulary["campaign"]],
        actor="bob@example.com",
    )
    keys.archive_common_key(
        live_postgres,
        project_id=project_id,
        common_key_id=created["id"],
        actor="carol@example.com",
    )

    assert _audit_actions(live_postgres, project_id) == [
        ("mdm.common_key.declared", "alice@example.com"),
        ("mdm.common_key.versioned", "bob@example.com"),
        ("mdm.common_key.archived", "carol@example.com"),
    ]


def test_a_refused_declaration_leaves_no_trace_of_having_happened(
    live_postgres, project, vocabulary
):
    """Le geste et sa preuve commitent ensemble, ou pas du tout.

    C'est la raison du seam transactionnel : `write_audit_row` ouvre sa PROPRE
    connexion, donc une declaration annulee aurait laisse derriere elle la ligne
    qui affirme qu'elle a eu lieu.
    """
    _org_id, project_id = project
    with pytest.raises(keys.CommonKeyRefused):
        keys.create_common_key(
            live_postgres,
            project_id=project_id,
            name="Spend as a key",
            canonical_field_ids=[vocabulary["spend"]],
            actor="alice@example.com",
        )

    assert _audit_actions(live_postgres, project_id) == []


# ---------------------------------------------------------------------------
# The Project proposes its shared identities, across every flow (2026-09-04).
# ---------------------------------------------------------------------------


def test_a_column_carried_by_two_flows_is_proposed_with_the_registry_field_of_its_name(
    live_postgres, project
):
    org_id, project_id = project
    date_field = make_canonical_field(live_postgres, project_id, "date", value_type="date")
    make_datastream(live_postgres, org_id, project_id, "Spend", unbound_fields=("date", "campaign"))
    make_datastream(live_postgres, org_id, project_id, "Clicks", unbound_fields=("date", "adgroup"))
    make_datastream(live_postgres, org_id, project_id, "Draft")  # publishes nothing: unknown, never counted

    proposed = keys.propose_shared_identities(live_postgres, project_id=project_id)

    assert proposed["state"] == "available"
    assert [u["name"] for u in proposed["unmapped_datastreams"]] == ["Draft"]
    by_identity = {p["identity"]: p for p in proposed["proposals"]}
    assert set(by_identity) == {"date"}
    date = by_identity["date"]
    assert date["carrier_count"] == 2
    assert date["canonical_field_id"] == date_field and date["canonical_name"] == "date"
    assert len(date["to_pin"]) == 2 and date["already_pinned"] == []
    assert "Pin `date` to `date`" in date["gesture"]


def test_the_pinned_carriers_name_the_canonical_field_and_the_others_are_to_pin(
    live_postgres, project, vocabulary
):
    org_id, project_id = project
    make_datastream(
        live_postgres, org_id, project_id, "Campaign spend",
        bindings={vocabulary["day"]: ("date", "confirmed")},
    )
    make_datastream(live_postgres, org_id, project_id, "Conversions", unbound_fields=("date",))

    proposed = keys.propose_shared_identities(live_postgres, project_id=project_id)

    by_identity = {p["identity"]: p for p in proposed["proposals"]}
    date = by_identity["date"]
    assert date["canonical_field_id"] == vocabulary["day"] and date["canonical_name"] == "day"
    assert len(date["already_pinned"]) == 1 and len(date["to_pin"]) == 1
    assert "on the Mapping of 1 flow(s)" in date["gesture"]


def test_an_unpinned_carrier_survives_when_its_twins_already_pin_the_canonical_field(live_postgres, project):
    """Measured 2026-09-05 on the reference project: six flows pinned `views`, the
    seventh -- unpinned -- vanished from the proposal because the column identity
    was dropped whole in favour of the canonical one. The carriers merge."""
    org_id, project_id = project
    views = make_canonical_field(live_postgres, project_id, "views", kind="metric", value_type="integer")
    make_datastream(live_postgres, org_id, project_id, "Pinned A", bindings={views: ("views", "confirmed")})
    make_datastream(live_postgres, org_id, project_id, "Pinned B", bindings={views: ("views", "confirmed")})
    seventh = make_datastream(live_postgres, org_id, project_id, "Unpinned", measures={"views": "confirmed"})

    proposed = keys.propose_shared_identities(live_postgres, project_id=project_id)

    by_identity = {p["identity"]: p for p in proposed["proposals"]}
    assert by_identity["views"]["carrier_count"] == 3
    assert by_identity["views"]["canonical_field_id"] == views
    assert by_identity["views"]["to_pin"] == [seventh]
    assert len(by_identity["views"]["already_pinned"]) == 2
    assert sum(1 for p in proposed["proposals"] if p["identity"] == "views") == 1  # one sentence, not two


def test_a_measure_is_proposed_to_govern_and_never_as_a_key_component(live_postgres, project):
    """2026-09-05: a measure is proposed as soon as one flow carries it, with a
    gesture that never says « key »; dimensions come first."""
    org_id, project_id = project
    make_datastream(live_postgres, org_id, project_id, "Left", measures={"views": "confirmed"}, unbound_fields=("date",))
    make_datastream(live_postgres, org_id, project_id, "Right", measures={"views": "confirmed"}, unbound_fields=("date",))
    make_datastream(live_postgres, org_id, project_id, "Alone", measures={"likes": "confirmed"})

    proposed = keys.propose_shared_identities(live_postgres, project_id=project_id)

    by_identity = {p["identity"]: p for p in proposed["proposals"]}
    assert set(by_identity) == {"date", "views", "likes"}
    assert [p["identity"] for p in proposed["proposals"]][0] == "date"  # dimensions first
    assert by_identity["date"]["role"] == "dimension"
    assert by_identity["views"]["role"] == "metric" and by_identity["views"]["carrier_count"] == 2
    assert by_identity["likes"]["role"] == "metric" and by_identity["likes"]["carrier_count"] == 1
    for measure in ("views", "likes"):
        assert "key" not in by_identity[measure]["gesture"].lower()
        assert "measure" in by_identity[measure]["gesture"] or "metric" in by_identity[measure]["gesture"]
