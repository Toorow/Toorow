"""Story 75-6 -- a presentation rides the cascade, against a real PostgreSQL.

WHY THESE CANNOT BE UNIT TESTS. What is proved here is what the DATABASE does:

  * an ORG override becomes visible on resolution while the DEFINITION row is
    untouched -- proved by reading the concept version's `content_hash` before and
    after and asserting it is the SAME string (the first `Incomplete if` of the
    amendment ratified 2026-09-05 in `docs/product-architecture/governance.md`);
  * a PROJECT override wins over an ORG one, LEAF BY LEAF, and `sources` says
    which scope each leaf came from;
  * clearing the project returns the organization's value -- and does it by
    APPENDING a version, so the history still holds both;
  * a refused colour, a refused operator and a refused PLATFORM scope name what is
    wrong and write nothing;
  * a PROJECT override naming a project of ANOTHER organization is refused by the
    database itself, not by a comment;
  * the render path serves the overridden format, and serves the STORED document
    byte for byte when nothing is overridden -- the definition's own format is a
    baseline a person reads, never a style the product applies behind them;
  * an override read that fails leaves the caller's transaction USABLE, so the
    render that called it can go on composing its figure;
  * a metric of another organization answers "not found", and a metric baseline is
    read at the most specific definition scope this caller can reach.

Every test rolls back. Nothing is left behind, even on a disposable database.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from core import presentation_extends as px
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


class Scope:
    """org -> project -> a published metric concept, so this file runs on an empty DB."""

    def __init__(self, conn):
        self.conn = conn
        self.org_id = _uid("org")
        self.other_org_id = _uid("org")
        self.project_id = _uid("proj")
        self.other_project_id = _uid("proj")
        self.concept_id = f"sc_{ULID()}"
        self.version_id = f"scv_{ULID()}"

    def _org(self, org_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 75-6 fixture', %s, 'active', 'test')",
                (org_id, org_id.replace("_", "-")),
            )

    def _project(self, project_id: str, org_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 75-6 fixture', %s, 'test')",
                (project_id, org_id, project_id.replace("_", "-")),
            )

    def build(self, *, definition_format: str | None = "currency") -> "Scope":
        self._org(self.org_id)
        self._org(self.other_org_id)
        self._project(self.project_id, self.org_id)
        self._project(self.other_project_id, self.other_org_id)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.semantic_concepts "
                "(id, project_id, kind, name, lifecycle_status, created_by) "
                "VALUES (%s, %s, 'metric', 'story_75_6_revenue', 'published', 'test')",
                (self.concept_id, self.project_id),
            )
            cur.execute(
                """
                INSERT INTO app.semantic_concept_versions
                    (id, concept_id, project_id, version_number, status, kind, name, label,
                     value_type, format, expression, aggregation, additivity_class,
                     content_hash, created_by)
                VALUES (%s, %s, %s, 1, 'published', 'metric', 'story_75_6_revenue',
                        'Revenue', 'money', %s, %s::jsonb, %s::jsonb, 'additive', %s, 'test')
                """,
                (
                    self.version_id,
                    self.concept_id,
                    self.project_id,
                    definition_format,
                    json.dumps({"op": "field", "field": "revenue"}),
                    json.dumps({"kind": "sum"}),
                    hashlib.sha256(self.version_id.encode()).hexdigest(),
                ),
            )
            cur.execute(
                "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
                (self.version_id, self.concept_id),
            )
        return self

    def metric(
        self,
        canonical_name: str,
        *,
        scope_level: str = "PLATFORM",
        org_id: str | None = None,
        project_id: str | None = None,
        format_text: str | None = "currency",
    ) -> str:
        """One `app.metric_definitions` row -- the OTHER object type of this rail."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.metric_definitions
                    (id, canonical_name, aggregation_type, additive, format,
                     scope_level, org_id, project_id, created_by)
                VALUES (%s, %s, 'sum', TRUE, %s, %s, %s, %s, 'test')
                """,
                (
                    f"metdef_{ULID()}",
                    canonical_name,
                    format_text,
                    scope_level,
                    org_id,
                    project_id,
                ),
            )
        return canonical_name

    def pinned_figure(self) -> tuple[str, str]:
        """A real pinned Query Spec version measuring this concept, and a figure on it.

        `apply_presentation_extends` is the function the render path actually
        calls, and nothing was calling it in a test: what it adds over
        `apply_display_to_document` is the READ of the pin -- which members of
        which stored plan are measures. A fabricated member list proves the merge
        and not the pin.

        Returns `(query_spec_version_id, visualization_spec_version_id)`.
        """
        view_id, view_version_id = _uid("sv"), _uid("svv")
        query_spec_id, query_spec_version_id = _uid("qs"), _uid("qsv")
        visualization_id, spec_version_id = _uid("viz"), _uid("vsv")
        digest = hashlib.sha256(query_spec_version_id.encode()).hexdigest()
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
                "VALUES (%s, %s, 'story_75_6_view', 'test')",
                (view_id, self.project_id),
            )
            cur.execute(
                """
                INSERT INTO app.semantic_view_versions
                    (id, view_id, project_id, version_number, status, name, label,
                     dependency_fingerprint, content_hash, created_by)
                VALUES (%s, %s, %s, 1, 'published', 'story_75_6_view', 'Story 75-6 view',
                        %s, %s, 'test')
                """,
                (view_version_id, view_id, self.project_id, digest, digest),
            )
            cur.execute(
                "INSERT INTO app.query_specs (id, org_id, project_id, semantic_view_id, "
                "created_by) VALUES (%s, %s, %s, %s, 'test')",
                (query_spec_id, self.org_id, self.project_id, view_id),
            )
            cur.execute(
                """
                INSERT INTO app.query_spec_versions
                    (id, query_spec_id, org_id, project_id, version_number,
                     semantic_view_id, semantic_view_version_id, spec, content_hash,
                     created_by)
                VALUES (%s, %s, %s, %s, 1, %s, %s, %s::jsonb, %s, 'test')
                """,
                (
                    query_spec_version_id,
                    query_spec_id,
                    self.org_id,
                    self.project_id,
                    view_id,
                    view_version_id,
                    json.dumps(
                        {
                            "measures": [
                                {"id": self.concept_id, "version_id": self.version_id}
                            ],
                            "dimensions": [],
                        }
                    ),
                    digest,
                ),
            )
            cur.execute(
                "INSERT INTO app.visualizations (id, org_id, project_id, query_spec_id, "
                "name, created_by) VALUES (%s, %s, %s, %s, 'Story 75-6 figure', 'test')",
                (visualization_id, self.org_id, self.project_id, query_spec_id),
            )
            cur.execute(
                """
                INSERT INTO app.visualization_spec_versions
                    (id, visualization_id, org_id, project_id, version_number,
                     query_spec_id, query_spec_version_id, spec_contract_version,
                     schema_version, family, spec, content_hash, created_by)
                VALUES (%s, %s, %s, %s, 1, %s, %s, 'visualization-spec.v1', 1, 'bar',
                        %s::jsonb, %s, 'test')
                """,
                (
                    spec_version_id,
                    visualization_id,
                    self.org_id,
                    self.project_id,
                    query_spec_id,
                    query_spec_version_id,
                    json.dumps(_document()),
                    digest,
                ),
            )
        return query_spec_version_id, spec_version_id

    def spec_version_hash(self, spec_version_id: str) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT content_hash FROM app.visualization_spec_versions WHERE id = %s",
                (spec_version_id,),
            )
            return cur.fetchone()[0]

    def definition_fingerprint(self) -> tuple:
        """What the DEFINITION row is, in the columns a presentation could corrupt."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT content_hash, format, display, unit FROM app.semantic_concept_versions "
                "WHERE id = %s",
                (self.version_id,),
            )
            return cur.fetchone()


