"""The Chart Template pin: refused while its registry is absent, RESOLVED once it exists.

WHY THIS FILE EXISTS. `presentation_kind = 'visualization_template_version'` has
been an accepted value of migration 154's CHECK since the day that migration
landed, `analysis_report_versions.presentation_version_id` carries no foreign key,
and `resolve_presentation` accepted the kind. So a Report version could be written
pinning a Chart Template version that nothing anywhere could resolve -- the exact
fabricated pin that function's own docstring says it refuses. It refused it for
the Visualization Spec and never for the Template.

Ratified 2026-08-31 (Jean, decision D4 of epic 72): refuse the kind by name while
the table is absent, symmetrically to the refusal already served for the Spec.

WHAT REDDENS HERE. The moment `resolve_presentation` accepts that kind again
against a deployment with no `app.visualization_template_versions`, the first test
fails and names it. And the day story 72.1 creates the table, the refusal LIFTS ON
ITS OWN -- the gate is a probe of the catalogue, never a constant somebody has to
remember to flip.

STORY 72.1 ADDED THE SECOND HALF, and it is a different question. Once a kind is
pinnable, "which registry answers this kind" is settled and "does that registry
hold THIS version, in THIS Project" is still open. Until 72.1 the two were
confused, so the day the table landed EVERY string would have been accepted again
-- the fabricated pin walking back in through the door that had just opened. The
lift tests below therefore go THROUGH the table-exists branch and assert what the
function does on the other side of it.

OFFLINE ON PURPOSE. Which branch the function takes for which catalogue is decided
entirely by `to_regclass`, and whether a pin resolves is one scoped SELECT. A fake
cursor answers both exactly, so this file runs with no PostgreSQL and gates every
session rather than only the ones with a database. The same properties are proved
against a real PostgreSQL, with the real keys, in
`test_visualization_templates_pg.py`.
"""

from __future__ import annotations

import pytest
from core import analyze_artifacts as svc

#: The two registries that DO ship. Named here so a test that wants "everything
#: except the Chart Template" says so rather than listing tables by accident.
_SHIPPED = (svc._VISUALIZATION_SPEC_TABLE, svc._RENDERER_REGISTRY_TABLE)

_ORG = "org_EXAMPLE"
_PROJECT = "proj_EXAMPLE"


