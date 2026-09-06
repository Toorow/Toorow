"""Tests du connecteur Google Business Profile (Story 30.1).

Ce fichier avait cesse d'etre execute: le dossier du module s'appelle
"google-business-profile" et, avec un tiret, ce n'est pas un identifiant Python,
donc le `from google_business_profile import connector` place apres un
sys.path.insert n'a jamais pu resoudre. L'ImportError faisait tomber la collecte
de toute la suite -- et pendant ce temps le connecteur a evolue sous des
assertions que plus personne ne voyait rougir (_insert_raw_location_daily_rows
renomme _insert_raw_rows, location_ids devenu location_id, pull_reviews
supprime, prefixe business_ ajoute aux impressions).

Le connecteur est desormais charge par chemin, comme le fait deja
test_pull_campaign_launch pour meta-ads, et les assertions sont reecrites sur
l'API reelle.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_CONNECTOR_PATH = REPO_ROOT / "server" / "modules" / "google-business-profile" / "connector.py"
_spec = importlib.util.spec_from_file_location("gbp_connector_under_test", _CONNECTOR_PATH)
connector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(connector)


def test_gbp_transform_canonical_field_mapping():
    """transform() renomme les enums DailyMetric via canonical_metric_mapping (AD-2)."""
    raw_rows = [
        {
            "date": "2026-07-01",
            "location_id": "locations/loc_100",
            "BUSINESS_IMPRESSIONS_DESKTOP_MAPS": 120,
            "CALL_CLICKS": 15,
        }
    ]

    transformed = connector.transform(raw_rows)
    assert len(transformed) == 1
    row = transformed[0]

    assert row["business_impressions_desktop_maps"] == 120
    assert row["call_clicks"] == 15
    # Les cles absentes du mapping passent inchangees.
    assert row["date"] == "2026-07-01"
    assert row["location_id"] == "locations/loc_100"


def test_gbp_insert_raw_rows_lands_one_wide_row_per_location_date(tmp_path, monkeypatch):
    """_insert_raw_rows ecrit UNE ligne large par (location, date), metriques en colonnes."""
    db_path = str(tmp_path / "test_gbp.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    rows = [
        {
            "date": "2026-07-01",
            "location_id": "locations/loc_100",
            "call_clicks": "15",
            "website_clicks": 7,
        }
    ]

    count = connector._insert_raw_rows(rows, "pull_001", "default")
    assert count == 1

    import duckdb

    con = duckdb.connect(db_path)
    res = con.execute(
        "SELECT date, location_id, call_clicks, website_clicks, "
        "business_bookings, pull_id, project_id FROM raw_gbp_location_daily"
    ).fetchall()
    con.close()

    assert len(res) == 1
    # _to_int coerce la valeur stringifiee; une metrique absente reste NULL,
    # jamais 0 -- le gap-fill est une affaire de mart, pas de landing.
    assert res[0] == ("2026-07-01", "locations/loc_100", 15, 7, None, "pull_001", "default")


def test_gbp_pull_location_daily_pivots_series_into_wide_rows(tmp_path, monkeypatch):
    """pull_location_daily pivote les series par metrique en une ligne par date."""
    db_path = str(tmp_path / "test_gbp_pull.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    def mock_get(url, params=None, headers=None, timeout=None):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "multiDailyMetricTimeSeries": [
                    {
                        "dailyMetricTimeSeries": [
                            {
                                "dailyMetric": "CALL_CLICKS",
                                "timeSeries": {
                                    "datedValues": [
                                        {"date": {"year": 2026, "month": 7, "day": 1},
                                         "value": "25"},
                                        {"date": {"year": 2026, "month": 7, "day": 2},
                                         "value": "31"},
                                    ]
                                },
                            },
                            {
                                "dailyMetric": "WEBSITE_CLICKS",
                                "timeSeries": {
                                    "datedValues": [
                                        {"date": {"year": 2026, "month": 7, "day": 1},
                                         "value": "4"},
                                    ]
                                },
                            },
                        ]
                    }
                ]
            },
        )

    with (
        patch("core.nango_client.get_fresh_token", return_value="fake_token"),
        patch("httpx.get", side_effect=mock_get),
    ):
        res = connector.pull_location_daily(
            connection_id="conn_gbp",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="default",
            pull_id="pull_gbp_001",
            location_id="loc_100",
        )

    assert res["pull_id"] == "pull_gbp_001"
    # Deux dates distinctes dans les series => deux lignes larges, pas quatre.
    assert res["row_count"] == 2

    import duckdb

    con = duckdb.connect(db_path)
    res_rows = con.execute(
        "SELECT date, location_id, call_clicks, website_clicks "
        "FROM raw_gbp_location_daily ORDER BY date"
    ).fetchall()
    con.close()

    # Le jour 2 n'a pas de website_clicks: il reste NULL au landing.
    assert res_rows == [
        ("2026-07-01", "locations/loc_100", 25, 4),
        ("2026-07-02", "locations/loc_100", 31, None),
    ]


def test_gbp_pull_without_location_refuses_instead_of_falling_back():
    """Sans location selectionnee, le pull refuse -- plus de repli par env-var (25.5+)."""
    with patch("core.nango_client.get_fresh_token", return_value="fake_token"):
        with pytest.raises(ValueError, match="location_id"):
            connector.pull_location_daily(
                connection_id="conn_gbp",
                date_from="2026-07-01",
                date_to="2026-07-02",
                project_id="default",
                pull_id="pull_gbp_002",
                location_id=None,
            )


def test_gbp_discover_accounts_topology():
    """discover_accounts enumere la hierarchie compte -> etablissement."""

    def mock_get(url, params=None, headers=None, timeout=None):
        request = httpx.Request("GET", url)
        if "accounts/acc_1/locations" in url:
            return httpx.Response(
                200,
                request=request,
                json={
                    "locations": [
                        {"name": "locations/loc_101", "title": "Store Downtown"}
                    ]
                },
            )
        if url.endswith("/accounts"):
            return httpx.Response(
                200,
                request=request,
                json={"accounts": [{"name": "accounts/acc_1", "accountName": "Bistro Group"}]},
            )
        return httpx.Response(404, request=request)

    with (
        patch("core.nango_client.get_fresh_token", return_value="fake_token"),
        patch("httpx.Client.get", side_effect=mock_get),
    ):
        topology = connector.discover_accounts("conn_gbp")

    assert topology == [
        {"id": "locations/loc_101", "label": "Store Downtown", "parent": "accounts/acc_1"}
    ]


def test_gbp_metric_columns_still_match_the_manifest_mapping():
    """Le residu nomme par la story 30.1 le 2026-07-31, desormais surveille.

    `_METRIC_COLUMNS` est une liste ecrite a la main sous une docstring qui
    annonce << AD-2: no hardcoded field list >>. Elle correspondait 11 pour 11 au
    mapping du manifeste -- et RIEN n'aurait rougi si elle avait derive : le
    connecteur aurait demande onze enums a Google et ecrit une autre serie de
    colonnes, en silence, avec des NULL partout.

    Ce test est cette alarme. Il ne supprime pas la liste (les 39 connecteurs
    ecrivent leur DDL de la meme facon) ; il rend sa derive impossible a rater.
    """
    manifest = connector._load_manifest()
    mapping = manifest["canonical_metric_mapping"]

    # Meme contenu ET meme ORDRE : l'INSERT est positionnel.
    assert connector._METRIC_COLUMNS == list(mapping.values())
    # Les enums demandes a l'API sont exactement les cles de ce mapping.
    assert connector._metric_source_tokens() == list(mapping.keys())

    # Et le profil location_daily du manifeste declare ces memes 11 metriques.
    daily = next(
        report
        for report in manifest["source_capabilities"]["reports"]
        if report["id"] == "location_daily"
    )
    assert daily["metrics"] == connector._METRIC_COLUMNS


def test_gbp_seed_loader_metric_columns_do_not_drift_from_the_connector():
    """Le seed ecrit un INSERT positionnel avec sa PROPRE copie de la liste.

    Deux copies de la meme sequence, dans deux fichiers, sans rien entre elles :
    si l'une bouge, le seed charge des valeurs dans les mauvaises colonnes et le
    mart est faux sans qu'une seule requete echoue.
    """
    import importlib.util as _il

    seed_path = (
        REPO_ROOT / "server" / "modules" / "google-business-profile"
        / "seeds" / "load_google_business_profile_seed.py"
    )
    spec = _il.spec_from_file_location("gbp_seed_under_test", seed_path)
    seed = _il.module_from_spec(spec)
    spec.loader.exec_module(seed)

    assert list(seed._METRIC_COLUMNS) == connector._METRIC_COLUMNS


# ---------------------------------------------------------------------------
# The 0-QPM gate on the DEFAULT profile (story 30.1 DoD :218, 2026-08-25).
# ---------------------------------------------------------------------------
#
# Every Business Profile project starts at 0 QPM and stays there until Google
# approves an access request BY HAND, so a 403 on the very first collection is
# the NORMAL first day of this connector, not an incident. The optional profiles
# return a prevented envelope; `pull_location_daily` cannot -- reporting an empty
# day as a success would fabricate a day -- so it raises, and the refusal is
# carried on the raised error for `core.pull_envelope.prevented_by_error` to
# read. Measured 2026-08-24, that reader did not exist and the two attributes had
# no consumer outside this module: the window was written
# `failed / permission_denied / reconnect`.


def _forbidden_response(url):
    return httpx.Response(
        403,
        request=httpx.Request("GET", url),
        json={
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": "Business Profile API has not been used in project ...",
            }
        },
    )


def test_gbp_default_pull_refused_at_zero_qpm_raises_a_readable_precondition():
    """Le chemin PRINCIPAL, pas un profil optionnel : `pull()` par defaut.

    L'assertion ne porte pas sur l'attribut brut mais sur ce que le LECTEUR DU
    CORE en tire -- reason ET phrase -- parce que c'est ce couple qui atteint
    l'ecran. Retirer `error.precondition` du connecteur fait rougir ici ; retirer
    le lecteur du core fait rougir dans `test_queue_prevented_pull.py`.
    """
    from core.pull_envelope import prevented_by_error

    def mock_get(url, params=None, headers=None, timeout=None):
        return _forbidden_response(url)

    with (
        patch("core.nango_client.get_fresh_token", return_value="fake_token"),
        patch("httpx.get", side_effect=mock_get),
    ):
        with pytest.raises(Exception) as caught:  # noqa: PT011 -- classe typee du core
            connector.pull(
                connection_id="conn_gbp",
                date_from="2026-07-01",
                date_to="2026-07-02",
                project_id="default",
                pull_id="pull_gbp_qpm",
                location_id="loc_100",
            )

    # La classe HTTP ne bouge pas : c'est bien un 403, et le core la connait.
    assert caught.value.error_class == "permission_denied"

    read = prevented_by_error(caught.value, module_name="google-business-profile")
    assert read is not None, "le 403 0-QPM ne porte aucune precondition lisible"
    assert read.reason == "google_access_pending"
    # La phrase nomme le GESTE qui debloque, jamais le code HTTP -- et elle dit
    # que rien n'est mal configure, parce que rien ne l'est.
    assert "Request Business Profile API quota" in read.message
    assert "403" not in read.message


def test_gbp_a_403_outside_a_gated_surface_stays_an_ordinary_refusal():
    """La precondition est une PORTE NOMMEE, pas une amnistie sur tous les 403.

    Sans cette borne, le lecteur du core transformerait n'importe quel refus
    d'acces reel en fenetre `prevented` -- une erreur reelle rendue muette.
    """
    from core.pull_envelope import prevented_by_error

    with pytest.raises(Exception) as caught:  # noqa: PT011
        connector._raise_for_status(
            _forbidden_response("https://example.com/v1/x"), surface="not_a_gate"
        )

    assert caught.value.error_class == "permission_denied"
    assert prevented_by_error(caught.value, module_name="google-business-profile") is None
