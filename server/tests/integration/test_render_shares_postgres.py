"""Story 50.7 -- the Share invariants, proved against a real PostgreSQL.

WHY NONE OF THIS CAN BE A MOCK. Every property below is a CHECK, a UNIQUE, a
composite foreign key, a trigger, a role grant or a transaction. A mocked cursor
accepts all of them cheerfully, which is exactly how a schema promise becomes a
comment. And a `skipped` line is not a pass: if `TEST_POSTGRES_DSN` is unset this
file proves nothing and says so.

TWO TRAPS THIS FILE IS WRITTEN AROUND, both recorded in
`scripts/disposable_postgres.py`:

  * A SUPERUSER bypasses RLS *and* role grants, so the 42501 assertion below would
    pass vacuously. `test_the_connected_role_is_ordinary` fails -- not skips -- if
    the connection is privileged, because a vacuous pass on an isolation test is
    worse than no test.
  * `conn.rollback()` destroys the fixtures. Each test that needs to keep its seed
    across a refusal rolls back to a SAVEPOINT instead.

WHAT IS NOT ASSERTED HERE, deliberately: any wall-clock duration. The
indistinguishability of the refusal paths is proved by counting SQL statements
(`test_every_refusal_path_costs_the_same_statements`), because a timing threshold
on shared hardware is an assertion any implementation can pass and any
implementation can be argued to fail.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")

_HASH_A = "a" * 64
_HASH_B = "b" * 64

# ---------------------------------------------------------------------------
# The runtime identity, READ OUT OF THE RUNTIME rather than retyped here.
#
# A Render pins the build that drew it, and Story 50.5's validator refuses a pin
# that is not the running build. A fixture with hand-typed pins would therefore
# either drift into a permanent build-mismatch panel or have to be loosened until
# it stopped proving anything. These four values are parsed from the runtime's own
# sources, so a bump lands here automatically and a rename fails loudly with the
# file it could not read. `ui/cards/shell/src/viz/__tests__/responsive.test.ts`
# reads the Python source for the same reason, in the other direction.
# ---------------------------------------------------------------------------

_VIZ = (
    pathlib.Path(__file__).resolve().parents[3] / "ui" / "cards" / "shell" / "src" / "viz"
)
_MIGRATION_248 = (
    pathlib.Path(__file__).resolve().parents[3]
    / "infra" / "nango" / "migrations" / "248_share_freezes_ai_path_evidence.sql"
)


def _ts_const(path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf'{name}\s*=\s*"([^"]+)"', text)
    if match is None:
        raise AssertionError(
            f"{name} is no longer a string literal in {path.name}; the Render pins in "
            "this fixture are read from it and cannot be guessed."
        )
    return match.group(1)


RUNTIME_BUILD = (
    "@toorow/card-shell/viz@"
    + _ts_const(_VIZ / "buildInfo.generated.ts", "RUNTIME_SEMVER")
    + "+"
    + _ts_const(_VIZ / "buildInfo.generated.ts", "RUNTIME_CONTENT_HASH")
)
THEME_VERSION = _ts_const(_VIZ / "buildInfo.ts", "THEME_VERSION")
FORMATTER_VERSION = _ts_const(_VIZ / "buildInfo.ts", "FORMATTER_VERSION")
#: `<family>/<renderer_id>@<semver>`, the shape `buildInfo.rendererBuild` builds.
RENDERER_BUILD = "table/toorow-table@" + _ts_const(_VIZ / "registry.ts", "RENDERER_SEMVER")

#: The frozen Result slice. Two columns and three rows -- small enough to read in a
#: failure message, real enough that a value can be asserted in the DOM.
RESULT_SCHEMA = {"fields": [{"name": "day"}, {"name": "sessions"}]}
RESULT_ROWS = [
    {"day": "2026-07-28", "sessions": 1412},
    {"day": "2026-07-29", "sessions": 1877},
    {"day": "2026-07-30", "sessions": 2043},
]
RESULT_MANIFEST = {
    "grain": "day",
    "freshness": "2026-07-30T00:00:00Z",
    "filters": ["country = FR"],
    "time_window": {"start": "2026-07-28", "end": "2026-07-30"},
    "comparison": "none",
    # No `truncation` key: nothing was truncated, and the runtime reads the PRESENCE
    # of that key as "the server returned part of the rows". A fixture that declared
    # `"truncation": "none"` made the share page show a truncation banner over a
    # complete answer -- an honest-looking disclosure that was false.
}

#: A `table` Visualization Spec document whose bindings name fields the Result
#: projection actually carries -- the validator refuses a binding that does not.
SPEC_DOCUMENT = {
    "spec_contract_version": "visualization-spec.v1",
    "schema_version": 1,
    "family": "table",
    "bindings": {"dimension": ["day"], "measure": ["sessions"]},
    "order": {"source": "result"},
    "top_n": None,
    "axes": {
        "x": {"scale": "categorical", "zero_baseline": False, "tick_density": "normal"},
        "y": {"scale": "linear", "zero_baseline": True, "tick_density": "normal"},
    },
    "legend": {"position": "none", "visible": False},
    "formatting": {"number_style": "auto", "date_style": "auto", "unit_source": "none"},
    "color": {"role": "none", "semantic_direction": "none"},
    "thresholds": [],
    "reference_lines": [],
    "annotations": [],
    "interactions": {
        "hover": True,
        "select": True,
        "zoom": False,
        "legend_toggle": False,
        "local_filter": False,
    },
    "evidence": {"datum_fields": ["sessions"], "mark_binding": "datum"},
    "responsive": {"profiles": ["console", "share"]},
    "accessibility": {"summary_source": "result_manifest", "table_fallback": "required"},
    "labels": {"override": {}},
}

# The pepper and the origin, for THIS module's tests only -- a module-level
# `os.environ.setdefault` wrote them for the whole session (AI-377).
from tests.support.render_share_env import render_share_env  # noqa: E402,F401


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


class Chain:
    """Org -> Project -> Semantic View -> Query Spec -> Result -> Spec -> Render.

    Seeded in full, inside the test's own transaction, so this file runs on a
    freshly migrated empty database instead of skipping when it finds nothing.
    """

    def __init__(self, conn, *, with_ai_path: bool = False):
        self.conn = conn
        self.with_ai_path = with_ai_path
        self.org_id = _uid("org")
        self.project_id = _uid("proj")
        self.view_id = _uid("sv")
        self.view_version_id = _uid("svv")
        self.query_spec_id = _uid("qs")
        self.query_spec_version_id = _uid("qsv")
        self.attempt_id = _uid("qea")
        self.result_id = _uid("qr")
        self.ai_path_id: str | None = None
        self.spec_version_id = _uid("vsv")
        self.render_id = _uid("rnd")

    def build(self) -> "Chain":
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 50.7 fixture', %s, 'active', 'test')",
                (self.org_id, self.org_id.replace("_", "-")),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 50.7 fixture', %s, 'test')",
                (self.project_id, self.org_id, self.project_id.replace("_", "-")),
            )
            # Migration 323: external sharing is a project-scoped capability and
            # its default is `forbidden`, so a Project that says nothing shares
            # nothing. This fixture says it explicitly -- the whole file is about
            # what happens AFTER the exit is allowed, and
            # `test_a_project_that_never_allowed_the_exit_refuses_the_request`
            # is the one that leaves the row out.
            cur.execute(
                "INSERT INTO app.project_preferences (project_id, external_sharing) "
                "VALUES (%s, 'allowed')",
                (self.project_id,),
            )
            if self.with_ai_path:
                from core import ai_paths  # noqa: PLC0415

                self.ai_path_id = ai_paths.begin_path(
                    self.conn,
                    org_id=self.org_id,
                    project_id=self.project_id,
                    actor="test@example.com",
                    policy_snapshot={"recorded_by": "share_fixture"},
                )["id"]
                ai_paths.append_step(
                    self.conn,
                    path_id=self.ai_path_id,
                    project_id=self.project_id,
                    step_kind="tool_call",
                    tool_name="execute_analyze_query_spec",
                    outcome="succeeded",
                )
                ai_paths.finalize_path(
                    self.conn,
                    path_id=self.ai_path_id,
                    project_id=self.project_id,
                    outcome="succeeded",
                )
            cur.execute(
                "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
                "VALUES (%s, %s, 'fixture_view', 'test')",
                (self.view_id, self.project_id),
            )
            cur.execute(
                "INSERT INTO app.semantic_view_versions (id, view_id, project_id, "
                "version_number, status, name, label, dependency_fingerprint, "
                "content_hash, created_by) VALUES (%s, %s, %s, 1, 'published', "
                "'fixture_view', 'Fixture', %s, %s, 'test')",
                (self.view_version_id, self.view_id, self.project_id, _HASH_A, _HASH_A),
            )
            cur.execute(
                "INSERT INTO app.semantic_compiled_artifacts (id, project_id, "
                "view_version_id, compiler_version, content_hash, queryability_matrix, "
                "ossie_projection, ossie_spec_version, toorow_extension_version) "
                "VALUES (%s, %s, %s, 'test', %s, '{}'::jsonb, '{}'::jsonb, '1', '1')",
                (_uid("sca"), self.project_id, self.view_version_id, _HASH_A),
            )
            cur.execute(
                "INSERT INTO app.query_specs (id, org_id, project_id, semantic_view_id, "
                "created_by) VALUES (%s, %s, %s, %s, 'test')",
                (self.query_spec_id, self.org_id, self.project_id, self.view_id),
            )
            cur.execute(
                "INSERT INTO app.query_spec_versions (id, query_spec_id, org_id, "
                "project_id, version_number, semantic_view_id, semantic_view_version_id, "
                "spec, content_hash, created_by) "
                "VALUES (%s, %s, %s, %s, 1, %s, %s, '{}'::jsonb, %s, 'test')",
                (
                    self.query_spec_version_id, self.query_spec_id, self.org_id,
                    self.project_id, self.view_id, self.view_version_id, _HASH_A,
                ),
            )
            cur.execute(
                "INSERT INTO app.query_execution_attempts (id, org_id, project_id, "
                "query_spec_version_id, result_id, requested_by) "
                "VALUES (%s, %s, %s, %s, %s, 'test')",
                (
                    self.attempt_id, self.org_id, self.project_id,
                    self.query_spec_version_id, self.result_id,
                ),
            )
            cur.execute(
                "INSERT INTO app.query_results (id, org_id, project_id, attempt_id, "
                "query_spec_version_id, outcome, ai_path_id, ai_path_absent_literal, "
                "content_hash, row_count, truncated, started_at, ended_at) "
                "VALUES (%s, %s, %s, %s, %s, 'success', %s, %s, %s, 3, FALSE, NOW(), NOW())",
                (
                    self.result_id, self.org_id, self.project_id, self.attempt_id,
                    self.query_spec_version_id, self.ai_path_id,
                    None if self.ai_path_id else "No AI path", _HASH_B,
                ),
            )
            visualization_id = _uid("viz")
            cur.execute(
                "INSERT INTO app.visualizations (id, org_id, project_id, query_spec_id, "
                "name, created_by) VALUES (%s, %s, %s, %s, 'Fixture', 'test')",
                (visualization_id, self.org_id, self.project_id, self.query_spec_id),
            )
            cur.execute(
                "INSERT INTO app.visualization_spec_versions (id, visualization_id, "
                "org_id, project_id, version_number, query_spec_id, query_spec_version_id, "
                "spec_contract_version, schema_version, family, spec, content_hash, "
                "created_by) VALUES (%s, %s, %s, %s, 1, %s, %s, 'visualization-spec.v1', "
                "1, 'table', %s::jsonb, %s, 'test')",
                (
                    self.spec_version_id, visualization_id, self.org_id, self.project_id,
                    self.query_spec_id, self.query_spec_version_id,
                    json.dumps(SPEC_DOCUMENT),
                    _HASH_A,
                ),
            )
            # The RETAINED payload. Without it a Share has nothing to freeze, and
            # `create_share` refuses -- which is the point of the refusal and the
            # reason this insert is part of the base fixture rather than an option.
            cur.execute(
                "INSERT INTO app.query_result_payloads (result_id, org_id, project_id, "
                "content_hash, result_schema, manifest, rows_chunk) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)",
                (
                    self.result_id, self.org_id, self.project_id, _HASH_B,
                    json.dumps(RESULT_SCHEMA),
                    json.dumps(RESULT_MANIFEST),
                    json.dumps(RESULT_ROWS),
                ),
            )
            cur.execute(
                "INSERT INTO app.renders (id, org_id, project_id, result_id, "
                "result_content_hash, visualization_spec_version_id, renderer_adapter, "
                "renderer_build_id, runtime_build_id, theme_version, formatter_version, "
                "responsive_profile, display_state, evidence_manifest, datum_evidence_keys, "
                "creation_surface, origin_kind, content_hash, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'toorow-table', %s, %s, %s, "
                "%s, 'share', '{}'::jsonb, %s::jsonb, %s::jsonb, 'explore', 'explore', "
                "%s, 'test')",
                (
                    self.render_id, self.org_id, self.project_id, self.result_id, _HASH_B,
                    self.spec_version_id,
                    RENDERER_BUILD, RUNTIME_BUILD, THEME_VERSION, FORMATTER_VERSION,
                    '{"freshness": "2026-07-30T00:00:00Z", "grain": "day"}',
                    json.dumps({"row:0:sessions": "ev_fixture_sessions"}),
                    _HASH_A,
                ),
            )
        return self

    def request_share(self, **overrides):
        """The FIRST half of the ceremony: a pending Share, and no link at all."""
        from core import render_shares  # noqa: PLC0415

        return render_shares.create_share(
            self.conn,
            org_id=overrides.get("org_id", self.org_id),
            project_id=overrides.get("project_id", self.project_id),
            render_id=overrides.get("render_id", self.render_id),
            actor=overrides.get("actor", "owner@example.com"),
            expires_at=overrides.get(
                "expires_at", datetime.now(timezone.utc) + timedelta(days=2)
            ),
            idempotency_key=overrides.get("idempotency_key", f"share-{ULID()}"),
        )

    def share(self, **overrides):
        """A LIVE Share: requested by one person, confirmed by a second.

        Since migration 323 that is the only way one exists, so every test below
        that needs a working link goes through both halves. A fixture that
        inserted an `active` row directly would be testing a state the product
        can no longer produce.
        """
        from core import render_shares  # noqa: PLC0415

        requested = self.request_share(**overrides)
        return render_shares.confirm_share(
            self.conn,
            org_id=overrides.get("org_id", self.org_id),
            project_id=overrides.get("project_id", self.project_id),
            share_id=requested.share_id,
            actor=overrides.get("confirmed_by", "second.holder@example.com"),
            idempotency_key=f"confirm-{requested.share_id}",
        )


@pytest.fixture()
def chain(live_postgres):
    built = Chain(live_postgres).build()
    yield built
    live_postgres.rollback()


@pytest.fixture()
def path_chain(live_postgres):
    built = Chain(live_postgres, with_ai_path=True).build()
    yield built
    live_postgres.rollback()


def _bearer_of(created) -> str:
    return created.delivery_url.split("#render=")[1]


class StatementCounter:
    """Count `cur.execute` calls on one connection.

    This is the falsifiable form of AC5's indistinguishability claim: the four
    refusal paths must issue the SAME number of statements. It replaces the timing
    assertion the story was repaired to remove.
    """

    def __init__(self, conn):
        self.conn = conn
        self.count = 0
        self._original = type(conn).cursor

    def __enter__(self):
        counter = self
        original = self._original

        class Counting:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, *args, **kwargs):
                counter.count += 1
                return self._inner.execute(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        type(self.conn).cursor = lambda conn_self, *a, **k: Counting(original(conn_self, *a, **k))
        return self

    def __exit__(self, *exc):
        type(self.conn).cursor = self._original


# ---------------------------------------------------------------------------
# The instrument itself, checked before anything is measured with it.
# ---------------------------------------------------------------------------


def test_the_connected_role_is_ordinary(live_postgres):
    """A superuser bypasses RLS and role grants, so every isolation test below
    would pass while proving nothing. This FAILS rather than skips."""
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
            "WHERE rolname = current_user"
        )
        role, superuser, bypassrls = cur.fetchone()
    assert not superuser, f"connected as superuser {role!r}: every RLS assertion here is vacuous"
    assert not bypassrls, f"{role!r} has BYPASSRLS: every RLS assertion here is vacuous"


# ---------------------------------------------------------------------------
# AC2 -- the schema refuses, not the application.
# ---------------------------------------------------------------------------


def test_migration_248_preserves_legacy_null_and_requires_new_object(live_postgres):
    """Run the reviewed migration itself over a pre-existing row, in pg_temp."""
    sql = _MIGRATION_248.read_text(encoding="utf-8")
    sql = sql.replace("BEGIN;", "").replace("COMMIT;", "")
    sql = sql.replace(
        "app.render_frozen_payloads", "pg_temp.legacy_render_frozen_payloads"
    )
    with live_postgres.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE legacy_render_frozen_payloads "
            "(render_id TEXT PRIMARY KEY) ON COMMIT DROP"
        )
        cur.execute(
            "INSERT INTO legacy_render_frozen_payloads (render_id) VALUES ('rnd_legacy')"
        )
        cur.execute(sql)
        # Idempotent when the migration runner retries after a transport failure.
        cur.execute(sql)
        cur.execute(
            "SELECT ai_path_evidence FROM legacy_render_frozen_payloads "
            "WHERE render_id = 'rnd_legacy'"
        )
        assert cur.fetchone()[0] is None

        cur.execute("SAVEPOINT new_null_payload")
        with pytest.raises(Exception) as exc:
            cur.execute(
                "INSERT INTO legacy_render_frozen_payloads (render_id) VALUES ('rnd_new_null')"
            )
        assert getattr(exc.value, "sqlstate", None) == "23514"
        cur.execute("ROLLBACK TO SAVEPOINT new_null_payload")
        cur.execute(
            "INSERT INTO legacy_render_frozen_payloads (render_id, ai_path_evidence) "
            "VALUES ('rnd_new', '{}'::jsonb)"
        )
    live_postgres.rollback()


def test_a_share_without_an_expiry_is_refused_by_the_schema(chain):
    """AD-20 says "revocable, EXPIRING and audited". The retired table had no
    expiry column at all, which is why every legacy link is unexpiring today."""
    with chain.conn.cursor() as cur, pytest.raises(Exception) as exc:
        cur.execute(
            "INSERT INTO app.render_shares (id, org_id, project_id, render_id, "
            "bearer_hash, expires_at, created_by, created_operation_id) "
            "VALUES (%s, %s, %s, %s, %s, NULL, 't', 'op_x')",
            (_uid("rsh"), chain.org_id, chain.project_id, chain.render_id, "c" * 64),
        )
    assert getattr(exc.value, "sqlstate", None) == "23502"
    chain.conn.rollback()


def test_a_share_expiring_before_it_was_created_is_refused(chain):
    with chain.conn.cursor() as cur, pytest.raises(Exception) as exc:
        cur.execute(
            "INSERT INTO app.render_shares (id, org_id, project_id, render_id, "
            "bearer_hash, expires_at, created_by, created_operation_id) "
            "VALUES (%s, %s, %s, %s, %s, NOW() - interval '1 day', 't', 'op_x')",
            (_uid("rsh"), chain.org_id, chain.project_id, chain.render_id, "d" * 64),
        )
    assert getattr(exc.value, "sqlstate", None) == "23514"
    chain.conn.rollback()


def test_a_share_whose_render_is_foreign_is_refused_by_the_composite_key(chain):
    """The FK is (render_id, org_id, project_id), so a Render from another Project
    cannot be named even when the application layer is wrong."""
    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, 'other', %s, 'active', 'test')",
            (_uid("org"), _uid("slug").replace("_", "-")),
        )
    with chain.conn.cursor() as cur, pytest.raises(Exception) as exc:
        cur.execute(
            "INSERT INTO app.render_shares (id, org_id, project_id, render_id, "
            "bearer_hash, expires_at, created_by, created_operation_id) "
            "VALUES (%s, %s, %s, %s, %s, NOW() + interval '1 day', 't', 'op_x')",
            (_uid("rsh"), chain.org_id, chain.project_id, "rnd_not_in_this_project", "e" * 64),
        )
    assert getattr(exc.value, "sqlstate", None) == "23503"
    chain.conn.rollback()


def test_the_share_table_names_no_object_other_than_a_render(live_postgres):
    """AC2: a Share cannot follow a Notebook, a Run, a Report, a Query Spec, a
    Result or a snapshot -- and the guarantee is the COLUMN LIST, not a convention.
    Asserted literally, because "we did not add one" is exactly the promise that
    erodes."""
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'app' AND table_name = 'render_shares'"
        )
        columns = {row[0] for row in cur.fetchall()}
    forbidden = {
        "notebook_id", "notebook_run_id", "report_id", "report_run_id",
        "query_spec_id", "query_spec_version_id", "result_id", "snapshot_id",
    }
    leaked = columns & forbidden
    assert not leaked, f"a Share must name only a Render; found {leaked}"
    assert "render_id" in columns


# ---------------------------------------------------------------------------
# proactive-assertions.md decision 2 -- the project-scoped capability, and the
# confirmation by a SECOND role holder. Criterion [7] of that page's
# `Incomplete if`.
# ---------------------------------------------------------------------------


def test_a_project_that_never_allowed_the_exit_refuses_the_request(chain):
    """The switch's DEFAULT, measured rather than assumed.

    A Project with no preferences row at all is the population every Project was
    in before migration 323, and it shares nothing: the refusal arrives before
    any freeze, and it names who can change that and where.
    """
    from core.project_external_sharing import ENABLE_GESTURE, ExternalSharingForbidden

    with chain.conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.project_preferences WHERE project_id = %s",
            (chain.project_id,),
        )
    with pytest.raises(ExternalSharingForbidden) as refused:
        chain.request_share()
    assert ENABLE_GESTURE in str(refused.value)
    assert "Manage role" in str(refused.value)
    chain.conn.rollback()


def test_a_project_that_forbids_the_exit_refuses_the_request(chain):
    from core.project_external_sharing import ExternalSharingForbidden

    with chain.conn.cursor() as cur:
        cur.execute(
            "UPDATE app.project_preferences SET external_sharing = 'forbidden' "
            "WHERE project_id = %s",
            (chain.project_id,),
        )
    with pytest.raises(ExternalSharingForbidden):
        chain.request_share()
    # Nothing was written on the way to the refusal.
    chain.conn.rollback()
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.render_shares WHERE project_id = %s",
            (chain.project_id,),
        )
        assert cur.fetchone()[0] == 0
    chain.conn.rollback()


def test_a_requested_share_is_pending_and_has_no_bearer_at_all(chain):
    """The guarantee is STRUCTURAL: with no `bearer_hash`, `exchange_bearer`'s
    UNIQUE lookup cannot resolve to this row. There is no branch to forget."""
    requested = chain.request_share()
    assert requested.state == "pending_confirmation"
    assert not hasattr(requested, "delivery_url")
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT state, bearer_hash FROM app.render_shares WHERE id = %s",
            (requested.share_id,),
        )
        assert cur.fetchone() == ("pending_confirmation", None)
    chain.conn.rollback()


def test_the_requester_cannot_confirm_their_own_request(chain):
    from core import render_shares  # noqa: PLC0415

    requested = chain.request_share(actor="owner@example.com")
    with pytest.raises(render_shares.RenderShareConfirmationRefused) as refused:
        render_shares.confirm_share(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            share_id=requested.share_id,
            actor="owner@example.com",
            idempotency_key=f"self-{requested.share_id}",
        )
    assert refused.value.code == "second_role_holder_required"
    chain.conn.rollback()


def test_the_schema_itself_refuses_a_self_confirmation(chain):
    """The code path above can be forgotten in a refactor. This cannot."""
    requested = chain.request_share(actor="owner@example.com")
    with chain.conn.cursor() as cur, pytest.raises(Exception) as exc:
        cur.execute(
            "UPDATE app.render_share_confirmations "
            "SET confirmed_by = 'owner@example.com', confirmed_at = NOW(), "
            "    confirmed_operation_id = 'op_x' WHERE share_id = %s",
            (requested.share_id,),
        )
    assert getattr(exc.value, "sqlstate", None) == "23514"
    chain.conn.rollback()


def test_a_second_holder_confirms_and_the_share_becomes_live(chain):
    """The state transition, audited, on the same row -- never a second row and
    never a delete (decision 4's rule, applied to the same object)."""
    from core import render_shares  # noqa: PLC0415

    requested = chain.request_share(actor="owner@example.com")
    confirmed = render_shares.confirm_share(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        share_id=requested.share_id,
        actor="second.holder@example.com",
        idempotency_key=f"confirm-{requested.share_id}",
    )
    assert confirmed.share_id == requested.share_id
    assert confirmed.state == "active"
    assert "#render=" in confirmed.delivery_url
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT state, bearer_hash FROM app.render_shares WHERE id = %s",
            (requested.share_id,),
        )
        state, digest = cur.fetchone()
        assert state == "active"
        assert digest == render_shares.bearer_hash(_bearer_of(confirmed))
        cur.execute(
            "SELECT count(*) FROM app.render_shares WHERE render_id = %s",
            (chain.render_id,),
        )
        assert cur.fetchone()[0] == 1
    chain.conn.rollback()