class _Cursor:
    """Answers `to_regclass`, the build ledger, and one scoped version lookup."""

    def __init__(self, present: frozenset[str], rows: frozenset[tuple[str, str, str, str]]):
        self._present = present
        self._rows = rows
        self._row: tuple | None = None
        self._many: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql: str, params=None):
        if "to_regclass" in sql:
            self._row = (params[0] in self._present,)
            return
        if "FROM app.renderer_runtime_builds" in sql:
            self._many = []
            return
        if sql.startswith("SELECT 1 FROM app.") and "WHERE id = %s" in sql:
            table = sql.split("FROM ", 1)[1].split(" ", 1)[0]
            version_id, org_id, project_id = params
            self._row = (1,) if (table, version_id, org_id, project_id) in self._rows else None
            return
        raise AssertionError(f"this fake answers to_regclass, the ledger and one lookup: {sql}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._many


class _Conn:
    """A deployment: the tables it carries, and the versions those tables hold."""

    def __init__(self, *present: str, rows: tuple[tuple[str, str, str, str], ...] = ()):
        self._present = frozenset(present)
        self._rows = frozenset(rows)

    def cursor(self):
        return _Cursor(self._present, self._rows)


def _resolve(conn, payload, *, org_id: str = _ORG, project_id: str = _PROJECT):
    return svc.resolve_presentation(conn, payload, org_id=org_id, project_id=project_id)


def test_the_chart_template_kind_is_refused_while_its_registry_is_absent():
    """The repair. It names the story that unblocks it, and stores nothing."""
    with pytest.raises(svc.ArtifactRefused) as raised:
        _resolve(
            _Conn(*_SHIPPED),
            {"kind": "visualization_template_version", "version_id": "vtv_EXAMPLE"},
        )
    refused = raised.value.as_dict()
    assert refused["code"] == "presentation_kind_unavailable"
    #  The sentence names the gesture that works, never the driver's words and
    #  never the identifier the caller typed.
    assert refused["message"] == svc.CHART_TEMPLATE_PIN_UNAVAILABLE
    assert "vtv_EXAMPLE" not in refused["message"]
    #  And the reason names the owner, so a reader can tell "not built yet" from
    #  "broken" without asking anyone.
    assert [r["subject"] for r in refused["refusals"]] == ["chart_template_version"]
    assert "72.1" in refused["refusals"][0]["message"]
    assert svc._CHART_TEMPLATE_TABLE in refused["refusals"][0]["message"]


def test_the_visualization_spec_kind_is_still_accepted():
    """THE HALF THAT PROVES THE REFUSAL IS NARROW.

    Folding the Chart Template into `missing` would have flipped `available` to
    false and started refusing the Spec pin this deployment serves. It does not.
    """
    resolved = _resolve(
        _Conn(*_SHIPPED, rows=((svc._VISUALIZATION_SPEC_TABLE, "vsv_EXAMPLE", _ORG, _PROJECT),)),
        {"kind": "visualization_spec_version", "version_id": "vsv_EXAMPLE"},
    )
    assert resolved == {
        "presentation_kind": "visualization_spec_version",
        "presentation_version_id": "vsv_EXAMPLE",
        "presentation_absent_literal": None,
    }


def test_the_contract_state_names_the_object_with_no_table():
    """What the console renders. The banner had nothing to say before this."""
    state = svc.render_contract_state(_Conn(*_SHIPPED))
    assert state["available"] is True, "the replay contract ships and must stay available"
    assert state["missing"] == []
    assert state["pinnable_kinds"] == ["visualization_spec_version"]
    assert state["unpinnable_kinds"] == [
        {
            "kind": "visualization_template_version",
            "contract": "chart_template_version",
            "owner_story": "72.1",
            "missing_link": svc._CHART_TEMPLATE_TABLE,
        }
    ]


def test_the_refusal_lifts_by_itself_the_day_the_table_exists():
    """The gate is a PROBE, not a constant. Story 72.1 opens it by creating a table.

    This is the test that would catch the opposite failure -- a hard-coded "the
    Chart Template is not built" that keeps refusing after 72.1 lands. The pin it
    carries is a version the deployment HOLDS, so the assertion is about the gate
    and not about the pin: the pin has its own tests below.
    """
    conn = _Conn(
        *_SHIPPED,
        svc._CHART_TEMPLATE_TABLE,
        rows=((svc._CHART_TEMPLATE_TABLE, "vtv_EXAMPLE", _ORG, _PROJECT),),
    )
    assert svc.render_contract_state(conn)["unpinnable_kinds"] == []
    resolved = _resolve(
        conn, {"kind": "visualization_template_version", "version_id": "vtv_EXAMPLE"}
    )
    assert resolved["presentation_kind"] == "visualization_template_version"
    assert resolved["presentation_version_id"] == "vtv_EXAMPLE"
    assert resolved["presentation_absent_literal"] is None


def test_a_kind_that_is_not_a_presentation_kind_is_still_refused_by_name():
    """The registry map replaced a literal set; the older refusal must survive it."""
    with pytest.raises(svc.ArtifactRefused) as raised:
        _resolve(_Conn(*_SHIPPED), {"kind": "widget_template", "version_id": "x_EXAMPLE"})
    assert raised.value.as_dict()["code"] == "invalid_presentation"


# ---------------------------------------------------------------------------
# Story 72.1 AC3. The kind being pinnable is not the pin resolving.
# ---------------------------------------------------------------------------


def test_a_chart_template_version_that_does_not_exist_is_refused_and_never_stored():
    """The defect the open gate would have re-created on its first day.

    The table exists, so the kind is pinnable -- and this version is not in it.
    Accepting it would store a pin no read can resolve, which is the whole reason
    story 72.1 exists.
    """
    conn = _Conn(*_SHIPPED, svc._CHART_TEMPLATE_TABLE)
    with pytest.raises(svc.ArtifactRefused) as raised:
        _resolve(conn, {"kind": "visualization_template_version", "version_id": "vtv_GHOST"})
    refused = raised.value.as_dict()
    assert refused["code"] == "unresolvable_pin"
    #  The product noun, never the wire token, and never the identifier typed.
    assert refused["message"] == "this Chart Template version does not resolve in this Project"
    assert "vtv_GHOST" not in refused["message"]
    assert "visualization_template_version" not in refused["message"]
    reason = refused["refusals"][0]
    assert reason["subject"] == "version_id"
    #  AC3 asks for the code, the subject AND the remedy: a refusal names the
    #  gesture that repairs it, not the cause.
    assert reason["remedy"] and "Chart Template version" in reason["remedy"]


def test_a_chart_template_of_another_project_answers_exactly_like_an_absent_one():
    """AC3's second half, and it is the non-disclosure rule.

    The version exists -- in another Project. A caller that could tell "yours does
    not exist" from "somebody else has it" holds a tenant-enumeration oracle, so
    the two answers are the same object, byte for byte.
    """
    foreign = _Conn(
        *_SHIPPED,
        svc._CHART_TEMPLATE_TABLE,
        rows=((svc._CHART_TEMPLATE_TABLE, "vtv_ELSEWHERE", "org_OTHER", "proj_OTHER"),),
    )
    absent = _Conn(*_SHIPPED, svc._CHART_TEMPLATE_TABLE)
    with pytest.raises(svc.ArtifactRefused) as foreign_raised:
        _resolve(foreign, {"kind": "visualization_template_version", "version_id": "vtv_ELSEWHERE"})
    with pytest.raises(svc.ArtifactRefused) as absent_raised:
        _resolve(absent, {"kind": "visualization_template_version", "version_id": "vtv_ELSEWHERE"})
    assert foreign_raised.value.as_dict() == absent_raised.value.as_dict()


def test_the_spec_pin_is_judged_the_same_way_because_it_is_the_same_defect():
    """The class, not the instance.

    `presentation_version_id` had no foreign key for EITHER kind. Migration 333
    keys both, so a Spec pin this function waved through would be refused by
    `fk_analysis_report_versions_presentation` with a 23503 nobody can read --
    AI-338, reintroduced one table over.
    """
    conn = _Conn(*_SHIPPED)
    with pytest.raises(svc.ArtifactRefused) as raised:
        _resolve(conn, {"kind": "visualization_spec_version", "version_id": "vsv_GHOST"})
    refused = raised.value.as_dict()
    assert refused["code"] == "unresolvable_pin"
    assert refused["message"] == (
        "this Visualization Spec version does not resolve in this Project"
    )


def test_naming_no_presentation_at_all_still_writes_the_exact_literal():
    """The honest absence is not a pin and is never sent to the lookup."""
    resolved = _resolve(_Conn(*_SHIPPED), None)
    assert resolved == {
        "presentation_kind": None,
        "presentation_version_id": None,
        "presentation_absent_literal": svc.NO_PRESENTATION_CONTRACT,
    }
