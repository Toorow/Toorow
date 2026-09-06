"""`e2e/entry_invitation.py` -- la consommation non interactive d'une invitation.

Pourquoi ces tests existent, et pourquoi ils sont HORS LIGNE
-----------------------------------------------------------
Le module a ete ecrit puis committe sans avoir jamais tourne contre un serveur :
la preuve locale exigeait un cluster jetable, et le montage est bloque depuis le
2026-08-04 par une migration non suivie d'une session voisine (209, absente du
manifeste -- le garde de derive est inconditionnel et bloque TOUTES les
sessions). Plutot que d'attendre, on prouve le contrat sans reseau, ce que la
maison demande de toute facon avant de reclamer un compte reel.

Ce qui est epingle ici n'est pas << la chaine repond 200 >>, mais les quatre
facons dont ce module serait FAUX en repondant 200 :

  1. il oublie de relayer le cookie d'echange -- pose `secure`, donc jamais
     stocke par un client qui parle `http://localhost`, et l'acceptation
     repondrait 404 sans jamais dire pourquoi ;
  2. il envoie un payload a la confirmation et un AUTRE a la creation -- la
     confirmation certifierait alors une valeur que personne n'a vue
     (exactement AI-77/AI-81) ;
  3. il lit le bearer ailleurs que dans le fragment ;
  4. il rend un echec muet la ou le serveur est volontairement muet -- un 404
     d'echange couvre trois causes distinctes, et ne pas les nommer laisse
     l'operateur sans prise.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# La suite tourne avec `server/` sur le chemin, pas la racine du depot -- et le
# module teste vit dans `e2e/`. On ajoute la racine ici plutot que d'installer
# `e2e` comme paquet : le harnais QA n'est pas une dependance du serveur, et le
# rendre importable partout dirait le contraire.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from e2e.entry_invitation import (  # noqa: E402
    ENV_INVITATION_URL,
    bearer_from_url,
    claim_entry_scope,
    invitation_url,
)

EXPECTED_BEARER = "brr_test_only_not_a_real_secret"
COOKIE = "toorow_invitation_exchange"


class _Recorder:
    """Ce que le serveur factice a REELLEMENT recu."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self.exchange_status = 200

    def payloads(self, path: str) -> list[dict]:
        return [body for p, body, _ in self.calls if p == path]

    def headers(self, path: str) -> dict:
        for p, _, hdrs in self.calls:
            if p == path:
                return hdrs
        return {}


