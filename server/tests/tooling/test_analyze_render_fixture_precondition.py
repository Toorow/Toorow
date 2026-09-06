"""AI-349: the render-fixture generator poses its precondition, and names it.

WHAT WENT WRONG, and it is a defect of the INSTRUMENT rather than of the product.
`scripts/generate_analyze_render_tool_result_fixture.py` seeds a governed chain
and then calls the real render tool as `owner@example.com`. The tool resolves its
caller through `project_access.resolve_strict_resource_access`, which TRANSLATES
the identity through `identity_bridge.canonical_identity` before comparing it to
`app.org_members.identity`. On a database where any earlier suite had registered
that subject -- `tests/conftest.py::canonical_person` does, and
`app.person_identities` is append-only, so the row outlives every rollback -- the
caller was compared as `person_<ULID>` while the membership row said
`owner@example.com`. The tool then answered the one `not_found` it answers for
every denial, three frames deep in a FastMCP client, naming neither the identity
nor the row.

The two halves of the repair are the two halves asserted here:

1. the generator seeds the membership under the identity the guard will COMPARE,
   so a database carrying that registration captures exactly like a fresh one;
2. when the decision is denied anyway, the generator refuses FIRST and its message
   names the precondition and the gesture.

Plus the guard that a script which writes must carry: it refuses a database whose
name does not end in `_test`, so `PLATFORM_DB_URL` pointed at production cannot
be seeded by a fixture generator.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest

_GENERATOR_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts/generate_analyze_render_tool_result_fixture.py"
)
_SPEC = importlib.util.spec_from_file_location("analyze_render_fixture_generator", _GENERATOR_PATH)
assert _SPEC and _SPEC.loader
GENERATOR = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(GENERATOR)


def _uid(prefix: str) -> str:
    from ulid import ULID

    return f"{prefix}_{ULID()}"


@pytest.fixture(autouse=True)
def _clean_identity_bridge():
    """The bridge caches subject -> person for the process; these tests move it."""
    from core import identity_bridge

    identity_bridge.reset_cache()
    yield
    identity_bridge.reset_cache()


def _register_subject(conn, subject: str) -> str:
    """Write the residue that made the capture unreachable: subject -> person.

    IDEMPOTENT ON PURPOSE, and for the reason this whole file exists: the residue
    is APPEND-ONLY and shared, so on any database a suite has already run against,
    the row is there. A test that insisted on writing it would fail on a unique
    violation exactly where the defect is most reproducible.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT person_id FROM app.person_identities WHERE subject = %s LIMIT 2",
            (subject,),
        )
        existing = cur.fetchall()
    if len(existing) == 1:
        return str(existing[0][0])
    if existing:
        pytest.skip(f"{subject!r} is ambiguously registered; the bridge refuses to resolve it")
    person = f"person_{uuid.uuid4().hex[:26].upper()}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app.persons (id) VALUES (%s)", (person,))
        cur.execute(
            "INSERT INTO app.person_identities (id, person_id, issuer, subject) "
            "VALUES (%s, %s, 'test', %s)",
            (f"pid_{uuid.uuid4().hex[:12]}", person, subject),
        )
    return person


def test_the_seeded_chain_holds_when_the_subject_is_already_registered(live_postgres):
    """THE MUTATION AI-349 IS MADE OF. Register the subject, then seed and read back.

    Before the repair the chain seeded here was unreadable by the identity that
    seeded it, and the only thing that said so was a `not_found` with no subject
    in it. The assertion is deliberately the ACCESS DECISION and not the wire: the
    wire is asserted by `tests/integration/test_mcp_analyze_app_payload.py`, and
    what broke here was who the guard thought was calling.
    """
    person = _register_subject(live_postgres, GENERATOR.CAPTURE_SUBJECT)

    resolved = GENERATOR.capture_identity(live_postgres)
    assert resolved == person, "the bridge must translate a registered subject"
    assert resolved != GENERATOR.CAPTURE_SUBJECT

    chain = GENERATOR.seed_governed_chain(live_postgres)
    assert chain["identity"] == person, (
        "the membership must be seeded under the identity the guard COMPARES, "
        "not under the subject the capture calls as"
    )
    # Raises `SystemExit` if the chain is unreachable; that is the whole point.
    GENERATOR.assert_capture_identity_can_view(
        live_postgres, project_id=chain["project_id"], identity=chain["identity"]
    )


def test_the_generator_names_the_precondition_instead_of_a_mute_not_found(live_postgres):
    """Remove the membership and the refusal must SAY membership, not `not_found`."""
    org_id, project_id = _uid("org"), _uid("proj")
    identity = GENERATOR.capture_identity(live_postgres)
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, "AI-349 precondition", org_id.lower(), identity),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, "AI-349 precondition", project_id.lower(), identity),
        )
        # No `app.org_members` row: the one precondition the message must name.

    with pytest.raises(SystemExit) as refusal:
        GENERATOR.assert_capture_identity_can_view(
            live_postgres, project_id=project_id, identity=identity
        )
    message = str(refusal.value)
    assert "app.org_members" in message, "the missing row is not named"
    assert project_id in message, "the project the capture seeded is not named"
    assert GENERATOR.CAPTURE_SUBJECT in message, "the identity that was refused is not named"
    assert message.strip() != "not_found"
    assert message.startswith("error: the capture identity cannot read the project")


def test_the_capture_refuses_a_database_that_is_not_disposable(monkeypatch):
    """A generator that WRITES must refuse production, whatever variable points at it.

    `_dsn` falls back to `PLATFORM_DB_URL`, which in a developer shell is usually
    the deployed database. The capture seeds an organization and appends to
    `app.query_results`, so there would be nothing to take back.
    """
    monkeypatch.delenv("TEST_POSTGRES_DSN", raising=False)
    monkeypatch.setenv("PLATFORM_DB_URL", "postgresql://someone@db.example.com:5432/postgres")

    with pytest.raises(SystemExit) as refusal:
        GENERATOR._dsn()
    message = str(refusal.value)
    assert "refusing" in message
    assert "'postgres'" in message
    assert "disposable_postgres.py up" in message, "the refusal must name the gesture"


def test_a_disposable_database_is_accepted(monkeypatch):
    """The negative control: the refusal must not close the door it exists to keep."""
    monkeypatch.setenv(
        "TEST_POSTGRES_DSN", "postgresql://connector:x@127.0.0.1:55432/toorow_test"
    )
    assert GENERATOR._dsn().endswith("/toorow_test")
