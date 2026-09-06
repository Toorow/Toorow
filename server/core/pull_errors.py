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
  error_for_class(error_class, status_code, payload) -> ConnectorError | None

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

# ---------------------------------------------------------------------------
# Le vocabulaire des actions affichables -- LU DANS LA CIBLE, pas invente ici.
#
# `datastream-workbench-and-wizard.md:107` nomme les destinations d'une reparation :
#
#   << Authentication repair links to `Sources`; schema/mapping drift links to
#      `Mapping`; policy failures link to the owning Project/Governance object. >>
#
# et son tableau d'ecarts ajoute la quatrieme, pour l'acces manquant :
#
#   << Select a usable Source Account; hand off to `Sources` and resume when
#      access is missing. >>
#
# La cible a donc TROIS destinations pour un echec de pull, et le livre n'en
# implementait qu'une : `datastream_diagnosis._ALLOWED_USER_ACTIONS` valait
# `{"reconnect"}`, et coercait tout le reste a None. Ce n'etait pas une fermeture
# voulue, c'etait une sous-implementation -- donc une reconciliation, pas un
# arbitrage a demander (CLAUDE.md sec. 1).
#
# Une valeur ici est un ENDROIT OU ALLER, pas un conseil : c'est ce qui distingue
# un echec que l'operateur peut reparer d'un echec ou il n'a rien a faire.
RECONNECT = "reconnect"  # -> Sources : le credential ne marche plus ou n'atteint rien
SELECT_SOURCE_ACCOUNT = "select_source_account"  # -> Sources : aucun compte utilisable choisi
REVIEW_MAPPING = "review_mapping"  # -> Mapping : le plan demande ce que la source ne rend plus

#: L'ensemble ferme. `datastream_diagnosis` le LIT au lieu d'en tenir une copie :
#: une seconde transcription derive, et c'est exactement ce que la fermeture
#: d'AI-114 venait de retirer ailleurs.
USER_ACTIONS = (RECONNECT, SELECT_SOURCE_ACCOUNT, REVIEW_MAPPING)

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


def error_for_class(
    error_class: str | None,
    status_code: int | None = None,
    payload=None,
) -> ConnectorError | None:
    """Build the canonical error named by ``error_class``, or None if unknown.

    Added for AI-114 (2026-08-01). A connector sometimes has to make a judgment
    that ``classify_http_error`` cannot make for it -- a status the pure-HTTP
    table leaves ``unclassified`` but that a given API always means something
    precise by (an API that answers 404 where the caller simply lacks access,
    say), or a poll-state verdict carrying no HTTP status at all. That judgment
    is module logic and it belongs in the module.

    What it must NOT come with is a second copy of the taxonomy. Before this
    helper, two modules each carried a private ``_canonical_error_classes()``
    dict re-listing every class -- a third and a fourth transcription of
    ``_CLASS_BY_NAME``, each free to drift. Modules now name a class STRING and
    core builds it, so ``ERROR_CLASSES`` stays the one place the taxonomy is
    written down.

    Returns None (never raises, never invents a class) when ``error_class`` is
    None or outside the taxonomy -- the caller falls back to
    ``classify_http_error``. This mirrors ``classify_http_error``'s own rule
    that an out-of-taxonomy ``error_map`` value is ignored.
    """
    if error_class is None:
        return None
    cls = _CLASS_BY_NAME.get(error_class)
    if cls is None:
        return None
    return cls(provider_status=status_code, provider_payload=payload)


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
    if status_code in (400, 405, 422):
        # 405 (Method Not Allowed) et 422 (Unprocessable Entity) rejoignent 400 le
        # 2026-08-01 : ce sont des semantiques HTTP GENERIQUES, pas du vocabulaire
        # fournisseur, donc les porter ici ne touche pas AD-2. Deux modules les
        # redeclaraient chez eux faute que core les connaisse -- et un module qui
        # n'y avait pas pense les laissait `unclassified`, donc rejouees jusqu'au
        # dead_letter alors que rejouer une methode interdite ne peut rien donner
        # d'autre.
        return InvalidRequestError
    if status_code == 425:
        # 425 (Too Early) dit litteralement << reessaie plus tard >>.
        return ProviderTransientError
    if 500 <= status_code <= 599:
        return ProviderTransientError
    # ---------------------------------------------------------------------
    # 404 RESTE ICI, ET C'EST UNE DECISION, PAS UN OUBLI.
    #
    # AI-116 proposait de le porter ici avec 405/422/425. La mesure du 2026-08-01
    # dit non : sur les six modules qui se sont prononces, le verdict se partage
    # TROIS CONTRE TROIS -- trois lisent << la ressource adressee n'existe pas >>
    # (`invalid_request`), trois lisent << l'API repond 404 la ou l'appelant n'a
    # simplement pas l'acces >> (`permission_denied`).
    #
    # Les deux lectures sont justes pour leur API. Trancher ici imposerait la
    # mauvaise a la moitie d'entre eux, et surtout la mauvaise ACTION : dans un
    # cas il n'y a rien a faire, dans l'autre il faut se reconnecter. Le verdict
    # local n'est donc PAS la duplication qu'AI-116 croyait voir -- c'est le seul
    # endroit qui detient l'information. `unclassified` (rejouable) reste le
    # defaut prudent pour les modules qui ne se sont pas prononces.
    #
    # Le detail nomme (qui lit quoi) vit dans
    # `tests/core/test_user_action_vocabulary.py`, ou AD-2 ne s'applique pas, et
    # un test y garde la mesure : le jour ou les modules s'accordent, la question
    # se repose.
    # ---------------------------------------------------------------------
    return UnclassifiedError


