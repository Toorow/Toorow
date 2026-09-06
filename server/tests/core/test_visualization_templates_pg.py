"""Story 72.1 -- the Chart Template in the database, proved against a real PostgreSQL.

WHY THESE CANNOT BE UNIT TESTS. Every property below is a CHECK, a UNIQUE, a
composite foreign key or a trigger in migration 333. A mocked cursor accepts all
of them happily, which is exactly how a schema promise becomes a comment. The
branch logic of `resolve_presentation` is proved offline in
`test_chart_template_pin_is_refused.py`; what is proved HERE is that the database
says the same thing when the service is bypassed.

THE OBJECT. `docs/product-architecture/visualization-and-rendering.md`
§ *Amendment, 2026-08-31 -- Chart Template, the ratified target*: a validated,
UNBOUND presentation starting point, with a stable identity and immutable
versions, that names no data.

THE DEFECT IT CLOSES. `app.analysis_report_versions.presentation_version_id` and
`app.analysis_notebook_version_blocks.presentation_version_id` had NO foreign key
of any kind (migration 154). `app.is_exact_pin` refused six placeholder words and
accepted every other string, so a pin naming nothing was storable -- for both
presentation kinds, not only the Chart Template.

Every test rolls back. Nothing is left behind, even on a disposable database.
"""

from __future__ import annotations

import json

import pytest
from core import analyze_artifacts as svc
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64

#: The contract literal migration 333 deliberately does NOT pin -- story 72.2 owns
#: the grammar and therefore owns the name of its contract. Written once here so
#: this file does not look like it is ratifying one.
_CONTRACT = "chart-template.v1"


def _uid(prefix: str) -> str:
    """A prefixed ULID. Several tables CHECK that shape (migration 142)."""
    return f"{prefix}_{ULID()}"


def _template_document(**overrides) -> dict:
    """A template document: the Spec grammar shape, with `requires` and no bindings.

    Story 72.2 owns the grammar and its walker. What migration 333 enforces, and
    all this fixture needs to exercise, is the OUTER shape: the document restates
    its own identity, and it binds nothing.
    """
    document = {
        "spec_contract_version": _CONTRACT,
        "schema_version": 1,
        "family": "bar",
        "answers_question": "How does one measure compare across a few categories?",
        "requires": {
            "measure": {"min": 1, "max": 1},
            "dimension": {"min": 1, "max": 1, "max_cardinality": 12},
        },
    }
    document.update(overrides)
    return document


class Chain:
    """org -> project, seeded here, so this file runs on an empty database."""

    def __init__(self, conn, *, org_id: str | None = None):
        self.conn = conn
        self.org_id = org_id or _uid("org")
        self.project_id = _uid("proj")

    def build(self, *, seed_org: bool = True) -> Chain:
        with self.conn.cursor() as cur:
            if seed_org:
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                    "VALUES (%s, 'Story 72.1 fixture', %s, 'active', 'test')",
                    (self.org_id, self.org_id.replace("_", "-")),
                )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 72.1 fixture', %s, 'test')",
                (self.project_id, self.org_id, self.project_id.replace("_", "-")),
            )
        return self

    # -- the Chart Template ------------------------------------------------

    def template(
        self,
        *,
        seed_origin: str = "project",
        seed_module_name: str | None = None,
        seed_template_id: str | None = None,
    ) -> str:
        """A head, with no version. A head alone is not a presentation (AC1)."""
        template_id = _uid("vtpl")
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.visualization_templates
                    (id, org_id, project_id, label, seed_origin, seed_module_name,
                     seed_template_id, created_by)
                VALUES (%s, %s, %s, 'Measure by category', %s, %s, %s, 'test')
                """,
                (
                    template_id,
                    self.org_id,
                    self.project_id,
                    seed_origin,
                    seed_module_name,
                    seed_template_id,
                ),
            )
        return template_id

    def template_version(
        self,
        template_id: str,
        *,
        version_number: int = 1,
        predecessor: str | None = None,
        document: dict | None = None,
        family: str = "bar",
        advance_head: bool = True,
    ) -> tuple[str, str]:
        """One immutable version, and the head advanced onto it."""
        version_id = _uid("vtv")
        body = document if document is not None else _template_document(family=family)
        content_hash = svc.canonical_hash(body)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.visualization_template_versions
                    (id, template_id, org_id, project_id, version_number, family,
                     spec_contract_version, schema_version, document, content_hash,
                     predecessor_version_id, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s::jsonb, %s, %s, 'test')
                """,
                (
                    version_id,
                    template_id,
                    self.org_id,
                    self.project_id,
                    version_number,
                    body["family"],
                    body["spec_contract_version"],
                    json.dumps(body),
                    content_hash,
                    predecessor,
                ),
            )
            if advance_head:
                cur.execute(
                    "UPDATE app.visualization_templates SET current_version_id = %s "
                    "WHERE id = %s",
                    (version_id, template_id),
                )
        return version_id, content_hash