def _make_handler(rec: _Recorder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # silence
            return

        def _send(self, status: int, body: dict, extra: list[tuple[str, str]] = ()):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in extra:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            rec.calls.append((self.path, body, dict(self.headers)))

            if self.path == "/api/invitations/exchange":
                if rec.exchange_status != 200:
                    self._send(rec.exchange_status, {"code": "not_found"})
                    return
                if body.get("bearer") != EXPECTED_BEARER:
                    self._send(404, {"code": "not_found", "message": "bearer mismatch"})
                    return
                self._send(
                    200,
                    {"ready_to_accept": True},
                    [("Set-Cookie", f"{COOKIE}=sess_value; Path=/api/invitations; Secure")],
                )
            elif self.path == "/api/invitations/accept":
                # L'acceptation EXIGE le cookie : sans lui, 404 -- comme le vrai.
                if f"{COOKIE}=sess_value" not in (self.headers.get("Cookie") or ""):
                    self._send(404, {"code": "not_found", "message": "no exchange cookie"})
                    return
                self._send(200, {"accepted": True})
            elif self.path == "/api/entry/scope/confirmation":
                self._send(200, {"confirmation_id": "cnf_1", "confirmation_secret": "sec_1"})
            elif self.path == "/api/entry/scope":
                if self.headers.get("X-Confirmation-Id") != "cnf_1":
                    self._send(422, {"code": "missing_confirmation"})
                    return
                self._send(201, {"organization_id": "org_TEST", "project_id": "proj_TEST"})
            else:
                self._send(404, {"code": "not_found"})

    return Handler


@pytest.fixture
def stub():
    rec = _Recorder()
    server = HTTPServer(("127.0.0.1", 0), _make_handler(rec))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", rec
    server.shutdown()
    server.server_close()


def test_bearer_is_read_from_the_fragment_in_both_shapes():
    """Le bearer vit apres le `#`, jamais dans la query -- c'est ce qui l'empeche
    d'atterrir dans les journaux du serveur."""
    assert bearer_from_url(f"https://x/invite#{EXPECTED_BEARER}") == EXPECTED_BEARER
    assert bearer_from_url(f"https://x/invite#token={EXPECTED_BEARER}") == EXPECTED_BEARER
    assert bearer_from_url("https://x/invite") is None
    # Une query n'est PAS un fragment : l'accepter donnerait un faux positif sur
    # une URL qui n'aurait jamais du fonctionner.
    assert bearer_from_url(f"https://x/invite?token={EXPECTED_BEARER}") is None


def test_missing_invitation_names_the_variable_to_set(monkeypatch):
    monkeypatch.delenv(ENV_INVITATION_URL, raising=False)
    result = claim_entry_scope("http://unused", "tok", label="t")
    assert not result.ok
    assert ENV_INVITATION_URL in result.detail


def test_full_chain_creates_the_first_organization(stub, monkeypatch):
    base, rec = stub
    monkeypatch.setenv(ENV_INVITATION_URL, f"{base}/invite#{EXPECTED_BEARER}")
    assert invitation_url().endswith(EXPECTED_BEARER)

    result = claim_entry_scope(base, "tok", label="stamp")

    assert result.ok, result.detail
    assert result.org_id == "org_TEST"
    # Les quatre appels, dans l'ordre du parcours de la console.
    assert [p for p, _, _ in rec.calls] == [
        "/api/invitations/exchange",
        "/api/invitations/accept",
        "/api/entry/scope/confirmation",
        "/api/entry/scope",
    ]


def test_the_exchange_cookie_is_relayed_to_the_acceptance(stub, monkeypatch):
    """Le cookie est pose `secure` ; un client en http ne le stockerait pas.

    Le serveur factice refuse l'acceptation sans lui, exactement comme le vrai --
    donc un module qui compterait sur un jar automatique echouerait ici.
    """
    base, rec = stub
    monkeypatch.setenv(ENV_INVITATION_URL, f"{base}/invite#{EXPECTED_BEARER}")
    assert claim_entry_scope(base, "tok", label="stamp").ok
    assert f"{COOKIE}=sess_value" in rec.headers("/api/invitations/accept").get("Cookie", "")


def test_confirmation_and_creation_certify_THE_SAME_payload(stub, monkeypatch):
    """AI-77/AI-81 : la confirmation certifie ce qui sera ecrit.

    Deux payloads differents feraient certifier a l'humain une valeur qu'il n'a
    jamais vue -- le defaut exact que le champ `currency` avait deja produit.
    """
    base, rec = stub
    monkeypatch.setenv(ENV_INVITATION_URL, f"{base}/invite#{EXPECTED_BEARER}")
    assert claim_entry_scope(base, "tok", label="stamp").ok
    confirmed = rec.payloads("/api/entry/scope/confirmation")[0]
    created = rec.payloads("/api/entry/scope")[0]
    assert confirmed == created
    # Et il ne fabrique aucune devise ni aucun fuseau que personne n'a choisi.
    assert "currency" not in confirmed or confirmed["currency"] is None


def test_a_muted_404_is_reported_with_its_three_possible_causes(stub, monkeypatch):
    """Le serveur est VOLONTAIREMENT muet ici. Le harnais ne doit pas l'etre."""
    base, rec = stub
    rec.exchange_status = 404
    monkeypatch.setenv(ENV_INVITATION_URL, f"{base}/invite#{EXPECTED_BEARER}")
    result = claim_entry_scope(base, "tok", label="stamp")
    assert not result.ok
    joined = " ".join(result.problems).lower()
    assert "expiree" in joined or "expir" in joined
    assert "identite" in joined  # l'invitation est liee a l'identite du jeton