@pytest.fixture
def scope(live_postgres):
    built = Scope(live_postgres).build()
    yield built
    live_postgres.rollback()


def _resolve(conn, scope: Scope, *, project_id: str | None) -> dict:
    return px.resolve_display(
        conn,
        org_id=scope.org_id,
        project_id=project_id,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
    )


# ---------------------------------------------------------------------------
# 1. The org override is visible, and the definition never moved.
# ---------------------------------------------------------------------------


def test_org_override_is_visible_and_the_definition_row_is_untouched(live_postgres, scope):
    before = scope.definition_fingerprint()
    baseline = _resolve(live_postgres, scope, project_id=scope.project_id)
    assert baseline["display"]["format"]["kind"] == "currency"
    assert baseline["sources"]["format.kind"] == px.SCOPE_PLATFORM

    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        scope_level=px.SCOPE_ORG,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"format": {"kind": "percent", "decimals": 1}},
        identity="owner@example.com",
    )

    resolved = _resolve(live_postgres, scope, project_id=scope.project_id)
    assert resolved["display"]["format"]["kind"] == "percent"
    assert resolved["display"]["format"]["decimals"] == 1
    assert resolved["sources"]["format.kind"] == px.SCOPE_ORG

    # THE CLAIM OF THE WHOLE STORY, and it is a string comparison, not a promise.
    assert scope.definition_fingerprint() == before


