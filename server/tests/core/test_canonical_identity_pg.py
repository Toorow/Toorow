"""Canonical identity against a REAL Postgres (story 43.2).

Why this file exists, separately from test_canonical_identity.py: every test in
that file drives a MagicMock connection, so the SQL text is never parsed by a
database. On 2026-07-27 that let two statements ship that Postgres refuses
outright --

    INSERT ... CASE WHEN %s IS NULL THEN NULL ELSE NOW() END
    UPDATE ... WHEN verified_email IS NULL AND %s IS NOT NULL

-- because a parameter appearing ONLY inside a NULL test has no inferable type:
`psycopg.errors.IndeterminateDatatype: could not determine data type of
parameter $6`. Both raised, both were swallowed into the deliberately sanitized
CanonicalIdentityUnavailable, and the caller answered a flat 401. Canonical
resolution being the only authentication path in production, NO ONE could
authenticate, and app.persons stayed permanently empty.

A mock cannot catch this class of defect -- only a real parse can. Gated on
TEST_POSTGRES_DSN / PLATFORM_DB_URL exactly like every other live-DB test, and
it cleans up the rows it writes.
"""

from __future__ import annotations

import os

import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN") or os.environ.get("PLATFORM_DB_URL", "")


def _pg_reachable() -> bool:
    if not _DSN:
        return False
    try:
        import psycopg  # noqa: PLC0415

        with psycopg.connect(_DSN, connect_timeout=2) as conn:
            return conn is not None
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="TEST_POSTGRES_DSN / PLATFORM_DB_URL not reachable -- skipping live identity tests",
)

_ISSUER = "https://test.invalid/canonical-identity-pg"
_SUBJECT = "subject-canonical-identity-pg"


@pytest.fixture
def conn():
    import psycopg  # noqa: PLC0415

    with psycopg.connect(_DSN, connect_timeout=5) as connection:
        yield connection
        with connection.cursor() as cur:
            cur.execute(
                "DELETE FROM app.persons WHERE id IN ("
                "SELECT person_id FROM app.person_identities WHERE issuer = %s)",
                (_ISSUER,),
            )
            cur.execute("DELETE FROM app.person_identities WHERE issuer = %s", (_ISSUER,))
        connection.commit()


def test_first_authentication_creates_the_person_with_a_verified_email(conn):
    """The INSERT must be accepted by Postgres, not merely by a mock."""
    from core.canonical_identity import resolve_canonical_identity

    resolved = resolve_canonical_identity(
        conn,
        issuer=_ISSUER,
        subject=_SUBJECT,
        verified_email="Person@Example.COM",
    )

    assert resolved.created is True
    assert resolved.person_id.startswith("person_")
    assert resolved.verified_email == "person@example.com"


def test_second_authentication_resolves_the_same_person(conn):
    """Exercises the UPDATE path, which every login after the first one takes."""
    from core.canonical_identity import resolve_canonical_identity

    first = resolve_canonical_identity(
        conn, issuer=_ISSUER, subject=_SUBJECT, verified_email="person@example.com"
    )
    second = resolve_canonical_identity(
        conn, issuer=_ISSUER, subject=_SUBJECT, verified_email="person@example.com"
    )

    assert second.created is False
    assert second.person_id == first.person_id


def test_identity_without_a_verified_email_is_accepted(conn):
    """The NULL branch is the one whose parameter type Postgres could not infer."""
    from core.canonical_identity import resolve_canonical_identity

    resolved = resolve_canonical_identity(
        conn, issuer=_ISSUER, subject=_SUBJECT + "-no-email", verified_email=None
    )

    assert resolved.created is True
    assert resolved.verified_email is None
