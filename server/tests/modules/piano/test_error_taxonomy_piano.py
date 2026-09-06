"""Piano error taxonomy -- story 67-21 (2026-08-17).

WHY THIS FILE EXISTS. ``manifest.error_map`` was emptied on 2026-08-01 with a
correct diagnosis and an incomplete search: its ten keys all restated the
pure-HTTP verdict, so declaring them made the taxonomy "look refined where it
was not". The note then asked for entries "the day a live pass shows a Category
whose class differs from its status". The full published table
(developers.piano.io/analytics/data-api/technical-information/error-codes/)
turns out to hold five, no live pass needed -- and they are exactly the ones a
scheduled pull walks into:

  * ``400 DataNotReady`` ("The data is not yet available. It is either currently
    being processed, or relates to a period too far in the past"),
    ``400 Timeout`` ("Time out. The request cannot be processed.") and
    ``400 CancelledRequest`` ("Request aborted") are RETRYABLE. core reads any
    400 as ``invalid_request``, which is never retried -- so a pull that landed
    in a processing window simply died, and the window was the only problem.
  * ``404 ResourceNotFound_*`` and ``409 ElementAlreadyExists_*`` are settled
    refusals. core leaves both ``unclassified``, i.e. retried to the dead letter
    for a resource that will never appear.

AUTH_REVOKED IS STILL NOT DECLARED, and that is a measurement, not an omission:
Piano's only authentication Category is ``BadAuthentication_*`` (401, "Incorrect
login or password") and no published Category means a revoked or disabled key.
Inventing one to satisfy a gate is the failure mode this whole repair exists to
remove.

The machinery was already right: ``_extract_api_code`` reads the API-Code,
``_code_prefix`` reduces ``Category_Subcode`` to its Category, and the Category
is fed to core as a top-level ``code``. Only the keys were missing.

Offline: no network, no DB, no key.
"""

from __future__ import annotations

import json

import pytest

from .conftest import MODULE_DIR


@pytest.fixture(scope="module")
def error_map() -> dict:
    """The REAL manifest map -- never a retyped copy of it."""
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    return manifest["error_map"]


#: A Piano v3 authentication refusal (API-Code = Category_Subcode).
_BAD_AUTHENTICATION = {
    "ErrorCode": "BadAuthentication_NoHeader",
    "ErrorMessage": "Incorrect login or password",
}

#: The one a scheduled pull meets most often: the day is still being processed.
_DATA_NOT_READY = {
    "ErrorCode": "DataNotReady",
    "ErrorMessage": (
        "The data is not yet available. It is either currently being processed, "
        "or relates to a period too far in the past."
    ),
}


def test_a_401_bad_authentication_keeps_the_payload_and_its_class(connector):
    """The documented 401, driven through the real raise site."""
    with pytest.raises(Exception) as exc_info:
        connector._raise_provider_error(401, body=_BAD_AUTHENTICATION)

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401

    # The Category is what core matches on; the full body stays as evidence.
    assert err.provider_payload["code"] == "BadAuthentication"
    assert err.provider_payload["_provider_body"] == _BAD_AUTHENTICATION


def test_a_400_data_not_ready_becomes_retryable(connector):
    """The refinement that actually rescues a pull.

    core reads 400 as ``invalid_request``: not retried, and reported as a
    broken integration. Piano is saying "come back later".
    """
    from core.pull_errors import classify_http_error

    with pytest.raises(Exception) as exc_info:
        connector._raise_provider_error(400, body=_DATA_NOT_READY)

    err = exc_info.value
    assert err.error_class == "provider_transient"
    assert err.retryable is True

    assert classify_http_error(400, _DATA_NOT_READY, None).error_class == "invalid_request"


def test_a_404_and_a_409_stop_being_retried_forever(connector):
    """core leaves both 'unclassified', which is RETRYABLE."""
    from core.pull_errors import classify_http_error

    for status, body in (
        (404, {"ErrorCode": "ResourceNotFound_Metric", "ErrorMessage": "Metric does not exist"}),
        (409, {"ErrorCode": "ElementAlreadyExists_Metric",
               "ErrorMessage": "The provided metric already exists"}),
    ):
        with pytest.raises(Exception) as exc_info:
            connector._raise_provider_error(status, body=body)
        assert exc_info.value.error_class == "invalid_request", status
        assert exc_info.value.retryable is False, status
        assert classify_http_error(status, None, None).error_class == "unclassified"


def test_the_invalid_columns_drift_signal_is_untouched(connector):
    """The 400 InvalidColumns family is THE dynamic-catalog drift key.

    Filling the map must not change it: an unknown Data Model column is still a
    typed ``invalid_request``, never a crash and never a silent drop.
    """
    body = {"ErrorCode": "InvalidColumns_UnknownColumns", "ErrorMessage": "Column unknown"}
    with pytest.raises(Exception) as exc_info:
        connector._raise_provider_error(400, body=body)
    assert exc_info.value.error_class == "invalid_request"


def test_every_key_is_a_bare_category_not_a_full_api_code(connector, error_map):
    """``_code_prefix`` cuts at the first underscore -- a full code is unreachable.

    A key like "404:ResourceNotFound_Metric" would never be looked up, so this
    is the shape rule that keeps the map alive rather than decorative.
    """
    for key in error_map:
        code = key.split(":", 1)[1]
        assert connector._code_prefix(code) == code, key


def test_the_map_refines_at_least_one_verdict(error_map):
    """A map that restates pure HTTP proves nothing its absence would not.

    Same criterion as the conformance gate
    (tests/conformance/test_pull_contract.py section 7), asserted here on the
    REAL manifest so this module fails first and locally.
    """
    from core.pull_errors import classify_http_error

    assert error_map, "error_map is declared and must not be empty"
    refining = {
        key: value
        for key, value in error_map.items()
        if value
        != classify_http_error(int(key.split(":", 1)[0]), None, None).error_class
    }
    assert refining == {
        "400:CancelledRequest": "provider_transient",
        "400:DataNotReady": "provider_transient",
        "400:Timeout": "provider_transient",
        "404:ResourceNotFound": "invalid_request",
        "409:ElementAlreadyExists": "invalid_request",
    }


def test_the_map_has_the_only_shape_core_can_read(error_map):
    """Keys are "<status>:<code>", values are canonical, and 429 is never keyed."""
    from core.pull_errors import ERROR_CLASSES

    for key, value in error_map.items():
        status, separator, code = key.partition(":")
        assert separator and status.isdigit() and len(status) == 3, key
        assert code, key
        assert status != "429", f"{key}: a 429 goes to the quota breaker, never here"
        assert value in ERROR_CLASSES, f"{key} -> {value}"


def test_no_quota_category_is_keyed(error_map):
    """509 and 429 QuotasExceeded_* belong to the quota breaker, not here."""
    assert not [key for key in error_map if key.split(":", 1)[1] == "QuotasExceeded"]


def test_the_connector_hands_the_map_to_core(connector, error_map):
    """A map nobody passes to classify_http_error classifies nothing."""
    assert connector._load_error_map() == error_map
    source = (MODULE_DIR / "connector.py").read_text(encoding="utf-8")
    assert "_load_error_map()" in source


def test_the_note_names_where_the_codes_come_from():
    """A code with no cited reference is indistinguishable from an invented one."""
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    note = manifest.get("_error_map_note")
    assert note, "a declared map must carry its reference"
    assert note.isascii()  # AI-03
