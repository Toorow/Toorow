"""AI-122 -- une seule porte pour brancher un adaptateur de decouverte.

Il y en avait deux. `core.datastream_setup_observations` portait un registre
public -- `register_setup_adapter` / `_ADAPTERS` / `observe_with_registered_adapter`
-- que personne n'alimentait, pendant que `datastream_preconfiguration_api`
construisait ses adaptateurs en closure et les passait a la main. Le registre
etait la porte VISIBLE et la porte MORTE : le prochain qui ajoutait un
adaptateur l'aurait enregistre, et il n'aurait jamais ete appele.

Le registre ne pouvait pas gagner l'arbitrage : ces closures capturent la
connexion de la requete et ses `path_params`, donc les poser dans un dict global
de processus ferait servir a la requete suivante -- celle d'un autre projet --
une closure liee au `project_id` du precedent. C'est la classe AI-125.

Ce fichier tient ce qui reste : l'injection est le seul chemin, et chacune des
six paires (mode, discovery_kind) est soit branchee, soit **nommee** comme non
couverte. Une septieme paire, ou une paire qu'on branche sans le declarer, rend
ce fichier rouge -- c'est tout l'objet.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.datastream_setup_observations import (
    DISCOVERY_KINDS,
    MODES,
    UNCOVERED_ADAPTERS,
    WIRED_ADAPTERS,
    uncovered_adapter_evidence,
)

_CORE = Path(__file__).resolve().parents[2] / "core"

_ALL_PAIRS = frozenset(
    (mode, kind) for mode in MODES for kind in DISCOVERY_KINDS[mode]
)


def test_every_pair_is_either_wired_or_named_uncovered():
    """Aucune paire ne tombe dans un trou muet."""
    declared = set(WIRED_ADAPTERS) | set(UNCOVERED_ADAPTERS)
    assert declared == set(_ALL_PAIRS), (
        "paires non declarees : "
        f"{sorted(set(_ALL_PAIRS) - declared)} ; declarees mais inexistantes : "
        f"{sorted(declared - set(_ALL_PAIRS))}"
    )
    assert not (set(WIRED_ADAPTERS) & set(UNCOVERED_ADAPTERS)), (
        "une paire ne peut pas etre a la fois branchee et non couverte"
    )


def test_the_measured_state_of_2026_08_05_after_57_3():
    """PLUS AUCUNE paire sur six n'est sans adaptateur.

    Le compte a bouge trois fois en une journee : deux paires non couvertes
    (57.1), puis une (57.2 a fait demenager `sheet_schema`), puis zero -- 57.3
    branche `channel_contract`, la derniere, et la seule qui n'avait aucune
    fonction d'observation du tout.

    AI-122 en annoncait quatre en comptant `connector_pull`. C'est faux :
    `create_observation` porte une branche explicite pour ce mode, qui couvre
    ses DEUX discovery_kinds. Le compte est repris ici pour que la prochaine
    session lise l'etat mesure et non l'estimation.
    """
    assert UNCOVERED_ADAPTERS == {}
    assert ("connector_pull", "connector_contract") in WIRED_ADAPTERS
    assert ("connector_pull", "connector_fields") in WIRED_ADAPTERS
    assert ("external_bq", "warehouse_schema") in WIRED_ADAPTERS
    assert ("managed_feed", "sheet_schema") in WIRED_ADAPTERS
    assert ("managed_feed", "channel_contract") in WIRED_ADAPTERS


def test_no_second_door_survives():
    """Le registre mort ne revient pas, sous aucun de ses trois noms."""
    source = (_CORE / "datastream_setup_observations.py").read_text(encoding="utf-8")
    executable = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    for symbol in ("register_setup_adapter", "observe_with_registered_adapter", "_ADAPTERS"):
        # `\b` sur le nom NU : `_ADAPTERS` est un suffixe de `WIRED_ADAPTERS` et de
        # `UNCOVERED_ADAPTERS`, qui sont la reparation, pas la rechute.
        assert not re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}\b", executable), (
            f"`{symbol}` est revenu : deux mecanismes pour brancher un adaptateur, "
            "c'est le piege qu'AI-122 a ferme."
        )


def test_connector_pull_really_is_wired_where_the_table_claims():
    """La table dit `create_observation` -- verifie plutot que cru."""
    source = (_CORE / "datastream_setup_observations.py").read_text(encoding="utf-8")
    start = source.index("def create_observation(")
    body = source[start:]
    assert 'normalized_request["mode"] == "connector_pull"' in body
    assert "observe_connector_contract" in body
    assert "uncovered_adapter_evidence(" in body, (
        "la branche de repli ne passe plus par l'evidence nommee"
    )


def test_the_injected_closures_are_built_for_the_pair_the_table_claims():
    """La table dit `managed_feed` + `file_schema` cote API -- verifie aussi."""
    source = (_CORE / "datastream_preconfiguration_api.py").read_text(encoding="utf-8")
    assert source.count('body.get("discovery_kind") == "file_schema"') == 2, (
        "les deux closures ne sont plus construites pour la seule paire file_schema ; "
        "WIRED_ADAPTERS ment"
    )
    assert 'body.get("channel") in ("inbound_email", "webhook")' in source, (
        "les deux branches de file_schema ne se distinguent plus par le canal"
    )


def test_the_external_bigquery_closure_is_really_built():
    """`WIRED_ADAPTERS` dit `external_bq` -- lu dans la source, pas cru.

    C'est la moitie que la declaration ne prouve pas. Le seul test qui traverse
    la route (`test_datastream_setup_observations_api.py`) patche entierement
    `create_observation`, donc il ne touche aucune closure : la preuve du
    branchement est ici, dans le texte du fichier qui la construit.
    """
    source = (_CORE / "datastream_preconfiguration_api.py").read_text(encoding="utf-8")
    assert 'body.get("mode") == "external_bq"' in source, (
        "aucune branche external_bq cote API ; WIRED_ADAPTERS ment"
    )
    assert 'body.get("discovery_kind") == "warehouse_schema"' in source, (
        "la branche external_bq ne se construit plus pour warehouse_schema"
    )
    assert "observe_external_bigquery" in source, (
        "la closure external_bq n'appelle plus sa fonction d'observation"
    )
    # Le client type est INSTANCIE DANS LA REQUETE. Un client au niveau module
    # survivrait a la requete, ce qui est la classe AI-125 refermee par AI-122.
    assert "def _bigquery_setup_reader(connection_id: str | None = None)" in source
    assert "reader = _bigquery_setup_reader(connection_id)" in source
    # ET IL RECOIT L'AUTORISATION DE LA PERSONNE (AI-285). BigQuery est une
    # source Google : lire l'entrepot d'un client avec les identifiants du
    # deploiement, c'est repondre a « que puis-je voir » avec le compte de
    # service au lieu du consentement.
    assert "_source_account_connection_id(" in source
    assert 'source_label="External BigQuery"' in source


def test_the_google_sheets_closure_is_really_built():
    """`WIRED_ADAPTERS` dit `sheet_schema` -- lu dans la source, pas cru.

    Meme moitie manquante que pour `external_bq` : la declaration ne prouve pas
    le branchement, et le seul test qui traverse la route patche entierement
    `create_observation`. La preuve est ici, dans le texte du fichier qui
    construit la closure.
    """
    source = (_CORE / "datastream_preconfiguration_api.py").read_text(encoding="utf-8")
    assert 'body.get("discovery_kind") == "sheet_schema"' in source, (
        "aucune branche sheet_schema cote API ; WIRED_ADAPTERS ment"
    )
    assert "observe_google_sheet" in source, (
        "la closure sheet_schema n'appelle plus sa fonction d'observation"
    )
    # Client INSTANCIE DANS LA REQUETE, sur la connexion de CE Source Account.
    # Un client au niveau module survivrait a la requete et servirait le projet
    # suivant avec l'autorisation du precedent -- la classe AI-125.
    assert "def _sheets_setup_reader(connection_id: str):" in source
    assert "reader = _sheets_setup_reader(connection_id)" in source


def test_the_inbound_channel_closure_is_really_built():
    """`WIRED_ADAPTERS` dit `channel_contract` -- lu dans la source, pas cru.

    Meme moitie manquante que pour les deux paires precedentes, et elle mord
    davantage ici : cette paire etait declaree « aucune fonction d'observation »
    la veille. Une declaration sans branchement rendrait `webhook` offert dans
    la liste des canaux et mort a l'etape suivante.
    """
    source = (_CORE / "datastream_preconfiguration_api.py").read_text(encoding="utf-8")
    assert 'body.get("discovery_kind") == "channel_contract"' in source, (
        "aucune branche channel_contract cote API ; WIRED_ADAPTERS ment"
    )
    assert "observe_channel_contract" in source, (
        "la closure channel_contract n'appelle plus sa fonction d'observation"
    )
    # L'etat est LU DANS LA REQUETE, sur la connexion de CE projet. Un etat de
    # canal au niveau module servirait le projet suivant -- classe AI-125.
    assert "read_channel_contract(" in source
    assert "managed_feed.channel_contract.v1" in source, (
        "le refus 503 ne nomme plus l'adaptateur qui n'a pas pu lire"
    )


def test_the_preview_refuses_a_scan_with_no_estimate():
    """« Estimer avant de scanner » se tient dans `_create_preview`, ou nulle part.

    La lecture qu'un operateur peut lancer est le preview de l'etape 5. Sans
    cette garde, une observation sans estimation passait a la lecture et le cout
    n'etait connu qu'apres la facture.
    """
    source = (_CORE / "datastream_preconfiguration_api.py").read_text(encoding="utf-8")
    start = source.index("async def _create_preview(")
    body = source[start : source.index("async def ", start + 10)]
    assert 'operator_input.get("mode") == "external_bq"' in body
    assert '.get("quota_cost")' in body
    assert "No scan estimate is attached to this observation" in body
    # Apres les pins : le refus porte sur l'observation reellement epinglee.
    assert body.index("_preview_pins(") < body.index("No scan estimate is attached")


def test_uncovered_evidence_says_which_pair():
    """`adapter_unavailable`, mais avec le nom de la paire dans `adapter_ref`.

    Sans cela, « personne n'a encore branche external_bq » et « l'adaptateur a
    casse » rendaient la meme chose.

    SUR UNE PAIRE FICTIVE, ET C'EST DELIBERE. Ce test etait
    `@pytest.mark.parametrize("pair", sorted(UNCOVERED_ADAPTERS))` : depuis que
    57.3 a vide la table, pytest rend un jeu de parametres vide et marque le cas
    `skipped` au lieu de le faire disparaitre -- un test qui ne s'execute plus,
    silencieusement. Or `uncovered_adapter_evidence` reste le repli de
    `create_observation`, donc elle doit rester prouvee meme quand plus aucune
    paire declaree ne l'atteint.
    """
    evidence = uncovered_adapter_evidence("mode", "kind")

    assert evidence["adapter_ref"] == "mode.kind.unavailable"
    assert evidence["coverage"] == {"discovery": "unavailable"}
    assert [item["code"] for item in evidence["exceptions"]] == ["adapter_unavailable"]
    assert evidence["evidence_fingerprint"]
