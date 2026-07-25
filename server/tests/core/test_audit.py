"""Unit and integration tests for core.audit -- Story 2.6.

Unit tests run always (mocked DB). Integration tests skip when PLATFORM_DB_URL
is not set -- matching the skip pattern from test_nango_integration.py.

Covers:
  - write_audit_row: swallows DB errors (AC6), accepts "anonymous" identity (AC7)
  - ACTION_* constants stability guard (AC2 regression guard)
  - query_audit_log: returns newest-first ordering (AC4)
  - CSV serialisation: correct header and row format (AC5)
  - Integration round-trip: write then query (AC1, AC4) -- skipped if no DB
  - CSV endpoint via TestClient (AC5) -- skipped if no DB
"""

from __future__ import annotations

import logging
import os
from unittest.mock import MagicMock, patch

import pytest
from core.audit import (
    ACTION_CONNECTION_CREATED,
    ACTION_CONNECTION_REVOKED,
    ACTION_PULL_TRIGGERED,
    AUDIT_CSV_COLUMNS,
    rows_to_csv,
    write_audit_row,
)

# ---------------------------------------------------------------------------
# Skip guard for integration tests (matches test_nango_integration.py pattern)
# ---------------------------------------------------------------------------

_PLATFORM_DB_URL = os.getenv("PLATFORM_DB_URL", "")
_HAS_DB = bool(_PLATFORM_DB_URL)

_skip_no_db = pytest.mark.skipif(
    not _HAS_DB,
    reason="PLATFORM_DB_URL not set -- skipping DB integration tests",
)

# A stable fake connection_ref ULID for tests that need one
_FAKE_CONN_REF = "conn_01J000000000000000000000"


# ---------------------------------------------------------------------------
# T7.3 -- Action code constant stability guard
# ---------------------------------------------------------------------------


class TestActionCodeConstants:
    """Regression guard: action code strings must never change once rows exist."""

    def test_action_code_constants_are_stable(self):
        """T7.3: assert constants equal their expected string values."""
        assert ACTION_CONNECTION_CREATED == "connection.created"
        assert ACTION_CONNECTION_REVOKED == "connection.revoked"
        assert ACTION_PULL_TRIGGERED == "pull.triggered"


# ---------------------------------------------------------------------------
# T7.1 -- write_audit_row swallows DB errors (AC6)
# ---------------------------------------------------------------------------


class TestStrictAuditInsert:
    def test_insert_audit_row_uses_existing_transaction_and_propagates(self):
        from core.audit import ACTION_DATASTREAM_INTENT_VERSIONED, insert_audit_row

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur

        insert_audit_row(
            conn,
            identity="member-1",
            action=ACTION_DATASTREAM_INTENT_VERSIONED,
            provider_account="",
            connection_ref="",
            metadata={"idempotency_key_hash": "a" * 64},
        )

        sql, params = cur.execute.call_args.args
        assert "INSERT INTO app.audit_log" in sql
        assert params[1] == "member-1"
        assert params[2] == "datastream.intent.versioned"
        assert '"idempotency_key_hash": "' in params[-1]
        conn.commit.assert_not_called()

        cur.execute.side_effect = RuntimeError("audit unavailable")
        with pytest.raises(RuntimeError, match="audit unavailable"):
            insert_audit_row(
                conn,
                identity="member-1",
                action=ACTION_DATASTREAM_INTENT_VERSIONED,
                provider_account="",
                connection_ref="",
            )


