"""Tests for API hardening fixes (review-global-gaps).

Covers:
  - GET /api/jobs state param validation (400 on unknown state, 400 on long conn_ref_id)
  - HostHeaderValidationMiddleware strict+empty ALLOWED_HOST: ERROR log at init,
    403 on every request
  - ALERT_LINK_BASE_URL validation in infra_alerts._validated_link_base_url
  - _csv_safe recursive metadata sanitization via _sanitize_metadata
  - Shared notebook rate limit: (IP, token_prefix) keying + global per-token ceiling
    + Retry-After header on 429
  - /internal/* Phase-B guard: 403 when INTERNAL_ENDPOINTS_REQUIRE_HEADER set and
    header missing/wrong; pass-through when env var unset
  - api_auth disabled-mode one-time WARNING log

Strategy:
  - All DB calls mocked -- no real Postgres required.
  - Background workers disabled via env vars set at module level.
  - New assertions only; do not import or modify existing test files.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# Disable background threads before any app import
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("ALERTS_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JOB_COLS = [
    "id",
    "pull_id",
    "connection_ref_id",
    "date_from",
    "date_to",
    "state",
    "requested_by",
    "error_detail",
    "attempt_count",
    "enqueued_at",
    "started_at",
    "completed_at",
]


def _make_empty_db():
    """Return a fake get_connection that yields an empty result set."""
    col_descs = [
        type("D", (), {"__getitem__": staticmethod(lambda i, c=c: c)})() for c in _JOB_COLS
    ]
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall = MagicMock(return_value=[])
    mock_cursor.description = col_descs

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.commit = MagicMock()
    mock_conn.close = MagicMock()

    @contextmanager
    def _fake_get_connection():
        yield mock_conn

    return _fake_get_connection


def _build_client():
    from core.main import build_asgi_app  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    app = build_asgi_app()
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /api/jobs -- param validation
# ---------------------------------------------------------------------------


class TestListJobsParamValidation:
    """Validate state enum + connection_ref_id length cap on GET /api/jobs."""

    def test_invalid_state_returns_400(self):
        """Unknown state value must return 400 with French message listing valid states."""
        with patch("core.db.get_connection", new=_make_empty_db()):
            client = _build_client()
            resp = client.get("/api/jobs?state=invalid_state")

        assert resp.status_code == 400
        body = resp.json()
        assert body.get("code") == "invalid_param"
        # Message must be in French and list valid states
        msg = body.get("message", "")
        assert "invalid_state" in msg
        # Check some valid states appear in the message
        assert "queued" in msg
        assert "dead_letter" in msg

    def test_valid_states_pass_through(self):
        """Each of the 5 valid state values must not trigger a 400."""
        valid_states = ("queued", "running", "done", "failed", "dead_letter")
        for state in valid_states:
            with patch("core.db.get_connection", new=_make_empty_db()):
                client = _build_client()
                resp = client.get(f"/api/jobs?state={state}")
            assert resp.status_code == 200, (
                f"Expected 200 for state={state!r}, got {resp.status_code}"
            )

    def test_connection_ref_id_too_long_returns_400(self):
        """connection_ref_id longer than 256 characters must return 400."""
        long_id = "x" * 257
        with patch("core.db.get_connection", new=_make_empty_db()):
            client = _build_client()
            resp = client.get(f"/api/jobs?connection_ref_id={long_id}")

        assert resp.status_code == 400
        body = resp.json()
        assert body.get("code") == "invalid_param"

    def test_connection_ref_id_exactly_256_passes(self):
        """connection_ref_id of exactly 256 characters must be accepted."""
        exact_id = "c" * 256
        with patch("core.db.get_connection", new=_make_empty_db()):
            client = _build_client()
            resp = client.get(f"/api/jobs?connection_ref_id={exact_id}")

        assert resp.status_code == 200

    def test_no_params_still_200(self):
        """GET /api/jobs with no filters must still return 200 (non-regression)."""
        with patch("core.db.get_connection", new=_make_empty_db()):
            client = _build_client()
            resp = client.get("/api/jobs")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# HostHeaderValidationMiddleware -- strict + empty ALLOWED_HOST
# ---------------------------------------------------------------------------


class TestHostGuardStrictEmptyAllowed:
    """strict mode with empty ALLOWED_HOST: ERROR logged at init, 403 on requests."""

    def _make_middleware(self, env_patch):
        from core.routing import HostHeaderValidationMiddleware  # noqa: PLC0415

        dummy_app = MagicMock()
        with patch.dict(os.environ, env_patch, clear=False):
            return HostHeaderValidationMiddleware(dummy_app)

    def test_error_logged_at_init_when_strict_and_no_allowed_host(self, caplog):
        """ERROR must be logged at middleware construction time."""
        env = {"HOST_HEADER_VALIDATION": "strict", "ALLOWED_HOST": ""}
        with patch.dict(os.environ, env, clear=False):
            # Remove ALLOWED_HOST to ensure it is empty
            os.environ.pop("ALLOWED_HOST", None)
            with caplog.at_level(logging.ERROR, logger="core.routing"):
                from core.routing import HostHeaderValidationMiddleware  # noqa: PLC0415

                mw = HostHeaderValidationMiddleware(MagicMock())

        assert not mw.allowed  # empty set
        assert any(
            "ALLOWED_HOST" in r.message for r in caplog.records if r.levelno >= logging.ERROR
        )

    def test_strict_empty_allowed_host_rejects_with_403(self):
        """Every HTTP request must be rejected with 403 when strict + ALLOWED_HOST empty."""
        import asyncio  # noqa: PLC0415

        from core.routing import HostHeaderValidationMiddleware  # noqa: PLC0415

        with patch.dict(os.environ, {"HOST_HEADER_VALIDATION": "strict"}, clear=False):
            os.environ.pop("ALLOWED_HOST", None)
            mw = HostHeaderValidationMiddleware(MagicMock())

        responses: list[dict] = []

        async def _run():
            sent: list[dict] = []

            async def fake_send(event):
                sent.append(event)

            scope = {
                "type": "http",
                "path": "/api/jobs",
                "headers": [(b"host", b"example.com")],
            }
            await mw(scope, MagicMock(), fake_send)
            responses.append({"sent": sent})

        asyncio.run(_run())
        assert responses[0]["sent"][0]["status"] == 403

    def test_non_strict_mode_with_empty_allowed_host_allows(self):
        """Non-strict mode must NOT reject requests even if ALLOWED_HOST is empty."""
        import asyncio  # noqa: PLC0415

        from core.routing import HostHeaderValidationMiddleware  # noqa: PLC0415

        forwarded: list[bool] = []

        async def inner_app(scope, receive, send):
            forwarded.append(True)

        with patch.dict(os.environ, {"HOST_HEADER_VALIDATION": ""}, clear=False):
            os.environ.pop("ALLOWED_HOST", None)
            mw = HostHeaderValidationMiddleware(inner_app)

        async def _run():
            scope = {
                "type": "http",
                "path": "/api/jobs",
                "headers": [(b"host", b"example.com")],
            }
            await mw(scope, MagicMock(), MagicMock())

        asyncio.run(_run())
        assert forwarded  # inner app was called


# ---------------------------------------------------------------------------
# ALERT_LINK_BASE_URL validation
# ---------------------------------------------------------------------------


class TestAlertLinkBaseUrlValidation:
    """_validated_link_base_url must accept http/https and reject others."""

    def _fn(self, url):
        from core.infra_alerts import _validated_link_base_url  # noqa: PLC0415

        return _validated_link_base_url(url)

    def test_valid_http_url_passes(self):
        assert self._fn("http://dashboard.example.com") == "http://dashboard.example.com"

    def test_valid_https_url_passes(self):
        assert self._fn("https://alerts.example.com/infra") == "https://alerts.example.com/infra"

    def test_invalid_scheme_falls_back(self):
        result = self._fn("ftp://bad.example.com")
        assert result == "http://localhost:5173"

    def test_empty_string_falls_back(self):
        result = self._fn("")
        assert result == "http://localhost:5173"

    def test_no_netloc_falls_back(self):
        result = self._fn("http://")
        assert result == "http://localhost:5173"

    def test_javascript_scheme_falls_back(self):
        result = self._fn("javascript:alert(1)")
        assert result == "http://localhost:5173"

    def test_invalid_url_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core.infra_alerts"):
            self._fn("not-a-url")
        assert any("ALERT_LINK_BASE_URL" in r.message for r in caplog.records)

    def test_valid_url_no_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core.infra_alerts"):
            self._fn("https://ok.example.com")
        assert not any("ALERT_LINK_BASE_URL" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# audit._sanitize_metadata -- recursive CSV injection guard
# ---------------------------------------------------------------------------


class TestSanitizeMetadata:
    """_sanitize_metadata recursively neutralises formula strings in metadata."""

    def _fn(self, obj):
        from core.audit import _sanitize_metadata  # noqa: PLC0415

        return _sanitize_metadata(obj)

    def test_top_level_formula_string_sanitized(self):
        result = self._fn('=HYPERLINK("http://evil.com")')
        assert result.startswith("'")

    def test_nested_dict_formula_sanitized(self):
        result = self._fn({"label": "=HYPERLINK(...)"})
        assert result["label"].startswith("'")

    def test_deeply_nested_formula_sanitized(self):
        result = self._fn({"a": {"b": {"c": "=SUM(1,2)"}}})
        assert result["a"]["b"]["c"].startswith("'")

    def test_formula_in_list_sanitized(self):
        result = self._fn(["=evil", "+evil", "-evil", "@evil"])
        for item in result:
            assert item.startswith("'"), f"Expected leading quote in {item!r}"

    def test_non_string_leaves_preserved(self):
        obj = {"count": 42, "ratio": 3.14, "active": True, "nothing": None}
        result = self._fn(obj)
        assert result["count"] == 42
        assert result["ratio"] == 3.14
        assert result["active"] is True
        assert result["nothing"] is None

    def test_plain_string_unchanged(self):
        result = self._fn({"key": "normal value"})
        assert result["key"] == "normal value"

    def test_rows_to_csv_sanitizes_the_nested_resource_blob(self):
        """rows_to_csv must neutralise nested formula strings before json.dumps.

        AI-133. THE COLUMN WAS NOT DELETED, IT WAS NARROWED, and this test kept
        aiming at the old name. `query_audit_log` no longer selects the whole
        `metadata` blob: it projects three NAMED fields out of it -- `outcome`,
        `trace_id`, and `resource` (`COALESCE(metadata->'resource_path',
        metadata->'resource')`). So the recursive sanitiser still runs, on
        `resource`, and this is the only test that proves it does: the flat-column
        test in test_audit.py passes `metadata: None` and never reaches it.
        Aiming the assertion at `resource` is what KEEPS the guard proved --
        deleting the test would have left `_sanitize_metadata` uncovered.
        """
        from core.audit import rows_to_csv  # noqa: PLC0415

        rows = [
            {
                "id": "audit_001",
                "identity": "user@example.com",
                "action": "connection.created",
                "provider_account": "acc_001",
                "connection_ref": "conn_001",
                "resource": {"label": '=HYPERLINK("http://evil.com")'},
                "created_at": "2026-07-10T00:00:00+00:00",
            }
        ]
        csv_output = rows_to_csv(rows)
        # The formula must be neutralised with the leading-quote guard: Excel
        # treats '=HYPERLINK as text. The raw substring still appears (after
        # the quote), so assert the GUARDED form is present and the unguarded
        # formula (a cell value beginning directly with =) is not.
        assert "'=HYPERLINK" in csv_output
        # In the JSON blob the value is rendered as ""'=HYPERLINK -- never ""=HYPERLINK.
        assert '""=HYPERLINK' not in csv_output

    def test_the_export_never_carries_the_whole_metadata_blob(self):
        """AI-133: the narrowing is a TENANT BOUNDARY, so it is pinned, not assumed.

        `app.audit_log.metadata` is free-form -- 214 distinct keys measured across
        `server/core` and `server/inbound` on 2026-08-01 -- and the export's only
        scope predicate is `metadata->>'project_id' = %s`. Several keys name
        things the requesting project does not own, and they are written EXACTLY
        on the rows a reader would most want: `claimed_project_id` and
        `attempted_project_id` (the project someone tried to reach, recorded on
        the refusal), `member_identity` / `target_identity` / `owner_identity`
        (other people), `grantee_org_id` (another org), `granted_scopes` and
        `activation_key_hash` (credential-adjacent).

        Dumping the blob would therefore export the cross-tenant fact that the
        refusal existed to protect. Widening it back must turn this red and say
        why, rather than pass quietly.
        """
        from core.audit import AUDIT_CSV_COLUMNS, rows_to_csv  # noqa: PLC0415

        assert "metadata" not in AUDIT_CSV_COLUMNS

        rows = [
            {
                "id": "audit_002",
                "identity": "user@example.com",
                "action": "access_denied",
                "provider_account": "",
                "connection_ref": "",
                "outcome": "refused",
                "trace_id": "trace_001",
                "resource": {"path": "/api/projects/proj_EXAMPLE/datastreams"},
                # Present on the row in the database, and deliberately NOT selected.
                "metadata": {
                    "claimed_project_id": "proj_SOMEONE_ELSE",
                    "member_identity": "someone.else@example.com",
                },
                "created_at": "2026-07-10T00:00:00+00:00",
            }
        ]
        csv_output = rows_to_csv(rows)

        assert "proj_SOMEONE_ELSE" not in csv_output
        assert "someone.else@example.com" not in csv_output
        # What the projection DOES carry still arrives, or the narrowing would
        # have cost the export its usefulness rather than only its blast radius.
        assert "refused" in csv_output
        assert "trace_001" in csv_output
        assert "/api/projects/proj_EXAMPLE/datastreams" in csv_output


# ---------------------------------------------------------------------------
# Shared notebook rate limit hardening
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RETIRED by Story 50.7. This class pinned the rate limiter that protected "
        "`GET /api/notebooks/shared/{token}` -- a plaintext bearer carried in a URL "
        "path, serving the LATEST notebook run, so the artefact behind a link "
        "changed under its recipient. The route is gone and `_check_shared_rate_limit` "
        "with it. Kept as strict-xfail rather than deleted: deleted, the retirement "
        "would leave no trace and the next reader would remount the route -- which is "
        "exactly what happened once already in this repository. In `strict` mode, a "
        "remount makes these UNEXPECTEDLY PASS and the suite says so. Sharing is now "
        "one revocable grant over one immutable Render (`core.render_shares_api`), "
        "and its own refusal paths are proved by an equal SQL-statement count in "
        "`server/tests/core/test_render_shares_api.py`."
    ),
)
class TestSharedNotebookRateLimit:
    """Rate limit keys on (IP, token_prefix) and has a global per-token ceiling."""

    def _fn(self, ip, token=""):
        from core.admin_api import _check_shared_rate_limit  # noqa: PLC0415

        return _check_shared_rate_limit(ip, token)

    def setup_method(self):
        """Clear in-memory rate limit state between tests."""
        import core.admin_api as m  # noqa: PLC0415

        m._shared_endpoint_rate.clear()

    def test_first_request_allowed(self):
        allowed, retry_after = self._fn("1.2.3.4", "tok_abc123")
        assert allowed is True
        assert retry_after == 0.0

    def test_different_ips_same_token_tracked_separately(self):
        """Two different IPs should each have their own per-(IP, token) bucket."""
        import core.admin_api as m  # noqa: PLC0415

        token = "tok_shared"
        # Exhaust the per-IP limit for IP A
        m._SHARED_RATE_LIMIT  # read to confirm constant exists
        for _ in range(m._SHARED_RATE_LIMIT):
            self._fn("10.0.0.1", token)
        allowed_a, _ = self._fn("10.0.0.1", token)
        # IP B should still be allowed (different bucket)
        allowed_b, _ = self._fn("10.0.0.2", token)
        assert allowed_a is False
        assert allowed_b is True

    def test_rotating_ips_blocked_by_global_token_ceiling(self):
        """Rotating IPs with the same token must be blocked by the global token counter."""
        import core.admin_api as m  # noqa: PLC0415

        token = "tok_rotating"
        ceiling = m._SHARED_TOKEN_RATE_LIMIT
        # Send ceiling requests from distinct IPs (each within their own IP limit)
        for i in range(ceiling):
            self._fn(f"192.168.1.{i % 254}", token)
        # One more from a fresh IP -- should be blocked by the global token counter
        allowed, retry_after = self._fn("10.99.99.99", token)
        assert allowed is False
        assert retry_after >= 1

    def test_retry_after_positive_when_limited(self):
        """Retry-After value must be >= 1 when rate limited."""
        import core.admin_api as m  # noqa: PLC0415

        for _ in range(m._SHARED_RATE_LIMIT):
            self._fn("5.5.5.5", "tok_rl")
        allowed, retry_after = self._fn("5.5.5.5", "tok_rl")
        assert not allowed
        assert retry_after >= 1

    def test_429_response_includes_retry_after_header(self):
        """The HTTP 429 response from _shared_notebook_endpoint must include Retry-After."""

        # Patch _check_shared_rate_limit to immediately return rate-limited
        with patch("core.admin_api._check_shared_rate_limit", return_value=(False, 42.0)):
            client = _build_client()
            resp = client.get("/api/notebooks/shared/some_token")

        assert resp.status_code == 429
        assert "retry-after" in {k.lower() for k in resp.headers}


# ---------------------------------------------------------------------------
# /internal/* authorization -- the real gate, not the Phase-A scaffold
# ---------------------------------------------------------------------------
#
# THIS CLASS USED TO PIN A FUNCTION NOBODY CALLED. It asserted the contract of
# `_check_internal_auth` (403 `forbidden` on a missing or wrong header), which
# story 56.5 stopped reaching and AI-127 removed. Four of its five tests were
# ALREADY RED on HEAD before that removal -- measured, not assumed -- and the
# fifth (`test_unset_env_var_allows_request`) would have turned green for the
# wrong reason: it asserts 404, and the refusal added by AI-127 is a 404.
#
# So the class is rewritten against the contract that exists, rather than
# deleted: deleted, the retirement leaves no trace and the next reader rebuilds
# the scaffold. Same reasoning story 50.7 applied to the shared-notebook rate
# limiter, higher up in this file.


class TestInternalEndpointAuthGuard:
    """`_authorize_internal`: OIDC, or the shared secret, or a platform admin."""

    def test_unset_env_var_no_longer_waves_a_plain_caller_through(self):
        """The Phase-A default was "allow through". AI-127 ended it.

        `_check_auth` is permissive unless TOOROW_AUTH_MODE=oauth, so before the
        repair this endpoint started platform-scale work for anyone who could
        reach the port.
        """
        with patch.dict(
            os.environ,
            {"INTERNAL_ENDPOINTS_REQUIRE_HEADER": "", "TOOROW_SUPER_ADMINS": ""},
            clear=False,
        ):
            client = _build_client()
            resp = client.post("/internal/scheduler/dispatch-nightly")

        assert resp.status_code == 404
        assert resp.json().get("code") == "not_found"

    def test_missing_header_falls_back_to_the_human_branch_and_is_refused(self):
        """404, not the old 403: we do not confirm the surface to a stranger."""
        with (
            patch.dict(
                os.environ,
                {"INTERNAL_ENDPOINTS_REQUIRE_HEADER": "secret-token-xyz",
                 "TOOROW_SUPER_ADMINS": ""},
                clear=False,
            ),
            patch("core.admin_api.write_audit_row"),
        ):
            client = _build_client()
            resp = client.post("/internal/scheduler/dispatch-nightly")

        assert resp.status_code == 404

    def test_wrong_header_is_refused_and_not_merely_audited(self):
        """The mismatch was written to the audit trail and then waved through."""
        with (
            patch.dict(
                os.environ,
                {"INTERNAL_ENDPOINTS_REQUIRE_HEADER": "secret-token-xyz",
                 "TOOROW_SUPER_ADMINS": ""},
                clear=False,
            ),
            patch("core.admin_api.write_audit_row"),
        ):
            client = _build_client()
            resp = client.post(
                "/internal/scheduler/dispatch-nightly",
                headers={"X-Internal-Auth": "wrong-value"},
            )

        assert resp.status_code == 404

    def test_correct_header_allows_the_machine_caller(self):
        """What the repair must NOT break: Cloud Tasks and Scheduler carry no Bearer.

        200, not the 404 this test used to expect -- that 404 came from the
        QUEUE_BACKEND gate story 56.5 removed, so the assertion had been
        describing a route that no longer behaved that way.
        """
        with (
            patch.dict(
                os.environ,
                {
                    "INTERNAL_ENDPOINTS_REQUIRE_HEADER": "correct-secret",
                    "TOOROW_SUPER_ADMINS": "",
                    "SCHEDULER_ENABLED": "false",
                },
                clear=False,
            ),
            # AI-166: the endpoint now runs the WHOLE nightly walk, not the bare
            # dispatch -- patch the walk, and pin that it is the walk being asked.
            patch("core.scheduler.run_nightly_steps", return_value=None) as steps,
        ):
            client = _build_client()
            resp = client.post(
                "/internal/scheduler/dispatch-nightly",
                headers={"X-Internal-Auth": "correct-secret"},
            )

        assert resp.status_code == 200
        assert resp.json().get("ran") == "nightly_steps"
        steps.assert_called_once()

    def test_wrong_header_writes_both_refusal_facts(self, caplog):
        """TWO audit rows, and they are not the same fact.

        The secret mismatch says a caller PRESENTED the wrong platform
        credential; the platform-admin denial says the human it fell back to was
        not allowed either. Collapsing them would lose which one happened.
        """
        with (
            patch.dict(
                os.environ,
                {"INTERNAL_ENDPOINTS_REQUIRE_HEADER": "secret", "TOOROW_SUPER_ADMINS": ""},
                clear=False,
            ),
            patch("core.admin_api.write_audit_row") as mock_audit,
            caplog.at_level(logging.WARNING, logger="core.admin_api"),
        ):
            client = _build_client()
            client.post("/internal/scheduler/dispatch-nightly")

        reasons = [
            call.kwargs.get("metadata", {}).get("reason") for call in mock_audit.call_args_list
        ]
        assert "internal_auth_mismatch" in reasons
        assert mock_audit.call_count == 2, reasons

# ---------------------------------------------------------------------------
# api_auth disabled-mode one-time WARNING
# ---------------------------------------------------------------------------


class TestApiAuthDisabledWarning:
    """TOOROW_AUTH_MODE=disabled emits one-time WARNING on first auth call."""

    def setup_method(self):
        """Reset verifier cache and warning flag between tests."""
        from core.api_auth import reset_verifier_cache  # noqa: PLC0415

        reset_verifier_cache()

    def test_warning_logged_once_on_first_call(self, caplog):
        """First call in disabled mode must log a WARNING."""
        import asyncio  # noqa: PLC0415

        from core.api_auth import authenticate_api_request, reset_verifier_cache  # noqa: PLC0415

        reset_verifier_cache()

        with (
            patch.dict(os.environ, {"TOOROW_AUTH_MODE": "disabled"}, clear=False),
            caplog.at_level(logging.WARNING, logger="core.api_auth"),
        ):
            # Build a minimal mock request
            mock_req = MagicMock()
            mock_req.headers = {}

            result = asyncio.run(authenticate_api_request(mock_req))

        assert result == (True, "anonymous")
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("TOOROW_AUTH_MODE=disabled" in m for m in warning_msgs)
        assert any("dev uniquement" in m for m in warning_msgs)

    def test_warning_logged_only_once(self, caplog):
        """Subsequent calls in disabled mode must NOT repeat the warning."""
        import asyncio  # noqa: PLC0415

        from core.api_auth import authenticate_api_request, reset_verifier_cache  # noqa: PLC0415

        reset_verifier_cache()

        mock_req = MagicMock()
        mock_req.headers = {}

        with (
            patch.dict(os.environ, {"TOOROW_AUTH_MODE": "disabled"}, clear=False),
            caplog.at_level(logging.WARNING, logger="core.api_auth"),
        ):
            for _ in range(5):
                asyncio.run(authenticate_api_request(mock_req))

        warning_msgs = [
            r.message
            for r in caplog.records
            if r.levelno == logging.WARNING and "TOOROW_AUTH_MODE=disabled" in r.message
        ]
        assert len(warning_msgs) == 1

    def test_reset_verifier_cache_resets_warning_flag(self, caplog):
        """reset_verifier_cache() must also reset the one-time warning flag."""
        import asyncio  # noqa: PLC0415

        from core.api_auth import authenticate_api_request, reset_verifier_cache  # noqa: PLC0415

        reset_verifier_cache()
        mock_req = MagicMock()
        mock_req.headers = {}

        with (
            patch.dict(os.environ, {"TOOROW_AUTH_MODE": "disabled"}, clear=False),
            caplog.at_level(logging.WARNING, logger="core.api_auth"),
        ):
            asyncio.run(authenticate_api_request(mock_req))
            reset_verifier_cache()
            asyncio.run(authenticate_api_request(mock_req))

        warning_msgs = [
            r.message
            for r in caplog.records
            if r.levelno == logging.WARNING and "TOOROW_AUTH_MODE=disabled" in r.message
        ]
        # Should have logged twice (once before reset, once after)
        assert len(warning_msgs) == 2