def _extract_provider_codes(payload) -> list[str]:
    """Every conventional error code a JSON payload offers, most specific first.

    Generic only -- inspects conventional shapes without knowing any specific
    provider. Returns a list (possibly empty) of string candidates, in the order
    a map should be consulted. Never raises.

    Recognised shapes (all provider-neutral):
      {"error": {"subcode": X}}          -> str(X)   (most specific)
      {"error": {"error_subcode": X}}    -> str(X)
      {"error": {"reason": X}}           -> str(X)
      {"error": {"errors": [{"reason": X}, ...]}} -> str(X)
      {"error": {"status": X}}           -> str(X)
      {"error": {"code": X}}             -> str(X)
      {"error": "X"}                     -> "X"      (OAuth 2.0, RFC 6749)
      {"errors": [{"code": X}, ...]}     -> str(X)   (JSON:API)
      {"errors": [{"reason": X}, ...]}   -> str(X)
      {"errors": "X"}                    -> "X"
      {"error_code": X}                  -> str(X)
      {"code": X}                        -> str(X)
      {"reason": X} / {"status": X}      -> str(X)
      {"message": "X"}                   -> "X"      (LAST -- see below)

    WHY A LIST AND NOT ONE CODE
        The first version returned a single candidate and stopped at
        ``error.code``. That was enough for the providers whose body is
        ``{"error": {"code": 190}}`` -- and structurally useless for the family
        whose body is ``{"error": {"code": 403, "status": "PERMISSION_DENIED",
        "errors": [{"reason": "insufficientPermissions"}]}}``: ``error.code``
        there merely REPEATS the HTTP status, so the only key an ``error_map``
        could ever match was ``"403:403"``, which refines nothing. Measured
        2026-08-17, that family is six of the shipped modules, and it is exactly
        the set whose ``error_map`` was empty -- a map nobody could write a
        useful key for does not get written.

        Returning candidates rather than one code only WIDENS what a map can
        match: a status/reason key is consulted before ``code``, and a map that
        declares neither behaves exactly as before.

    WHY THE LIST IS LONG, AND WHY IT LIVES HERE
        Five connectors reached the same conclusion independently on 2026-08-17
        and each wrote a private ``_<module>_error_payload()`` that COPIED its
        code into a slot this function already read. That works and it costs the
        one thing an error payload is for: ``ConnectorError.provider_payload`` is
        EVIDENCE (AD-2), and a rewritten body is no longer the body the API sent.
        A module test caught it immediately -- the preserved payload had grown a
        ``reason`` key the API never wrote.

        The shapes those five needed are not vocabulary, they are conventions
        with specifications behind them: ``{"error": "invalid_grant"}`` is
        RFC 6749, ``{"errors": [{"code": ...}]}`` is JSON:API. So core learns the
        SHAPES (no vendor name, no vendor code -- AD-2 / HG-1 hold) and the
        connectors hand the body over untouched.

        ``message`` is deliberately LAST. It is free text, and one API's whole
        error vocabulary ("Bad credentials"); consulted after every structured
        candidate, it can only ever match a key a manifest deliberately wrote.
    """
    if not isinstance(payload, dict):
        return []
    candidates: list[str] = []

    def add(value) -> None:
        if value is None:
            return
        text = str(value)
        if text and text not in candidates:
            candidates.append(text)

    def add_from_list(entries) -> None:
        if not isinstance(entries, list):
            return
        for entry in entries:
            if isinstance(entry, dict):
                add(entry.get("code"))
                add(entry.get("reason"))

    err = payload.get("error")
    if isinstance(err, dict):
        for key in ("subcode", "error_subcode", "reason"):
            add(err.get(key))
        add_from_list(err.get("errors"))
        add(err.get("status"))
        add(err.get("code"))
    elif isinstance(err, str):
        # RFC 6749: the OAuth error response is {"error": "invalid_grant", ...}.
        add(err)

    errors = payload.get("errors")
    if isinstance(errors, str):
        add(errors)
    else:
        add_from_list(errors)

    for key in ("error_code", "reason", "status", "code"):
        add(payload.get(key))
    # Free text, so it never outranks a structured code. It is nonetheless the
    # entire error vocabulary of at least one shipped API.
    message = payload.get("message")
    if isinstance(message, str):
        add(message)
    return candidates


def _extract_provider_code(payload) -> str | None:
    """The single most specific provider code of *payload*, or None.

    Kept as the named seam three connectors' comments point at; the classifier
    itself consults every candidate.
    """
    codes = _extract_provider_codes(payload)
    return codes[0] if codes else None


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
        for provider_code in _extract_provider_codes(payload):
            refined = error_map.get(f"{status_code}:{provider_code}")
            if refined in _CLASS_BY_NAME:
                cls = _CLASS_BY_NAME[refined]
                break

    return cls(provider_status=status_code, provider_payload=payload)
