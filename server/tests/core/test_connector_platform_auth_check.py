"""AI-206 -- what a PLATFORM-scoped verification may check, and what it may not.

THE QUESTION THIS ANSWERS. `_build_checks` returned `[]`, so every real
deployment evaluated zero checks and the core read `not_configured` -- honest,
and the reason no connector ever reaches READY. Six locks were removed on
2026-08-16; this was the seventh, and it was left open because it looked like a
design question: an installation is PLATFORM-scoped (`environment` +
`connector_name`), while every authorization in this product is a client's OAuth
consent, held per PROJECT. There is no platform-level credential, and inventing
one would contradict the ratified Google-stack decision of 2026-08-11.

THE ANSWER IS THAT THE QUESTION HAD THE SCOPE BACKWARDS. A platform check does
not ask "has someone authorized this connector" -- nobody can answer that at
platform scope. It asks the question that IS platform-scoped:

    can THIS DEPLOYMENT present the authorization path this connector declares?

That is a fact about the environment, not about a client. A Google-direct
connector needs this deployment's OAuth client; a Nango-backed one needs the
Nango secret; a connector that declares no authorization needs nothing. None of
those three needs a client credential, and none of them is invented here: the
path comes from the connector's own manifest.

WHAT IT DELIBERATELY DOES NOT PROVE, stated because a verification that
overclaims is worse than none: it does not prove any client can connect, that a
token is valid, or that the provider is reachable. Those are per-project facts,
answered by the OAuth flow and by account discovery, on a different surface.
"""

from __future__ import annotations

from unittest.mock import patch

from core import connector_verification_api as api

_GOOGLE_ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": "client-EXAMPLE",
    "GOOGLE_OAUTH_CLIENT_SECRET": "secret-EXAMPLE",
    "GOOGLE_OAUTH_REDIRECT_URI": "https://example.com/oauth/callback",
}


def _check(connector_name: str):
    """Evaluate the one check `_build_checks` returns, or report its absence."""
    checks = api._build_checks(connector_name=connector_name, environment="production")
    assert checks, (
        "_build_checks returned no check: a deployment then evaluates nothing, the "
        "core reads `not_configured`, and no installation can ever reach READY."
    )
    assert len(checks) == 1, "one platform question, one check"
    return checks[0]()


def test_a_google_direct_connector_passes_when_this_deployment_has_its_oauth_client(
    monkeypatch,
):
    for name, value in _GOOGLE_ENV.items():
        monkeypatch.setenv(name, value)
    with patch.object(api, "_declared_auth_path", return_value="google_direct"):
        passed, evidence_class, reason = _check("any-connector")
    assert passed is True
    assert evidence_class == "auth_check"
    assert reason == ""


def test_the_same_connector_fails_when_the_deployment_has_no_oauth_client(monkeypatch):
    for name in _GOOGLE_ENV:
        monkeypatch.delenv(name, raising=False)
    with patch.object(api, "_declared_auth_path", return_value="google_direct"):
        passed, evidence_class, reason = _check("any-connector")
    assert passed is False
    # `auth_check` even on failure: migration 268 requires exactly this class for a
    # run with no routing contract, and a module connector never has one.
    assert evidence_class == "auth_check"
    assert reason == "dependency_unavailable"


def test_a_nango_backed_connector_asks_for_the_nango_secret(monkeypatch):
    monkeypatch.setenv("NANGO_SECRET_KEY", "nango-EXAMPLE")
    with patch.object(api, "_declared_auth_path", return_value="nango"):
        passed, _, reason = _check("any-connector")
    assert passed is True and reason == ""

    monkeypatch.delenv("NANGO_SECRET_KEY", raising=False)
    with patch.object(api, "_declared_auth_path", return_value="nango"):
        passed, _, reason = _check("any-connector")
    assert passed is False and reason == "dependency_unavailable"


def test_a_connector_declaring_no_authorization_has_nothing_to_configure():
    """`none` is a real answer and passes. It is not an unknown."""
    with patch.object(api, "_declared_auth_path", return_value="none"):
        passed, evidence_class, reason = _check("any-connector")
    assert passed is True
    assert evidence_class == "auth_check"
    assert reason == ""


def test_an_unreadable_manifest_fails_closed_and_names_no_provider():
    """No manifest, no declared path, no pass -- and no guess at which path it is."""
    with patch.object(api, "_declared_auth_path", return_value=None):
        passed, evidence_class, reason = _check("no-such-connector")
    assert passed is False
    assert evidence_class == "auth_check"
    assert reason == "dependency_unavailable"


def test_the_declared_path_is_read_from_the_manifest_not_hard_coded():
    """AD-2: the check contains no provider name; it reads what the module declares.

    The two path names it branches on are AUTHORIZATION vocabulary, not provider
    vocabulary -- the same status the mart names hold in `metric_reconciliation`.
    """
    import pathlib

    source = pathlib.Path(api.__file__).read_text(encoding="utf-8").lower()
    for provider in ("google-ads", "meta-ads", "tiktok", "linkedin", "shopify", "stripe"):
        assert provider not in source, f"provider name {provider!r} hard-coded"


def test_every_shipped_manifest_resolves_to_a_HANDLED_path(monkeypatch):
    """Not "resolves something" -- resolves to a branch the check actually handles.

    THIS ASSERTION IS THE REPAIR OF ITS OWN FIRST VERSION, 2026-08-17. It read
    `len(resolved) >= 30`, which passed while **20 of the 39** connectors were
    falling straight through to the fail-closed branch: they declare the legacy
    `auth_type` scalar (`oauth2`, `api_key`, `basic`), which names a mechanism
    and not a path. A weak assertion is worse than none — it reports coverage it
    never checked, and it did.

    So the check is behavioural: with every platform prerequisite present, NO
    shipped connector may report `dependency_unavailable`. A connector that does
    can never reach READY, and its absence from the wizard would be blamed on the
    deployment rather than on its manifest.
    """
    import pathlib

    for name, value in _GOOGLE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("NANGO_SECRET_KEY", "nango-EXAMPLE")

    modules = pathlib.Path(api.__file__).parents[1] / "modules"
    names = sorted(p.parent.name for p in modules.glob("*/manifest.json"))
    assert len(names) >= 30, "the module tree is not where this test thinks it is"

    unhandled = {
        name: api._declared_auth_path(name)
        for name in names
        if api._platform_authorization_is_configured(name)[0] is False
    }
    assert not unhandled, (
        "these connectors declare an authorization path the platform check does "
        f"not handle, so they fail verification on a fully configured deployment: "
        f"{unhandled}"
    )


def test_a_legacy_scalar_still_needs_its_platform_configuration(monkeypatch):
    """Resolving the legacy scalars must not become a free pass.

    They route to the Nango path; without the Nango secret they fail, exactly
    like a connector that declares that path in full.
    """
    monkeypatch.delenv("NANGO_SECRET_KEY", raising=False)
    with patch.object(api, "_declared_auth_path", return_value="api_key"):
        passed, _, reason = _check("any-connector")
    assert passed is False and reason == "dependency_unavailable"
