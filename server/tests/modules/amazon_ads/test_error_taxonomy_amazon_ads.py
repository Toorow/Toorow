"""Amazon Ads error taxonomy -- story 67-21 (2026-08-17).

WHY THIS FILE EXISTS, AND WHY THE MAP IS STILL EMPTY. Repair 6 of
reviews/audit-2026-08-17/13-connecteurs.md asked for this manifest's
``error_map`` to be filled. It is not, and that is the measured answer rather
than an unfinished one:

  * Amazon reporting bodies are ``{"code", "details"}`` where ``code`` merely
    restates the HTTP status and ``details`` is free text. A key of the form
    ``"404:404"`` would satisfy a guard by accident and express nothing.
  * The one Amazon vocabulary that WOULD refine something is the Login-with-
    Amazon OAuth error set (``invalid_grant`` on a revoked refresh token,
    ``unauthorized_client``, ``invalid_client``). This connector never sees it:
    every token comes from ``core.nango_client.get_fresh_token``, so the LwA
    exchange happens outside the module and an LwA body never reaches an httpx
    response here. Keying it would create a DEAD key -- the exact defect this
    repair exists to remove.

So the two things this file pins are: the map is declared EMPTY (a statement:
"we looked") rather than absent ("nobody looked"), and the seam is LIVE -- the
map is loaded and handed to ``classify_http_error`` on every raise, so the day
a live capture (AI-13) yields a real ``"<status>:<code>"`` the refinement works
without anyone having to remember this module.

The judgments Amazon DOES let us make are module logic and stay where they
apply (``_DETAILS_REFINEMENTS`` on free-text substrings, ``_STATUS_OVERRIDES``
on statuses core leaves unclassified). They are pinned here too, because an
empty map must not read as an unclassified connector.

Offline: no network, no DB, no token.
"""

from __future__ import annotations

import json

import pytest

from .conftest import CONNECTOR_PATH, MODULE_DIR


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))


#: The shape Amazon actually returns -- 'code' restates the status.
_UNAUTHORIZED = {"code": "401", "details": "Unauthorized"}


def test_a_401_is_auth_expired_and_keeps_the_payload(connector):
    """The real raise site, the real body, the payload preserved as evidence."""
    with pytest.raises(Exception) as exc_info:
        connector._raise_provider_error(401, body=_UNAUTHORIZED)

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    assert err.provider_payload == _UNAUTHORIZED


def test_the_only_code_amazon_sends_is_the_status_itself(connector):
    """The measurement behind the empty map, made executable.

    If a future Amazon body ever carries a code that is NOT the status, this
    assertion is where the taxonomy gap becomes visible.
    """
    assert _UNAUTHORIZED["code"] == "401"


def test_error_map_is_declared_empty_not_absent(connector, manifest):
    """`{}` says "we looked"; a missing key says "nobody looked". Keep the former."""
    assert "error_map" in manifest
    assert manifest["error_map"] == {}
    assert connector._load_error_map() == {}
    note = manifest.get("_error_map_note")
    assert note, "an empty map must carry its reason"
    assert note.isascii()  # AI-03


def test_the_seam_is_live_even_though_the_map_is_empty(connector):
    """The map is HANDED to core, so a future key needs no code change.

    Before this story ``_raise_provider_error`` called ``classify_http_error``
    with two arguments: the manifest could have grown a perfect key and nothing
    would have read it.
    """
    source = CONNECTOR_PATH.read_text(encoding="utf-8")
    assert "classify_http_error(status_code, body, _load_error_map())" in source


def test_the_module_side_judgments_still_apply(connector):
    """An empty map must not read as "this connector classifies nothing"."""
    # A details substring Amazon documents, refined in the module.
    with pytest.raises(Exception) as exc_info:
        connector._raise_provider_error(
            401, body={"code": "401", "details": "Not authorized to access scope 12345"}
        )
    assert exc_info.value.error_class == "permission_denied"

    # A status core leaves retryable-unknown, settled in the module.
    with pytest.raises(Exception) as exc_info:
        connector._raise_provider_error(404, body={"code": "404", "details": "no report"})
    assert exc_info.value.error_class == "invalid_request"
    assert exc_info.value.retryable is False


def test_a_429_never_reaches_the_classifier(connector):
    """Which is also why a 429 entry in any error_map is dead by construction."""
    from core.pull_errors import classify_http_error
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError):
        connector._raise_provider_error(429, body={"code": "429"})

    with pytest.raises(ValueError):
        classify_http_error(429, {})
