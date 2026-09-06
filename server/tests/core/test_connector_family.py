"""AI-206 -- one derivation, and it fails closed.

`core.connector_family` exists because two steps of the same lifecycle needed the
same fact and each answered it its own way: `connector_installation_api` asked the
registry, `run_verification` read the transient installation state. These tests
pin the three things the shared answer has to hold, none of which needs a
database.
"""

from __future__ import annotations

from core import connector_family


def test_a_shipped_module_owes_no_routing_contract():
    """The 39 modules are Connectors with a manifest, not transports."""
    assert connector_family.is_module_connector("google-ads") is True
    assert connector_family.owes_routing_contract("google-ads") is False


def test_the_inbound_transport_owes_its_routing_contract():
    """`managed_feed` is the inbound family's only member and is not a module.

    `app.connector_domain_configs` carries `domain`, `provider_adapter`,
    `webhook_endpoint_version` and `dns_evidence_*`; it was designed for this
    connector and for nothing else.
    """
    assert connector_family.is_module_connector("managed_feed") is False
    assert connector_family.owes_routing_contract("managed_feed") is True


def test_an_unreadable_registry_keeps_the_stricter_path(monkeypatch, caplog):
    """No answer is not « no domain owed ».

    A connector that stalls visibly is recoverable; one that skips a step it owed
    is not. So an unreadable registry answers « owes a routing contract », and
    says so in the log rather than silently.
    """
    import core.main as main  # noqa: PLC0415

    def _explode():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(main, "get_loaded_modules", _explode)

    assert connector_family.is_module_connector("google-ads") is False
    assert connector_family.owes_routing_contract("google-ads") is True


def test_a_blank_connector_name_is_not_a_module():
    """Guards the callers: a missing name must not be read as a shipped module."""
    for value in ("", "   ", None):
        assert connector_family.is_module_connector(value) is False
        assert connector_family.owes_routing_contract(value) is True
