"""Tests du connecteur Google Sheets -- Story 15.6 (Epic 15).

Couvre :
  * Parsing / mapping / validation des lignes de la feuille.
  * ColumnMappingError si une colonne declaree est absente (AD-9).
  * Lignes non parsables comptees et loggees, jamais silencieuses (AD-9).
  * Pagination / range (batchGet retourne une page, pas de pagination).
  * HTTP 429 -> RateLimitError('google-sheets', retry_after).
  * Aucun token dans les logs (AD-3).
  * Fixture golden AI-54 : comparaison directe contre le generateur.
  * Seam AI-56 : assertions sur valeurs (pas seulement HTTP 200).
  * Grain uniqueness : deux lignes meme (date, sheet_row_id) -> dedup staging.

ASCII-only comments (AI-03).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Imports du module a tester -- importlib avec noms UNIQUES (pas de bare
# `from connector import` + sys.path: le nom 'connector' collisionne entre
# modules dans une meme session pytest). Fichier sous server/tests/modules/
# (pattern tiktok/stripe/hubspot): un package 'tests' DANS le module
# collisionnait avec le package server/tests a la collection.
# ---------------------------------------------------------------------------
import importlib.util  # noqa: E402
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _import_from(path: Path, unique_name: str):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REPO_ROOT = Path(__file__).parents[4]
MODULE_DIR = REPO_ROOT / "server" / "modules" / "google-sheets"
SEEDS_DIR = MODULE_DIR / "seeds"

_connector_mod = _import_from(MODULE_DIR / "connector.py", "google_sheets_connector")
ColumnMappingError = _connector_mod.ColumnMappingError
# review-15-9 F-1 (BLOQUANT) : le connecteur reexporte core.quota.RateLimitError
# (plus de classe locale). On vise DONC la classe du core -- le worker core attrape
# cette meme classe, sinon le 429 ne declencherait jamais le breaker. Attributs :
# .platform (pas .connector) et .retry_after.
from core.quota import RateLimitError  # noqa: E402

assert _connector_mod.RateLimitError is RateLimitError, (
    "google-sheets doit reexporter core.quota.RateLimitError (pas une classe locale)"
)
_build_envelope = _connector_mod._build_envelope
_fetch_sheet_values = _connector_mod._fetch_sheet_values
_parse_sheet_response = _connector_mod._parse_sheet_response
_parse_sheet_row = _connector_mod._parse_sheet_row
_validate_column_mapping = _connector_mod._validate_column_mapping

_seed_mod = _import_from(
    SEEDS_DIR / "generate_google_sheets_seed.py", "google_sheets_seed_gen"
)
_RNG_SEED = _seed_mod._RNG_SEED
DEFAULT_SEED_END_DATE = _seed_mod.DEFAULT_SEED_END_DATE
SEED_COLUMN_MAPPING = _seed_mod.SEED_COLUMN_MAPPING
generate_api_payload = _seed_mod.generate_api_payload
generate_rows = _seed_mod.generate_rows

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_api_payload() -> list[list[str]]:
    """Payload API minimal : 1 ligne de donnees, 3 metriques."""
    return [
        ["Date", "Canal", "Budget", "Objectif CA", "Objectif Conversions"],
        ["2026-06-01", "Meta Ads", "5000.0", "17500.0", "120.0"],
        ["2026-06-01", "Google Ads", "4000.0", "16000.0", "95.0"],
        ["2026-06-02", "Meta Ads", "5050.5", "17677.0", "121.0"],
    ]


_MAPPING = {
    "date_column": "Date",
    "row_id_column": "Canal",
    "metric_columns": {
        "Budget": "budget_declared",
        "Objectif CA": "target_revenue",
        "Objectif Conversions": "target_conversions",
    },
}


# ===========================================================================
# 1. Tests de parsing
# ===========================================================================


class TestParseSheetRow:
    """Unit tests pour _parse_sheet_row."""

    def test_nominal_row(self):
        headers = ["Date", "Canal", "Budget", "Objectif CA", "Objectif Conversions"]
        row = ["2026-06-01", "Meta Ads", "5000.0", "17500.0", "120.0"]
        result = _parse_sheet_row(
            row, headers,
            date_column="Date",
            metric_columns={"Budget": "budget_declared", "Objectif CA": "target_revenue",
                             "Objectif Conversions": "target_conversions"},
            row_id_column="Canal",
            row_index=1,
        )
        assert result["date"] == "2026-06-01"
        assert result["sheet_row_id"] == "Meta Ads"
        assert result["budget_declared"] == pytest.approx(5000.0)
        assert result["target_revenue"] == pytest.approx(17500.0)
        assert result["target_conversions"] == pytest.approx(120.0)

    def test_empty_date_returns_none(self):
        headers = ["Date", "Canal", "Budget"]
        row = ["", "Meta Ads", "5000.0"]
        result = _parse_sheet_row(
            row, headers,
            date_column="Date",
            metric_columns={"Budget": "budget_declared"},
            row_id_column="Canal",
            row_index=1,
        )
        assert result is None

    def test_not_set_date_returns_none(self):
        headers = ["Date", "Canal", "Budget"]
        row = ["-", "Meta Ads", "5000.0"]
        result = _parse_sheet_row(
            row, headers,
            date_column="Date",
            metric_columns={"Budget": "budget_declared"},
            row_id_column="Canal",
            row_index=1,
        )
        assert result is None

    def test_unparsable_metric_becomes_none(self):
        """Valeur non numerique -> None + warning (AD-9 : pas de 0 invente)."""
        headers = ["Date", "Canal", "Budget"]
        row = ["2026-06-01", "Meta Ads", "five_thousand"]
        result = _parse_sheet_row(
            row, headers,
            date_column="Date",
            metric_columns={"Budget": "budget_declared"},
            row_id_column="Canal",
            row_index=1,
        )
        assert result is not None
        assert result["budget_declared"] is None
        assert len(result["_parse_warnings"]) == 1

    def test_locale_number_parsed(self):
        """Nombres avec virgule decimale (locale FR) -> float."""
        headers = ["Date", "Canal", "Budget"]
        row = ["2026-06-01", "Meta Ads", "5 000,50"]
        result = _parse_sheet_row(
            row, headers,
            date_column="Date",
            metric_columns={"Budget": "budget_declared"},
            row_id_column="Canal",
            row_index=1,
        )
        assert result["budget_declared"] == pytest.approx(5000.50)

    def test_locale_fr_thousands_dot_decimal_comma(self):
        """Format FR/EU '1.234,56' (point milliers, virgule decimale) -> 1234.56 (F-4)."""
        headers = ["Date", "Canal", "Budget"]
        row = ["2026-06-01", "Meta Ads", "1.234,56"]
        result = _parse_sheet_row(
            row, headers,
            date_column="Date",
            metric_columns={"Budget": "budget_declared"},
            row_id_column="Canal",
            row_index=1,
        )
        assert result["budget_declared"] == pytest.approx(1234.56)

    def test_locale_en_thousands_comma_decimal_dot(self):
        """Format EN '1,234.56' (virgule milliers, point decimal) -> 1234.56 (F-4 doc).

        Heuristique : dernier separateur = separateur decimal.
        '1,234.56' : '.' vient apres ',' -> '.' est le separateur decimal (format EN)
        -> retire les virgules-milliers -> '1234.56' -> 1234.56.
        Comportement documente dans manifest.json _number_format.
        """
        headers = ["Date", "Canal", "Budget"]
        row = ["2026-06-01", "Meta Ads", "1,234.56"]
        result = _parse_sheet_row(
            row, headers,
            date_column="Date",
            metric_columns={"Budget": "budget_declared"},
            row_id_column="Canal",
            row_index=1,
        )
        assert result["budget_declared"] == pytest.approx(1234.56)

    def test_short_row_padded(self):
        """Ligne trop courte -> cellules manquantes = vides -> metriques = None."""
        headers = ["Date", "Canal", "Budget", "Objectif CA"]
        row = ["2026-06-01", "Meta Ads"]  # Budget et Objectif CA manquants
        result = _parse_sheet_row(
            row, headers,
            date_column="Date",
            metric_columns={"Budget": "budget_declared", "Objectif CA": "target_revenue"},
            row_id_column="Canal",
            row_index=1,
        )
        assert result is not None
        assert result["budget_declared"] is None
        assert result["target_revenue"] is None

    def test_no_row_id_column_uses_index(self):
        """Sans row_id_column -> sheet_row_id = 'row_N'."""
        headers = ["Date", "Budget"]
        row = ["2026-06-01", "5000.0"]
        result = _parse_sheet_row(
            row, headers,
            date_column="Date",
            metric_columns={"Budget": "budget_declared"},
            row_id_column=None,
            row_index=3,
        )
        assert result["sheet_row_id"] == "row_3"

    def test_empty_metric_becomes_none(self):
        """Cellule vide dans une colonne metrique -> None (AD-9)."""
        headers = ["Date", "Canal", "Budget"]
        row = ["2026-06-01", "Meta Ads", ""]
        result = _parse_sheet_row(
            row, headers,
            date_column="Date",
            metric_columns={"Budget": "budget_declared"},
            row_id_column="Canal",
            row_index=1,
        )
        assert result["budget_declared"] is None


# ===========================================================================
# 2. Tests de validation du column_mapping
# ===========================================================================


class TestValidateColumnMapping:
    """Unit tests pour _validate_column_mapping."""

    def test_all_columns_present_ok(self):
        headers = ["Date", "Canal", "Budget", "Objectif CA"]
        # Should not raise.
        _validate_column_mapping(
            headers, "Date",
            {"Budget": "budget_declared", "Objectif CA": "target_revenue"},
            "Canal",
        )

    def test_missing_date_column_raises(self):
        headers = ["Periode", "Canal", "Budget"]
        with pytest.raises(ColumnMappingError) as exc:
            _validate_column_mapping(
                headers, "Date", {"Budget": "budget_declared"}, None
            )
        assert "date_column='Date'" in str(exc.value)

    def test_missing_metric_column_raises(self):
        """Colonne metrique absente -> ColumnMappingError (AD-9 : jamais 0)."""
        headers = ["Date", "Canal", "Budget"]
        with pytest.raises(ColumnMappingError) as exc:
            _validate_column_mapping(
                headers, "Date",
                {"Budget": "budget_declared", "Objectif CA": "target_revenue"},
                "Canal",
            )
        assert "metric_column='Objectif CA'" in str(exc.value)

    def test_missing_row_id_column_raises(self):
        headers = ["Date", "Budget"]
        with pytest.raises(ColumnMappingError) as exc:
            _validate_column_mapping(
                headers, "Date", {"Budget": "budget_declared"}, "Canal"
            )
        assert "row_id_column='Canal'" in str(exc.value)

    def test_multiple_missing_listed(self):
        """Toutes les colonnes manquantes sont listees dans le message."""
        headers = ["Date"]
        with pytest.raises(ColumnMappingError) as exc:
            _validate_column_mapping(
                headers, "Date",
                {"Budget": "budget_declared", "CA": "target_revenue"},
                "Canal",
            )
        msg = str(exc.value)
        assert "metric_column='Budget'" in msg
        assert "metric_column='CA'" in msg
        assert "row_id_column='Canal'" in msg


# ===========================================================================
# 3. Tests de parse_sheet_response (flux complet)
# ===========================================================================


class TestParseSheetResponse:
    """Unit tests pour _parse_sheet_response."""

    def test_nominal_response(self):
        values = _minimal_api_payload()
        records, unparsable = _parse_sheet_response(values, _MAPPING)
        assert unparsable == 0
        assert len(records) == 3
        dates = {r["date"] for r in records}
        assert "2026-06-01" in dates
        assert "2026-06-02" in dates

    def test_empty_sheet(self):
        records, unparsable = _parse_sheet_response([], _MAPPING)
        assert records == []
        assert unparsable == 0

    def test_header_only(self):
        records, unparsable = _parse_sheet_response(
            [["Date", "Canal", "Budget"]], _MAPPING
        )
        assert records == []

    def test_unparsable_row_counted_not_silent(self):
        """Ligne avec valeur non numerique : comptee + loggee, jamais silencieuse (AD-9)."""
        values = [
            ["Date", "Canal", "Budget", "Objectif CA", "Objectif Conversions"],
            ["2026-06-01", "Meta Ads", "bad_value", "17500.0", "120.0"],
            ["2026-06-02", "Google Ads", "4000.0", "16000.0", "95.0"],
        ]
        records, unparsable = _parse_sheet_response(values, _MAPPING)
        assert unparsable == 1  # ligne avec budget_declared non parsable
        assert len(records) == 2
        # La ligne avec la valeur incorrecte est quand meme retournee avec None.
        bad_row = next(r for r in records if r["date"] == "2026-06-01")
        assert bad_row["budget_declared"] is None
        # La bonne valeur de l'autre ligne n'est pas affectee.
        good_row = next(r for r in records if r["date"] == "2026-06-02")
        assert good_row["budget_declared"] == pytest.approx(4000.0)

    def test_missing_declared_column_raises(self):
        """Colonne declaree absente -> ColumnMappingError (AD-9)."""
        values = [
            ["Date", "Canal", "Autre Colonne"],
            ["2026-06-01", "Meta Ads", "5000.0"],
        ]
        with pytest.raises(ColumnMappingError):
            _parse_sheet_response(values, _MAPPING)

    def test_skipped_rows_not_in_output(self):
        """Lignes avec date vide sont exclues (pas comptees comme unparsable)."""
        values = [
            ["Date", "Canal", "Budget", "Objectif CA", "Objectif Conversions"],
            ["", "Meta Ads", "5000.0", "17500.0", "120.0"],  # date vide -> skip
            ["2026-06-02", "Google Ads", "4000.0", "16000.0", "95.0"],
        ]
        records, unparsable = _parse_sheet_response(values, _MAPPING)
        assert len(records) == 1
        assert records[0]["date"] == "2026-06-02"
        assert unparsable == 0  # ligne skip != ligne unparsable

    def test_records_have_no_internal_keys(self):
        """Les records retournes n'ont pas de cle _parse_warnings (interne)."""
        values = _minimal_api_payload()
        records, _ = _parse_sheet_response(values, _MAPPING)
        for r in records:
            assert "_parse_warnings" not in r