def test_one_confirmation_reference_joins_the_request_to_its_confirmation(chain):
    """Both halves are `human` with the SAME real reference, so the audit answers
    "who asked and who authorized" with one lookup and no new column."""
    from core import render_shares  # noqa: PLC0415

    requested = chain.request_share(actor="owner@example.com")
    render_shares.confirm_share(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        share_id=requested.share_id,
        actor="second.holder@example.com",
        idempotency_key=f"confirm-{requested.share_id}",
    )
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT command_type, actor, confirmation_mode "
            "  FROM app.operations "
            " WHERE confirmation_reference_hash = encode(digest(%s, 'sha256'), 'hex') "
            " ORDER BY command_type",
            (requested.confirmation_id,),
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["render_share.confirm", "render_share.create"]
    assert {r[2] for r in rows} == {"human"}
    assert {r[1] for r in rows} == {"owner@example.com", "second.holder@example.com"}
    chain.conn.rollback()


def test_a_confirmation_ticket_is_consumed_exactly_once(chain):
    from core import render_shares  # noqa: PLC0415

    requested = chain.request_share(actor="owner@example.com")
    render_shares.confirm_share(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        share_id=requested.share_id,
        actor="second.holder@example.com",
        idempotency_key=f"confirm-{requested.share_id}",
    )
    with pytest.raises(render_shares.RenderShareConfirmationRefused) as refused:
        render_shares.confirm_share(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            share_id=requested.share_id,
            actor="third.holder@example.com",
            idempotency_key=f"confirm-again-{requested.share_id}",
        )
    assert refused.value.code == "confirmation_already_used"
    chain.conn.rollback()