@pytest.fixture
def chain(live_postgres):
    built = Chain(live_postgres).build()
    yield built
    live_postgres.rollback()


# ---------------------------------------------------------------------------
# AC1. A head without a version is not a presentation; a version is immutable.
# ---------------------------------------------------------------------------


def test_a_head_carries_no_version_until_one_is_written(chain):
    template_id = chain.template()
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT current_version_id FROM app.visualization_templates WHERE id = %s",
            (template_id,),
        )
        assert cur.fetchone()[0] is None
    #  And it is unreadable AS A PRESENTATION: the head id is not a version id, so
    #  pinning it resolves nothing and is refused rather than stored.
    with pytest.raises(svc.ArtifactRefused) as raised:
        svc.resolve_presentation(
            chain.conn,
            {"kind": "visualization_template_version", "version_id": template_id},
            org_id=chain.org_id,
            project_id=chain.project_id,
        )
    assert raised.value.as_dict()["code"] == "unresolvable_pin"
    chain.conn.rollback()


def test_the_same_document_written_twice_produces_the_same_content_hash(chain):
    """AC1's first half. The canonical serializer is one authority, not two."""
    first = chain.template()
    second = chain.template()
    _v1, hash_one = chain.template_version(first)
    _v2, hash_two = chain.template_version(second)
    assert hash_one == hash_two
    #  And key order in the document does not move it: the hash is of the MEANING.
    reordered = dict(reversed(list(_template_document().items())))
    assert svc.canonical_hash(reordered) == hash_one
    chain.conn.rollback()


def test_an_edit_appends_a_version_and_names_its_predecessor(chain):
    template_id = chain.template()
    first, first_hash = chain.template_version(template_id)
    second, second_hash = chain.template_version(
        template_id,
        version_number=2,
        predecessor=first,
        document=_template_document(family="line"),
    )
    assert second_hash != first_hash
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT id, predecessor_version_id, content_hash "
            "FROM app.visualization_template_versions WHERE template_id = %s "
            "ORDER BY version_number",
            (template_id,),
        )
        rows = cur.fetchall()
    #  The first version is still there, byte for byte. That is the whole point of
    #  appending rather than rewriting.
    assert [row[0] for row in rows] == [first, second]
    assert [row[1] for row in rows] == [None, first]
    assert rows[0][2] == first_hash
    chain.conn.rollback()


@pytest.mark.parametrize(
    ("version_number", "predecessor"),
    [(1, "vtv_INVENTED"), (2, None)],
)
def test_a_version_without_its_lineage_is_refused(chain, version_number, predecessor):
    """Version 1 has no predecessor; every later version names one."""
    template_id = chain.template()
    with pytest.raises(Exception, match="lineage"):
        chain.template_version(
            template_id, version_number=version_number, predecessor=predecessor
        )
    chain.conn.rollback()


