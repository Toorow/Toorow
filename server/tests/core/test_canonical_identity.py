"""Focused tests for Story 43.2 canonical identity foundation."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from core.canonical_identity import (
    CanonicalIdentityClaimConflict,
    CanonicalIdentityUnavailable,
    CanonicalIdentityValidationError,
    normalize_verified_email,
    resolve_canonical_identity,
)


def _conn(*rows):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = rows
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.transaction.side_effect = lambda: nullcontext()
    return conn, cur


def test_existing_pair_resolves_same_person_and_refreshes_verified_claim():
    conn, cur = _conn(
        ("person_existing", "old@example.com"),
        ("person_existing", "new@example.com"),
    )

    result = resolve_canonical_identity(
        conn,
        issuer="https://issuer.example",
        subject="opaque-subject",
        verified_email=" New@Example.COM ",
    )

    assert result.person_id == "person_existing"
    assert result.verified_email == "new@example.com"
    assert result.created is False
    update_params = cur.execute.call_args_list[1].args[1]
    assert update_params == (
        "new@example.com",
        "new@example.com",
        "https://issuer.example",
        "opaque-subject",
        "new@example.com",
        "new@example.com",
    )


def test_changed_verified_email_claim_fails_closed_with_correlation_id():
    conn, _ = _conn(("person_existing",), None, (1,))

    with pytest.raises(CanonicalIdentityClaimConflict) as caught:
        resolve_canonical_identity(
            conn,
            issuer="https://issuer.example",
            subject="opaque-subject",
            verified_email="changed@example.com",
        )

    assert str(caught.value) == "verified identity claim changed"
    assert caught.value.correlation_id.startswith("identity_")
    assert "changed@example.com" not in str(caught.value)


def test_new_subject_creates_person_without_email_lookup_or_merge():
    conn, cur = _conn(None, ("person_new", "shared@example.com"))

    result = resolve_canonical_identity(
        conn,
        issuer="issuer-a",
        subject="subject-a",
        verified_email="shared@example.com",
    )

    assert result.person_id == "person_new"
    assert result.created is True
    sql = "\n".join(call.args[0] for call in cur.execute.call_args_list)
    assert "ON CONFLICT (issuer, subject) DO NOTHING" in sql
    assert "WHERE verified_email" not in sql


def test_same_email_with_different_stable_pair_creates_a_distinct_person():
    first, _ = _conn(None, ("person_a", "same@example.com"))
    second, _ = _conn(None, ("person_b", "same@example.com"))

    a = resolve_canonical_identity(
        first, issuer="issuer-a", subject="sub-a", verified_email="same@example.com"
    )
    b = resolve_canonical_identity(
        second, issuer="issuer-b", subject="sub-b", verified_email="same@example.com"
    )

    assert a.person_id != b.person_id


def test_concurrent_insert_loser_discards_candidate_and_resolves_winner():
    conn, cur = _conn(None, None, ("person_winner", "verified@example.com"))

    result = resolve_canonical_identity(
        conn,
        issuer="issuer",
        subject="subject",
        verified_email="verified@example.com",
    )

    assert result.person_id == "person_winner"
    assert result.created is False
    assert any("DELETE FROM app.persons" in call.args[0] for call in cur.execute.call_args_list)


def test_resolution_is_fail_closed_and_error_is_sanitized():
    conn, cur = _conn()
    cur.execute.side_effect = RuntimeError("db says victim@example.com")

    with pytest.raises(CanonicalIdentityUnavailable) as caught:
        resolve_canonical_identity(
            conn,
            issuer="issuer-secret",
            subject="subject-secret",
            verified_email="victim@example.com",
        )

    assert str(caught.value) == "canonical identity resolution failed"
    assert caught.value.correlation_id.startswith("identity_")
    assert "victim" not in str(caught.value)


def test_transaction_support_is_required():
    conn = MagicMock()
    conn.transaction = None

    with pytest.raises(CanonicalIdentityUnavailable, match="transactional"):
        resolve_canonical_identity(conn, issuer="issuer", subject="subject")


@pytest.mark.parametrize("field", ["issuer", "subject"])
def test_stable_identity_claims_reject_ambiguous_whitespace(field):
    claims = {"issuer": "issuer", "subject": "subject"}
    claims[field] = f" {claims[field]}"

    with pytest.raises(CanonicalIdentityValidationError):
        resolve_canonical_identity(MagicMock(), **claims)


def test_verified_email_normalization_is_bounded_and_idna_safe():
    assert normalize_verified_email(" User@ÉXAMPLE.test ") == "user@xn--xample-9ua.test"
    with pytest.raises(CanonicalIdentityValidationError):
        normalize_verified_email("not-an-email")


def test_migration_keeps_verified_email_non_unique():
    root = Path(__file__).resolve().parents[3]
    sql = (root / "infra" / "nango" / "migrations" / "111_canonical_identity.sql").read_text(
        encoding="utf-8"
    )
    folded = " ".join(sql.lower().split())

    assert "unique (issuer, subject)" in folded
    assert "unique (verified_email)" not in folded
    assert "create unique index" not in folded
    assert "protect_person_identity_binding" in folded
    assert "verified_email_at" in folded
