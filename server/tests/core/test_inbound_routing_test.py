"""Story 38.15 AC5 -- the Datastream-level synthetic routing test.

Ces tests sont PURS : une fausse connexion, aucun DSN. Un test pg-gated skippe
sans base et une regression y reste invisible ; celle-ci porte sur un ordre de
verifications et une forme de rapport, et les deux se prouvent hors ligne.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from core.inbound_routing_test import (
    ROUTING_STEPS,
    RoutingTestValidationError,
    run_datastream_routing_test,
)


class _Conn:
    """Une connexion qui ne repond a rien -- les seams sont patches."""

    def cursor(self):  # pragma: no cover -- jamais atteint dans ces tests
        raise AssertionError("aucun test ici ne doit toucher la base")


def _healthy_info(**overrides):
    info = {
        "connector_name": "managed_feed",
        "org_id": "org_1",
        "enabled": True,
        "lifecycle_state": "active",
        "channels": {"email", "webhook"},
    }
    info.update(overrides)
    return info


def _run(**patches):
    """Lance le test avec chaque seam remplace par un double."""
    base = {
        "_get_datastream_status": lambda conn, **kw: _healthy_info(),
        "_require_receivable": lambda info, **kw: None,
        "_ready_domain": lambda conn, **kw: "inbound.example.com",
        "_load_active_credential": lambda conn, **kw: {
            "id": "dic_1",
            "token_hash": "a" * 64,
            "safe_suffix": "abcdef",
            "state": "ACTIVE",
            "version": 3,
            "expires_at": None,
            "overlap_until": None,
        },
        "_effective_state": lambda state, **kw: state,
        "resolve_by_token_hash": lambda conn, **kw: {
            "allowed": True,
            "scope": {"datastream_id": "ds_1", "channel": "email"},
        },
    }
    base.update(patches)
    with patch.multiple("core.inbound_credentials", **{
        name: value for name, value in base.items()
    }):
        return run_datastream_routing_test(_Conn(), datastream_id="ds_1", channel="email")


def test_a_healthy_chain_routes():
    report = _run()
    assert report["routes"] is True
    assert report["blocking_step"] is None
    assert [entry["step"] for entry in report["steps"]] == list(ROUTING_STEPS)
    assert all(entry["passed"] for entry in report["steps"])


def test_the_result_says_it_is_synthetic_in_its_own_payload():
    """L AC5 exige qu il soit << clearly distinguished from provider deliveries >>.

    Ce resultat voyage vers un bandeau console ET vers un hote MCP. Les deux
    doivent pouvoir dire << c etait un test >> sans se souvenir de quel point
    d entree ils l ont obtenu.
    """
    assert _run()["synthetic"] is True


def test_it_stops_at_the_first_broken_link():
    """Un rapport qui continue liste les consequences de la premiere panne
    comme si elles etaient des problemes independants.

    Ici le canal n est pas configure. Le domaine, la cle et la resolution ne
    sont pas < faux > -- personne ne les a essayes.
    """
    from core.inbound_credentials import InboundCredentialUnavailable

    def _refuse(info, **kw):
        raise InboundCredentialUnavailable("delivery channel is not configured")

    report = _run(_require_receivable=_refuse)
    assert report["routes"] is False
    assert report["blocking_step"] == "datastream_receivable"
    by_step = {entry["step"]: entry for entry in report["steps"]}
    assert by_step["domain_ready"]["passed"] is None
    assert by_step["domain_ready"]["reason"] == "not_reached"
    assert by_step["credential_active"]["passed"] is None
    assert by_step["credential_resolves_back"]["passed"] is None


def test_the_report_has_a_fixed_shape_whatever_fails():
    """Un rapport dont la longueur varie avec la panne est un rapport qu un
    ecran doit deviner."""
    report = _run(_get_datastream_status=lambda conn, **kw: None)
    assert [entry["step"] for entry in report["steps"]] == list(ROUTING_STEPS)
    assert report["blocking_step"] == "datastream_exists"


def test_a_credential_marked_active_but_expired_is_refused():
    """L ETAT STOCKE N EST PAS L ETAT EFFECTIF.

    Une ligne encore marquee ACTIVE dont `expires_at` est passe est refusee a la
    livraison, et un operateur qui lit l ecran des cles voit << ACTIVE >> et en
    conclut le contraire. C est exactement le genre d ecart que ce test existe
    pour rendre visible avant qu un fournisseur n envoie.
    """
    report = _run(_effective_state=lambda state, **kw: "EXPIRED")
    assert report["routes"] is False
    assert report["blocking_step"] == "credential_active"
    assert report["blocking_reason"] == "credential_not_effective"


def test_a_credential_that_resolves_elsewhere_is_the_whole_point():
    """L aller-retour est ce que les cinq etapes precedentes ne prouvent pas.

    Tout peut tenir en amont pendant que la cle resout vers un Datastream depuis
    desactive -- et le refus a forme constante fait que le fournisseur n apprend
    rien non plus.
    """
    report = _run(
        resolve_by_token_hash=lambda conn, **kw: {
            "allowed": True,
            "scope": {"datastream_id": "ds_someone_else", "channel": "email"},
        }
    )
    assert report["routes"] is False
    assert report["blocking_step"] == "credential_resolves_back"
    assert report["blocking_reason"] == "resolves_elsewhere"


def test_a_denied_resolution_stays_opaque():
    """Le refus est deliberement opaque (38.7 AC4) et ne se relache pas pour un
    test. Ce qu on peut dire honnetement, c est qu il a ete refuse."""
    report = _run(
        resolve_by_token_hash=lambda conn, **kw: {"allowed": False, "scope": None}
    )
    assert report["blocking_reason"] == "delivery_would_be_denied"


def test_no_secret_leaves_this_surface():
    """La cle est decrite par son etat, sa version et son suffixe sur, jamais
    par son empreinte -- et encore moins par son jeton."""
    import json

    report = _run()
    rendered = json.dumps(report)
    assert "a" * 64 not in rendered
    assert "token_hash" not in rendered


def test_an_unsupported_channel_is_refused_before_any_read():
    with pytest.raises(RoutingTestValidationError):
        run_datastream_routing_test(_Conn(), datastream_id="ds_1", channel="carrier_pigeon")


def test_it_writes_nothing():
    """La preuve la plus forte de << without publishing analytical rows >>.

    Aucun des seams que ce module appelle n est un ecrivain. Ce test le tient
    par la liste : y ajouter un appel qui ecrit doit rougir ici.
    """
    import inspect

    from core import inbound_routing_test

    source = inspect.getsource(inbound_routing_test)
    for writer in ("INSERT", "UPDATE", "DELETE", "execute_operation", "record_receipt"):
        assert writer not in source, writer


def test_both_surfaces_call_the_same_command():
    """38.15 AC2 : la console et le MCP ne peuvent pas decrire le meme
    Datastream differemment s il n y a qu une implementation."""
    import inspect

    from core import inbound_health_api, inbound_mcp

    assert "run_datastream_routing_test" in inspect.getsource(inbound_mcp)
    assert "run_datastream_routing_test" in inspect.getsource(inbound_health_api)


def test_the_route_is_actually_mounted():
    """Une surface construite et non routee est le defaut exact que cet epic a
    rencontre dix fois."""
    from core.inbound_health_api import INBOUND_HEALTH_ROUTES

    paths = {route.path for route in INBOUND_HEALTH_ROUTES if hasattr(route, "path")}
    assert (
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/routing-test"
        in paths
    )