def test_the_document_of_a_version_cannot_be_updated(chain):
    """AC1: the guard is a trigger, not a service. A service can be bypassed."""
    template_id = chain.template()
    version_id, _hash = chain.template_version(template_id)
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute(
            "UPDATE app.visualization_template_versions SET document = %s::jsonb WHERE id = %s",
            (json.dumps(_template_document(family="line")), version_id),
        )
    chain.conn.rollback()


def test_a_version_cannot_be_deleted(chain):
    template_id = chain.template()
    version_id, _hash = chain.template_version(template_id)
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute(
            "DELETE FROM app.visualization_template_versions WHERE id = %s", (version_id,)
        )
    chain.conn.rollback()


def test_a_head_is_archived_rather_than_deleted(chain):
    template_id = chain.template()
    chain.template_version(template_id)
    #  A savepoint, not a rollback: the refusal leaves the transaction in error and
    #  the second half of this test needs the same seeded Project.
    with chain.conn.cursor() as cur:
        cur.execute("SAVEPOINT before_delete")
        with pytest.raises(Exception, match="archive"):
            cur.execute("DELETE FROM app.visualization_templates WHERE id = %s", (template_id,))
        cur.execute("ROLLBACK TO SAVEPOINT before_delete")
        #  Archiving is the gesture that exists instead, and it is not refused.
        cur.execute(
            "UPDATE app.visualization_templates SET archived_at = NOW() WHERE id = %s",
            (template_id,),
        )
        assert cur.rowcount == 1
    chain.conn.rollback()


def test_a_head_never_walks_its_pointer_backwards(chain):
    """The head guard is migration 154's, extended -- so it must actually fire."""
    template_id = chain.template()
    first, _h1 = chain.template_version(template_id)
    chain.template_version(template_id, version_number=2, predecessor=first)
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="NEWER version"):
        cur.execute(
            "UPDATE app.visualization_templates SET current_version_id = %s WHERE id = %s",
            (first, template_id),
        )
    chain.conn.rollback()


@pytest.mark.pg_owner
def test_the_immutability_of_a_version_is_the_trigger_and_nothing_else(live_postgres):
    """THE MUTATION. Kill the guard and the write goes through.

    Without this, `test_the_document_of_a_version_cannot_be_updated` proves only
    that SOMETHING refused -- a CHECK, a privilege, a typo in the fixture. Dropping
    the named trigger inside the transaction and watching the same UPDATE succeed
    is what identifies WHICH thing holds the invariant. The transaction is rolled
    back, so the guard is never actually gone.
    """
    chain = Chain(live_postgres).build()
    template_id = chain.template()
    version_id, _hash = chain.template_version(template_id)
    #  An edit the OTHER constraints accept, so the only thing that can refuse it
    #  is the immutability guard. Changing the family here would trip
    #  `ck_..._document_pins` instead, and the mutation would measure that.
    edited = json.dumps(_template_document(answers_question="Reworded, same family."))

    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT before_mutation")
        try:
            cur.execute(
                "UPDATE app.visualization_template_versions SET document = %s::jsonb "
                "WHERE id = %s",
                (edited, version_id),
            )
        except Exception as exc:  # noqa: BLE001 -- the refusal is the measurement
            refusal = str(exc)
        else:
            pytest.fail("the guard did not fire; there is nothing to mutate")
        cur.execute("ROLLBACK TO SAVEPOINT before_mutation")
        assert "immutable" in refusal

        cur.execute(
            "DROP TRIGGER trg_visualization_template_versions_immutable "
            "ON app.visualization_template_versions"
        )
        cur.execute(
            "UPDATE app.visualization_template_versions SET document = %s::jsonb WHERE id = %s",
            (edited, version_id),
        )
        assert cur.rowcount == 1, (
            "with the trigger dropped the UPDATE still failed, so the refusal above "
            "came from something else and this file measures the wrong guard"
        )
    live_postgres.rollback()


