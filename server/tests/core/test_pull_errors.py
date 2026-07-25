"""Unit tests for server/core/pull_errors.py (Story 25.2, AC1, AC2, AC7).

Covers:
  - classify_http_error mapping table (401/403/400/5xx/other)
  - error_map refinement override (generic, no provider vocabulary)
  - provider_payload preservation + 4000-char truncation
  - retryable/user_action per class
  - AD-2/HG-1: the core file contains no provider names or provider codes
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from core.pull_errors import (
    AuthExpiredError,
    AuthRevokedError,
    ConnectorError,
    InvalidRequestError,
    PermissionDeniedError,
    ProviderTransientError,
    UnclassifiedError,
    classify_http_error,
)

# ---------------------------------------------------------------------------
# Classifier mapping table
# ---------------------------------------------------------------------------


class TestClassifierMapping:
    def test_401_is_auth_expired_non_retryable_reconnect(self):
        err = classify_http_error(401, {"error": "expired"})
        assert isinstance(err, AuthExpiredError)
        assert err.error_class == "auth_expired"
        assert err.retryable is False
        assert err.user_action == "reconnect"
        assert err.provider_status == 401

    def test_403_is_permission_denied_non_retryable_reconnect(self):
        err = classify_http_error(403, {"error": "forbidden"})
        assert isinstance(err, PermissionDeniedError)
        assert err.error_class == "permission_denied"
        assert err.retryable is False
        assert err.user_action == "reconnect"

    def test_400_is_invalid_request_non_retryable_no_action(self):
        err = classify_http_error(400, {"error": "bad param"})
        assert isinstance(err, InvalidRequestError)
        assert err.error_class == "invalid_request"
        assert err.retryable is False
        assert err.user_action is None

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
    def test_5xx_is_provider_transient_retryable(self, status):
        err = classify_http_error(status, "upstream boom")
        assert isinstance(err, ProviderTransientError)
        assert err.error_class == "provider_transient"
        assert err.retryable is True
        assert err.user_action is None

    @pytest.mark.parametrize("status", [402, 404, 418, 451])
    def test_other_statuses_are_unclassified_retryable(self, status):
        err = classify_http_error(status, {"whatever": True})
        assert isinstance(err, UnclassifiedError)
        assert err.error_class == "unclassified"
        assert err.retryable is True

    def test_all_returned_errors_are_connector_errors(self):
        for status in (400, 401, 403, 500, 404):
            assert isinstance(classify_http_error(status, {}), ConnectorError)

    def test_connector_error_is_runtimeerror_subclass(self):
        # Legacy call sites / tests asserting RuntimeError must keep working.
        assert issubclass(ConnectorError, RuntimeError)
        assert isinstance(classify_http_error(500, "x"), RuntimeError)


# ---------------------------------------------------------------------------
# error_map refinement (generic; provider codes live in the map, not in core)
# ---------------------------------------------------------------------------


class TestErrorMapRefinement:
    def test_error_map_refines_403_to_auth_revoked(self):
        # Provider-neutral fixture: a 403 with subcode 458 means revoked token.
        payload = {"error": {"code": 190, "subcode": 458, "message": "session invalid"}}
        error_map = {"403:458": "auth_revoked"}
        err = classify_http_error(403, payload, error_map=error_map)
        assert isinstance(err, AuthRevokedError)
        assert err.error_class == "auth_revoked"
        assert err.retryable is False
        assert err.user_action == "reconnect"

    def test_error_map_uses_top_level_code_shape(self):
        payload = {"error": {"code": 190}}
        error_map = {"403:190": "auth_revoked"}
        err = classify_http_error(403, payload, error_map=error_map)
        assert err.error_class == "auth_revoked"

    def test_error_map_miss_falls_back_to_http_classification(self):
        payload = {"error": {"code": 999}}
        error_map = {"403:458": "auth_revoked"}
        err = classify_http_error(403, payload, error_map=error_map)
        # No matching key -> pure HTTP classification (403 -> permission_denied).
        assert err.error_class == "permission_denied"

    def test_error_map_ignored_when_no_provider_code_extractable(self):
        err = classify_http_error(403, "plain text body", error_map={"403:1": "auth_revoked"})
        assert err.error_class == "permission_denied"

    def test_error_map_unknown_class_string_ignored(self):
        payload = {"error": {"code": 7}}
        err = classify_http_error(403, payload, error_map={"403:7": "not_a_real_class"})
        # Unknown class value must not crash; falls back to HTTP classification.
        assert err.error_class == "permission_denied"

    def test_no_error_map_is_pure_http(self):
        payload = {"error": {"code": 458}}
        err = classify_http_error(403, payload)
        assert err.error_class == "permission_denied"


# ---------------------------------------------------------------------------
# Payload preservation + truncation
# ---------------------------------------------------------------------------


class TestPayloadPreservation:
    def test_dict_payload_preserved_as_is(self):
        payload = {"error": {"code": 4, "fbtrace_id": "AbCdEf", "message": "rate"}}
        err = classify_http_error(500, payload)
        assert err.provider_payload == payload
        assert err.provider_payload["error"]["fbtrace_id"] == "AbCdEf"

    def test_text_payload_truncated_at_4000_chars(self):
        big = "x" * 10000
        err = classify_http_error(500, big)
        assert isinstance(err.provider_payload, str)
        assert len(err.provider_payload) == 4000

    def test_short_text_payload_not_truncated(self):
        err = classify_http_error(500, "short")
        assert err.provider_payload == "short"

    def test_none_payload_becomes_empty_string(self):
        err = classify_http_error(500, None)
        assert err.provider_payload == ""

    def test_non_str_non_dict_payload_coerced_to_str(self):
        err = classify_http_error(500, 12345)
        assert err.provider_payload == "12345"


# ---------------------------------------------------------------------------
# AD-2 / HG-1: no provider vocabulary in the core file
# ---------------------------------------------------------------------------


class TestNoProviderVocabulary:
    def test_core_file_has_no_provider_names(self):
        source = (
            Path(__file__).parents[2] / "core" / "pull_errors.py"
        ).read_text(encoding="utf-8").lower()
        forbidden = [
            "meta",
            "facebook",
            "graph.facebook",
            "google",
            "ga4",
            "gsc",
            "tiktok",
            "shopify",
            "stripe",
            "hubspot",
            "klaviyo",
            "linkedin",
            "github",
            "fbtrace",
        ]
        # Word-boundary match (review F-C4): plain substring search would
        # false-positive on innocuous words ("metadata" contains "meta").
        hits = [w for w in forbidden if re.search(rf"\b{re.escape(w)}\b", source)]
        assert not hits, f"pull_errors.py must contain no provider vocabulary, found: {hits}"


class TestRateLimit429Guard:
    """Review F-B1: a misrouted 429 must fail loudly, never degrade to a
    retryable unclassified error that would bypass the breaker."""

    def test_429_raises_value_error(self):
        with pytest.raises(ValueError, match="RateLimitError"):
            classify_http_error(429, {"error": {"code": "throttle"}})