def test_a_closed_confirmation_window_refuses_and_names_the_repair(chain, monkeypatch):
    """The window is IMMUTABLE once the request exists -- the trigger of migration
    323 refuses an UPDATE of `expires_at` -- so this test moves the only thing a
    lapsed window can move: the clock."""
    from core import render_shares  # noqa: PLC0415

    requested = chain.request_share(actor="owner@example.com")

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=30)

    monkeypatch.setattr(render_shares, "datetime", Later)
    with pytest.raises(render_shares.RenderShareConfirmationRefused) as refused:
        render_shares.confirm_share(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            share_id=requested.share_id,
            actor="second.holder@example.com",
            idempotency_key=f"late-{requested.share_id}",
        )
    assert refused.value.code == "confirmation_window_closed"
    assert "Create a new share" in str(refused.value)
    chain.conn.rollback()


def test_a_pending_share_can_be_withdrawn_and_never_confirmed_after(chain):
    from core import render_shares  # noqa: PLC0415

    requested = chain.request_share(actor="owner@example.com")
    render_shares.revoke_share(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        share_id=requested.share_id,
        actor="owner@example.com",
        idempotency_key=f"withdraw-{requested.share_id}",
    )
    with pytest.raises(render_shares.RenderShareConfirmationRefused) as refused:
        render_shares.confirm_share(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            share_id=requested.share_id,
            actor="second.holder@example.com",
            idempotency_key=f"too-late-{requested.share_id}",
        )
    assert refused.value.code == "share_revoked"
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT state, bearer_hash FROM app.render_shares WHERE id = %s",
            (requested.share_id,),
        )
        assert cur.fetchone() == ("revoked", None)
    chain.conn.rollback()


def test_a_live_share_can_never_go_back_to_awaiting_confirmation(chain):
    created = chain.share()
    with chain.conn.cursor() as cur, pytest.raises(Exception) as exc:
        cur.execute(
            "UPDATE app.render_shares SET state = 'pending_confirmation' WHERE id = %s",
            (created.share_id,),
        )
    assert "awaiting confirmation" in str(exc.value)
    chain.conn.rollback()


def test_the_listing_says_who_may_confirm_and_who_may_not(chain):
    from core import render_shares  # noqa: PLC0415

    requested = chain.request_share(actor="owner@example.com")
    rows = render_shares.list_shares(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        render_id=chain.render_id,
        viewer="owner@example.com",
        viewer_may_confirm=True,
    )
    assert len(rows) == 1
    assert rows[0]["state"] == "pending_confirmation"
    assert rows[0]["confirmation_state"] == "pending"
    assert rows[0]["confirmation_requested_by"] == "owner@example.com"
    assert rows[0]["can_confirm"] is False
    assert rows[0]["awaiting_your_own_request"] is True

    other = render_shares.list_shares(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        render_id=chain.render_id,
        viewer="second.holder@example.com",
        viewer_may_confirm=True,
    )
    assert other[0]["can_confirm"] is True

    # A reader who holds only `viewer` is never offered the control, even though
    # they are a different person: the second holder must hold the ROLE too.
    read_only = render_shares.list_shares(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        render_id=chain.render_id,
        viewer="second.holder@example.com",
        viewer_may_confirm=False,
    )
    assert read_only[0]["can_confirm"] is False
    assert requested.share_id == rows[0]["share_id"]
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# AC5 -- single use, atomic, and equally cheap on every refusal.
# ---------------------------------------------------------------------------