# ---------------------------------------------------------------------------
# AC2. A connector seed names both coordinates or neither -- the CHECK, never a
# service validation.
# ---------------------------------------------------------------------------


def test_a_connector_seed_names_both_coordinates(chain):
    template_id = chain.template(
        seed_origin="connector_seed",
        seed_module_name="gsc",
        seed_template_id="clicks_by_query",
    )
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT seed_origin, seed_module_name, seed_template_id "
            "FROM app.visualization_templates WHERE id = %s",
            (template_id,),
        )
        assert cur.fetchone() == ("connector_seed", "gsc", "clicks_by_query")
    chain.conn.rollback()


@pytest.mark.parametrize(
    ("seed_origin", "module_name", "template_ref"),
    [
        ("connector_seed", "gsc", None),
        ("connector_seed", None, "clicks_by_query"),
        ("connector_seed", None, None),
        ("project", "gsc", "clicks_by_query"),
        ("project", "gsc", None),
        ("platform_seed", None, "clicks_by_query"),
    ],
)
def test_half_a_seed_reference_is_refused(chain, seed_origin, module_name, template_ref):
    """Half a seed reference is how a template claims a provenance nobody resolves."""
    with pytest.raises(Exception, match="seed_pair"):
        chain.template(
            seed_origin=seed_origin,
            seed_module_name=module_name,
            seed_template_id=template_ref,
        )
    chain.conn.rollback()


def test_an_origin_the_amendment_does_not_name_is_refused(chain):
    """Three provenances are ratified: platform, connector expert pack, Project."""
    with pytest.raises(Exception, match="seed_origin"):
        chain.template(seed_origin="explore")
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# AC3. A pin of template kind resolves, or it is refused and never stored.
# ---------------------------------------------------------------------------


def test_a_stored_template_version_is_read_back_by_the_pin(chain):
    """The lift, against a real catalogue: the table exists, so the kind is pinnable."""
    template_id = chain.template()
    version_id, _hash = chain.template_version(template_id)
    assert svc.render_contract_state(chain.conn)["unpinnable_kinds"] == []
    resolved = svc.resolve_presentation(
        chain.conn,
        {"kind": "visualization_template_version", "version_id": version_id},
        org_id=chain.org_id,
        project_id=chain.project_id,
    )
    assert resolved == {
        "presentation_kind": "visualization_template_version",
        "presentation_version_id": version_id,
        "presentation_absent_literal": None,
    }
    chain.conn.rollback()


def test_a_template_version_of_another_project_answers_like_an_absent_one(chain):
    """AC3's second half. Scope is part of the LOOKUP, never checked afterwards."""
    elsewhere = Chain(chain.conn, org_id=chain.org_id).build(seed_org=False)
    foreign_template = elsewhere.template()
    foreign_version, _hash = elsewhere.template_version(foreign_template)

    def refuse(version_id: str) -> dict:
        with pytest.raises(svc.ArtifactRefused) as raised:
            svc.resolve_presentation(
                chain.conn,
                {"kind": "visualization_template_version", "version_id": version_id},
                org_id=chain.org_id,
                project_id=chain.project_id,
            )
        return raised.value.as_dict()

    assert refuse(foreign_version) == refuse("vtv_ABSENT")
    chain.conn.rollback()