def test_the_override_is_invisible_to_another_organization(live_postgres, scope):
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        scope_level=px.SCOPE_ORG,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"format": {"kind": "percent"}},
        identity="owner@example.com",
    )
    foreign = px.resolve_display(
        live_postgres,
        org_id=scope.other_org_id,
        project_id=None,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
    )
    # Neither the neighbour's choice NOR the concept it was made on: the concept
    # belongs to a project of another organization and is not reachable from here,
    # so the answer is empty rather than half-true.
    assert foreign == {"display": {}, "sources": {}}


# ---------------------------------------------------------------------------
# 2. The project wins, leaf by leaf.
# ---------------------------------------------------------------------------


def test_project_wins_over_org_leaf_by_leaf(live_postgres, scope):
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        scope_level=px.SCOPE_ORG,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={
            "format": {"kind": "currency", "currency_display": "code", "decimals": 2},
            "color": {"series": "#1F77B4"},
        },
        identity="owner@example.com",
    )
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=scope.project_id,
        scope_level=px.SCOPE_PROJECT,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"format": {"decimals": 0}},
        identity="owner@example.com",
    )

    resolved = _resolve(live_postgres, scope, project_id=scope.project_id)
    fmt = resolved["display"]["format"]
    assert fmt["decimals"] == 0                      # the project's
    assert fmt["currency_display"] == "code"         # still the organization's
    assert resolved["display"]["color"]["series"] == "#1f77b4"
    assert resolved["sources"]["format.decimals"] == px.SCOPE_PROJECT
    assert resolved["sources"]["format.currency_display"] == px.SCOPE_ORG
    assert resolved["sources"]["color.series"] == px.SCOPE_ORG


def test_a_default_filter_merges_whole_never_half(live_postgres, scope):
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        scope_level=px.SCOPE_ORG,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"default_filter": {"field": "country", "op": "eq", "value": "FR"}},
        identity="owner@example.com",
    )
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=scope.project_id,
        scope_level=px.SCOPE_PROJECT,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"default_filter": {"field": "channel", "op": "in", "value": ["paid"]}},
        identity="owner@example.com",
    )
    resolved = _resolve(live_postgres, scope, project_id=scope.project_id)
    assert resolved["display"]["default_filter"] == {
        "field": "channel",
        "op": "in",
        "value": ["paid"],
    }
    assert resolved["sources"]["default_filter"] == px.SCOPE_PROJECT


# ---------------------------------------------------------------------------
# 3. Clearing returns to the parent, and it is a version.
# ---------------------------------------------------------------------------