def test_the_bearer_is_stored_only_as_a_peppered_hmac(chain):
    """A database dump must contain nothing from which a live link can be rebuilt."""
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    bearer = _bearer_of(created)
    with chain.conn.cursor() as cur:
        cur.execute("SELECT bearer_hash FROM app.render_shares WHERE id = %s", (created.share_id,))
        stored = cur.fetchone()[0]
    assert bearer not in stored
    assert stored == render_shares.bearer_hash(bearer)
    assert len(stored) == 64


def test_the_platform_role_may_become_the_share_reader(chain):
    """Migration 344. `render_share_connection()` issues `SET LOCAL ROLE
    toorow_share_reader` on the platform connection, so the platform role must be
    a member of the reader -- 162 granted it to the migration runner instead, and
    in production every exchange ended in InsufficientPrivilege (2026-09-04).
    """
    with chain.conn.cursor() as cur:
        cur.execute("SELECT pg_has_role('connector', 'toorow_share_reader', 'member')")
        assert cur.fetchone()[0] is True, "the platform role cannot become the share reader"


def test_a_dossier_share_exchanges_and_its_session_names_the_version(chain):
    """A Share opens exactly one target (341): a Render OR a Dossier version.

    Measured 2026-09-04 (G15-T07): the exchange looked up the Render only, so a
    dossier link a second holder had confirmed was refused as `render_unreadable`
    and the recipient read « This shared result is not available ».
    """
    from core import render_shares  # noqa: PLC0415
    from core.dossiers import create_dossier  # noqa: PLC0415

    created = create_dossier(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        label="Shareable",
        actor="owner@example.com",
        blocks=[{"kind": "render", "render_id": chain.render_id}],
    )
    requested = render_shares.create_share(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        render_id=None,
        dossier_version_id=created["version_id"],
        actor="owner@example.com",
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        idempotency_key=f"share-{ULID()}",
    )
    confirmed = render_shares.confirm_share(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        share_id=requested.share_id,
        actor="second.holder@example.com",
        idempotency_key=f"confirm-{requested.share_id}",
    )
    exchanged = render_shares.exchange_bearer(
        chain.conn, bearer=_bearer_of(confirmed), ip_hash=None, client_class="browser"
    )
    assert exchanged.next_url == "/share/view"
    session = render_shares.resolve_session(chain.conn, session_value=exchanged.session_value)
    assert session.render_id is None
    assert session.dossier_version_id == created["version_id"]


def test_a_bearer_can_be_exchanged_exactly_once(chain):
    """D4 / the epic acceptance: "exchanged once"."""
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    bearer = _bearer_of(created)
    first = render_shares.exchange_bearer(
        chain.conn, bearer=bearer, ip_hash=None, client_class="browser"
    )
    assert first.next_url == "/share/view"
    with pytest.raises(render_shares.RenderShareUnavailable):
        render_shares.exchange_bearer(
            chain.conn, bearer=bearer, ip_hash=None, client_class="browser"
        )


def test_a_consumed_bearer_appends_its_reason_code_without_returning_it(chain):
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    bearer = _bearer_of(created)
    render_shares.exchange_bearer(chain.conn, bearer=bearer, ip_hash=None, client_class="browser")
    with pytest.raises(render_shares.RenderShareUnavailable) as exc:
        render_shares.exchange_bearer(
            chain.conn, bearer=bearer, ip_hash=None, client_class="browser"
        )
    # The refusal itself carries no detail...
    assert "consumed" not in str(exc.value)
    # ...but the auditor can see exactly what happened.
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT reason_code FROM app.render_share_access_events "
            "WHERE share_id = %s ORDER BY occurred_at DESC LIMIT 1",
            (created.share_id,),
        )
        assert cur.fetchone()[0] == "bearer_already_consumed"


def test_every_refusal_path_costs_the_same_statements(chain):
    """AC5, the falsifiable form. Unknown, consumed, revoked and expired must each
    execute the same number of SQL statements, so response cost is not an
    enumeration oracle. NO wall-clock assertion is made here, on purpose."""
    from core import render_shares  # noqa: PLC0415

    counts: dict[str, int] = {}

    def measure(label: str, bearer: str) -> None:
        counter = StatementCounter(chain.conn)
        with counter:
            with pytest.raises(render_shares.RenderShareUnavailable):
                render_shares.exchange_bearer(
                    chain.conn, bearer=bearer, ip_hash=None, client_class="browser"
                )
        counts[label] = counter.count

    # unknown
    measure("unknown", render_shares.mint_bearer())

    # consumed
    consumed = chain.share()
    consumed_bearer = _bearer_of(consumed)
    render_shares.exchange_bearer(
        chain.conn, bearer=consumed_bearer, ip_hash=None, client_class="browser"
    )
    measure("consumed", consumed_bearer)

    # revoked
    revoked = chain.share()
    revoked_bearer = _bearer_of(revoked)
    render_shares.revoke_share(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        share_id=revoked.share_id, actor="owner@example.com",
        idempotency_key=f"revoke-{ULID()}",
    )
    measure("revoked", revoked_bearer)

    # expired
    from core import render_shares as rs  # noqa: PLC0415

    expired_bearer = rs.mint_bearer()
    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.render_shares (id, org_id, project_id, render_id, "
            "bearer_hash, state, expires_at, created_at, created_by, created_operation_id) "
            "VALUES (%s, %s, %s, %s, %s, 'active', NOW() - interval '1 hour', "
            "NOW() - interval '2 hour', 't', 'op_x')",
            (
                _uid("rsh"), chain.org_id, chain.project_id, chain.render_id,
                rs.bearer_hash(expired_bearer),
            ),
        )
    measure("expired", expired_bearer)

    assert len(set(counts.values())) == 1, (
        f"refusal paths differ in SQL statement count and are therefore "
        f"distinguishable: {counts}"
    )


def test_two_concurrent_exchanges_of_one_bearer_yield_exactly_one_success(chain, live_postgres):
    """The `rowcount != 1` abort under FOR UPDATE, proved with two real
    connections rather than argued from the source."""
    import psycopg  # noqa: PLC0415
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    bearer = _bearer_of(created)
    chain.conn.commit()  # both connections must be able to see the row

    second = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        outcomes = []
        for conn in (chain.conn, second):
            try:
                render_shares.exchange_bearer(
                    conn, bearer=bearer, ip_hash=None, client_class="browser"
                )
                outcomes.append("granted")
            except render_shares.RenderShareUnavailable:
                outcomes.append("refused")
        assert outcomes.count("granted") == 1, outcomes
    finally:
        second.close()


def test_the_failed_attempt_ceiling_revokes_the_share(chain):
    """AC9: consumption bounds successes at one, so the ceiling bounds FAILURES --
    which is where brute force and replay live.

    THE BRANCH THIS REACHES, and why the previous version of this test could not
    reach it. `exchange_bearer` fires the ceiling only when
    `attempts >= ceiling AND state == 'active'`. A test that first sets
    `state='expired'` makes the second conjunct false, so the ceiling can never
    fire and the test measures the expiry refusal instead -- which is a different
    guarantee wearing this test's name.

    The path driven here is the real one: a forwarded or replayed link. The bearer
    is single-use, so every attempt after the first is refused with
    `bearer_already_consumed` while the Share is still `active` -- and the ceiling
    is exactly what stops that replay from running forever.
    """
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    with chain.conn.cursor() as cur:
        cur.execute(
            "UPDATE app.render_shares SET exchange_attempt_ceiling = 3 WHERE id = %s",
            (created.share_id,),
        )
    bearer = _bearer_of(created)

    # Attempt 1: the legitimate recipient. Succeeds, and counts.
    render_shares.exchange_bearer(
        chain.conn, bearer=bearer, ip_hash=None, client_class="browser"
    )
    assert _share_row(chain, created.share_id)["exchange_attempt_count"] == 1

    # Attempt 2: a replay. Refused as already-consumed, Share still active, below
    # the ceiling -- so nothing is revoked yet. This is the assertion that proves
    # the next one is the ceiling and not merely "any second failure".
    with pytest.raises(render_shares.RenderShareUnavailable):
        render_shares.exchange_bearer(
            chain.conn, bearer=bearer, ip_hash=None, client_class="browser"
        )
    row = _share_row(chain, created.share_id)
    assert row["exchange_attempt_count"] == 2
    assert row["state"] == "active", "the ceiling fired one attempt early"
    assert row["revoked_at"] is None
    assert _last_reason(chain, created.share_id) == "bearer_already_consumed"

    # Attempt 3: crosses the ceiling. The Share is revoked BY THE SYSTEM, with the
    # ceiling's own reason code -- on the row and in the appended evidence.
    with pytest.raises(render_shares.RenderShareUnavailable):
        render_shares.exchange_bearer(
            chain.conn, bearer=bearer, ip_hash=None, client_class="browser"
        )
    row = _share_row(chain, created.share_id)
    assert row["exchange_attempt_count"] == 3
    assert row["state"] == "revoked", "the attempt ceiling did not revoke the Share"
    assert row["revoked_at"] is not None
    assert row["revoked_by"] == "system"
    assert row["revoke_reason_code"] == "exchange_attempt_ceiling_exceeded"
    assert _last_reason(chain, created.share_id) == "exchange_attempt_ceiling_exceeded"

    # And the refusal a caller meets is still the common envelope: the ceiling
    # changes what the server records, never what the recipient is told.
    with pytest.raises(render_shares.RenderShareUnavailable) as excinfo:
        render_shares.exchange_bearer(
            chain.conn, bearer=bearer, ip_hash=None, client_class="browser"
        )
    assert str(excinfo.value) == "render share is unavailable"