def test_a_fabricated_template_pin_is_refused_by_the_key_when_the_service_is_bypassed(chain):
    """The half a service cannot hold: a psql session, a repair script, a fixture.

    Migration 333 keys `presentation_version_id` for the first time. Before it this
    exact INSERT succeeded and stored a pin no read could ever resolve.
    """
    report_id, _version_id = _report(chain)
    with chain.conn.cursor() as cur, pytest.raises(
        Exception, match="fk_analysis_report_versions_presentation"
    ):
        cur.execute(
            """
            INSERT INTO app.analysis_report_versions
                (id, report_id, org_id, project_id, version_number, label, query_spec_id,
                 query_spec_version_id, presentation_kind, presentation_version_id,
                 content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, 'v2', %s, %s, 'visualization_template_version', %s,
                    %s, (SELECT current_version_id FROM app.analysis_reports WHERE id = %s),
                    'test')
            """,
            (
                _uid("repv"), report_id, chain.org_id, chain.project_id,
                chain.query_spec_id, chain.query_spec_version_id, "vtv_INVENTED",
                _HASH_B, report_id,
            ),
        )
    chain.conn.rollback()


def test_a_real_template_pin_is_held_by_the_key(chain):
    """The other direction, and it is what proves the registry trigger fires.

    Nothing writes `app.presentation_version_registry` by hand: inserting the
    version is what registers the pair.
    """
    report_id, _version_id = _report(chain)
    template_id = chain.template()
    version_id, _hash = chain.template_version(template_id)
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.presentation_version_registry "
            "WHERE presentation_kind = 'visualization_template_version' "
            "AND presentation_version_id = %s AND org_id = %s AND project_id = %s",
            (version_id, chain.org_id, chain.project_id),
        )
        assert cur.fetchone()[0] == 1, "the version was written but never registered"
        cur.execute(
            """
            INSERT INTO app.analysis_report_versions
                (id, report_id, org_id, project_id, version_number, label, query_spec_id,
                 query_spec_version_id, presentation_kind, presentation_version_id,
                 content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, 'v2', %s, %s, 'visualization_template_version', %s,
                    %s, (SELECT current_version_id FROM app.analysis_reports WHERE id = %s),
                    'test')
            """,
            (
                _uid("repv"), report_id, chain.org_id, chain.project_id,
                chain.query_spec_id, chain.query_spec_version_id, version_id,
                _HASH_B, report_id,
            ),
        )
    chain.conn.rollback()


def test_a_report_may_not_pin_a_template_version_of_another_project(chain):
    """The Project is IN the key, so the application layer being wrong is not enough."""
    report_id, _version_id = _report(chain)
    elsewhere = Chain(chain.conn, org_id=chain.org_id).build(seed_org=False)
    foreign_version, _hash = elsewhere.template_version(elsewhere.template())
    with chain.conn.cursor() as cur, pytest.raises(
        Exception, match="fk_analysis_report_versions_presentation"
    ):
        cur.execute(
            """
            INSERT INTO app.analysis_report_versions
                (id, report_id, org_id, project_id, version_number, label, query_spec_id,
                 query_spec_version_id, presentation_kind, presentation_version_id,
                 content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, 'v2', %s, %s, 'visualization_template_version', %s,
                    %s, (SELECT current_version_id FROM app.analysis_reports WHERE id = %s),
                    'test')
            """,
            (
                _uid("repv"), report_id, chain.org_id, chain.project_id,
                chain.query_spec_id, chain.query_spec_version_id, foreign_version,
                _HASH_B, report_id,
            ),
        )
    chain.conn.rollback()


