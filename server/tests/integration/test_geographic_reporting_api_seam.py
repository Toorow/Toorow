"""build_asgi_app seams for Story 37.1 project geography contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from core.main import build_asgi_app
from starlette.testclient import TestClient

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

# ---------------------------------------------------------------------------
# AI-317: EVERY STATEMENT `PATCH /api/projects/{project_id}` ISSUES.
#
# Measured, not guessed. With `execute()` instrumented, the four seams below
# issue ZERO statements: each is refused above `conn.cursor()` --
# `_geographic_posture_retired_response` on the BODY (projects_api.py:628-629)
# and `_project_not_found_response` on the capability (projects_api.py:642-648).
# Three of the four assert exactly that, and the fourth reads the 404 that comes
# with it. The old dispatch was therefore answering nobody, and it had rotted
# accordingly:
#
#   * `SELECT COUNT(*) FROM app.datastreams` and
#     `SELECT id, name, slug, status, currency, timezone` -- neither statement
#     exists anywhere in `core/` any more (the impact preview went with the
#     retired door; the currency projection has no caller at all);
#   * the posture read got a TWO-value row against a THREE-column projection
#     (`local_markets`, geographic_reporting.py:490-493);
#   * the `INSERT INTO app.project_preferences` branch read `params[1]` and
#     `params[2]` from a statement that carries ONE parameter
#     (verification_prefs.py:37-44) -- an IndexError waiting for a caller;
#   * `UPDATE app.projects ... RETURNING` (projects_api.py:718-723) matched no
#     branch: it fell to the mute `else`, kept the previous statement's
#     `description`, and answered `row = None` -- "project not found".
#
# The inventory is the route as it stands today, so the fake is a tripwire: a
# refusal that stops refusing lands on a NAMED statement with a row shape the
# product really reads, instead of on a plausible answer, and a statement nobody
# taught it RAISES.
# ---------------------------------------------------------------------------
_PATCH_PROJECT = StatementInventory(
    "test_geographic_reporting_api_seam._Cursor",
    # geographic_reporting.py:490-493, via `_fetch_geographic_prefs`.
    geographic_prefs="select geographic_mode, local_market_country_codes, local_markets",
    # verification_prefs.py:60-66.
    verification_prefs="select verification_source_type, verification_source_id",
    # projects_api.py:659-661 -- the cross-project scope check on the vs id.
    datastream_owner="select project_id from app.datastreams",
    # projects_api.py:718-723. `app.projects set`, which `app.project_preferences
    # set` does not contain -- the two writes are told apart on the relation.
    project_update="update app.projects set",
    # projects_api.py:735-739 -- the vs-only branch, which reads instead of writing.
    project_read=(
        "select id, name, slug, status, description, org_id",
        "from app.projects",
    ),
    # verification_prefs.py:37-44 -- the row that must exist before the update.
    preferences_row="insert into app.project_preferences (project_id)",
    # verification_prefs.py:47-51.
    preferences_update="update app.project_preferences set",
)

# The nine columns `UPDATE ... RETURNING` and the vs-only `SELECT` both project,
# in that order (projects_api.py:718-723 and 735-739). One row, not two shapes:
# `_project_row_to_dict` zips it against the description the fake derives.
_PROJECT_ROW = (
    "proj_geo",
    "Geo project",
    "geo-project",
    "active",
    "A governed geography fixture",
    "org_EXAMPLE",
    None,
    datetime(2026, 7, 22, tzinfo=timezone.utc),
    datetime(2026, 7, 22, tzinfo=timezone.utc),
)


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        statement = _PATCH_PROJECT.match(sql)
        self.row = None
        # DERIVED from the statement -- the two hand-written column lists this
        # fake used to carry were a second copy of a projection it never read.
        # The two writes below report no result set, which is what `None` means.
        self.description = None if statement.startswith("preferences_") else describe(sql)
        if statement == "geographic_prefs":
            self.row = (
                self.connection.geographic_mode,
                list(self.connection.country_codes),
                None,
            )
        elif statement == "verification_prefs":
            self.row = (None, None, None)
        elif statement == "datastream_owner":
            # `None` is a real answer here, not an absence of one: the product
            # accepts an id that is not a Datastream (projects_api.py:662-666).
            self.row = self.connection.datastream_owner
        elif statement in ("project_update", "project_read"):
            self.row = _PROJECT_ROW

    def fetchone(self):
        return self.row

    def fetchall(self):
        # No statement in the inventory is read with `fetchall` -- every one of
        # them is a `fetchone`. Answering `[]` here would be the same silence
        # this fake was converted out of.
        raise AssertionError(
            "no statement of _PATCH_PROJECT is read with fetchall; "
            "the product path that would is not modelled here"
        )


class _Connection:
    def __init__(self):
        self.geographic_mode = "global"
        self.country_codes = []
        self.governed_count = 0
        # projects_api.py:659-666: an id that is not a Datastream is
        # accepted, so `None` is this fake's honest default answer.
        self.datastream_owner = None
        self.executed = []
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_patch_refuses_the_retired_geographic_posture_and_writes_nothing(monkeypatch):
    """The end-to-end proof that the legacy door is shut.

    This replaces the seam that asserted the opposite (PATCH 200, columns
    written, `project.geographic_posture.updated` audited). That behaviour was
    real and it moved nothing: `country_activation.governed_posture` reads the
    project capability and the PUBLISHED country projection, never these columns.
    """
    connection = _Connection()
    monkeypatch.setattr("core.db.get_connection", lambda: connection)
    monkeypatch.setattr(
        "core.admin_api._strict_project_capability_allowed",
        lambda conn, *, identity, project_id, minimum_capability, **_kwargs: True,
    )

    app = build_asgi_app()
    with patch("core.admin_api._check_auth", return_value=(True, "operator@test")):
        patch_response = TestClient(app).patch(
            "/api/projects/proj_geo",
            json={
                "geographic_mode": "local_markets",
                "local_market_country_codes": ["FR", "DE"],
            },
        )

    assert patch_response.status_code == 422
    assert patch_response.json()["code"] == "geographic_posture_retired"
    # Nothing written, nothing audited, nothing committed -- a refusal that left a
    # trace would be the same defect wearing a 422.
    assert not any(
        sql.startswith("INSERT INTO app.project_preferences")
        for sql, _params in connection.executed
    )
    assert not any(
        sql.startswith("INSERT INTO app.audit_log") for sql, _params in connection.executed
    )
    assert connection.committed is False


def test_the_refusal_does_not_depend_on_governed_datastreams(monkeypatch):
    """The impact preview went with the door it guarded.

    `geographic_preview_required` (409) existed because a direct PATCH could
    change the geography under governed Datastream plans. It cannot any more, so
    the answer is the same refusal whether or not the project has governed plans
    -- and the handler does not even look.
    """
    connection = _Connection()
    connection.governed_count = 1
    monkeypatch.setattr("core.db.get_connection", lambda: connection)
    monkeypatch.setattr(
        "core.admin_api._strict_project_capability_allowed",
        lambda conn, *, identity, project_id, minimum_capability, **_kwargs: True,
    )

    with patch("core.admin_api._check_auth", return_value=(True, "operator@test")):
        response = TestClient(build_asgi_app()).patch(
            "/api/projects/proj_geo",
            json={
                "geographic_mode": "local_markets",
                "local_market_country_codes": ["FR"],
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "geographic_posture_retired"
    assert not any(
        sql.startswith("SELECT COUNT(*) FROM app.datastreams")
        for sql, _params in connection.executed
    )


def test_the_retired_posture_refusal_discloses_no_project(monkeypatch):
    """Refused before the project is read, so it cannot answer "does it exist?".

    The retired fields are refused on the BODY, which means the answer is
    byte-identical for a project the caller may see and one they may not. That is
    a stronger non-disclosure than the 404 it replaces on this path, and it is
    asserted rather than assumed.
    """
    connection = _Connection()
    monkeypatch.setattr("core.db.get_connection", lambda: connection)
    monkeypatch.setattr(
        "core.admin_api._strict_project_capability_allowed",
        lambda conn, *, identity, project_id, minimum_capability, **_kwargs: False,
    )

    body = {"geographic_mode": "local_markets", "local_market_country_codes": ["FR"]}
    with patch("core.admin_api._check_auth", return_value=(True, "intruder@test")):
        client = TestClient(build_asgi_app())
        mine = client.patch("/api/projects/proj_geo", json=body)
        theirs = client.patch("/api/projects/proj_other", json=body)

    assert mine.status_code == theirs.status_code == 422
    assert mine.json() == theirs.json()
    assert connection.executed == []


def test_cross_project_patch_is_non_disclosing_and_does_not_mutate(monkeypatch):
    connection = _Connection()
    refusals = []
    monkeypatch.setattr("core.db.get_connection", lambda: connection)
    monkeypatch.setattr(
        "core.admin_api._strict_project_capability_allowed",
        lambda conn, *, identity, project_id, minimum_capability, **_kwargs: False,
    )
    monkeypatch.setattr(
        "core.projects_api.write_audit_row",
        lambda **kwargs: refusals.append(kwargs),
    )

    # A field that is still writable: this seam is about the capability check,
    # and a body refused on its shape would never reach it.
    with patch("core.admin_api._check_auth", return_value=(True, "intruder@test")):
        response = TestClient(build_asgi_app()).patch(
            "/api/projects/proj_other",
            json={"name": "Renamed by an intruder"},
        )

    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "Project not found"}
    assert not any(
        sql.startswith("INSERT INTO app.project_preferences")
        for sql, _params in connection.executed
    )
    # Two tests in this repository asserted OPPOSITE things about auditing a
    # refusal: this one required an `access_denied` row, and
    # `test_connector_patch_denial_has_no_mutation_commit_or_audit` requires none.
    # The newer posture is the one production implements -- `_patch_project` returns
    # the shared non-disclosing response without writing -- and it is the right one:
    # a caller who is refused must not be able to append to the audit log.
    assert refusals == []


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: the cursor above answers seven statements and REFUSES the rest.

    Its old fallthrough answered nothing at all -- `row = None` and whatever
    `description` the previous call had left behind. On this seam that reads as
    "project not found", which is the answer three of these tests are about, so
    a read that leaked past a refusal could have been mistaken for the refusal
    working.
    """
    assert (
        _PATCH_PROJECT.find(
            "SELECT geographic_mode, local_market_country_codes, local_markets "
            "FROM app.project_preferences WHERE project_id = %s"
        )
        == "geographic_prefs"
    )
    assert (
        _PATCH_PROJECT.find(
            "UPDATE app.projects SET name = %s, updated_at = NOW() WHERE id = %s "
            "RETURNING id, name, slug, status, description, org_id, "
            "active_configuration_version_id, created_at, updated_at"
        )
        == "project_update"
    )
    # Plausible on this route -- it is the shape of the impact preview the
    # retired posture used to run -- and the product issues it nowhere.
    with pytest.raises(UnknownStatement) as raised:
        _PATCH_PROJECT.match(
            "SELECT COUNT(*) FROM app.datastreams WHERE project_id = %s AND enabled = TRUE"
        )
    assert "count(*) from app.datastreams where project_id" in str(raised.value)
    assert "geographic_prefs" in str(raised.value)