def _share_row(chain, share_id: str) -> dict:
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT state, exchange_attempt_count, exchange_attempt_ceiling, "
            "revoked_at, revoked_by, revoke_reason_code "
            "FROM app.render_shares WHERE id = %s",
            (share_id,),
        )
        row = cur.fetchone()
    assert row is not None, f"share {share_id} vanished"
    return {
        "state": row[0],
        "exchange_attempt_count": row[1],
        "exchange_attempt_ceiling": row[2],
        "revoked_at": row[3],
        "revoked_by": row[4],
        "revoke_reason_code": row[5],
    }


def _last_reason(chain, share_id: str) -> str:
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT reason_code FROM app.render_share_access_events "
            "WHERE share_id = %s ORDER BY occurred_at DESC, id DESC LIMIT 1",
            (share_id,),
        )
        row = cur.fetchone()
    assert row is not None, f"no access event was appended for {share_id}"
    return row[0]


# ---------------------------------------------------------------------------
# AC8 -- revocation and expiry end access immediately, and leave evidence.
# ---------------------------------------------------------------------------


def test_revocation_refuses_the_same_cookie_on_its_next_call(chain):
    """The session is revalidated against the Share's LIVE state on every call, not
    only at exchange. Otherwise "revoke" means "stop issuing new sessions", which is
    not what the person who clicked it believes."""
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    exchanged = render_shares.exchange_bearer(
        chain.conn, bearer=_bearer_of(created), ip_hash=None, client_class="browser"
    )
    session = render_shares.resolve_session(chain.conn, session_value=exchanged.session_value)
    assert session.share_id == created.share_id

    render_shares.revoke_share(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        share_id=created.share_id, actor="owner@example.com",
        idempotency_key=f"revoke-{ULID()}",
    )
    with pytest.raises(render_shares.RenderShareUnavailable):
        render_shares.resolve_session(chain.conn, session_value=exchanged.session_value)


def test_a_revoked_share_keeps_its_row_and_its_creation_time(chain):
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    with chain.conn.cursor() as cur:
        cur.execute("SELECT created_at FROM app.render_shares WHERE id = %s", (created.share_id,))
        before = cur.fetchone()[0]
    render_shares.revoke_share(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        share_id=created.share_id, actor="owner@example.com",
        idempotency_key=f"revoke-{ULID()}",
    )
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT state, revoked_at, created_at FROM app.render_shares WHERE id = %s",
            (created.share_id,),
        )
        state, revoked_at, after = cur.fetchone()
    assert state == "revoked" and revoked_at is not None and after == before


def test_a_share_row_can_never_be_deleted(chain):
    """Deleting it would erase the only proof that a public grant existed and was
    closed."""
    created = chain.share()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="revoked, never deleted"):
        cur.execute("DELETE FROM app.render_shares WHERE id = %s", (created.share_id,))
    chain.conn.rollback()


def test_a_render_backing_a_share_cannot_be_deleted(chain):
    """AC8: no retention path can delete a Render that backs a Share.

    MEASURED, not assumed: the refusal that actually fires is Story 50.3's own
    insert-once trigger on `app.renders`, which forbids deleting ANY Render, share
    or no share. So this story's `ON DELETE RESTRICT` is a SECOND layer that a plain
    DELETE never reaches -- and asserting a foreign-key SQLSTATE here would be
    asserting an error this database cannot produce.

    Both halves are therefore checked: the delete is refused, AND the constraint is
    declared RESTRICT rather than CASCADE. The second half is the one that matters,
    because CASCADE is precisely what the retired
    `054_render_snapshot_shares.sql:60-65` declares -- and combined with the
    retention purge at `snapshots.py:134` it lets a background job delete the
    evidence that a public grant existed and was revoked.
    """
    chain.share()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute("DELETE FROM app.renders WHERE id = %s", (chain.render_id,))
    chain.conn.rollback()

    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT confdeltype FROM pg_constraint WHERE conname = 'fk_render_shares_render'"
        )
        row = cur.fetchone()
    # 'r' = RESTRICT, 'c' = CASCADE, 'a' = NO ACTION.
    assert row is not None and row[0] == "r", (
        f"the Share->Render foreign key must be RESTRICT; found {row!r}. CASCADE is "
        "the legacy defect this story repairs."
    )


# ---------------------------------------------------------------------------
# AC6 layer 2 -- the database role, not a docstring.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    [
        "query_results",
        "query_result_payloads",
        "query_specs",
        "query_spec_versions",
        "query_execution_attempts",
        "ai_paths",
        "ai_path_steps",
        "projects",
        "organizations",
        # Added by the F1 repair: the frozen payload is a COPY the Render owns, and
        # the two tables it was copied from stay refused. If a future change were to
        # grant either of them "just for the share page", these two rows fail.
        "visualization_spec_versions",
        "visualizations",
    ],
)
def test_the_share_reader_role_is_refused_on_project_data(live_postgres, table):
    """AC6.2: a public handler that asks for Project data is refused by PostgreSQL
    with 42501 -- not by a handler remembering not to ask.

    `snapshot_shares.py` promises the same restriction today, in a docstring."""
    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT share_reader_probe")
        cur.execute("SET LOCAL ROLE toorow_share_reader")
        with pytest.raises(Exception) as exc:
            cur.execute(f"SELECT 1 FROM app.{table} LIMIT 1")
        assert getattr(exc.value, "sqlstate", None) == "42501", table
        cur.execute("ROLLBACK TO SAVEPOINT share_reader_probe")


@pytest.mark.parametrize(
    "table",
    ["render_shares", "renders", "render_share_access_events", "render_frozen_payloads"],
)
def test_the_share_reader_role_can_read_what_a_share_needs(live_postgres, table):
    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT share_reader_allow")
        cur.execute("SET LOCAL ROLE toorow_share_reader")
        cur.execute(f"SELECT 1 FROM app.{table} LIMIT 1")
        cur.execute("ROLLBACK TO SAVEPOINT share_reader_allow")


# ---------------------------------------------------------------------------
# AC6 / AC7 / AC11 -- the frozen payload, and the RenderInput composed from it.
#
# The review finding these answer (F1/F2): `/session/render` handed the runtime
# this module's metadata dict instead of Story 50.5's five-field `RenderInput`, so
# the recipient met a field-by-field validator refusal -- not a chart, not the
# accessible table fallback, not an honest unavailable state. Underneath it there
# was nothing to send at all: the rows live in `app.query_result_payloads`, which
# the reader role is refused by design.
# ---------------------------------------------------------------------------


def _session_of(chain, created):
    from core import render_shares  # noqa: PLC0415

    exchanged = render_shares.exchange_bearer(
        chain.conn, bearer=_bearer_of(created), ip_hash=None, client_class="browser"
    )
    return render_shares.resolve_session(chain.conn, session_value=exchanged.session_value)


@pytest.mark.anyio
async def test_one_real_result_path_is_byte_identical_across_mcp_workbench_and_share(
    live_postgres, tmp_path
):
    """AC3: one FastMCP-produced Result crosses all three real readers unchanged."""
    from tests.fixture_generators.observed_ai_path_parity import (
        build_fixture,
        canonical_bytes,
    )

    fixture = await build_fixture(live_postgres.info.dsn, tmp_path)
    assert fixture["schema_version"] == "observed-ai-path-parity.v1"
    assert len(
        {
            canonical_bytes(fixture["mcp"]),
            canonical_bytes(fixture["workbench"]),
            canonical_bytes(fixture["share"]),
        }
    ) == 1
    assert fixture["mcp"]["path_id"] == "aip_FIXTURE"
    assert [step["ordinal"] for step in fixture["mcp"]["steps"]] == [0]


def test_creating_a_share_freezes_the_exact_bytes_the_render_pinned(chain):
    """The copy is provably the same bytes: same content hash, same rows."""
    created = chain.share()
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT result_id, result_content_hash, outcome, result_schema, "
            "result_manifest, rows_chunk, row_count, truncated, "
            "visualization_spec_version_id, spec_contract_version, schema_version, "
            "family, spec_document FROM app.render_frozen_payloads WHERE render_id = %s",
            (chain.render_id,),
        )
        row = cur.fetchone()
    assert row is not None, "creating a Share froze no payload"
    assert row[0] == chain.result_id
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT result_content_hash FROM app.renders WHERE id = %s", (chain.render_id,)
        )
        pinned = cur.fetchone()[0]
    assert row[1] == pinned, "the frozen copy does not carry the hash the Render pinned"
    assert row[2] == "success"
    assert row[3] == RESULT_SCHEMA
    assert row[5] == RESULT_ROWS, "the frozen rows are not the Result's rows"
    assert row[8] == chain.spec_version_id
    assert row[9] == "visualization-spec.v1"
    assert row[11] == "table"
    assert row[12] == SPEC_DOCUMENT
    assert created.state == "active"


def test_new_share_freezes_the_canonical_ai_path_once(path_chain, monkeypatch):
    """Replay uses only the frozen projection, even if the live owner disappears."""
    from core import ai_paths, render_shares, warehouse  # noqa: PLC0415

    created = path_chain.share()
    session = _session_of(path_chain, created)
    with path_chain.conn.cursor() as cur:
        cur.execute(
            "SELECT ai_path_evidence FROM app.render_frozen_payloads WHERE render_id = %s",
            (path_chain.render_id,),
        )
        stored = cur.fetchone()[0]
    assert stored["schema_version"] == "observed-ai-path.v1"
    assert stored["state"] == "completed"
    assert stored["path_id"] == path_chain.ai_path_id
    stored_bytes = json.dumps(stored, sort_keys=True, separators=(",", ":"))

    monkeypatch.setattr(
        ai_paths,
        "project_observed_ai_path",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Share replay must not read live AI Path evidence")
        ),
    )
    def warehouse_escape(*_args, **_kwargs):
        raise AssertionError("Share replay must not query a warehouse")

    monkeypatch.setattr(warehouse, "_query_bigquery", warehouse_escape)
    monkeypatch.setattr(warehouse, "_query_duckdb", warehouse_escape)
    replay = render_shares.load_frozen_render(path_chain.conn, session)
    assert replay["ai_path_evidence"] == stored
    assert json.dumps(
        replay["ai_path_evidence"], sort_keys=True, separators=(",", ":")
    ) == stored_bytes