def test_the_honest_absence_still_writes(chain):
    """MATCH SIMPLE: a version with no presentation is not a pin, and the key knows it.

    A key that refused this row would have made "No accepted presentation contract"
    unwritable, which is the state most Report versions are in today.
    """
    report_id, _version_id = _report(chain)
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_report_versions
                (id, report_id, org_id, project_id, version_number, label, query_spec_id,
                 query_spec_version_id, presentation_absent_literal, content_hash,
                 predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, 'v2', %s, %s,
                    'No accepted presentation contract', %s,
                    (SELECT current_version_id FROM app.analysis_reports WHERE id = %s),
                    'test')
            """,
            (
                _uid("repv"), report_id, chain.org_id, chain.project_id,
                chain.query_spec_id, chain.query_spec_version_id, _HASH_C, report_id,
            ),
        )
    chain.conn.rollback()


def test_a_family_the_shipped_registry_cannot_draw_is_refused_by_name(chain):
    """A template may not ask for one of the ten unbuilt families."""
    template_id = chain.template()
    with pytest.raises(Exception, match="ck_visualization_template_versions_family"):
        chain.template_version(
            template_id, document=_template_document(family="sankey"), family="sankey"
        )
    chain.conn.rollback()


def test_the_family_check_mirrors_the_shipped_registry_exactly():
    """One vocabulary, imported rather than transcribed -- measured on the SQL."""
    import pathlib
    import re

    from core.visualization_families import SPEC_SELECTABLE_FAMILY_IDS

    migration = (
        pathlib.Path(__file__).resolve().parents[3]
        / "infra"
        / "nango"
        / "migrations"
        / "333_a_chart_template_is_an_object_a_pin_can_reach.sql"
    ).read_text(encoding="utf-8")
    block = migration.split("ck_visualization_template_versions_family CHECK", 1)[1]
    listed = tuple(re.findall(r"'([^']+)'", block.split(")", 1)[0]))
    assert listed == SPEC_SELECTABLE_FAMILY_IDS


def test_a_bound_document_is_refused_because_that_is_a_visualization(chain):
    """The one line that makes it a template. Story 72.2 owns the walker; this is
    the outer shape of it, in the layer that cannot be bypassed."""
    template_id = chain.template()
    with chain.conn.cursor() as cur:
        for bound in (
            {"bindings": {"measure": ["clicks"]}},
            {"query_spec_version_id": "qsv_EXAMPLE"},
            {"result_id": "qr_EXAMPLE"},
        ):
            cur.execute("SAVEPOINT before_bound")
            with pytest.raises(Exception, match="is_unbound"):
                chain.template_version(template_id, document=_template_document(**bound))
            cur.execute("ROLLBACK TO SAVEPOINT before_bound")
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# AC4. The erasure hatch, carried by the trigger of each append-only table this
# migration created, in the same migration.
# ---------------------------------------------------------------------------


def test_the_erasure_deletes_what_immutability_otherwise_protects(chain):
    """An erasure is a different, human-gated, audited operation -- migration 099."""
    template_id = chain.template()
    version_id, _hash = chain.template_version(template_id)
    with chain.conn.cursor() as cur:
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        cur.execute(
            "UPDATE app.visualization_templates SET current_version_id = NULL WHERE id = %s",
            (template_id,),
        )
        cur.execute(
            "DELETE FROM app.visualization_template_versions WHERE id = %s", (version_id,)
        )
        assert cur.rowcount == 1
        cur.execute("DELETE FROM app.visualization_templates WHERE id = %s", (template_id,))
        assert cur.rowcount == 1
    chain.conn.rollback()


def test_both_guards_of_the_version_ledger_yield_to_the_erasure(chain):
    """Read from `pg_trigger`, not from the migration text: the row guard AND the
    truncate guard. A table whose rows are insert-once but whose contents can be
    emptied in one statement is not immutable, and a guard the erasure cannot pass
    makes a tenant's right to erasure impossible."""
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.tgname, pg_get_triggerdef(t.oid) ILIKE '%%rgpd_erasure%%'
            FROM pg_trigger t
            WHERE t.tgrelid = 'app.visualization_template_versions'::regclass
              AND NOT t.tgisinternal
              AND pg_get_triggerdef(t.oid) ILIKE '%%reject_analytical_evidence_mutation%%'
            ORDER BY t.tgname
            """
        )
        guards = cur.fetchall()
    assert [name for name, _ in guards] == [
        "trg_visualization_template_versions_immutable",
        "trg_visualization_template_versions_no_truncate",
    ]
    assert all(yields for _name, yields in guards)
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# The registry is the union it claims to be -- migration 330's own assertion,
# re-run here so a later writer cannot drift it.
# ---------------------------------------------------------------------------


def test_the_pin_registry_holds_exactly_what_the_two_ledgers_number(chain):
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM (
                SELECT 'visualization_spec_version' AS kind, v.id, v.org_id, v.project_id
                  FROM app.visualization_spec_versions v
                UNION ALL
                SELECT 'visualization_template_version', t.id, t.org_id, t.project_id
                  FROM app.visualization_template_versions t
            ) numbered
            WHERE NOT EXISTS (
                SELECT 1 FROM app.presentation_version_registry r
                 WHERE r.presentation_kind = numbered.kind
                   AND r.presentation_version_id = numbered.id
                   AND r.org_id = numbered.org_id
                   AND r.project_id = numbered.project_id)
            """
        )
        assert cur.fetchone()[0] == 0, "a numbered version is absent from the registry"
        cur.execute(
            """
            SELECT count(*) FROM app.presentation_version_registry r
             WHERE NOT EXISTS (
                    SELECT 1 FROM app.visualization_spec_versions v
                     WHERE r.presentation_kind = 'visualization_spec_version'
                       AND v.id = r.presentation_version_id
                       AND v.org_id = r.org_id AND v.project_id = r.project_id)
               AND NOT EXISTS (
                    SELECT 1 FROM app.visualization_template_versions t
                     WHERE r.presentation_kind = 'visualization_template_version'
                       AND t.id = r.presentation_version_id
                       AND t.org_id = r.org_id AND t.project_id = r.project_id)
            """
        )
        assert cur.fetchone()[0] == 0, "a registered pair names a version neither ledger carries"
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# The Report chain, seeded only where a test needs a carrier for the pin.
# ---------------------------------------------------------------------------


