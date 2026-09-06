"""BigQuery error taxonomy -- a map that was empty, and a payload that was mute.

Two things were wrong here, and only one of them was the empty map.

``manifest.error_map`` was ``{}`` with a note saying the actionable classes "map
one-to-one from the HTTP status", which was a statement about the CLASSIFIER as
it stood: core extracted ``error.code``, and BigQuery -- like every Google API --
sets it to the numeric HTTP status. BigQuery does publish a reason registry
(https://cloud.google.com/bigquery/docs/error-messages) in ``error.errors[].reason``,
and ``_extract_provider_codes`` now offers it first.

The second, quieter defect: ``_classify_bigquery_error`` handed the classifier
``str(exc)`` -- a plain string, from which no code can be extracted. So even a
filled map would have been consulted on every failure and matched nothing.
``_error_payload`` now rebuilds the REST envelope from the exception's ``errors``
attribute, which is where google-api-core keeps the reasons.

No network, no client: the exceptions are built to the shape google-api-core
raises them in.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "bigquery"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_MANIFEST_PATH = _MODULE_DIR / "manifest.json"

_KEY = re.compile(r"^(\d{3}):(.+)$")


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_bigquery_taxonomy", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _error_map() -> dict:
    return _manifest()["error_map"]


class _BigQueryError(Exception):
    """The two attributes google.api_core.exceptions.GoogleAPICallError carries
    that this connector reads: the HTTP ``code`` and the REST ``errors`` array."""

    def __init__(self, code: int, message: str, errors=None):
        super().__init__(message)
        self.code = code
        self.errors = errors or []


def _reason(reason: str, message: str) -> list[dict]:
    return [{"reason": reason, "location": "query", "message": message}]


def test_a_401_is_auth_expired_and_the_provider_body_survives(connector):
    """The rebuilt envelope IS the evidence -- message included, not replaced."""
    message = "Request had invalid authentication credentials."
    exc = _BigQueryError(401, message, _reason("authError", message))

    err = connector._classify_bigquery_error(exc)

    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    assert err.provider_payload["error"]["code"] == 401
    assert err.provider_payload["error"]["message"] == message
    assert err.provider_payload["error"]["errors"] == _reason("authError", message)


def test_the_map_turns_a_rate_limited_403_into_something_worth_retrying(connector):
    """The refinement, measured against what core says WITHOUT the map.

    BigQuery returns concurrency and rate limits on an HTTP 403 and documents
    them as retryable. Pure HTTP reads 403 as ``permission_denied`` --
    retryable=False, user_action "reconnect" -- so a query refused for one minute
    of over-use was abandoned, and the operator was sent to re-authorise a
    credential that had every right it needed.
    """
    from core.pull_errors import classify_http_error

    message = "Exceeded rate limits: too many concurrent queries."
    exc = _BigQueryError(403, message, _reason("rateLimitExceeded", message))

    err = connector._classify_bigquery_error(exc)

    assert err.error_class == "provider_transient"
    assert err.retryable is True
    assert err.provider_status == 403
    assert err.error_class != classify_http_error(403, None, None).error_class


def test_a_real_access_denial_stays_a_permission_refusal(connector):
    """The map refines; it does not blanket-rewrite 403."""
    message = "Access Denied: Table project.dataset.table: User does not have permission."
    exc = _BigQueryError(403, message, _reason("accessDenied", message))

    err = connector._classify_bigquery_error(exc)
    assert err.error_class == "permission_denied"
    assert err.retryable is False


def test_a_missing_table_stops_being_replayed(connector):
    """404 is ``unclassified`` (retryable) in core on purpose -- half the
    connectors read it as "no access", half as "no such thing", and core refuses
    to impose either. BigQuery answers 403 accessDenied for the access case, so a
    404 here really is a selection drift: not retryable, and no reconnect."""
    from core.pull_errors import classify_http_error

    message = "Not found: Table project.dataset.table"
    exc = _BigQueryError(404, message, _reason("notFound", message))

    err = connector._classify_bigquery_error(exc)
    assert err.error_class == "invalid_request"
    assert err.retryable is False
    assert classify_http_error(404, None, None).error_class == "unclassified"


def test_an_exception_without_reasons_is_preserved_exactly_as_before(connector):
    """The rebuild is additive. A client exception carrying no ``errors`` keeps
    passing its message through as the payload, and falls back to pure HTTP."""
    exc = _BigQueryError(503, "Backend Error")

    err = connector._classify_bigquery_error(exc)
    assert err.provider_payload == "Backend Error"
    assert err.error_class == "provider_transient"


def test_an_exception_with_no_status_is_still_classified(connector):
    """A client error with no HTTP code at all is treated as a 5xx, unchanged."""
    err = connector._classify_bigquery_error(Exception("connection reset"))
    assert err.provider_status == 500
    assert err.error_class == "provider_transient"


def test_the_connector_reads_the_manifest_map_and_caches_it(connector):
    """A map the connector never loads classifies nothing (playbook step 4)."""
    assert connector._load_error_map() == _error_map()
    assert connector._load_error_map() is connector._load_error_map()


def test_the_map_has_the_only_shape_its_reader_can_look_up():
    """Keys ``<status>:<provider_code>``, values inside the taxonomy, no 429."""
    from core.pull_errors import ERROR_CLASSES, classify_http_error

    error_map = _error_map()
    assert error_map, "bigquery must declare a filled error_map"

    refining = []
    for key, value in error_map.items():
        match = _KEY.match(key)
        assert match, f"{key!r} is not '<status>:<provider_code>'"
        status = int(match.group(1))
        assert status != 429, f"{key!r}: a 429 never reaches classify_http_error"
        assert value in ERROR_CLASSES, f"{key!r} -> {value!r} is outside the taxonomy"
        if value != classify_http_error(status, None, None).error_class:
            refining.append(key)

    assert refining, "a map that restates pure HTTP everywhere proves nothing"

    note = _manifest().get("_error_map_note")
    assert isinstance(note, str) and note.strip()
    assert note.isascii()  # AI-03


def test_a_bigquery_rate_limit_reaches_the_breaker_not_a_valueerror(connector):
    """429 takes the quota path, like every other module's 429.

    `classify_http_error` raises ValueError on 429 BY DESIGN, so that a rate
    limit can never degrade into a retryable `unclassified` that skips the
    breaker. Every other connector honours that by branching on the status before
    classifying. This one had no 429 branch at all -- the BigQuery client raises a
    typed exception rather than returning a response, so nothing ever looked --
    and a `TooManyRequests` therefore raised ValueError out of the pull: the
    breaker recorded nothing, the job re-queued nowhere, and the operator saw a
    crash instead of the documented recovery.
    """
    from core.quota import RateLimitError

    class _TooManyRequests(Exception):
        code = 429
        retry_after = 30
        errors = [{"reason": "rateLimitExceeded", "message": "too many"}]

    error = connector._classify_bigquery_error(_TooManyRequests("quota exceeded"))
    assert isinstance(error, RateLimitError), (
        "a 429 must become RateLimitError (the breaker path), never go through "
        "classify_http_error, which raises ValueError on 429"
    )
    assert error.platform == "bigquery"
    assert error.retry_after == 30


def test_a_rate_limit_without_retry_after_is_still_a_rate_limit(connector):
    from core.quota import RateLimitError

    class _TooManyRequests(Exception):
        code = 429

    error = connector._classify_bigquery_error(_TooManyRequests("slow down"))
    assert isinstance(error, RateLimitError)
    assert error.retry_after is None