def test_historical_null_ai_path_evidence_is_disclosed_not_guessed(chain):
    """The reader maps a pre-248 NULL to one explicit non-invented state."""
    from core import render_shares  # noqa: PLC0415

    # The migration's pg_temp test proves such rows survive.  This focused reader
    # test supplies that persisted shape directly so it does not alter the shared
    # application table's global constraint under parallel pytest workers.
    legacy_payload = (
        "success",
        RESULT_SCHEMA,
        RESULT_MANIFEST,
        RESULT_ROWS,
        3,
        False,
        chain.spec_version_id,
        "visualization-spec.v1",
        1,
        SPEC_DOCUMENT,
        None,
    )

    class LegacyCursor:
        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, _sql, _params):
            self.calls += 1

        def fetchone(self):
            if self.calls == 1:
                return (
                    chain.render_id, chain.result_id, _HASH_B, True,
                    chain.spec_version_id, "toorow-table", RENDERER_BUILD,
                    RUNTIME_BUILD, THEME_VERSION, FORMATTER_VERSION, "share", {},
                    {}, {}, _HASH_A, datetime.now(timezone.utc),
                )
            return legacy_payload

    cursor = LegacyCursor()

    class LegacyConnection:
        def cursor(self):
            return cursor

    frozen = render_shares.load_frozen_render(
        LegacyConnection(),
        render_shares.ShareSession(
            share_id="rsh_legacy",
            org_id=chain.org_id,
            project_id=chain.project_id,
            render_id=chain.render_id,
        ),
    )
    assert frozen["ai_path_evidence"] == {
        "schema_version": "observed-ai-path.v1",
        "state": "unavailable",
        "reason": "evidence_not_frozen",
    }


def test_the_public_read_composes_the_runtime_input_the_runtime_accepts(chain):
    """Five fields, no sixth, and the values are the frozen ones.

    `validate.ts` refuses an envelope with an unknown top-level key BY NAME, so
    "five and only five" is not tidiness -- a sixth key is a refusal panel on a
    recipient's screen.
    """
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    session = _session_of(chain, created)
    frozen = render_shares.load_frozen_render(chain.conn, session)
    envelope = frozen["runtime_input"]

    assert frozen["missing_link"] is None
    assert envelope is not None, "the public read composed no runtime input"
    assert set(envelope) == {"result", "spec", "pins", "profile", "display"}, sorted(envelope)

    assert envelope["result"]["rows"] == RESULT_ROWS
    assert envelope["result"]["schema"] == RESULT_SCHEMA
    assert envelope["result"]["outcome"] == "success"
    assert envelope["result"]["content_hash"] == _HASH_B
    assert envelope["result"]["manifest"]["grain"] == "day"
    assert envelope["result"]["evidence"] == {
        "row:0:sessions": {"evidence_id": "ev_fixture_sessions"}
    }

    assert envelope["spec"]["visualization_spec_version_id"] == chain.spec_version_id
    assert envelope["spec"]["spec_contract_version"] == "visualization-spec.v1"
    assert envelope["spec"]["schema_version"] == 1
    assert envelope["spec"]["document"] == SPEC_DOCUMENT

    # The pins are the Render's own, not this build's: a replay pin that resolved
    # to "whatever is running" would never refuse.
    assert envelope["pins"] == {
        "theme_version": THEME_VERSION,
        "formatter_version": FORMATTER_VERSION,
        "renderer_build": RENDERER_BUILD,
        "runtime_build": RUNTIME_BUILD,
    }
    # A Share is always opened under the share profile.
    assert envelope["profile"] == "share"
    assert envelope["display"] == {}


def test_a_render_whose_payload_was_not_retained_cannot_be_shared(chain):
    """The refusal reaches the OPERATOR, not the recipient.

    A Share created over a Render with nothing frozen behind it is a link that
    opens a page with no values on it -- and the person who would find out is the
    one who cannot do anything about it.
    """
    from core import render_shares  # noqa: PLC0415

    # `app.renders` is insert-once (migration 151's immutability trigger), so the
    # unretained case is a SECOND Render, not an UPDATE of the first. That the
    # UPDATE is refused is itself the reason a Render can be trusted as frozen.
    unretained = _uid("rnd")
    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.renders (id, org_id, project_id, result_id, "
            "result_content_hash, result_payload_retained, "
            "visualization_spec_version_id, renderer_adapter, renderer_build_id, "
            "runtime_build_id, theme_version, formatter_version, responsive_profile, "
            "display_state, evidence_manifest, creation_surface, origin_kind, "
            "content_hash, created_by) "
            "VALUES (%s, %s, %s, %s, %s, FALSE, %s, 'toorow-table', %s, %s, %s, %s, "
            "'share', '{}'::jsonb, %s::jsonb, 'explore', 'explore', %s, 'test')",
            (
                unretained, chain.org_id, chain.project_id, chain.result_id, _HASH_B,
                chain.spec_version_id, RENDERER_BUILD, RUNTIME_BUILD, THEME_VERSION,
                FORMATTER_VERSION, '{"freshness": "2026-07-30T00:00:00Z"}', _HASH_A,
            ),
        )
    with pytest.raises(render_shares.RenderShareValidationError) as excinfo:
        chain.share(render_id=unretained)
    assert "not retained" in str(excinfo.value)
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.render_shares WHERE render_id = %s", (unretained,)
        )
        assert cur.fetchone()[0] == 0, "a refused freeze still committed a grant"


def test_a_render_with_no_retained_payload_row_names_the_missing_link(chain):
    """`app.query_result_payloads` empty for this Result -> refusal, by name."""
    from core import render_shares  # noqa: PLC0415

    with chain.conn.cursor() as cur:
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        cur.execute(
            "DELETE FROM app.query_result_payloads WHERE result_id = %s", (chain.result_id,)
        )
        cur.execute("SET LOCAL app.rgpd_erasure = 'off'")
    with pytest.raises(render_shares.RenderShareValidationError) as excinfo:
        chain.share()
    assert "app.query_result_payloads" in str(excinfo.value)


def test_a_frozen_payload_is_insert_once(chain):
    """Frozen means frozen: the copy cannot be rewritten under a delivered link."""
    chain.share()
    with chain.conn.cursor() as cur:
        cur.execute("SAVEPOINT frozen_probe")
        with pytest.raises(Exception) as exc:
            cur.execute(
                "UPDATE app.render_frozen_payloads SET rows_chunk = '[]'::jsonb "
                "WHERE render_id = %s",
                (chain.render_id,),
            )
        assert "insert-once" in str(exc.value)
        cur.execute("ROLLBACK TO SAVEPOINT frozen_probe")
        cur.execute("SAVEPOINT frozen_delete_probe")
        with pytest.raises(Exception):
            cur.execute(
                "DELETE FROM app.render_frozen_payloads WHERE render_id = %s",
                (chain.render_id,),
            )
        cur.execute("ROLLBACK TO SAVEPOINT frozen_delete_probe")


def test_a_second_share_over_the_same_render_reuses_the_same_frozen_copy(chain):
    """Two grants, one frozen artifact. Two copies could diverge; one cannot."""
    chain.share()
    chain.share(idempotency_key=f"share-second-{ULID()}")
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.render_frozen_payloads WHERE render_id = %s",
            (chain.render_id,),
        )
        assert cur.fetchone()[0] == 1


def test_the_export_carries_the_frozen_values_and_no_internal_identity(chain):
    """AC11, the half the previous test could not see.

    The export used to contain the disclosure block and the evidence manifest and
    nothing else -- an "export of a shared result" with no result in it. The test
    that passed proved the file was inert, never that it carried anything.
    """
    from core import render_shares  # noqa: PLC0415
    from core.render_shares_api import _build_export_document  # noqa: PLC0415

    created = chain.share()
    session = _session_of(chain, created)
    frozen = render_shares.load_frozen_render(chain.conn, session)
    disclosure = render_shares.share_disclosure(chain.conn, session)
    document = _build_export_document(frozen, disclosure)

    for row in RESULT_ROWS:
        assert str(row["sessions"]) in document, f"the export omits {row['sessions']}"
        assert row["day"] in document
    assert "<table>" in document and "<th scope=\"col\">sessions</th>" in document
    assert "Shared on" in document and "Data as of" in document
    # Inert, and it stays inert.
    assert "<script" not in document
    assert "http://" not in document and "https://" not in document
    # And it names nothing the recipient could use anywhere else.
    for identity in (
        chain.project_id,
        chain.org_id,
        chain.result_id,
        chain.render_id,
        chain.query_spec_id,
        chain.spec_version_id,
        created.share_id,
    ):
        assert identity not in document, f"the export discloses {identity}"


def test_a_render_with_no_frozen_payload_reads_as_unavailable_and_names_it(chain):
    """The honest half of the same fix.

    A Share whose frozen copy is absent must produce an `unavailable` state that
    NAMES the missing link -- never a chart, never an empty table, and never the
    validator's field-by-field refusal, which reads to a recipient like a bug in
    the product.
    """
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    session = _session_of(chain, created)
    with chain.conn.cursor() as cur:
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        cur.execute(
            "DELETE FROM app.render_frozen_payloads WHERE render_id = %s", (chain.render_id,)
        )
        cur.execute("SET LOCAL app.rgpd_erasure = 'off'")
    frozen = render_shares.load_frozen_render(chain.conn, session)
    assert frozen["runtime_input"] is None
    assert frozen["missing_link"] == "app.render_frozen_payloads"


