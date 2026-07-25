"""toorow -- Canonical connector error taxonomy (Story 25.2).

Every connector failure is classified into a small, canonical set of error
classes so the worker can apply a per-class retry policy and the platform can
show the user the action to take (e.g. reconnect) while preserving the raw
provider error payload as evidence.

Exports:
  class ConnectorError        -- base of the typed-error hierarchy
  class AuthExpiredError      -- error_class="auth_expired"      (retryable=False)
  class AuthRevokedError      -- error_class="auth_revoked"      (retryable=False)
  class PermissionDeniedError -- error_class="permission_denied" (retryable=False)
  class InvalidRequestError   -- error_class="invalid_request"   (retryable=False)
  class ProviderTransientError-- error_class="provider_transient"(retryable=True)
  class UnclassifiedError     -- error_class="unclassified"      (retryable=True)
  classify_http_error(status_code, payload, error_map=None) -> ConnectorError

Design constraints (AD-2 / HG-1):
  - This file contains ZERO provider names or provider-specific error codes.
    Provider-specific refinements are supplied by the CALLER via an optional
    ``error_map`` argument (typically read from the module's manifest). The
    classifier applies the map generically; core never hardcodes a provider code.
  - HTTP 429 is NOT handled here. Connectors keep raising
    ``core.quota.RateLimitError`` directly on 429 so the breaker/requeue path
    stays bit-identical (Story 3.3 contract preserved).
  - ConnectorError subclasses RuntimeError so legacy call sites and tests that
    catch/assert RuntimeError keep working (the generic ``except Exception`` in
    the worker also still catches these as a last resort).
  - ASCII-only log/message strings (AI-03).

RateLimitError (error_class "rate_limited") lives in core/quota.py and keeps its
own contract; it is intentionally not part of this hierarchy's raise path.
"""

from __future__ import annotations

# Canonical error-class string constants (the taxonomy).
AUTH_EXPIRED = "auth_expired"
AUTH_REVOKED = "auth_revoked"
PERMISSION_DENIED = "permission_denied"
INVALID_REQUEST = "invalid_request"
PROVIDER_TRANSIENT = "provider_transient"
UNCLASSIFIED = "unclassified"

# The full set of canonical classes (excluding rate_limited, which is quota.py's).
ERROR_CLASSES = (
    AUTH_EXPIRED,
    AUTH_REVOKED,
    PERMISSION_DENIED,
    INVALID_REQUEST,
    PROVIDER_TRANSIENT,
    UNCLASSIFIED,
)

# Maximum characters of provider payload preserved (evidence, not full body).
_PAYLOAD_MAX_CHARS = 4000


def _truncate_payload(payload):
    """Return payload as a dict (untouched) or a string truncated to 4000 chars.

    Dicts are preserved as-is (they are already bounded provider JSON bodies).
    Anything else is coerced to str and truncated so a huge HTML error page can
    never blow up the error_detail column.
    """
    if isinstance(payload, dict):
        return payload
    if payload is None:
        return ""
    text = payload if isinstance(payload, str) else str(payload)
    if len(text) > _PAYLOAD_MAX_CHARS:
        return text[:_PAYLOAD_MAX_CHARS]
    return text


class ConnectorError(RuntimeError):
    """Base class for typed connector errors.

    Subclasses RuntimeError so existing ``except RuntimeError`` / ``except
    Exception`` handlers (and tests asserting RuntimeError) keep working.

    Attributes:
        error_class:      One of the canonical taxonomy strings.
        provider_status:  The originating HTTP status code (int) or None.
        provider_payload: Parsed JSON dict or raw text (truncated at 4000 chars).
        user_action:      Action the user should take ("reconnect") or None.
        retryable:        Whether the worker should retry/backoff this class.
    """

    error_class: str = UNCLASSIFIED
    user_action: str | None = None
    retryable: bool = True

    def __init__(
        self,
        provider_status: int | None = None,
        provider_payload=None,
        *,
        message: str | None = None,
    ) -> None:
        self.provider_status = provider_status
        self.provider_payload = _truncate_payload(provider_payload)
        if message is None:
            message = (
                f"{self.error_class}: provider_status={provider_status}"
            )
        self.message = message
        super().__init__(message)


class AuthExpiredError(ConnectorError):
    """Credentials expired (typically HTTP 401). User must reconnect."""

    error_class = AUTH_EXPIRED
    user_action = "reconnect"
    retryable = False


class AuthRevokedError(ConnectorError):
    """Credentials revoked by the provider. User must reconnect."""

    error_class = AUTH_REVOKED
    user_action = "reconnect"
    retryable = False