def _report(chain) -> tuple[str, str]:
    """org -> project -> Semantic View -> Query Spec version -> Report version 1.

    Seeded here rather than in `Chain.build` so the template tests -- which are
    most of this file -- do not pay for a chain they never touch.
    """
    view_id, view_version_id = _uid("sv"), _uid("svv")
    chain.query_spec_id, chain.query_spec_version_id = _uid("qs"), _uid("qsv")
    report_id, version_id = _uid("rep"), _uid("repv")
    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
            "VALUES (%s, %s, 'fixture_view', 'test')",
            (view_id, chain.project_id),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', 'fixture_view', 'Fixture view',
                    %s, %s, 'test')
            """,
            (view_version_id, view_id, chain.project_id, _HASH_A, _HASH_A),
        )
        cur.execute(
            "INSERT INTO app.query_specs (id, org_id, project_id, semantic_view_id, "
            "created_by) VALUES (%s, %s, %s, %s, 'test')",
            (chain.query_spec_id, chain.org_id, chain.project_id, view_id),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, %s, '{}'::jsonb, %s, 'test')
            """,
            (
                chain.query_spec_version_id, chain.query_spec_id, chain.org_id,
                chain.project_id, view_id, view_version_id, _HASH_A,
            ),
        )
        cur.execute(
            "INSERT INTO app.analysis_reports (id, org_id, project_id, label, created_by) "
            "VALUES (%s, %s, %s, 'Fixture report', 'test')",
            (report_id, chain.org_id, chain.project_id),
        )
        cur.execute(
            """
            INSERT INTO app.analysis_report_versions
                (id, report_id, org_id, project_id, version_number, label, query_spec_id,
                 query_spec_version_id, presentation_absent_literal, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, 'v1', %s, %s,
                    'No accepted presentation contract', %s, 'test')
            """,
            (
                version_id, report_id, chain.org_id, chain.project_id,
                chain.query_spec_id, chain.query_spec_version_id, _HASH_A,
            ),
        )
        cur.execute(
            "UPDATE app.analysis_reports SET current_version_id = %s WHERE id = %s",
            (version_id, report_id),
        )
    return report_id, version_id