# ---------------------------------------------------------------------------
# AC9 / AC12 -- append-only evidence with every identity pinned.
# ---------------------------------------------------------------------------


def test_access_events_are_append_only(chain):
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    render_shares.exchange_bearer(
        chain.conn, bearer=_bearer_of(created), ip_hash=None, client_class="browser"
    )
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.render_share_access_events WHERE share_id = %s LIMIT 1",
            (created.share_id,),
        )
        event_id = cur.fetchone()[0]
    for statement in (
        "UPDATE app.render_share_access_events SET outcome = 'granted' WHERE id = %s",
        "DELETE FROM app.render_share_access_events WHERE id = %s",
    ):
        with chain.conn.cursor() as cur, pytest.raises(Exception, match="append-only"):
            cur.execute(statement, (event_id,))
        chain.conn.rollback()


def test_no_access_event_carries_bearer_or_session_material(chain):
    """AC9: not the bearer, not a prefix of it, not the session value, not a prefix.
    The retired path persisted `token[:8]` into the audit spine."""
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    bearer = _bearer_of(created)
    exchanged = render_shares.exchange_bearer(
        chain.conn, bearer=bearer, ip_hash=render_shares.client_ip_hash("203.0.113.9"),
        client_class="browser",
    )
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM app.render_share_access_events WHERE share_id = %s",
            (created.share_id,),
        )
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
    assert rows
    blob = " ".join(str(value) for row in rows for value in row)
    for secret, label in (
        (bearer, "bearer"),
        (bearer[:8], "bearer prefix"),
        (exchanged.session_value, "session value"),
        (exchanged.session_value[:8], "session prefix"),
    ):
        assert secret not in blob, f"{label} leaked into an access event"
    assert "client_ip_hash" in columns and "203.0.113.9" not in blob


def test_feedback_pins_every_identity_and_is_append_only(chain):
    """AC12: twelve exact identities, all non-null, so Epic 51 can distinguish a
    semantic error from a rendering or evidence-binding one."""
    from core import render_shares  # noqa: PLC0415

    created = chain.share()
    exchanged = render_shares.exchange_bearer(
        chain.conn, bearer=_bearer_of(created), ip_hash=None, client_class="browser"
    )
    session = render_shares.resolve_session(chain.conn, session_value=exchanged.session_value)
    frozen = render_shares.load_frozen_render(chain.conn, session)
    feedback_id = render_shares.record_feedback(
        chain.conn, session, frozen, polarity="helpful",
        comment="Clear enough to act on.", selected_datum_key=None,
    )
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT render_id, result_id, visualization_spec_version_id, renderer_build, "
            "runtime_build, theme_version, formatter_version, responsive_profile, "
            "evidence_manifest_hash FROM app.render_share_feedback WHERE id = %s",
            (feedback_id,),
        )
        pins = cur.fetchone()
    assert all(pins), f"a null pin makes the row unusable to Epic 51: {pins}"
    assert pins[7] == "share"
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="append-only"):
        cur.execute(
            "UPDATE app.render_share_feedback SET polarity = 'not_helpful' WHERE id = %s",
            (feedback_id,),
        )
    chain.conn.rollback()