class PermissionDeniedError(ConnectorError):
    """Authenticated but not authorized (typically HTTP 403). Reconnect/grant."""

    error_class = PERMISSION_DENIED
    user_action = "reconnect"
    retryable = False


class InvalidRequestError(ConnectorError):
    """Malformed request (typically HTTP 400).

    Not user-actionable: this is a catalog/integration drift signal the worker
    logs at WARNING with a distinct event key for downstream alerting (25.4+).
    """

    error_class = INVALID_REQUEST
    user_action = None
    retryable = False


class ProviderTransientError(ConnectorError):
    """Transient provider-side failure (typically HTTP 5xx). Retry with backoff."""

    error_class = PROVIDER_TRANSIENT
    user_action = None
    retryable = True


class UnclassifiedError(ConnectorError):
    """No taxonomy match. Retried conservatively and logged as a taxonomy gap."""

    error_class = UNCLASSIFIED
    user_action = None
    retryable = True


# Map from canonical error-class string -> the raiseable subclass. Used to apply
# an ``error_map`` refinement generically (the map's values are class strings).
_CLASS_BY_NAME = {
    AUTH_EXPIRED: AuthExpiredError,
    AUTH_REVOKED: AuthRevokedError,
    PERMISSION_DENIED: PermissionDeniedError,
    INVALID_REQUEST: InvalidRequestError,
    PROVIDER_TRANSIENT: ProviderTransientError,
    UNCLASSIFIED: UnclassifiedError,
}


def _base_class_for_status(status_code: int) -> type[ConnectorError]:
    """Pure-HTTP classification (no provider vocabulary).

    401 -> auth_expired, 403 -> permission_denied, 400 -> invalid_request,
    5xx -> provider_transient, everything else -> unclassified.

    Note: 429 is intentionally NOT handled here (connectors raise RateLimitError).
    """
    if status_code == 401:
        return AuthExpiredError
    if status_code == 403:
        return PermissionDeniedError
    if status_code == 400:
        return InvalidRequestError
    if 500 <= status_code <= 599:
        return ProviderTransientError
    return UnclassifiedError


def _extract_provider_code(payload) -> str | None:
    """Best-effort extraction of a provider error code from a JSON payload.

    Generic only -- inspects a few conventional shapes without knowing any
    specific provider. Returns a string code or None. Never raises.

    Recognised shapes (all provider-neutral):
      {"error": {"code": X}}         -> str(X)
      {"error": {"subcode": X}}      -> str(X)   (checked before "code")
      {"error_code": X}              -> str(X)
      {"code": X}                    -> str(X)
    """
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if isinstance(err, dict):
        for key in ("subcode", "error_subcode", "code"):
            if key in err and err[key] is not None:
                return str(err[key])
    for key in ("error_code", "code"):
        if key in payload and payload[key] is not None:
            return str(payload[key])
    return None


def classify_http_error(
    status_code: int,
    payload=None,
    error_map: dict | None = None,
) -> ConnectorError:
    """Return a raiseable ConnectorError for a non-2xx (non-429) HTTP response.

    Args:
        status_code: The HTTP status code returned by the provider.
        payload:     The response body -- a parsed JSON dict when available,
                     otherwise the raw text. Preserved (truncated) as evidence.
        error_map:   Optional refinement map, generally read from a module's
                     manifest. Keys are "<status>:<provider_code>" (e.g.
                     "403:190"); values are canonical error-class strings
                     (e.g. "auth_revoked"). When a key matches the response's
                     status + extracted provider code, that class overrides the
                     pure-HTTP classification. Provider codes live ONLY in
                     manifests -- never in this core file (AD-2 / HG-1).

    Returns:
        A ConnectorError subclass instance. The caller raises it.

    HTTP 429 must NOT reach this function -- connectors raise
    core.quota.RateLimitError on 429 so the breaker/requeue path is unchanged.
    A misrouted 429 raises ValueError loudly instead of silently degrading to a
    retryable ``unclassified`` (which would bypass the breaker).
    """
    if status_code == 429:
        raise ValueError(
            "classify_http_error must not receive 429; "
            "raise core.quota.RateLimitError instead"
        )
    cls = _base_class_for_status(status_code)

    if error_map:
        provider_code = _extract_provider_code(payload)
        if provider_code is not None:
            refined = error_map.get(f"{status_code}:{provider_code}")
            if refined in _CLASS_BY_NAME:
                cls = _CLASS_BY_NAME[refined]

    return cls(provider_status=status_code, provider_payload=payload)