def test_clearing_the_project_returns_the_org_and_keeps_the_history(live_postgres, scope):
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        scope_level=px.SCOPE_ORG,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"format": {"kind": "percent"}},
        identity="owner@example.com",
    )
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=scope.project_id,
        scope_level=px.SCOPE_PROJECT,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"format": {"kind": "duration"}},
        identity="owner@example.com",
    )
    assert _resolve(live_postgres, scope, project_id=scope.project_id)["display"]["format"][
        "kind"
    ] == "duration"

    px.clear_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=scope.project_id,
        scope_level=px.SCOPE_PROJECT,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        identity="owner@example.com",
    )

    resolved = _resolve(live_postgres, scope, project_id=scope.project_id)
    assert resolved["display"]["format"]["kind"] == "percent"
    assert resolved["sources"]["format.kind"] == px.SCOPE_ORG

    versions = px.history(
        live_postgres,
        org_id=scope.org_id,
        project_id=scope.project_id,
        scope_level=px.SCOPE_PROJECT,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
    )
    # NOT a DELETE: both the choice and the clearing of it are still readable.
    assert [(v["version_number"], v["cleared"]) for v in versions] == [(2, True), (1, False)]


def test_a_stored_version_cannot_be_rewritten(live_postgres, scope):
    written = px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        scope_level=px.SCOPE_ORG,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"format": {"kind": "percent"}},
        identity="owner@example.com",
    )
    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT immutable_probe")
        with pytest.raises(Exception) as caught:
            cur.execute(
                "UPDATE app.presentation_override_versions SET display = '{}'::jsonb "
                "WHERE id = %s",
                (written["version"]["id"],),
            )
        assert "immutable" in str(caught.value).lower()
        cur.execute("ROLLBACK TO SAVEPOINT immutable_probe")


# ---------------------------------------------------------------------------
# 4. Refusals, each by name, each writing nothing.
# ---------------------------------------------------------------------------


def _refuse(live_postgres, scope, display):
    with pytest.raises(px.PresentationRefused) as caught:
        px.set_override(
            live_postgres,
            org_id=scope.org_id,
            project_id=None,
            scope_level=px.SCOPE_ORG,
            object_type=px.OBJECT_SEMANTIC_CONCEPT,
            object_id=scope.concept_id,
            display=display,
            identity="owner@example.com",
        )
    return caught.value


def test_an_invalid_colour_is_refused_by_name(live_postgres, scope):
    exc = _refuse(live_postgres, scope, {"color": {"series": "rebeccapurple"}})
    codes = {r["code"] for r in exc.refusals}
    assert "invalid_colour" in codes
    assert any(r["subject"] == "/display/color/series" for r in exc.refusals)
    assert _resolve(live_postgres, scope, project_id=None)["sources"] == {
        "format.kind": px.SCOPE_PLATFORM
    }


def test_an_invalid_filter_operator_is_refused_by_name(live_postgres, scope):
    exc = _refuse(
        live_postgres,
        scope,
        {"default_filter": {"field": "country", "op": "regex", "value": "FR"}},
    )
    codes = {r["code"] for r in exc.refusals}
    assert "invalid_filter_op" in codes


def test_out_of_range_decimals_and_unknown_leaves_are_refused_together(live_postgres, scope):
    exc = _refuse(live_postgres, scope, {"format": {"decimals": 12, "kind": "loud"}})
    codes = {r["code"] for r in exc.refusals}
    # EVERY reason at once, never just the first.
    assert {"invalid_decimals", "invalid_format_kind"} <= codes


def test_the_platform_scope_cannot_be_written(live_postgres, scope):
    with pytest.raises(px.PresentationRefused) as caught:
        px.set_override(
            live_postgres,
            org_id=scope.org_id,
            project_id=None,
            scope_level=px.SCOPE_PLATFORM,
            object_type=px.OBJECT_SEMANTIC_CONCEPT,
            object_id=scope.concept_id,
            display={"format": {"kind": "percent"}},
            identity="owner@example.com",
        )
    assert caught.value.code == "platform_scope_forbidden"


def test_a_project_of_another_organization_is_refused_at_the_door(live_postgres, scope):
    """The store refuses BEFORE the insert, and with the non-disclosing answer.

    A concept of one organization cannot be dressed from a project of another, and
    the caller learns "not found" -- the same answer an absent concept gets, so the
    two cannot be compared to enumerate the platform.
    """
    with pytest.raises(px.PresentationNotFound):
        px.set_override(
            live_postgres,
            org_id=scope.org_id,
            project_id=scope.other_project_id,
            scope_level=px.SCOPE_PROJECT,
            object_type=px.OBJECT_SEMANTIC_CONCEPT,
            object_id=scope.concept_id,
            display={"format": {"kind": "percent"}},
            identity="owner@example.com",
        )