def test_exact_share_feedback_is_frozen_scoped_idempotent_and_narrow(path_chain):
    """Story 65.5: one frozen datum write, replay, conflict and SQL boundary."""
    from core import render_shares  # noqa: PLC0415

    created = path_chain.share()
    exchanged = render_shares.exchange_bearer(
        path_chain.conn,
        bearer=_bearer_of(created),
        ip_hash=None,
        client_class="browser",
    )
    session = render_shares.resolve_session(
        path_chain.conn, session_value=exchanged.session_value
    )
    frozen = render_shares.load_frozen_render(path_chain.conn, session)
    context = render_shares.mint_share_feedback_context(path_chain.conn, session, frozen)
    payload = {
        "context": context,
        "target": {"kind": "datum", "row_index": 1, "field": "sessions"},
        "polarity": "positive",
        "comment": "This frozen point is useful.",
        "retry_key": f"share-feedback-{ULID()}",
    }

    with path_chain.conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE toorow_share_reader")
    try:
        recorded = render_shares.record_targeted_feedback(
            path_chain.conn, session, frozen, payload=payload
        )
        replayed = render_shares.record_targeted_feedback(
            path_chain.conn, session, frozen, payload=payload
        )
    finally:
        with path_chain.conn.cursor() as cur:
            cur.execute("RESET ROLE")
    assert recorded["status"] == "recorded"
    assert replayed == {**recorded, "status": "replayed"}
    with path_chain.conn.cursor() as cur:
        cur.execute(
            """
            SELECT target_schema_version, target_kind, datum_row_index, datum_field,
                   ai_path_id, retry_key_hash, request_hash, polarity, result_content_hash
            FROM app.render_share_feedback WHERE id = %s
            """,
            (recorded["feedback_id"],),
        )
        stored = cur.fetchone()
    assert stored[:5] == (
        "exact-feedback.v1",
        "datum",
        1,
        "sessions",
        path_chain.ai_path_id,
    )
    assert len(stored[5]) == len(stored[6]) == 64
    assert stored[7:] == ("helpful", _HASH_B)

    negative = render_shares.record_targeted_feedback(
        path_chain.conn,
        session,
        frozen,
        payload={
            **payload,
            "target": {"kind": "answer"},
            "polarity": "negative",
            "retry_key": f"share-feedback-{ULID()}",
        },
    )
    with path_chain.conn.cursor() as cur:
        cur.execute(
            "SELECT polarity FROM app.render_share_feedback WHERE id = %s",
            (negative["feedback_id"],),
        )
        assert cur.fetchone()[0] == "not_helpful"

    changed = {**payload, "comment": "Changed"}
    with pytest.raises(render_shares.RenderShareConflict):
        render_shares.record_targeted_feedback(
            path_chain.conn, session, frozen, payload=changed
        )

    absent_field = {
        **payload,
        "target": {"kind": "datum", "row_index": 1, "field": "not_in_row"},
        "retry_key": f"share-feedback-{ULID()}",
    }
    with path_chain.conn.cursor() as cur:
        cur.execute("SAVEPOINT invalid_share_datum")
    with pytest.raises(render_shares.RenderShareUnavailable):
        render_shares.record_targeted_feedback(
            path_chain.conn, session, frozen, payload=absent_field
        )
    with path_chain.conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT invalid_share_datum")

    with path_chain.conn.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege('toorow_share_reader', "
            "'app.render_share_feedback', 'INSERT')"
        )
        assert cur.fetchone()[0] is False
        cur.execute(
            "SELECT has_function_privilege('toorow_share_reader', "
            "'app.record_render_share_feedback_v1("
            "text,text,text,text,integer,text,integer,text,text,text,text)', "
            "'EXECUTE')"
        )
        assert cur.fetchone()[0] is True
        cur.execute(
            """
            SELECT p.prosecdef, p.proconfig, r.rolname,
                   has_function_privilege(
                     'public', p.oid, 'EXECUTE'
                   )
              FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
              JOIN pg_roles r ON r.oid = p.proowner
             WHERE n.nspname = 'app' AND p.proname = 'record_render_share_feedback_v1'
            """
        )
        prosecdef, proconfig, owner, public_execute = cur.fetchone()
        assert prosecdef is True
        assert proconfig == ["search_path=pg_catalog, app"]
        assert owner not in {"connector", "toorow_share_reader"}
        assert public_execute is False
        cur.execute(
            "SELECT pg_get_functiondef('app.record_render_share_feedback_v1"
            "(text,text,text,text,integer,text,integer,text,text,text,text)'::regprocedure)"
        )
        definition = cur.fetchone()[0]
    assert "SET search_path TO 'pg_catalog', 'app'" in definition
    for forbidden in ("query_results", "ai_paths", "ai_path_steps", "warehouse"):
        assert forbidden not in definition

    with path_chain.conn.cursor() as cur:
        cur.execute("SAVEPOINT denied_direct_share_insert")
        cur.execute("SET LOCAL ROLE toorow_share_reader")
        with pytest.raises(Exception) as denied:
            cur.execute(
                "INSERT INTO app.render_share_feedback "
                "(id, share_id, org_id, project_id, render_id, result_id, "
                "visualization_spec_version_id, renderer_build, runtime_build, "
                "theme_version, formatter_version, responsive_profile, "
                "evidence_manifest_hash, polarity) VALUES "
                "('rsfb_01ARZ3NDEKTSV4RRFFQ69G5FAV', %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, 'share', %s, 'helpful')",
                (
                    session.share_id,
                    session.org_id,
                    session.project_id,
                    session.render_id,
                    frozen["result_id"],
                    frozen["visualization_spec_version_id"],
                    frozen["renderer_build"],
                    frozen["runtime_build"],
                    frozen["theme_version"],
                    frozen["formatter_version"],
                    "a" * 64,
                ),
            )
        assert getattr(denied.value, "sqlstate", None) == "42501"
        cur.execute("ROLLBACK TO SAVEPOINT denied_direct_share_insert")
        cur.execute("RESET ROLE")

    for kind, datum_index, datum_field, path_ordinal in (
        ("datum", 0, None, None),
        ("path_step", None, None, None),
    ):
        with pytest.raises(Exception) as invalid, path_chain.conn.transaction():
            with path_chain.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.render_share_feedback
                        (id, share_id, org_id, project_id, render_id, result_id,
                         visualization_spec_version_id, renderer_build, runtime_build,
                         theme_version, formatter_version, responsive_profile,
                         evidence_manifest_hash, polarity, comment,
                         target_schema_version, interaction_ref, result_content_hash,
                         ai_path_id, target_kind, datum_row_index, datum_field,
                         path_step_ordinal, retry_key_hash, request_hash)
                    SELECT %s, share_id, org_id, project_id, render_id, result_id,
                           visualization_spec_version_id, renderer_build, runtime_build,
                           theme_version, formatter_version, responsive_profile,
                           evidence_manifest_hash, polarity, comment,
                           'exact-feedback.v1', interaction_ref, result_content_hash,
                           ai_path_id, %s, %s, %s, %s, %s, %s
                      FROM app.render_share_feedback WHERE id = %s
                    """,
                    (
                        f"rsfb_{ULID()}",
                        kind,
                        datum_index,
                        datum_field,
                        path_ordinal,
                        hashlib.sha256(f"retry-{kind}".encode()).hexdigest(),
                        hashlib.sha256(f"request-{kind}".encode()).hexdigest(),
                        recorded["feedback_id"],
                    ),
                )
        assert getattr(invalid.value, "sqlstate", None) == "23514"


def test_share_feedback_refuses_cross_share_foreign_revoked_and_expired(path_chain):
    from core import analyze_feedback, render_shares  # noqa: PLC0415

    first = path_chain.share()
    first_exchange = render_shares.exchange_bearer(
        path_chain.conn,
        bearer=_bearer_of(first),
        ip_hash=None,
        client_class="browser",
    )
    first_session = render_shares.resolve_session(
        path_chain.conn, session_value=first_exchange.session_value
    )
    first_frozen = render_shares.load_frozen_render(path_chain.conn, first_session)
    first_context = render_shares.mint_share_feedback_context(
        path_chain.conn, first_session, first_frozen
    )

    second = path_chain.share(idempotency_key=f"share-{ULID()}")
    second_exchange = render_shares.exchange_bearer(
        path_chain.conn,
        bearer=_bearer_of(second),
        ip_hash=None,
        client_class="browser",
    )
    second_session = render_shares.resolve_session(
        path_chain.conn, session_value=second_exchange.session_value
    )
    second_frozen = render_shares.load_frozen_render(path_chain.conn, second_session)
    payload = {
        "context": first_context,
        "target": {"kind": "answer"},
        "polarity": "negative",
        "comment": None,
        "retry_key": f"share-feedback-{ULID()}",
    }
    with pytest.raises(render_shares.RenderShareUnavailable):
        render_shares.record_targeted_feedback(
            path_chain.conn, second_session, second_frozen, payload=payload
        )

    foreign = Chain(path_chain.conn).build()
    foreign_share = foreign.share()
    foreign_exchange = render_shares.exchange_bearer(
        path_chain.conn,
        bearer=_bearer_of(foreign_share),
        ip_hash=None,
        client_class="browser",
    )
    foreign_session = render_shares.resolve_session(
        path_chain.conn, session_value=foreign_exchange.session_value
    )
    foreign_context = render_shares.mint_share_feedback_context(
        path_chain.conn,
        foreign_session,
        render_shares.load_frozen_render(path_chain.conn, foreign_session),
    )
    with pytest.raises(render_shares.RenderShareUnavailable):
        render_shares.record_targeted_feedback(
            path_chain.conn,
            first_session,
            first_frozen,
            payload={**payload, "context": foreign_context},
        )

    verified = analyze_feedback.verify_feedback_context(first_context)
    expired = analyze_feedback.mint_feedback_context(
        verified,
        now=datetime.now(timezone.utc) - timedelta(minutes=2),
        ttl=timedelta(minutes=1),
    )
    refreshed = render_shares.record_targeted_feedback(
        path_chain.conn,
        first_session,
        first_frozen,
        payload={**payload, "context": expired},
    )
    assert set(refreshed) == {
        "schema_version",
        "status",
        "code",
        "message",
        "feedback_context",
    }
    assert refreshed["status"] == "refresh_required"

    with path_chain.conn.cursor() as cur:
        cur.execute("SAVEPOINT revoked_share_feedback")
        cur.execute(
            "UPDATE app.render_shares SET state = 'revoked', revoked_at = NOW(), "
            "revoked_by = 'test', revoke_reason_code = 'test' WHERE id = %s",
            (first_session.share_id,),
        )
    with pytest.raises(render_shares.RenderShareUnavailable):
        render_shares.record_targeted_feedback(
            path_chain.conn,
            first_session,
            first_frozen,
            payload={**payload, "retry_key": f"share-feedback-{ULID()}"},
        )
    with path_chain.conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT revoked_share_feedback")

        cur.execute("SAVEPOINT expired_share_session")
        cur.execute(
            "UPDATE app.render_share_exchange_sessions "
            "SET created_at = NOW() - interval '2 hours', "
            "expires_at = NOW() - interval '1 hour' "
            "WHERE session_hash = %s",
            (first_session.session_hash,),
        )
    with pytest.raises(render_shares.RenderShareUnavailable):
        render_shares.record_targeted_feedback(
            path_chain.conn,
            first_session,
            first_frozen,
            payload={**payload, "retry_key": f"share-feedback-{ULID()}"},
        )
    with path_chain.conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT expired_share_session")
        cur.execute(
            "SELECT count(*) FROM app.render_share_feedback WHERE share_id = %s",
            (first_session.share_id,),
        )
        assert cur.fetchone()[0] == 0


def test_two_share_feedback_transactions_replay_one_exact_request(path_chain):
    import psycopg  # noqa: PLC0415
    from core import render_shares  # noqa: PLC0415

    created = path_chain.share()
    exchanged = render_shares.exchange_bearer(
        path_chain.conn,
        bearer=_bearer_of(created),
        ip_hash=None,
        client_class="browser",
    )
    session = render_shares.resolve_session(
        path_chain.conn, session_value=exchanged.session_value
    )
    frozen = render_shares.load_frozen_render(path_chain.conn, session)
    payload = {
        "context": render_shares.mint_share_feedback_context(path_chain.conn, session, frozen),
        "target": {"kind": "path_step", "ordinal": 0},
        "polarity": "positive",
        "comment": "Same exact frozen step.",
        "retry_key": f"share-feedback-{ULID()}",
    }
    path_chain.conn.commit()
    barrier = threading.Barrier(2)

    def write() -> dict:
        conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL ROLE toorow_share_reader")
            barrier.wait(timeout=10)
            receipt = render_shares.record_targeted_feedback(
                conn, session, frozen, payload=payload
            )
            conn.commit()
            return receipt
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write) for _ in range(2)]
        receipts = [future.result(timeout=20) for future in futures]
    assert sorted(receipt["status"] for receipt in receipts) == ["recorded", "replayed"]
    assert len({receipt["feedback_id"] for receipt in receipts}) == 1
    with path_chain.conn.cursor() as cur:
        cur.execute(
            "SELECT target_kind, path_step_ordinal, ai_path_id "
            "FROM app.render_share_feedback WHERE id = %s",
            (receipts[0]["feedback_id"],),
        )
        assert cur.fetchone() == ("path_step", 0, path_chain.ai_path_id)


def test_share_http_mints_and_records_one_frozen_target(path_chain, monkeypatch):
    from core.render_shares_api import render_share_routes  # noqa: PLC0415
    from starlette.applications import Starlette  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    created = path_chain.share()
    path_chain.conn.commit()
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])
    http = TestClient(
        Starlette(routes=render_share_routes),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    assert http.post(
        "/api/render-shares/exchange", json={"bearer": _bearer_of(created)}
    ).status_code == 200
    render_response = http.get("/api/render-shares/session/render")
    assert render_response.status_code == 200
    sidecar = render_response.json()["feedback_context"]
    command = {
        "context": sidecar,
        "target": {"kind": "datum", "row_index": 1, "field": "sessions"},
        "polarity": "positive",
        "comment": "The frozen value is clear.",
        "retry_key": f"share-feedback-{ULID()}",
    }
    first = http.post("/api/render-shares/session/feedback", json=command)
    replay = http.post("/api/render-shares/session/feedback", json=command)
    conflict = http.post(
        "/api/render-shares/session/feedback",
        json={**command, "comment": "A different semantic request."},
    )
    assert first.status_code == replay.status_code == 200
    assert first.json()["status"] == "recorded"
    assert replay.json() == {**first.json(), "status": "replayed"}
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


def test_a_feedback_row_cannot_pin_a_placeholder_word(chain):
    """`app.is_exact_pin` refuses 'legacy', 'current', 'deferred', 'latest'. A
    feedback row pinned to 'current' cannot distinguish the thing it exists to
    distinguish."""
    created = chain.share()
    with chain.conn.cursor() as cur, pytest.raises(Exception) as exc:
        cur.execute(
            "INSERT INTO app.render_share_feedback (id, share_id, org_id, project_id, "
            "render_id, result_id, visualization_spec_version_id, renderer_build, "
            "runtime_build, theme_version, formatter_version, responsive_profile, "
            "evidence_manifest_hash, polarity) VALUES (%s, %s, %s, %s, %s, %s, "
            "'current', 'rb', 'rt', 'th', 'fmt', 'share', %s, 'helpful')",
            (
                _uid("rsfb"), created.share_id, chain.org_id, chain.project_id,
                chain.render_id, chain.result_id, _HASH_A,
            ),
        )
    assert getattr(exc.value, "sqlstate", None) == "23514"
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# AC10 -- the retirement writes actually happened.
# ---------------------------------------------------------------------------


def test_no_open_legacy_snapshot_share_survives_the_migration(live_postgres):
    with live_postgres.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.render_snapshot_shares WHERE revoked_at IS NULL")
        assert cur.fetchone()[0] == 0


def test_no_notebook_carries_a_share_token(live_postgres):
    with live_postgres.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.notebooks WHERE share_token IS NOT NULL")
        assert cur.fetchone()[0] == 0


def test_the_notebook_share_token_column_is_check_constrained_to_null(live_postgres):
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = 'ck_notebooks_share_token_retired'"
        )
        assert cur.fetchone()[0] == 1


def test_a_new_legacy_snapshot_share_is_refused_at_the_database(live_postgres):
    """The loud failure a remounted route must hit. The table and its rows are kept
    deliberately: they are the only proof the open grants were closed."""
    with live_postgres.cursor() as cur, pytest.raises(Exception, match="RETIRED"):
        cur.execute(
            "INSERT INTO app.render_snapshot_shares (id, snapshot_id, share_token, shared_by) "
            "VALUES ('rss_probe', 'rsn_probe', 'plaintext', 't')"
        )
    live_postgres.rollback()
