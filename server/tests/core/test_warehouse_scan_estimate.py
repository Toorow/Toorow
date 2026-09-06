"""Ce qu'une requete VA scanner, demande avant de la lancer (story 66.10, AR8).

CE QUE CETTE MOITIE FERMAIT. AR8 exige que les estimations de cout/scan soient
calculees cote serveur AVANT l'execution. La moitie << limites de securite >>
etait livree -- plafond de lignes, budget en octets, preflight de pivotabilite.
La moitie ESTIMATION ne l'etait pas, et la story la nommait absente en disant
<< aucune API de cout n'est consultee dans cet epic >>. C'etait vrai a
l'ecriture et ne l'etait plus : 66.3 lit `total_bytes_billed` APRES coup. Ce qui
manquait est l'estimation AVANT, et BigQuery la donne gratuitement.

LES TROIS ETATS, ET AUCUN N'EST UN ZERO. Un `0` de DuckDB serait une MESURE
(<< cette requete ne scanne rien >>) alors que la verite est qu'une lecture de
fichier local ne facture pas d'octets. Un estimateur injoignable rend
`unavailable`, jamais 0 -- et surtout, il ne fait pas echouer une execution
parfaitement valide pour n'avoir pas su dire ce qu'elle allait couter.
"""

from __future__ import annotations

import pytest
from core import warehouse


def test_duckdb_is_not_applicable_never_a_scan_of_zero(monkeypatch):
    monkeypatch.setattr(warehouse, "_db_mode", lambda: "duckdb")
    estimate = warehouse.estimate_scan("SELECT 1", [])
    assert estimate.engine == "duckdb"
    # `0` se lirait comme << cette requete ne scanne rien >>, ce qui est une
    # affirmation. La verite est qu'un fichier local ne facture pas d'octets.
    assert estimate.scanned_bytes is None
    assert estimate.scanned_bytes_state == "not_applicable"
    assert estimate.unavailable_reason is None


def test_a_bigquery_dry_run_reports_what_it_would_scan(monkeypatch):
    class _Job:
        total_bytes_processed = 1_234_567

    class _Client:
        def __init__(self, project=None):
            self.project = project

        def query(self, sql, job_config=None):
            # LE POINT DU DRY RUN : il ne lance rien.
            assert job_config.dry_run is True
            # ...et il ignore le cache : avec lui, un rejeu rendrait 0 octet
            # parce que la MEME question a deja ete posee -- ce qui dit ce que
            # ce rejeu couterait, pas ce que la requete coute.
            assert job_config.use_query_cache is False
            return _Job()

    module = pytest.importorskip("google.cloud.bigquery")
    monkeypatch.setattr(module, "Client", _Client)
    monkeypatch.setattr(warehouse, "_db_mode", lambda: "bigquery")

    estimate = warehouse.estimate_scan("SELECT 1", ["a"])
    assert estimate.engine == "bigquery"
    assert estimate.scanned_bytes == 1_234_567
    assert estimate.scanned_bytes_state == "exact"


def test_a_dry_run_without_a_byte_count_is_unavailable_not_zero(monkeypatch):
    class _Job:
        total_bytes_processed = None

    class _Client:
        def __init__(self, project=None):
            pass

        def query(self, sql, job_config=None):
            return _Job()

    module = pytest.importorskip("google.cloud.bigquery")
    monkeypatch.setattr(module, "Client", _Client)
    monkeypatch.setattr(warehouse, "_db_mode", lambda: "bigquery")

    estimate = warehouse.estimate_scan("SELECT 1", [])
    assert estimate.scanned_bytes is None
    assert estimate.scanned_bytes_state == "unavailable"
    assert "no byte count" in (estimate.unavailable_reason or "")


def test_an_estimator_that_breaks_never_breaks_the_query(monkeypatch):
    def _boom(sql, params):
        raise RuntimeError("credentials expired")

    monkeypatch.setattr(warehouse, "_db_mode", lambda: "bigquery")
    monkeypatch.setattr(warehouse, "estimate_scan_bigquery", _boom)

    # Ne pas savoir ce qu'une requete coutera n'est pas une raison de refuser de
    # repondre : la fonction rend un etat, elle ne leve pas.
    estimate = warehouse.estimate_scan("SELECT 1", [])
    assert estimate.scanned_bytes_state == "unavailable"
    # La panne est NOMMEE : << indisponible >> sans cause envoie chercher au
    # mauvais endroit.
    assert "RuntimeError" in (estimate.unavailable_reason or "")
    assert "credentials expired" in (estimate.unavailable_reason or "")


def test_the_estimate_is_a_dict_with_its_state_never_a_bare_number(monkeypatch):
    monkeypatch.setattr(warehouse, "_db_mode", lambda: "duckdb")
    payload = warehouse.estimate_scan("SELECT 1", []).as_dict()
    assert set(payload) == {
        "engine",
        "scanned_bytes",
        "scanned_bytes_state",
        "unavailable_reason",
    }