def test_the_database_itself_refuses_a_row_whose_project_is_another_orgs(
    live_postgres, scope
):
    """The second barrier, measured: the composite foreign key, not a comment."""
    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT foreign_project_probe")
        with pytest.raises(Exception) as caught:
            cur.execute(
                "INSERT INTO app.presentation_overrides "
                "(id, org_id, scope_level, project_id, object_type, object_id, created_by) "
                "VALUES (%s, %s, 'PROJECT', %s, 'semantic_concept', %s, 'test')",
                (
                    f"pxo_{ULID()}",
                    scope.org_id,
                    scope.other_project_id,
                    scope.concept_id,
                ),
            )
        assert "fk_presentation_overrides_project" in str(caught.value)
        cur.execute("ROLLBACK TO SAVEPOINT foreign_project_probe")


def test_an_object_this_scope_cannot_reach_answers_not_found(live_postgres, scope):
    with pytest.raises(px.PresentationNotFound):
        px.set_override(
            live_postgres,
            org_id=scope.org_id,
            project_id=None,
            scope_level=px.SCOPE_ORG,
            object_type=px.OBJECT_SEMANTIC_CONCEPT,
            object_id=f"sc_{ULID()}",
            display={"format": {"kind": "percent"}},
            identity="owner@example.com",
        )


# ---------------------------------------------------------------------------
# 5. The render path -- the ONE server-side place a resolved format reaches a
#    figure spec (`core/analyze_render_mcp.py`, `finalize_meta`).
# ---------------------------------------------------------------------------


def _document(number_style: str = "auto") -> dict:
    return {
        "spec_contract_version": "visualization-spec.v1",
        "schema_version": 1,
        "family": "bar",
        "bindings": {"measure": ["m"], "dimension": ["d"]},
        # The grammar's floor, and the database's: `ck_visualization_spec_versions
        # _document_pins` (migration 159) refuses a stored document without it, so
        # the fixture that STORES this one carries it too.
        "accessibility": {"table_fallback": "required"},
        "formatting": {
            "number_style": number_style,
            "date_style": "auto",
            "unit_source": "semantic_view",
        },
    }


def _served(live_postgres, scope, document):
    return px.apply_display_to_document(
        live_postgres,
        document=document,
        org_id=scope.org_id,
        project_id=scope.project_id,
        member_ids=[scope.concept_id],
    )


def test_with_no_override_the_served_document_is_the_stored_one_byte_for_byte(
    live_postgres, scope
):
    """The `Incomplete if` this file previously asserted the opposite of.

    The definition of this concept DOES carry a format (`currency`) -- the
    resolution below reads it, and says PLATFORM. But nobody chose it: a baseline
    is what a person is inheriting, not a decision a scope made, and serving it as
    a `number_style` would make every figure whose measure says `currency` differ
    from the document stored for it, with no override anywhere. So the served
    document is the stored object itself, and the bytes are compared.
    """
    resolved = _resolve(live_postgres, scope, project_id=scope.project_id)
    assert resolved["display"]["format"]["kind"] == "currency"
    assert resolved["sources"]["format.kind"] == px.SCOPE_PLATFORM

    document = _document()
    before = json.dumps(document, sort_keys=True)
    served = _served(live_postgres, scope, document)
    assert served is document
    assert json.dumps(served, sort_keys=True) == before


def test_the_figure_spec_carries_the_overridden_format(live_postgres, scope):
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=scope.project_id,
        scope_level=px.SCOPE_PROJECT,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"format": {"kind": "number", "compact": True}},
        identity="owner@example.com",
    )
    served = _served(live_postgres, scope, _document())
    assert served["formatting"]["number_style"] == "compact"


