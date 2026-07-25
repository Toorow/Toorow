"""Tests for the GitHub context connector pull() function — Story 4.5 (AC8).

Mock strategy: respx intercepts httpx calls at the transport level.
All tests are offline (no real GitHub API calls).

Postgres writes are intercepted by monkeypatching core.db.get_connection
with an in-memory mock, so these tests run without a live Postgres instance.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import pytest
import respx

# Ensure the server/ directory is on sys.path for core imports in tests
_SERVER_DIR = Path(__file__).parent.parent.parent.parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))


# ---------------------------------------------------------------------------
# Fixtures: Postgres mock (in-memory row capture)
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Captures INSERT parameters for assertion in tests."""

    def __init__(self, rows_store: list[dict]) -> None:
        self._rows_store = rows_store
        self.description: list = []

    def execute(self, sql: str, params=None) -> None:
        if params is not None and "context_events" in sql:
            # Capture inserted row for test assertions
            self._rows_store.append(
                {
                    "id": params[0],
                    "project_id": params[1],
                    "event_date": str(params[2]),
                    "type": params[3],
                    "label": params[4],
                    "description": params[5],
                    "created_by": params[6],
                }
            )

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_) -> None:
        pass


class _FakeConn:
    """Context-manager-compatible fake psycopg connection."""

    def __init__(self, rows_store: list[dict]) -> None:
        self._rows_store = rows_store

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows_store)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


@contextmanager
def _fake_get_connection(rows_store: list[dict]) -> Iterator[_FakeConn]:
    yield _FakeConn(rows_store)


# ---------------------------------------------------------------------------
# Nango mock: always return a fake PAT
# ---------------------------------------------------------------------------

_FAKE_TOKEN = "ghp_test_token_1234"  # noqa: S105 -- not a real secret; test fixture


def _mock_nango_get_fresh_token(connection_id: str, provider: str | None = None) -> str:
    return _FAKE_TOKEN


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def inserted_rows() -> list[dict]:
    """Returns a shared list that captures rows inserted into context_events."""
    return []


@pytest.fixture(autouse=True)
def _patch_nango(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch nango_client.get_fresh_token globally for all tests in this file."""
    # Ensure the module under test is importable
    import server.modules.github.connector as connector_mod  # noqa: PLC0415

    monkeypatch.setattr(
        "core.nango_client.get_fresh_token",
        _mock_nango_get_fresh_token,
        raising=False,
    )
    # Also patch on the connector module itself in case it's already imported
    monkeypatch.setattr(
        connector_mod,
        "_get_github_token",
        lambda connection_id: _FAKE_TOKEN,
    )


@respx.mock
def test_pull_releases(inserted_rows: list[dict], monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock GET /releases → 2 releases in range → 2 context_events rows with type='release'."""
    import server.modules.github.connector as connector_mod

    monkeypatch.setattr(
        "core.db.get_connection",
        lambda: _fake_get_connection(inserted_rows),
        raising=False,
    )
    monkeypatch.setattr(
        connector_mod,
        "_write_context_event",
        lambda **kwargs: inserted_rows.append(kwargs),
    )

    respx.get("https://api.github.com/repos/owner/repo/releases").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "tag_name": "v2.3.1",
                    "name": "Release 2.3.1",
                    "published_at": "2026-07-04T10:00:00Z",
                },
                {
                    "tag_name": "v2.3.2",
                    "name": "Hotfix 2.3.2",
                    "published_at": "2026-07-05T14:30:00Z",
                },
                {
                    "tag_name": "v2.3.0",
                    "name": "Old release",
                    "published_at": "2026-06-01T10:00:00Z",  # out of range
                },
            ],
        )
    )

    result = connector_mod.pull(
        connection_id="conn-test",
        date_from="2026-07-01",
        date_to="2026-07-10",
        project_id="proj-test",
        pull_id="pull_TEST001",
        profile_id="releases",
        connection_config={"owner": "owner", "repo": "repo"},
    )

    assert result["profile_id"] == "releases"
    assert result["rows_written"] == 2
    assert result["pull_id"] == "pull_TEST001"
    # Verify rows captured
    assert len(inserted_rows) == 2
    assert all(
        r.get("event_type") == "release" or r.get("type") == "release"
        for r in inserted_rows
    )
    labels = [r.get("label") or r.get("label") for r in inserted_rows]
    assert "v2.3.1" in labels
    assert "v2.3.2" in labels


@respx.mock
def test_pull_deployments(inserted_rows: list[dict], monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock GET /deployments → 1 deployment in range → 1 context_event with type='deployment'."""
    import server.modules.github.connector as connector_mod

    monkeypatch.setattr(
        connector_mod,
        "_write_context_event",
        lambda **kwargs: inserted_rows.append(kwargs),
    )

    respx.get("https://api.github.com/repos/owner/repo/deployments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "ref": "main",
                    "environment": "production",
                    "created_at": "2026-07-04T12:00:00Z",
                },
            ],
        )
    )

    result = connector_mod.pull(
        connection_id="conn-test",
        date_from="2026-07-01",
        date_to="2026-07-10",
        project_id="proj-test",
        pull_id="pull_TEST002",
        profile_id="deployments",
        connection_config={"owner": "owner", "repo": "repo"},
    )

    assert result["profile_id"] == "deployments"
    assert result["rows_written"] == 1
    assert result["pull_id"] == "pull_TEST002"
    assert len(inserted_rows) == 1
    row = inserted_rows[0]
    assert row.get("event_type") == "deployment"
    assert "main" in (row.get("label") or "")
    assert "production" in (row.get("label") or "")


@respx.mock
def test_pull_empty_range(inserted_rows: list[dict], monkeypatch: pytest.MonkeyPatch) -> None:
    """No releases in date range → 0 rows, pull summary rows_written=0."""
    import server.modules.github.connector as connector_mod

    monkeypatch.setattr(
        connector_mod,
        "_write_context_event",
        lambda **kwargs: inserted_rows.append(kwargs),
    )

    respx.get("https://api.github.com/repos/owner/repo/releases").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "tag_name": "v1.0.0",
                    "name": "Old release",
                    "published_at": "2026-01-01T10:00:00Z",  # way out of range
                },
            ],
        )
    )

    result = connector_mod.pull(
        connection_id="conn-test",
        date_from="2026-07-01",
        date_to="2026-07-10",
        project_id="proj-test",
        pull_id="pull_TEST003",
        profile_id="releases",
        connection_config={"owner": "owner", "repo": "repo"},
    )

    assert result["rows_written"] == 0
    assert len(inserted_rows) == 0


@respx.mock
def test_pull_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub API 403 → module raises PermissionError; no partial writes."""
    import server.modules.github.connector as connector_mod

    write_calls: list = []
    monkeypatch.setattr(
        connector_mod,
        "_write_context_event",
        lambda **kwargs: write_calls.append(kwargs),
    )

    respx.get("https://api.github.com/repos/owner/repo/releases").mock(
        return_value=httpx.Response(403, json={"message": "Bad credentials"})
    )

    with pytest.raises(PermissionError):
        connector_mod.pull(
            connection_id="conn-test",
            date_from="2026-07-01",
            date_to="2026-07-10",
            project_id="proj-test",
            pull_id="pull_TEST004",
            profile_id="releases",
            connection_config={"owner": "owner", "repo": "repo"},
        )

    # No rows should have been written (error before any write)
    assert len(write_calls) == 0
