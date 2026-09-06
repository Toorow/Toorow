"""Story 72.5 -- the Chart Template reads and writes, against a real PostgreSQL.

WHY THESE CANNOT BE UNIT TESTS. What is proved here is what the DATABASE does
when the service is bypassed or when two statements have to agree:

  * AC15 -- a connector seed lands as `connector_seed` WITH both coordinates,
    running the gesture twice creates nothing the first run created, and the
    FIRST EDIT makes the head project-owned: `seed_origin` becomes `project` and
    migration 333's pair CHECK nulls both coordinates in the same statement. The
    seed VERSION is byte-for-byte what it was -- "a seed is read, never
    rewritten";
  * a version is APPENDED with its predecessor, never rewritten, and the head
    advances onto it;
  * the list states the version count and the current version the database
    holds, not a count a service kept.

Every test rolls back. Nothing is left behind, even on a disposable database.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core import chart_template_store as store
from core import connector_chart_template_seeds as seeds
from core.visualization_specs import VisualizationSpecRefused
from core.visualization_templates import CHART_TEMPLATE_CONTRACT_VERSION
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _document(**overrides) -> dict:
    document = {
        "spec_contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
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


class Scope:
    """org -> project, seeded here, so this file runs on an empty database."""

    def __init__(self, conn):
        self.conn = conn
        self.org_id = _uid("org")
        self.project_id = _uid("proj")

    def build(self) -> "Scope":
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 72.5 fixture', %s, 'active', 'test')",
                (self.org_id, self.org_id.replace("_", "-")),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 72.5 fixture', %s, 'test')",
                (self.project_id, self.org_id, self.project_id.replace("_", "-")),
            )
        return self


@pytest.fixture
def scope(live_postgres):
    built = Scope(live_postgres).build()
    yield built
    live_postgres.rollback()


@pytest.fixture
def connector_module(tmp_path: Path) -> Path:
    """One connector declaring one Chart Template, as DATA on disk."""
    directory = tmp_path / "example-connector" / seeds.SEED_DIRNAME
    directory.mkdir(parents=True)
    (directory / "spend_by_channel.json").write_text(
        json.dumps({"label": "Spend by channel", **_document()}), encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# The head and its versions.
# ---------------------------------------------------------------------------


def test_a_new_template_lands_project_owned_with_its_first_version(scope):
    created = store.create_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        label="Spend by channel",
        description=None,
        document=_document(),
        actor="owner@example.com",
    )
    rows = store.list_chart_templates(
        scope.conn, org_id=scope.org_id, project_id=scope.project_id
    )
    assert [row["id"] for row in rows] == [created["template_id"]]
    assert rows[0]["seed_origin"] == "project"
    assert rows[0]["current_version_id"] == created["version_id"]
    assert rows[0]["version_count"] == 1
    assert rows[0]["readable"] is True


def test_a_document_the_grammar_refuses_creates_no_head_at_all(scope):
    with pytest.raises(VisualizationSpecRefused):
        store.create_chart_template(
            scope.conn,
            org_id=scope.org_id,
            project_id=scope.project_id,
            label="Bound already",
            description=None,
            #  A template that binds a member is a Visualization.
            document={**_document(), "bindings": {"measure": ["mdm_EXAMPLE"]}},
            actor="owner@example.com",
        )
    #  A head with no version is unreadable as a presentation. Creating one and
    #  then failing would have left exactly that.
    assert store.list_chart_templates(
        scope.conn, org_id=scope.org_id, project_id=scope.project_id
    ) == []


def test_an_edit_appends_a_version_naming_its_predecessor(scope):
    created = store.create_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        label="Spend by channel",
        description=None,
        document=_document(),
        actor="owner@example.com",
    )
    second = store.append_chart_template_version(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
        document=_document(answers_question="Which channel moved the most?"),
        actor="owner@example.com",
    )
    assert second["version_number"] == 2
    assert second["predecessor_version_id"] == created["version_id"]
    assert second["content_hash"] != created["content_hash"]

    detail = store.load_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )
    assert [v["version_number"] for v in detail["versions"]] == [2, 1]
    #  Version 1 is byte-for-byte what it was: an edit succeeds a version, it
    #  never rewrites one.
    assert detail["versions"][1]["content_hash"] == created["content_hash"]
    assert detail["current_version_id"] == second["version_id"]


def test_the_version_a_successor_replaces_is_untouched_in_the_table(scope):
    """AI-347 -- the Presentation tab saves a SUCCESSOR, and rewrites nothing.

    The service's own answer is not the proof: what is read back here is the
    predecessor ROW -- its document, its hash, its family, its
    `predecessor_version_id` and its `created_at` -- after the edit landed. A
    version rewritten in place would answer the same thing through the service
    and a different thing here.
    """
    created = store.create_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        label="Spend by channel",
        description=None,
        document=_document(),
        actor="owner@example.com",
    )
    columns = (
        "document, content_hash, family, version_number, predecessor_version_id, created_at"
    )
    with scope.conn.cursor() as cur:
        cur.execute(
            f"SELECT {columns} FROM app.visualization_template_versions WHERE id = %s",
            (created["version_id"],),
        )
        before = cur.fetchone()

    #  Exactly what the editing screen posts: the same wells, at the bounds the
    #  Bar family holds, with the question and one bound changed.
    store.append_chart_template_version(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
        document=_document(
            answers_question="Which channel moved the most?",
            requires={
                "measure": {"min": 1, "max": 3},
                "dimension": {"min": 1, "max": 1, "max_cardinality": 50},
            },
        ),
        actor="owner@example.com",
    )

    with scope.conn.cursor() as cur:
        cur.execute(
            f"SELECT {columns} FROM app.visualization_template_versions WHERE id = %s",
            (created["version_id"],),
        )
        assert cur.fetchone() == before
        #  And the successor NAMES it rather than replacing it.
        cur.execute(
            "SELECT version_number, predecessor_version_id "
            "FROM app.visualization_template_versions "
            "WHERE template_id = %s ORDER BY version_number",
            (created["template_id"],),
        )
        assert cur.fetchall() == [(1, None), (2, created["version_id"])]


def test_a_template_of_another_project_does_not_resolve(scope):
    created = store.create_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        label="Spend by channel",
        description=None,
        document=_document(),
        actor="owner@example.com",
    )
    other = Scope(scope.conn).build()
    from core.visualization_specs import VisualizationNotFound  # noqa: PLC0415

    with pytest.raises(VisualizationNotFound):
        store.load_chart_template(
            scope.conn,
            org_id=other.org_id,
            project_id=other.project_id,
            template_id=created["template_id"],
        )


# ---------------------------------------------------------------------------
# AC15 -- the connector seeds it, and never owns it.
# ---------------------------------------------------------------------------


def test_a_connector_seed_lands_with_both_coordinates_and_is_idempotent(scope, connector_module):
    first = seeds.seed_connector_chart_templates(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        module_name="example-connector",
        root=connector_module,
    )
    assert len(first["created"]) == 1

    rows = store.list_chart_templates(
        scope.conn, org_id=scope.org_id, project_id=scope.project_id
    )
    assert rows[0]["seed_origin"] == "connector_seed"
    assert rows[0]["seed"] == {
        "module_name": "example-connector",
        "template_id": "spend_by_channel",
    }
    assert rows[0]["origin_label"] == "Seeded by example-connector"

    second = seeds.seed_connector_chart_templates(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        module_name="example-connector",
        root=connector_module,
    )
    #  Nothing created, nothing rewritten. A Report may already pin the version
    #  the first run wrote.
    assert second["created"] == []
    assert len(second["already_present"]) == 1
    assert (
        len(
            store.list_chart_templates(
                scope.conn, org_id=scope.org_id, project_id=scope.project_id
            )
        )
        == 1
    )


def test_the_first_edit_of_a_connector_seed_makes_it_project_owned(scope, connector_module):
    seeded = seeds.seed_connector_chart_templates(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        module_name="example-connector",
        root=connector_module,
    )
    head_id = seeded["created"][0]["template_id"]
    seed_version_id = seeded["created"][0]["version_id"]

    with scope.conn.cursor() as cur:
        cur.execute(
            "SELECT content_hash FROM app.visualization_template_versions WHERE id = %s",
            (seed_version_id,),
        )
        seed_hash_before = cur.fetchone()[0]

    edited = store.append_chart_template_version(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=head_id,
        document=_document(answers_question="Which channel moved the most?"),
        actor="owner@example.com",
    )
    assert edited["became_project_owned"] is True

    with scope.conn.cursor() as cur:
        cur.execute(
            "SELECT seed_origin, seed_module_name, seed_template_id "
            "FROM app.visualization_templates WHERE id = %s",
            (head_id,),
        )
        #  Migration 333's pair CHECK forces both coordinates to NULL at the same
        #  instant. The database says what the target says: a connector never owns
        #  a Chart Template.
        assert cur.fetchone() == ("project", None, None)
        cur.execute(
            "SELECT content_hash FROM app.visualization_template_versions WHERE id = %s",
            (seed_version_id,),
        )
        assert cur.fetchone()[0] == seed_hash_before

    #  And re-running the gesture does NOT resurrect the seed on a head a person
    #  has taken over: the head id is derived, so it already exists.
    again = seeds.seed_connector_chart_templates(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        module_name="example-connector",
        root=connector_module,
    )
    assert again["created"] == []
    rows = store.list_chart_templates(
        scope.conn, org_id=scope.org_id, project_id=scope.project_id
    )
    assert [row["seed_origin"] for row in rows] == ["project"]


# ---------------------------------------------------------------------------
# AI-351 -- archive and restore. A version and a date, never a DELETE.
# ---------------------------------------------------------------------------


def _template(scope, label: str = "Spend by channel") -> dict:
    return store.create_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        label=label,
        description=None,
        document=_document(),
        actor="owner@example.com",
    )


def test_an_archived_template_keeps_every_version_readable_and_refuses_a_new_one(scope):
    created = _template(scope)

    state = store.archive_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )
    assert state["archived"] is True
    assert state["unchanged"] is False
    assert state["archived_at"] is not None

    #  NOTHING WAS DELETED. The head loads, the version list is intact, and the
    #  current version is the one it had. This is the whole archive pattern of
    #  the product -- a date on the head -- and a test that only checked the
    #  refusal below would not notice a DELETE that also produced it.
    loaded = store.load_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )
    assert loaded["archived"] is True
    assert [v["id"] for v in loaded["versions"]] == [created["version_id"]]
    assert loaded["current_version_id"] == created["version_id"]
    assert loaded["readable"] is True

    #  What it costs, and the gesture that repairs it -- both named by the
    #  service, not by a screen.
    with pytest.raises(VisualizationSpecRefused) as refused:
        store.append_chart_template_version(
            scope.conn,
            org_id=scope.org_id,
            project_id=scope.project_id,
            template_id=created["template_id"],
            document=_document(answers_question="Where did it go, really?"),
            actor="owner@example.com",
        )
    body = refused.value.as_dict()
    assert body["code"] == "chart_template_archived"
    assert "restore" in " ".join(r["remedy"] or "" for r in body["refusals"]).lower()


def test_restoring_takes_the_new_version_again(scope):
    created = _template(scope)
    store.archive_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )

    state = store.restore_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )
    assert (state["archived"], state["archived_at"], state["unchanged"]) == (False, None, False)

    appended = store.append_chart_template_version(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
        document=_document(answers_question="Where did it go, really?"),
        actor="owner@example.com",
    )
    assert appended["version_number"] == 2
    assert appended["predecessor_version_id"] == created["version_id"]


def test_asking_twice_for_the_same_state_answers_the_state_and_writes_nothing(scope):
    created = _template(scope)
    first = store.archive_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )
    second = store.archive_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )
    #  A double-submit, or a second person clicking the same control, is answered
    #  with the state -- and the DATE does not move, which is what proves the
    #  second call wrote nothing.
    assert second["archived"] is True
    assert second["unchanged"] is True
    assert second["archived_at"] == first["archived_at"]

    #  Same rule the other way: restoring a live template is not an error.
    store.restore_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )
    again = store.restore_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )
    assert (again["archived"], again["unchanged"]) == (False, True)


@pytest.mark.parametrize("gesture", ["archive_chart_template", "restore_chart_template"])
def test_a_template_of_another_project_is_absent_rather_than_denied(scope, gesture):
    from core.visualization_specs import VisualizationNotFound  # noqa: PLC0415

    created = _template(scope)
    other = Scope(scope.conn).build()
    with pytest.raises(VisualizationNotFound):
        getattr(store, gesture)(
            scope.conn,
            org_id=other.org_id,
            project_id=other.project_id,
            template_id=created["template_id"],
        )
    #  And the template of the Project that owns it did not move.
    with scope.conn.cursor() as cur:
        cur.execute(
            "SELECT archived_at FROM app.visualization_templates WHERE id = %s",
            (created["template_id"],),
        )
        assert cur.fetchone()[0] is None


def test_archiving_a_seed_head_retires_it_here_and_never_mutates_the_seed(scope, connector_module):
    """A seed is made INACTIVE in this Project. It is not taken away from its origin.

    `append_chart_template_version` takes a head away from its seed because
    EDITING the document is what makes it project-owned. Archiving edits no
    document, so the three seed columns are untouched -- and the seed itself is
    `server/modules/<module>/chart_templates/*.json`, on disk, which nothing in
    this transaction can reach.
    """
    seeds.seed_connector_chart_templates(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        module_name="example-connector",
        root=connector_module,
    )
    head_id = seeds.seed_head_id("example-connector", "spend_by_channel", scope.project_id)

    store.archive_chart_template(
        scope.conn, org_id=scope.org_id, project_id=scope.project_id, template_id=head_id
    )
    with scope.conn.cursor() as cur:
        cur.execute(
            "SELECT seed_origin, seed_module_name, seed_template_id, archived_at "
            "FROM app.visualization_templates WHERE id = %s",
            (head_id,),
        )
        origin, module_name, seed_template_id, archived_at = cur.fetchone()
    assert (origin, module_name, seed_template_id) == (
        "connector_seed",
        "example-connector",
        "spend_by_channel",
    )
    assert archived_at is not None

    #  It leaves the live list and stays reachable under `include_archived`, so
    #  the way back the confirmation promises is a read that really answers.
    rows = store.list_chart_templates(
        scope.conn, org_id=scope.org_id, project_id=scope.project_id
    )
    assert head_id not in {row["id"] for row in rows}
    with_archived = store.list_chart_templates(
        scope.conn, org_id=scope.org_id, project_id=scope.project_id, include_archived=True
    )
    assert head_id in {row["id"] for row in with_archived}


# ---------------------------------------------------------------------------
# What an archived template does to a pin that already names one of its versions.
# ---------------------------------------------------------------------------


def test_a_pin_on_an_archived_template_version_still_resolves(scope):
    from core.analyze_artifacts import resolve_presentation  # noqa: PLC0415

    created = _template(scope)
    payload = {"kind": "visualization_template_version", "version_id": created["version_id"]}
    before = resolve_presentation(
        scope.conn, payload, org_id=scope.org_id, project_id=scope.project_id
    )
    store.archive_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )
    #  IDENTICAL. Archiving deletes nothing, so the pin an immutable Report
    #  version already carries is exactly as resolvable as it was -- that is the
    #  reason the archive can be a state at all rather than a destruction.
    assert (
        resolve_presentation(
            scope.conn, payload, org_id=scope.org_id, project_id=scope.project_id
        )
        == before
    )


def test_an_archived_template_version_is_no_longer_offered_for_a_new_pin_but_is_named(scope):
    from core.analyze_artifacts import (  # noqa: PLC0415
        archived_template_pins,
        list_pinnable_presentation_versions,
    )

    created = _template(scope)
    kind = "visualization_template_version"

    offered = list_pinnable_presentation_versions(
        scope.conn, org_id=scope.org_id, project_id=scope.project_id
    )
    assert created["version_id"] in {e["version_id"] for e in offered[kind]}
    assert (
        archived_template_pins(
            scope.conn,
            org_id=scope.org_id,
            project_id=scope.project_id,
            version_ids=[created["version_id"]],
        )
        == set()
    )

    store.archive_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )

    #  THE TWO FACTS, AND THEY ARE NOT THE SAME FACT. Not offered for a NEW pin;
    #  named as archived on a pin that already exists.
    offered = list_pinnable_presentation_versions(
        scope.conn, org_id=scope.org_id, project_id=scope.project_id
    )
    assert created["version_id"] not in {e["version_id"] for e in offered[kind]}
    assert archived_template_pins(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        version_ids=[created["version_id"]],
    ) == {created["version_id"]}

    #  Restoring puts it back on offer, with no other gesture.
    store.restore_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )
    offered = list_pinnable_presentation_versions(
        scope.conn, org_id=scope.org_id, project_id=scope.project_id
    )
    assert created["version_id"] in {e["version_id"] for e in offered[kind]}


def test_the_archived_state_of_a_pin_never_crosses_a_project_boundary(scope):
    from core.analyze_artifacts import archived_template_pins  # noqa: PLC0415

    created = _template(scope)
    store.archive_chart_template(
        scope.conn,
        org_id=scope.org_id,
        project_id=scope.project_id,
        template_id=created["template_id"],
    )
    other = Scope(scope.conn).build()
    #  Non-disclosing, the rule every read of this surface follows: another
    #  Project learns nothing about this one's archived heads.
    assert (
        archived_template_pins(
            scope.conn,
            org_id=other.org_id,
            project_id=other.project_id,
            version_ids=[created["version_id"]],
        )
        == set()
    )