def test_a_chosen_number_style_outranks_a_scope_preference(live_postgres, scope):
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=scope.project_id,
        scope_level=px.SCOPE_PROJECT,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"format": {"kind": "percent"}},
        identity="owner@example.com",
    )
    document = _document("integer")
    served = _served(live_postgres, scope, document)
    assert served is document
    assert served["formatting"]["number_style"] == "integer"


def test_a_definition_without_a_recognised_format_leaves_the_document_alone(live_postgres):
    scope = Scope(live_postgres).build(definition_format="bigint")
    document = _document()
    assert _served(live_postgres, scope, document) is document
    live_postgres.rollback()


def test_apply_presentation_extends_reads_the_real_pin_and_moves_no_stored_hash(
    live_postgres, scope
):
    """The function the render path ACTUALLY calls, through a stored pin.

    `apply_display_to_document` takes the member list as an argument; this one
    READS it -- which members of which stored Query Spec version are measures --
    and nothing exercised that. The Visualization Spec version's `content_hash` is
    read before and after and must be the same string: what is extended is the
    copy handed to the runtime, never the stored version.
    """
    query_spec_version_id, spec_version_id = scope.pinned_figure()
    before = scope.spec_version_hash(spec_version_id)

    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=scope.project_id,
        scope_level=px.SCOPE_PROJECT,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"format": {"kind": "number", "decimals": 0}},
        identity="owner@example.com",
    )

    stored = _document()
    served = px.apply_presentation_extends(
        live_postgres,
        document=stored,
        org_id=scope.org_id,
        project_id=scope.project_id,
        query_spec_version_id=query_spec_version_id,
    )
    assert served["formatting"]["number_style"] == "integer"
    assert stored["formatting"]["number_style"] == "auto"
    assert scope.spec_version_hash(spec_version_id) == before


def test_apply_presentation_extends_serves_the_stored_document_with_no_override(
    live_postgres, scope
):
    query_spec_version_id, spec_version_id = scope.pinned_figure()
    before = scope.spec_version_hash(spec_version_id)
    stored = _document()
    served = px.apply_presentation_extends(
        live_postgres,
        document=stored,
        org_id=scope.org_id,
        project_id=scope.project_id,
        query_spec_version_id=query_spec_version_id,
    )
    assert served is stored
    assert scope.spec_version_hash(spec_version_id) == before


# ---------------------------------------------------------------------------
# 6. Fail-soft that does NOT poison the caller's transaction.
# ---------------------------------------------------------------------------


def _break_the_override_read(monkeypatch) -> None:
    """Make the override read fail the way a real one does: by ABORTING the transaction.

    Raising a plain Python exception would prove nothing -- the defect is that
    PostgreSQL puts the whole transaction in an error state, so the caller's NEXT
    statement is the one that fails. This runs a statement that really aborts it.
    """

    def _explode(conn, **_kwargs):
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM app.presentation_overrides_no_such_table")
        raise AssertionError("unreachable")

    monkeypatch.setattr(px, "_load_override_layers", _explode)


def test_a_failed_override_read_yields_the_baseline_and_a_usable_transaction(
    live_postgres, scope, monkeypatch
):
    _break_the_override_read(monkeypatch)
    resolved = _resolve(live_postgres, scope, project_id=scope.project_id)
    assert resolved["display"]["format"]["kind"] == "currency"
    assert resolved["sources"] == {"format.kind": px.SCOPE_PLATFORM}

    # THE ASSERTION OF THIS TEST. Without the SAVEPOINT this raises
    # `current transaction is aborted, commands ignored until end of transaction`.
    with live_postgres.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


def test_a_failed_read_mid_render_lets_the_render_complete(
    live_postgres, scope, monkeypatch
):
    query_spec_version_id, _spec_version_id = scope.pinned_figure()
    _break_the_override_read(monkeypatch)

    stored = _document()
    served = px.apply_presentation_extends(
        live_postgres,
        document=stored,
        org_id=scope.org_id,
        project_id=scope.project_id,
        query_spec_version_id=query_spec_version_id,
    )
    assert served is stored

    # The render goes on: the next read of the composition still answers.
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.semantic_concepts WHERE id = %s",
            (scope.concept_id,),
        )
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 7. The OTHER object type: a metric definition.
# ---------------------------------------------------------------------------