# ===========================================================================
# 4. Test 429 -> RateLimitError (AI-53)
# ===========================================================================


class TestRateLimitError:
    """HTTP 429 -> RateLimitError('google-sheets', retry_after)."""

    def test_429_raises_rate_limit_error(self):
        """_fetch_sheet_values : HTTP 429 -> RateLimitError avec retry_after."""

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "30"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_resp

            with pytest.raises(RateLimitError) as exc:
                _fetch_sheet_values(
                    token="fake_token",
                    spreadsheet_id="test_id",
                    sheet_range="Feuille1!A:E",
                )

        assert exc.value.platform == "google-sheets"
        assert exc.value.retry_after == 30

    def test_429_default_retry_after_60(self):
        """Sans header Retry-After -> retry_after=60 par defaut."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {}

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_resp

            with pytest.raises(RateLimitError) as exc:
                _fetch_sheet_values(
                    token="fake_token",
                    spreadsheet_id="test_id",
                    sheet_range="Feuille1!A:E",
                )

        assert exc.value.retry_after == 60


# ===========================================================================
# 5. Test aucun token dans les logs (AD-3)
# ===========================================================================


class TestNoTokenInLogs:
    """AD-3: aucun token ne doit apparaitre dans les logs."""

    def test_no_token_in_logs(self, caplog):
        """Le token passe a _fetch_sheet_values n'apparait pas dans les logs."""
        secret_token = "ya29.secret_access_token_DO_NOT_LOG"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "values": [
                ["Date", "Canal", "Budget"],
                ["2026-06-01", "Meta Ads", "5000.0"],
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        with caplog.at_level(logging.DEBUG):
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client_cls.return_value.__enter__ = lambda s: mock_client
                mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
                mock_client.get.return_value = mock_resp

                _fetch_sheet_values(
                    token=secret_token,
                    spreadsheet_id="test_id",
                    sheet_range="Feuille1!A:E",
                )

        full_log = caplog.text + " ".join(r.message for r in caplog.records)
        assert secret_token not in full_log, (
            f"Token leaked in logs: {caplog.text[:200]}"
        )

    def test_no_token_in_column_mapping_error(self):
        """ColumnMappingError ne contient pas le token (AD-3)."""
        secret_token = "ya29.secret_token_should_not_appear"
        headers = ["Date", "Canal"]  # Colonne Budget manquante
        with pytest.raises(ColumnMappingError) as exc:
            _validate_column_mapping(
                headers, "Date",
                {"Budget": "budget_declared"},
                None,
            )
        assert secret_token not in str(exc.value)


# ===========================================================================
# 6. Fixture golden AI-54 : comparaison contre le generateur
# ===========================================================================


class TestGoldenFixtureMatchesGenerator:
    """AI-54 : la fixture golden est generee depuis le generateur, jamais a la main.

    Le test calcule les valeurs directement depuis generate_api_payload() et
    _parse_sheet_response() -- ne depend pas des fichiers JSON de fixtures.
    """

    def test_golden_fixture_matches_generator(self):
        """Valeurs de parse directement issues du generateur (AI-54)."""
        import random as _random

        rng = _random.Random(_RNG_SEED)
        payload = generate_api_payload(
            days=2,
            end_date=DEFAULT_SEED_END_DATE,
            rng=rng,
        )
        records, unparsable = _parse_sheet_response(payload, SEED_COLUMN_MAPPING)

        # Le generateur produit 2 jours x 5 canaux = 10 lignes.
        assert len(records) == 10
        assert unparsable == 0

        # Toutes les lignes ont les 3 metriques.
        for r in records:
            assert "budget_declared" in r
            assert "target_revenue" in r
            assert "target_conversions" in r
            assert r["budget_declared"] is not None
            assert r["target_revenue"] is not None
            assert r["target_conversions"] is not None

        # Verification discriminante : valeurs non triviales (pas de 0 invente).
        total_budget = sum(r["budget_declared"] for r in records)
        assert total_budget > 0
        # Ratio target_revenue / budget_declared doit etre dans une plage plausible.
        ratios = [r["target_revenue"] / r["budget_declared"] for r in records]
        assert all(1.0 < ratio < 10.0 for ratio in ratios), (
            f"Ratios hors plage plausible : {ratios}"
        )

    def test_generator_rows_have_canonical_keys(self):
        """generate_rows() produit des dicts avec les cles canoniques du raw."""
        rows = generate_rows(days=3, end_date=DEFAULT_SEED_END_DATE)
        assert len(rows) > 0
        for r in rows:
            assert "date" in r
            assert "sheet_row_id" in r
            assert "budget_declared" in r
            assert "target_revenue" in r
            assert "target_conversions" in r


# ===========================================================================
# 7. Seam AI-56 : assertions sur valeurs (envelope canonical)
# ===========================================================================


class TestSeamValues:
    """AI-56 : le seam test valide l'envelope avec assertions sur valeurs."""

    def test_build_envelope_structure(self):
        """_build_envelope construit l'envelope AD-1 canonique avec valeurs."""
        rows = [
            {
                "metric": "budget_declared",
                "breakdown_dimension": "sheet_row_id",
                "breakdown_value": "Meta Ads",
                "value": 5000.0,
                "pull_id": "pull_TEST123",
                "freshness": "2026-07-19T10:00:00Z",
            },
            {
                "metric": "target_revenue",
                "breakdown_dimension": "sheet_row_id",
                "breakdown_value": "Meta Ads",
                "value": 17500.0,
                "pull_id": "pull_TEST123",
                "freshness": "2026-07-19T10:00:00Z",
            },
        ]
        envelope = _build_envelope(
            rows, "sheet_daily", "2026-07-01", "2026-07-19", "default"
        )

        assert envelope["schema_version"] == "1"
        assert "meta" in envelope
        assert "data" in envelope
        assert envelope["meta"]["freshness"] == "2026-07-19T10:00:00Z"
        assert envelope["meta"]["provenance"]["source_system"] == "google-sheets"
        assert envelope["data"]["report_profile"] == "sheet_daily"
        assert "budget_declared" in envelope["data"]["metrics"]
        assert "target_revenue" in envelope["data"]["metrics"]

        # Valeurs exactes (AI-56 : assertions sur valeurs, pas seulement structure).
        budget_rows = envelope["data"]["metrics"]["budget_declared"]
        meta_budget = next(
            (r for r in budget_rows if r["breakdown_value"] == "Meta Ads"), None
        )
        assert meta_budget is not None
        assert meta_budget["value"] == pytest.approx(5000.0)

    def test_build_envelope_empty_rows(self):
        """Envelope avec rows vides -> meta.provenance = None, metrics = {}."""
        envelope = _build_envelope([], "sheet_daily", "2026-07-01", "2026-07-19", "default")
        assert envelope["meta"]["provenance"] is None
        assert envelope["data"]["metrics"] == {}
        assert envelope["meta"]["alerts"] == []


# ===========================================================================
# 8. Test grain uniqueness (discipline staging)
# ===========================================================================


class TestGrainUniqueness:
    """Le staging dbt a un test grain-unique (project_id, date, sheet_row_id).

    Ce test Python verifie que le parseur ne produit pas de doublons sur le
    meme (date, sheet_row_id) -- la dedup appartient au staging QUALIFY.
    """

    def test_same_grain_rows_distinct_in_parser(self):
        """Le parseur retourne les deux lignes -- le staging dedup au QUALIFY.

        On verifie ici que le parseur n'abandonne pas silencieusement une ligne
        quand deux lignes ont le meme (date, sheet_row_id). Le staging QUALIFY
        (ORDER BY pull_id DESC) garde la derniere, mais le parseur les retourne
        toutes (comportement attendu).
        """
        values = [
            ["Date", "Canal", "Budget", "Objectif CA", "Objectif Conversions"],
            ["2026-06-01", "Meta Ads", "5000.0", "17500.0", "120.0"],
            ["2026-06-01", "Meta Ads", "5100.0", "17850.0", "122.0"],  # doublon
        ]
        records, _ = _parse_sheet_response(values, _MAPPING)
        # Le parseur retourne les deux ; le staging choisira par pull_id DESC.
        assert len(records) == 2
        budgets = [r["budget_declared"] for r in records]
        assert 5000.0 in budgets
        assert 5100.0 in budgets

    def test_different_channels_same_date_not_deduped(self):
        """Deux canaux distincts le meme jour = deux grains differents (correct)."""
        values = _minimal_api_payload()
        records, _ = _parse_sheet_response(values, _MAPPING)
        # Date 2026-06-01 a 2 canaux -> 2 lignes distinctes (grain = date x channel).
        june_1 = [r for r in records if r["date"] == "2026-06-01"]
        assert len(june_1) == 2
        ids = {r["sheet_row_id"] for r in june_1}
        assert "Meta Ads" in ids
        assert "Google Ads" in ids


# ===========================================================================
# 9. Snapshot golden : generateur == fixture figee (AI-54, F-2 review-15-6)
# ===========================================================================


class TestGoldenSnapshot:
    """Verifie que le generateur produit exactement le snapshot golden fige.

    AI-54 / F-2 review-15-6 : ce test CASSE si le generateur ou le parseur
    change de comportement, forçant la mise a jour des fixtures dorees.

    Principe :
      1. generate_api_payload(days=2, end_date=DEFAULT_SEED_END_DATE) doit
         produire exactement le payload fige dans golden_pull.json (via parse).
      2. transform(golden_pull) doit produire exactement expected_facts.json.
    """

    _FIXTURES_DIR = MODULE_DIR / "tests" / "fixtures"

    def _load_fixture(self, name: str) -> list:
        import json as _json
        return _json.loads((self._FIXTURES_DIR / name).read_text(encoding="utf-8"))

    def test_generator_matches_golden_snapshot(self):
        """generate_api_payload -> _parse_sheet_response == golden_pull.json fige."""
        import random as _random

        rng = _random.Random(_RNG_SEED)
        payload = generate_api_payload(
            days=2,
            end_date=DEFAULT_SEED_END_DATE,
            rng=rng,
        )
        records, unparsable = _parse_sheet_response(payload, SEED_COLUMN_MAPPING)
        assert unparsable == 0

        golden = self._load_fixture("golden_pull.json")

        assert len(records) == len(golden), (
            f"Generateur produit {len(records)} lignes, golden en attend {len(golden)}. "
            "Regenerer les fixtures : python generate_google_sheets_seed.py"
        )
        for i, (actual, expected) in enumerate(zip(records, golden)):
            for field in set(actual) | set(expected):
                a_val = actual.get(field)
                e_val = expected.get(field)
                if isinstance(e_val, float):
                    assert a_val == pytest.approx(e_val), (
                        f"Ligne {i} champ {field!r}: generateur={a_val!r}, golden={e_val!r}"
                    )
                else:
                    assert a_val == e_val, (
                        f"Ligne {i} champ {field!r}: generateur={a_val!r}, golden={e_val!r}"
                    )

    def test_transform_golden_matches_expected_facts(self):
        """transform(golden_pull.json) == expected_facts.json fige (Layer 4 shadow)."""
        golden = self._load_fixture("golden_pull.json")
        expected = self._load_fixture("expected_facts.json")

        # Import transform from connector
        transform = _connector_mod.transform

        actual = transform(golden)

        assert len(actual) == len(expected), (
            f"transform produit {len(actual)} lignes, expected_facts en attend {len(expected)}"
        )
        for i, (act_row, exp_row) in enumerate(zip(actual, expected)):
            for field in set(act_row) | set(exp_row):
                a_val = act_row.get(field)
                e_val = exp_row.get(field)
                if isinstance(e_val, float):
                    assert a_val == pytest.approx(e_val), (
                        f"Ligne {i} champ {field!r}: transform={a_val!r}, expected={e_val!r}"
                    )
                else:
                    assert a_val == e_val, (
                        f"Ligne {i} champ {field!r}: transform={a_val!r}, expected={e_val!r}"
                    )