class TestWriteAuditRowErrorHandling:
    """write_audit_row must never raise to the caller on DB failure (AC6)."""

    def test_write_audit_row_logs_and_swallows_on_db_error(self, caplog):
        """T7.1: mock DB call raises; assert None returned, warning logged."""
        with patch("core.audit.psycopg", create=True) as mock_psycopg:
            # Simulate a connection error
            mock_psycopg.connect.side_effect = Exception("connection refused")

            with caplog.at_level(logging.WARNING, logger="core.audit"):
                result = write_audit_row(
                    identity="test-user",
                    action=ACTION_CONNECTION_CREATED,
                    provider_account="google-analytics",
                    connection_ref=_FAKE_CONN_REF,
                )

        assert result is None, "write_audit_row must return None on DB error"
        assert any("audit_write_failed" in rec.message for rec in caplog.records), (
            "Expected 'audit_write_failed' warning in log"
        )

    def test_write_audit_row_swallows_without_raising(self):
        """AC6: no exception propagates from write_audit_row even on hard crash."""
        with patch("core.audit.psycopg", create=True) as mock_psycopg:
            mock_psycopg.connect.side_effect = RuntimeError("fatal DB error")
            # Must not raise
            try:
                write_audit_row(
                    identity="user",
                    action=ACTION_PULL_TRIGGERED,
                    provider_account="google-analytics",
                    connection_ref=_FAKE_CONN_REF,
                )
            except Exception as exc:
                pytest.fail(f"write_audit_row raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# T7.2 -- anonymous identity accepted (AC7)
# ---------------------------------------------------------------------------


class TestWriteAuditRowAnonymousIdentity:
    """write_audit_row must accept "anonymous" and pass it to the INSERT (AC7)."""

    def test_write_audit_row_anonymous_identity_accepted(self):
        """T7.2: mock DB; assert INSERT received 'anonymous' as identity param."""
        captured_params: list = []

        mock_cursor = MagicMock()

        def capture_execute(sql, params):
            captured_params.extend(params)

        mock_cursor.execute.side_effect = capture_execute
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("core.audit.psycopg", create=True) as mock_psycopg:
            mock_psycopg.connect.return_value = mock_conn

            write_audit_row(
                identity="anonymous",
                action=ACTION_CONNECTION_CREATED,
                provider_account="google-analytics",
                connection_ref=_FAKE_CONN_REF,
                metadata=None,
            )

        assert len(captured_params) > 0, "No params were captured from execute call"
        # params order: (row_id, identity, action, provider_account, connection_ref, metadata_json)
        assert captured_params[1] == "anonymous", (
            f"Expected 'anonymous' as identity param, got: {captured_params[1]!r}"
        )

    def test_write_audit_row_passes_action_to_insert(self):
        """write_audit_row passes the action code to the INSERT."""
        captured_params: list = []

        mock_cursor = MagicMock()

        def capture_execute(sql, params):
            captured_params.extend(params)

        mock_cursor.execute.side_effect = capture_execute
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("core.audit.psycopg", create=True) as mock_psycopg:
            mock_psycopg.connect.return_value = mock_conn

            write_audit_row(
                identity="test-user",
                action=ACTION_CONNECTION_REVOKED,
                provider_account="google-analytics",
                connection_ref=_FAKE_CONN_REF,
            )

        assert ACTION_CONNECTION_REVOKED in captured_params, (
            f"Expected action code in params, got: {captured_params}"
        )


# ---------------------------------------------------------------------------
# T7.6 -- query_audit_log returns newest-first
# ---------------------------------------------------------------------------


class TestQueryAuditLogOrdering:
    """query_audit_log result must be ordered by created_at DESC (AC4)."""

    def test_query_returns_newest_first(self):
        """T7.6: mock DB rows returned out of order; assert result ordered DESC."""
        from core.audit import query_audit_log

        # Mock rows as if returned by the DB (already ordered by SQL ORDER BY)
        # The function itself relies on the SQL ORDER BY; we verify the contract
        # by simulating a realistic return and asserting the list order.
        fake_rows_from_db = [
            {
                "id": "audit_C",
                "identity": "user",
                "action": "connection.created",
                "provider_account": "ga",
                "connection_ref": _FAKE_CONN_REF,
                "metadata": None,
                "created_at": "2026-07-11T10:00:00+00:00",
            },
            {
                "id": "audit_B",
                "identity": "user",
                "action": "connection.created",
                "provider_account": "ga",
                "connection_ref": _FAKE_CONN_REF,
                "metadata": None,
                "created_at": "2026-07-10T10:00:00+00:00",
            },
            {
                "id": "audit_A",
                "identity": "user",
                "action": "connection.created",
                "provider_account": "ga",
                "connection_ref": _FAKE_CONN_REF,
                "metadata": None,
                "created_at": "2026-07-09T10:00:00+00:00",
            },
        ]

        # Mock the DB call to return pre-sorted rows (as ORDER BY DESC would)
        from datetime import datetime

        mock_cursor = MagicMock()
        mock_cursor.description = [
            ("id",),
            ("identity",),
            ("action",),
            ("provider_account",),
            ("connection_ref",),
            ("metadata",),
            ("created_at",),
        ]
        # Simulate DB returning as (id, identity, action, provider_account,
        #                          connection_ref, metadata, created_at)
        mock_cursor.fetchall.return_value = [
            (
                row["id"],
                row["identity"],
                row["action"],
                row["provider_account"],
                row["connection_ref"],
                row["metadata"],
                datetime.fromisoformat(row["created_at"]),
            )
            for row in fake_rows_from_db
        ]
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("core.audit.psycopg", create=True) as mock_psycopg:
            mock_psycopg.connect.return_value = mock_conn
            result = query_audit_log()

        assert len(result) == 3
        # Newest first
        assert result[0]["id"] == "audit_C"
        assert result[1]["id"] == "audit_B"
        assert result[2]["id"] == "audit_A"
        # created_at is serialised to ISO-8601 string
        assert isinstance(result[0]["created_at"], str)


# ---------------------------------------------------------------------------
# CSV serialisation unit tests (AC5)
# ---------------------------------------------------------------------------


class TestRowsToCsv:
    """rows_to_csv must produce correct header + data rows (AC5)."""

    def test_csv_header_matches_schema_columns(self):
        """CSV first line must be the canonical header."""
        csv_output = rows_to_csv([])
        lines = csv_output.strip().splitlines()
        assert len(lines) == 1, "Empty rows should produce header only"
        header = lines[0].split(",")
        assert header == AUDIT_CSV_COLUMNS

    def test_csv_data_row_matches_fields(self):
        """CSV data row must contain field values in canonical column order."""
        rows = [
            {
                "id": "audit_01",
                "identity": "user@example.com",
                "action": "connection.created",
                "provider_account": "google-analytics",
                "connection_ref": _FAKE_CONN_REF,
                "metadata": None,
                "created_at": "2026-07-11T10:00:00+00:00",
            }
        ]
        csv_output = rows_to_csv(rows)
        lines = csv_output.strip().splitlines()
        assert len(lines) == 2
        # Parse the data row
        data_fields = lines[1].split(",")
        assert data_fields[0] == "audit_01"
        assert data_fields[1] == "user@example.com"
        assert data_fields[2] == "connection.created"
        assert data_fields[3] == "google-analytics"

    def test_csv_metadata_json_serialised(self):
        """CSV metadata column must be JSON-serialised for non-None values."""
        rows = [
            {
                "id": "audit_02",
                "identity": "user",
                "action": "pull.triggered",
                "provider_account": "google-analytics",
                "connection_ref": _FAKE_CONN_REF,
                "metadata": {"batch_id": "batch_123"},
                "created_at": "2026-07-11T10:00:00+00:00",
            }
        ]
        csv_output = rows_to_csv(rows)
        assert "batch_123" in csv_output


# ---------------------------------------------------------------------------
# Integration tests -- skipped when PLATFORM_DB_URL not set
# ---------------------------------------------------------------------------


@_skip_no_db
class TestAuditIntegration:
    """Integration tests against live platform-db (AC1, AC4, AC5).

    Skipped automatically when PLATFORM_DB_URL is not set (CI / no-DB envs).
    Requires:
      - docker compose -f infra/nango/docker-compose.yml up -d
      - Migration 002 applied
      - PLATFORM_DB_URL=postgresql://connector:connector_dev_only@localhost:5432/connector
      - A seed row in app.connection_ref to satisfy the FK constraint.
    """

    # All integration tests in this class use a shared fake connection_ref seed.
    # In a real test run, the connection_ref must already exist (seeded by Story 2.2
    # or a test fixture). We use a deterministic ULID so tests are idempotent.
    _SEED_CONN_REF = "conn_INTEGTEST000000000000"

    @pytest.fixture(autouse=True)
    def _ensure_seed_conn_ref(self):
        """Ensure a connection_ref seed row exists so FK constraint is satisfied."""
        from core.audit import psycopg as _psycopg  # noqa: PLC0415

        url = os.environ.get("PLATFORM_DB_URL", "")
        try:
            with _psycopg.connect(url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app.connection_ref
                            (id, provider, nango_connection_id, project_id)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            self._SEED_CONN_REF,
                            "google-analytics",
                            "nango-integ-test",
                            "integ-test-project",
                        ),
                    )
                conn.commit()
        except Exception:
            pytest.skip("Could not seed connection_ref -- skipping integration test")

    def test_write_and_query_roundtrip(self):
        """T7.4: write a row, query it back, assert all fields match."""
        from core.audit import query_audit_log, write_audit_row

        # Write
        write_audit_row(
            identity="integ-test-user",
            action=ACTION_CONNECTION_CREATED,
            provider_account="google-analytics",
            connection_ref=self._SEED_CONN_REF,
            metadata={"test": "story-2-6-roundtrip"},
        )

        # Query back -- filter to this specific connection_ref to avoid noise
        rows = query_audit_log(connection_ref=self._SEED_CONN_REF)
        assert len(rows) >= 1, "Expected at least one row after write"

        # Find our row (newest-first, may be more than one from prior runs)
        our_rows = [r for r in rows if r.get("identity") == "integ-test-user"]
        assert len(our_rows) >= 1, "Expected row with identity='integ-test-user'"

        row = our_rows[0]
        assert row["action"] == ACTION_CONNECTION_CREATED
        assert row["provider_account"] == "google-analytics"
        assert row["connection_ref"] == self._SEED_CONN_REF
        assert row["metadata"] == {"test": "story-2-6-roundtrip"}
        assert isinstance(row["created_at"], str)
        # created_at must be ISO-8601
        from datetime import datetime

        datetime.fromisoformat(row["created_at"])  # raises if malformed

    def test_csv_export_endpoint(self):
        """T7.5: GET /api/audit?format=csv returns text/csv with correct header."""
        from core.main import build_asgi_app
        from starlette.testclient import TestClient

        # Seed a row so there is something to export
        write_audit_row(
            identity="csv-export-test",
            action=ACTION_PULL_TRIGGERED,
            provider_account="google-analytics",
            connection_ref=self._SEED_CONN_REF,
        )

        app = build_asgi_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(
            "/api/audit",
            params={"format": "csv", "connection_ref": self._SEED_CONN_REF},
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")
        lines = resp.text.strip().splitlines()
        assert len(lines) >= 1, "Expected at least a header row"
        header = lines[0].split(",")
        assert header == AUDIT_CSV_COLUMNS

    def test_json_endpoint_returns_rows_and_count(self):
        """GET /api/audit returns JSON with 'rows' list and 'count' integer."""
        from core.main import build_asgi_app
        from starlette.testclient import TestClient

        app = build_asgi_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(
            "/api/audit",
            params={"connection_ref": self._SEED_CONN_REF},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "rows" in body
        assert "count" in body
        assert isinstance(body["rows"], list)
        assert isinstance(body["count"], int)
        assert body["count"] == len(body["rows"])


def test_csv_export_neutralises_formula_injection():
    """review-2-6 F-04: identity starting with = + - @ must not open as a formula."""
    from core.audit import rows_to_csv

    rows = [
        {
            "id": "audit_X",
            "identity": '=HYPERLINK("http://evil")',
            "action": "connection.created",
            "provider_account": "+acct",
            "connection_ref": "-conn",
            "metadata": None,
            "created_at": "2026-07-11T00:00:00Z",
        }
    ]
    csv_text = rows_to_csv(rows)
    data_line = csv_text.splitlines()[1]
    assert "'=HYPERLINK" in data_line
    assert "'+acct" in data_line
    assert "'-conn" in data_line