def test_a_metric_baseline_is_the_definitions_format_through_the_alias_table(
    live_postgres, scope
):
    """`integer` is the alias entry that also decides a leaf, so it is the one asked."""
    name = f"story_75_6_clicks_{ULID()}"
    scope.metric(name, format_text="integer")
    resolved = px.resolve_display(
        live_postgres,
        org_id=scope.org_id,
        project_id=scope.project_id,
        object_type=px.OBJECT_METRIC,
        object_id=name,
    )
    assert resolved["display"]["format"] == {"kind": "number", "decimals": 0}
    assert resolved["sources"]["format.decimals"] == px.SCOPE_PLATFORM


def test_a_metric_baseline_reads_the_most_specific_definition_this_scope_reaches(
    live_postgres, scope
):
    """`_SCOPE_RANK` honoured: an ORG definition is what this organization is served."""
    name = f"story_75_6_spend_{ULID()}"
    scope.metric(name, format_text="currency")
    scope.metric(name, scope_level="ORG", org_id=scope.org_id, format_text="percent")
    resolved = px.resolve_display(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        object_type=px.OBJECT_METRIC,
        object_id=name,
    )
    assert resolved["display"]["format"]["kind"] == "percent"

    # The neighbour still reads the platform default: an ORG definition is one org's.
    foreign = px.resolve_display(
        live_postgres,
        org_id=scope.other_org_id,
        project_id=None,
        object_type=px.OBJECT_METRIC,
        object_id=name,
    )
    assert foreign["display"]["format"]["kind"] == "currency"


def test_an_override_dresses_a_metric_and_the_definition_row_is_untouched(
    live_postgres, scope
):
    name = f"story_75_6_impressions_{ULID()}"
    scope.metric(name, format_text="currency")
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        scope_level=px.SCOPE_ORG,
        object_type=px.OBJECT_METRIC,
        object_id=name,
        display={"format": {"kind": "number", "compact": True}},
        identity="owner@example.com",
    )
    resolved = px.resolve_display(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        object_type=px.OBJECT_METRIC,
        object_id=name,
    )
    assert resolved["display"]["format"]["kind"] == "number"
    assert resolved["display"]["format"]["compact"] is True
    assert resolved["sources"]["format.kind"] == px.SCOPE_ORG
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT format FROM app.metric_definitions WHERE canonical_name = %s",
            (name,),
        )
        assert cur.fetchone()[0] == "currency"


def test_a_metric_of_another_organization_answers_not_found(live_postgres, scope):
    """The existence oracle this rail had, measured.

    `OR project_id = COALESCE(%s, project_id)` is `project_id = project_id` when no
    project is named, so an ORG request reached EVERY project's metric of EVERY
    organization -- and a caller could learn that a metric exists next door by
    watching which of two identical calls refused.
    """
    name = f"story_75_6_secret_{ULID()}"
    scope.metric(
        name,
        scope_level="PROJECT",
        org_id=scope.other_org_id,
        project_id=scope.other_project_id,
        format_text="currency",
    )
    with pytest.raises(px.PresentationNotFound):
        px.set_override(
            live_postgres,
            org_id=scope.org_id,
            project_id=None,
            scope_level=px.SCOPE_ORG,
            object_type=px.OBJECT_METRIC,
            object_id=name,
            display={"format": {"kind": "percent"}},
            identity="owner@example.com",
        )
    assert px.resolve_display(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        object_type=px.OBJECT_METRIC,
        object_id=name,
    ) == {"display": {}, "sources": {}}


