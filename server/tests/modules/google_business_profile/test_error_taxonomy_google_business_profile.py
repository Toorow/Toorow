"""GBP error taxonomy -- AI-114 (2026-08-01), the seam that was declared and dead.

manifest.error_map carried two keys, "404" -> permission_denied and "429" ->
provider_transient, and NEITHER could ever be read:

  * the map's only reader is ``core.pull_errors.classify_http_error``, whose key
    grammar is "<status>:<provider_code>" -- a bare "404" is never looked up, so
    a GBP 404 came out ``unclassified`` (retryable, and mute about what the
    person should do about it);
  * a 429 never reaches that function at all -- the connector raises
    ``core.quota.RateLimitError`` so the breaker path stays intact, and
    ``classify_http_error`` raises ValueError if a 429 is handed to it anyway.

The map was then declared EMPTY (an explicit statement -- "we looked, this
provider refines nothing core can key on" -- which is not the same as an absent
key), and the 404 judgment moved to ``connector._STATUS_OVERRIDES`` where it
applies.

2026-08-17 -- THE MAP IS FILLED. The emptiness was honest about the CLASSIFIER
and wrong about GBP: core extracted ``error.code`` alone, and every Google API
sets it to the numeric HTTP status, so no key could ever discriminate.
``_extract_provider_codes`` now offers ``error.errors[].reason`` and the
``error.status`` enum first, and GBP publishes both. The two holes AI-114 named
are kept as holes on purpose and pinned below: no 404 key (a status-only rule the
'<status>:<code>' grammar cannot express) and no 429 key (the breaker path).

These tests pin the seam, not the plumbing: no network, no DB.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = (
    Path(__file__).parents[4] / "server" / "modules" / "google-business-profile"
)
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_gbp_errors", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status, json=body, request=httpx.Request("GET", "https://example.com/v1/x")
    )


_NOT_FOUND_BODY = {
    "error": {
        "code": 404,
        "message": "Requested entity was not found.",
        "status": "NOT_FOUND",
    }
}


def test_404_is_permission_denied_and_is_not_retried(connector):
    """The judgment the dead key expressed, now actually applied.

    Google answers 404 for a location the token cannot reach as readily as for
    one that does not exist. The actionable verdict is therefore "ask for access
    to this location", not "unknown, try again later" -- and the difference is
    not cosmetic: ``unclassified`` is retryable, ``permission_denied`` is not,
    so the old behaviour re-queued a call that could never succeed and told the
    person nothing.
    """
    from core.pull_errors import classify_http_error

    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(404, _NOT_FOUND_BODY))

    err = exc_info.value
    assert err.error_class == "permission_denied"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 404
    assert err.provider_payload == _NOT_FOUND_BODY

    # What core says without the module -- i.e. exactly what the dead key
    # produced for as long as it was declared.
    assert classify_http_error(404, _NOT_FOUND_BODY).error_class == "unclassified"


def test_429_never_reaches_the_classifier(connector):
    """The removed "429" key could not have fired even with a readable grammar.

    A 429 goes to the quota breaker. This is also why a 429 entry in any
    error_map is a dead entry by construction, not merely a mis-keyed one.
    """
    from core.pull_errors import classify_http_error
    from core.quota import RateLimitError

    resp = httpx.Response(
        429,
        headers={"Retry-After": "42"},
        json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
        request=httpx.Request("GET", "https://example.com/v1/x"),
    )
    with pytest.raises(RateLimitError) as exc_info:
        connector._raise_for_status(resp)
    assert exc_info.value.retry_after == 42

    with pytest.raises(ValueError):
        classify_http_error(429, {})


def test_pure_http_statuses_are_left_to_core(connector):
    """_STATUS_OVERRIDES claims 404 and nothing else.

    A module-side table is only defensible while it stays the exception. If it
    grows to shadow statuses core already classifies, the taxonomy has drifted
    back into two implementations -- so the boundary is pinned here.
    """
    assert set(connector._STATUS_OVERRIDES) == {404}

    for status, expected in ((401, "auth_expired"), (403, "permission_denied"),
                             (400, "invalid_request"), (503, "provider_transient")):
        with pytest.raises(Exception) as exc_info:
            connector._raise_for_status(_response(status, {"error": {"code": status}}))
        assert exc_info.value.error_class == expected, status


# ---------------------------------------------------------------------------
# 2026-08-17 -- the filled map: what it changes, and the two holes it keeps.
# ---------------------------------------------------------------------------

_ERROR_MAP_KEY = re.compile(r"^(\d{3}):(.+)$")

_UNAUTHENTICATED_BODY = {
    "error": {
        "code": 401,
        "message": "Request had invalid authentication credentials.",
        "status": "UNAUTHENTICATED",
    }
}

#: The v4 host (reviews, local posts) still answers a spent quota with an HTTP
#: 403 whose reason -- not whose status -- is the only thing that says "wait".
_QUOTA_BODY = {
    "error": {
        "code": 403,
        "message": "Rate Limit Exceeded",
        "errors": [
            {
                "message": "Rate Limit Exceeded",
                "domain": "usageLimits",
                "reason": "rateLimitExceeded",
            }
        ],
    }
}


def test_a_401_is_auth_expired_and_the_provider_body_survives(connector):
    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(401, _UNAUTHENTICATED_BODY))

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    assert err.provider_payload == _UNAUTHENTICATED_BODY


def test_a_spent_quota_is_not_the_same_refusal_as_a_missing_grant(connector):
    """The refinement, measured against what core says WITHOUT the map.

    This connector's whole doctrine is that a GBP 403 usually means "the grant
    has not landed yet" -- and that reading is exactly wrong for the one 403 that
    means "come back in a minute". Pure HTTP calls both ``permission_denied``,
    retryable=False; the reason string is the only place they differ, and now the
    quota one is retried instead of abandoned.
    """
    from core.pull_errors import classify_http_error

    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(403, _QUOTA_BODY), surface="reviews_v4")

    err = exc_info.value
    assert err.error_class == "provider_transient"
    assert err.retryable is True
    assert err.provider_status == 403
    assert err.provider_payload == _QUOTA_BODY
    assert err.error_class != classify_http_error(403, None, None).error_class


def test_a_pending_grant_403_is_untouched_by_the_map(connector):
    """The provisioning case keeps its class AND its precondition marker."""
    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(403, _FORBIDDEN_BODY), surface="performance")

    err = exc_info.value
    assert err.error_class == "permission_denied"
    assert connector.precondition_of(err) == "google_access_pending"


def test_the_two_unreachable_statuses_carry_no_key(connector):
    """AI-114's holes, kept: a key there would be a decoration nobody reads.

    404 is decided by ``_STATUS_OVERRIDES`` before the classifier is consulted,
    and 429 never reaches the classifier at all (breaker path). Declaring either
    would put back exactly the dead entries AI-114 removed.
    """
    error_map = connector._load_manifest()["error_map"]
    assert not [k for k in error_map if k.startswith("404:")]
    assert not [k for k in error_map if k.startswith("429:")]


def test_error_map_is_declared_filled_and_the_connector_reads_it(connector):
    """Declared, well-formed, refining, and actually loaded (playbook step 4)."""
    from core.pull_errors import ERROR_CLASSES, classify_http_error

    manifest = connector._load_manifest()
    error_map = manifest.get("error_map")
    assert isinstance(error_map, dict) and error_map
    assert connector._load_error_map() == error_map

    refining = []
    for key, value in error_map.items():
        match = _ERROR_MAP_KEY.match(key)
        assert match, f"{key!r} is not '<status>:<provider_code>'"
        status = int(match.group(1))
        assert value in ERROR_CLASSES, f"{key!r} -> {value!r} is outside the taxonomy"
        if value != classify_http_error(status, None, None).error_class:
            refining.append(key)

    assert refining, "a map that restates pure HTTP everywhere proves nothing"

    note = manifest.get("_error_map_note")
    assert isinstance(note, str) and note.strip(), "a map must carry its reference"
    assert note.isascii()  # AI-03


# ---------------------------------------------------------------------------
# Story 30.1 -- the precondition classification.
#
# Added 2026-08-04, after the 2026-07-31 audit found the story's central
# doctrinal nuance missing: every 403 went to permission_denied with no branch
# saying "this grant has not landed yet", so the 0-QPM gate produced exactly the
# failure loop the story promised to avoid.
#
# The taxonomy is NOT extended for this -- there is no sixth error class. A
# precondition is a READING of permission_denied, carried as an attribute, so
# core keeps one taxonomy and the module keeps its product judgment.
# ---------------------------------------------------------------------------

_FORBIDDEN_BODY = {
    "error": {
        "code": 403,
        "message": "The caller does not have permission",
        "status": "PERMISSION_DENIED",
    }
}


def test_a_403_on_a_gated_surface_is_marked_as_a_pending_grant(connector):
    """Still permission_denied -- and now it says WHICH gate it is stuck behind."""
    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(403, _FORBIDDEN_BODY), surface="reviews_v4")

    err = exc_info.value
    assert err.error_class == "permission_denied"
    assert err.retryable is False
    assert connector.precondition_of(err) == "reviews_access_pending"
    assert "allowlist" in err.precondition_message.lower()

    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(403, _FORBIDDEN_BODY), surface="performance")
    assert connector.precondition_of(exc_info.value) == "google_access_pending"


def test_only_403_is_a_precondition(connector):
    """A gate is a 403. A 401 is a broken credential and a 404 is a missing grant
    on ONE resource -- neither is "wait for Google", and marking them so would
    tell the person to wait for something that will never arrive."""
    for status in (400, 401, 404, 500, 503):
        with pytest.raises(Exception) as exc_info:
            connector._raise_for_status(
                _response(status, {"error": {"code": status}}), surface="reviews_v4"
            )
        assert connector.precondition_of(exc_info.value) is None, status


def test_an_ungated_surface_keeps_a_403_as_a_plain_refusal(connector):
    """Every gated surface is named. A 403 from anywhere else is just forbidden,
    and calling it "pending" would promise a grant that is not coming."""
    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(403, _FORBIDDEN_BODY), surface="something_else")
    assert connector.precondition_of(exc_info.value) is None


def test_every_gated_surface_has_a_sentence_for_the_person(connector):
    """A reason code nobody can read is not a surfaced precondition."""
    for reason in set(connector._GATED_SURFACES.values()):
        message = connector._PRECONDITION_MESSAGES[reason]
        assert message.strip()
        assert message.isascii(), reason  # AI-03


def test_the_prevented_envelope_carries_the_documented_pull_keys(connector):
    """A refusal is still a pull result: the four documented keys survive it.

    AI-307 moved the SHAPE into `core.pull_envelope`, so what this asserts is
    that the connector still speaks the contract the worker reads -- and that
    `prevented_by`, the worker's own reader, accepts what this module builds.
    """
    from core.pull_envelope import prevented_by

    envelope = connector._prevented_envelope(
        "pull_1", "2026-07-01", "2026-07-31", "reviews_access_pending"
    )
    assert {"pull_id", "row_count", "date_from", "date_to"} <= set(envelope)
    assert envelope["row_count"] == 0
    assert envelope["prevented"] is True
    read = prevented_by(envelope, module_name="google-business-profile")
    assert read is not None
    assert read.reason == "reviews_access_pending"
    assert read.message == connector._PRECONDITION_MESSAGES["reviews_access_pending"]


def test_every_sentence_names_the_gesture_and_not_the_status_code(connector):
    """AI-307: a message a person cannot act on is not a surfaced precondition.

    "403 on the legacy host" is the cause; "request the allowlist, then re-ask
    these dates" is what releases the window. Only the second belongs on a screen.
    """
    for reason, message in connector._PRECONDITION_MESSAGES.items():
        assert "equest" in message, reason  # Request / request
        assert "re-ask" in message, reason
        assert "403" not in message, reason
        assert "QPM" not in message, reason