def test_a_metric_override_is_stored_and_read_back_but_dresses_no_figure(
    live_postgres, scope
):
    """The exclusion the amendment names, said in the payload rather than by silence."""
    name = f"story_75_6_reach_{ULID()}"
    scope.metric(name, format_text="currency")
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        scope_level=px.SCOPE_ORG,
        object_type=px.OBJECT_METRIC,
        object_id=name,
        display={"format": {"kind": "percent"}, "color": {"series": "#1F77B4"}},
        identity="owner@example.com",
    )
    resolved = px.resolve_display(
        live_postgres,
        org_id=scope.org_id,
        project_id=None,
        object_type=px.OBJECT_METRIC,
        object_id=name,
    )
    notes = px.application_notes(object_type=px.OBJECT_METRIC, resolved=resolved)
    assert notes["format"]["applied"] is False
    assert "metric" in notes["format"]["reason"]
    assert notes["color"]["applied"] is False
    assert notes["default_filter"]["applied"] is False


def test_the_application_notes_say_applied_only_for_a_concept_a_scope_dressed(
    live_postgres, scope
):
    baseline = _resolve(live_postgres, scope, project_id=scope.project_id)
    # A baseline is not an application: nobody chose it.
    assert (
        px.application_notes(object_type=px.OBJECT_SEMANTIC_CONCEPT, resolved=baseline)[
            "format"
        ]["applied"]
        is False
    )
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=scope.project_id,
        scope_level=px.SCOPE_PROJECT,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"format": {"kind": "percent"}},
        identity="owner@example.com",
    )
    chosen = _resolve(live_postgres, scope, project_id=scope.project_id)
    assert (
        px.application_notes(object_type=px.OBJECT_SEMANTIC_CONCEPT, resolved=chosen)[
            "format"
        ]["applied"]
        is True
    )

    # A duration has no style in the figure grammar: stored, resolved, not applied.
    px.set_override(
        live_postgres,
        org_id=scope.org_id,
        project_id=scope.project_id,
        scope_level=px.SCOPE_PROJECT,
        object_type=px.OBJECT_SEMANTIC_CONCEPT,
        object_id=scope.concept_id,
        display={"format": {"kind": "duration"}},
        identity="owner@example.com",
    )
    lasting = _resolve(live_postgres, scope, project_id=scope.project_id)
    assert (
        px.application_notes(object_type=px.OBJECT_SEMANTIC_CONCEPT, resolved=lasting)[
            "format"
        ]["applied"]
        is False
    )


# ---------------------------------------------------------------------------
# 8. The default filter's own grammar: shape, bounds, and NaN by name.
# ---------------------------------------------------------------------------


def test_a_list_under_a_scalar_operator_is_refused_by_name(live_postgres, scope):
    exc = _refuse(
        live_postgres,
        scope,
        {"default_filter": {"field": "country", "op": "eq", "value": ["FR", "BE"]}},
    )
    assert "invalid_filter_value_shape" in {r["code"] for r in exc.refusals}


def test_a_scalar_under_a_list_operator_is_refused_by_name(live_postgres, scope):
    exc = _refuse(
        live_postgres,
        scope,
        {"default_filter": {"field": "channel", "op": "in", "value": "paid"}},
    )
    assert "invalid_filter_value_shape" in {r["code"] for r in exc.refusals}


def test_a_value_longer_than_the_field_it_pre_fills_is_refused(live_postgres, scope):
    exc = _refuse(
        live_postgres,
        scope,
        {
            "default_filter": {
                "field": "country",
                "op": "eq",
                "value": "F" * (px.MAX_FILTER_VALUE_LENGTH + 1),
            }
        },
    )
    assert "invalid_filter_value" in {r["code"] for r in exc.refusals}


def test_not_a_number_is_refused_by_name(live_postgres, scope):
    exc = _refuse(
        live_postgres,
        scope,
        {"default_filter": {"field": "spend", "op": "gt", "value": float("nan")}},
    )
    assert any(
        r["code"] == "invalid_filter_value" and "NaN" in r["remedy"] for r in exc.refusals
    )


def test_an_infinity_inside_a_list_is_refused_by_name(live_postgres, scope):
    exc = _refuse(
        live_postgres,
        scope,
        {"default_filter": {"field": "spend", "op": "in", "value": [1, float("inf")]}},
    )
    assert "invalid_filter_value" in {r["code"] for r in exc.refusals}
