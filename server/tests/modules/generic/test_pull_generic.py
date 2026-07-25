"""Tests for the generic connector pull() and transform() -- Story 8.10.

Coverage:
  - _normalize_date: ISO default, custom format, timezone, invalid -> None
  - transform(): wide -> long, dimension detection, mapping, rejected rows
  - pull() csv_inline: rows written count, rejected_rows for bad dates
  - pull() url: mocked via respx (httpx intercept)
  - pull() unknown source: raises ValueError
  - E2E test (live Postgres + DuckDB): annotated BLOCKED

No real DuckDB required for unit tests (TOOROW_DUCKDB_PATH left unset).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_TOOROW_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "generic" / "connector.py"
)


def _import_connector():
    module_name = "connector_generic_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _normalize_date
# ---------------------------------------------------------------------------


def test_normalize_date_iso_default():
    mod = _import_connector()
    result = mod._normalize_date("2026-07-13")
    assert result == "2026-07-13"


def test_normalize_date_custom_format():
    mod = _import_connector()
    # French date format DD/MM/YYYY, Europe/Paris timezone
    result = mod._normalize_date("13/07/2026", "%d/%m/%Y", "Europe/Paris")
    # Europe/Paris in July = UTC+2; midnight Paris = 22:00 UTC previous day
    # So 2026-07-13 00:00 Paris -> 2026-07-12 22:00 UTC -> date = 2026-07-12
    assert result == "2026-07-12"


def test_normalize_date_utc_no_shift():
    mod = _import_connector()
    # UTC with non-ISO format
    result = mod._normalize_date("13/07/2026", "%d/%m/%Y", "UTC")
    assert result == "2026-07-13"


def test_normalize_date_invalid_returns_none():
    mod = _import_connector()
    result = mod._normalize_date("not-a-date", "%Y-%m-%d")
    assert result is None


def test_normalize_date_wrong_format_returns_none():
    mod = _import_connector()
    result = mod._normalize_date("13/07/2026", "%Y-%m-%d")
    assert result is None


def test_normalize_date_empty_returns_none():
    mod = _import_connector()
    assert mod._normalize_date("") is None


# ---------------------------------------------------------------------------
# transform() -- wide -> long format
# ---------------------------------------------------------------------------


def test_transform_basic_no_dimensions():
    mod = _import_connector()
    raw = [
        {"date": "2026-07-01", "cost": 100.0, "revenue": 200.0},
    ]
    result = mod.transform(raw)
    # Two metrics: cost, revenue (sorted)
    assert len(result) == 2
    metrics = {r["metric"] for r in result}
    assert metrics == {"cost", "revenue"}
    for r in result:
        assert r["date"] == "2026-07-01"
        assert r["breakdown_dimension"] is None
        assert r["breakdown_value"] is None
        assert r["pull_id"] == "pull_GENERIC_TRANSFORM"
        assert "loaded_at" not in r  # transform() omits loaded_at


def test_transform_with_dimension():
    mod = _import_connector()
    raw = [
        {"date": "2026-07-01", "clicks": 42.0, "campaign": "promo"},
    ]
    result = mod.transform(raw)
    assert len(result) == 1
    r = result[0]
    assert r["metric"] == "clicks"
    assert r["value"] == 42.0
    assert r["breakdown_dimension"] == "campaign"
    assert r["breakdown_value"] == "promo"


def test_transform_with_mappings():
    mod = _import_connector()
    raw = [
        {"date": "2026-07-01", "cout": 50.0},
    ]
    cfg = {
        "mappings": [{"source_field": "cout", "target_field": "cost"}]
    }
    result = mod.transform(raw, connection_config=cfg)
    assert len(result) == 1
    assert result[0]["metric"] == "cost"


def test_transform_rejects_unparseable_date():
    mod = _import_connector()
    raw = [
        {"date": "nope", "cost": 10.0},
        {"date": "2026-07-02", "cost": 20.0},
    ]
    result = mod.transform(raw)
    # Only the second row should produce a result
    assert len(result) == 1
    assert result[0]["date"] == "2026-07-02"


def test_transform_custom_date_format():
    mod = _import_connector()
    raw = [
        {"jour": "13/07/2026", "chiffre_affaires": 500.0},
    ]
    cfg = {"date_format": "%d/%m/%Y", "source_timezone": "UTC"}
    result = mod.transform(raw, connection_config=cfg)
    assert len(result) == 1
    assert result[0]["date"] == "2026-07-13"
    assert result[0]["metric"] == "chiffre_affaires"


def test_transform_skips_rows_without_date_column():
    mod = _import_connector()
    raw = [
        {"no_date_column": "2026-07-01", "cost": 10.0},
    ]
    result = mod.transform(raw)
    assert result == []


# ---------------------------------------------------------------------------
# pull() -- csv_inline (no DuckDB -- TOOROW_DUCKDB_PATH unset)
# ---------------------------------------------------------------------------


def test_pull_csv_inline_counts():
    mod = _import_connector()
    # TOOROW_DUCKDB_PATH unset -> rows counted but not persisted
    os.environ.pop("TOOROW_DUCKDB_PATH", None)

    result = mod.pull(
        connection_id="",
        date_from="2026-07-01",
        date_to="2026-07-03",
        project_id="proj-test",
        pull_id="pull_TEST_001",
        profile_id="tabular_daily",
        connection_config={
            "source": "csv_inline",
            "rows": [
                {"date": "2026-07-01", "cost": 100.0, "revenue": 200.0},
                {"date": "2026-07-02", "cost": 90.0, "revenue": 180.0},
            ],
        },
    )
    # 2 rows x 2 metrics = 4 long rows
    assert result["rows_written"] == 4
    assert result["rejected_rows"] == 0
    assert result["pull_id"] == "pull_TEST_001"


def test_pull_rejects_bad_date():
    mod = _import_connector()
    os.environ.pop("TOOROW_DUCKDB_PATH", None)

    result = mod.pull(
        connection_id="",
        date_from="2026-07-01",
        date_to="2026-07-03",
        project_id="proj-test",
        pull_id="pull_TEST_002",
        connection_config={
            "source": "csv_inline",
            "rows": [
                {"date": "not-a-date", "cost": 100.0},
                {"date": "2026-07-02", "revenue": 200.0},
            ],
        },
    )
    assert result["rejected_rows"] == 1
    assert result["rows_written"] == 1


def test_pull_date_window_filter():
    mod = _import_connector()
    os.environ.pop("TOOROW_DUCKDB_PATH", None)

    result = mod.pull(
        connection_id="",
        date_from="2026-07-01",
        date_to="2026-07-02",  # excludes 2026-07-03
        project_id="proj-test",
        pull_id="pull_TEST_003",
        connection_config={
            "source": "csv_inline",
            "rows": [
                {"date": "2026-07-01", "cost": 10.0},
                {"date": "2026-07-02", "cost": 20.0},
                {"date": "2026-07-03", "cost": 30.0},  # outside window
            ],
        },
    )
    assert result["rows_written"] == 2
    assert result["rejected_rows"] == 0


def test_pull_unknown_source_raises():
    mod = _import_connector()
    with pytest.raises(ValueError, match="Source inconnue"):
        mod.pull(
            connection_id="",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="proj-test",
            pull_id="pull_TEST_004",
            connection_config={"source": "webhook"},
        )


# ---------------------------------------------------------------------------
# pull() -- url source (respx mock)
# ---------------------------------------------------------------------------


def _public_ip_getaddrinfo(host, port, *args, **kwargs):
    """Mock socket.getaddrinfo to return a public IP (bypasses SSRF guard for tests)."""
    import socket as _socket
    return [(_socket.AF_INET, _socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]


def test_pull_url_csv(tmp_path):
    """pull() source='url' fetches CSV and lands rows (httpx mocked via respx)."""
    try:
        import httpx  # noqa: PLC0415
        import respx  # noqa: PLC0415
    except ImportError:
        pytest.skip("respx not available")

    from unittest.mock import patch

    mod = _import_connector()
    os.environ.pop("TOOROW_DUCKDB_PATH", None)

    csv_content = "date,impressions\n2026-07-01,1000\n2026-07-02,1200\n"

    with patch("socket.getaddrinfo", side_effect=_public_ip_getaddrinfo):
        with respx.mock:
            respx.get("https://example.com/data.csv").mock(
                return_value=httpx.Response(
                    200,
                    text=csv_content,
                    headers={"content-type": "text/csv"},
                )
            )
            result = mod.pull(
                connection_id="",
                date_from="2026-07-01",
                date_to="2026-07-02",
                project_id="proj-test",
                pull_id="pull_TEST_URL",
                connection_config={
                    "source": "url",
                    "url": "https://example.com/data.csv",
                },
            )

    assert result["rows_written"] == 2
    assert result["rejected_rows"] == 0


def test_pull_url_json():
    """pull() source='url' fetches JSON rows and lands them."""
    try:
        import httpx  # noqa: PLC0415
        import respx  # noqa: PLC0415
    except ImportError:
        pytest.skip("respx not available")

    import json
    from unittest.mock import patch

    mod = _import_connector()
    os.environ.pop("TOOROW_DUCKDB_PATH", None)

    payload = [
        {"date": "2026-07-01", "cost": 50.0},
        {"date": "2026-07-02", "cost": 60.0},
    ]

    with patch("socket.getaddrinfo", side_effect=_public_ip_getaddrinfo):
        with respx.mock:
            respx.get("https://api.example.com/metrics.json").mock(
                return_value=httpx.Response(
                    200,
                    text=json.dumps(payload),
                    headers={"content-type": "application/json"},
                )
            )
            result = mod.pull(
                connection_id="",
                date_from="2026-07-01",
                date_to="2026-07-02",
                project_id="proj-test",
                pull_id="pull_TEST_JSON",
                connection_config={
                    "source": "url",
                    "url": "https://api.example.com/metrics.json",
                },
            )

    assert result["rows_written"] == 2


def test_pull_url_http_error():
    """pull() raises RuntimeError on non-200 HTTP response."""
    try:
        import httpx  # noqa: PLC0415
        import respx  # noqa: PLC0415
    except ImportError:
        pytest.skip("respx not available")

    from unittest.mock import patch

    mod = _import_connector()

    with patch("socket.getaddrinfo", side_effect=_public_ip_getaddrinfo):
        with respx.mock:
            respx.get("https://example.com/broken.csv").mock(
                return_value=httpx.Response(404, text="Not Found")
            )
            with pytest.raises(RuntimeError, match="Erreur HTTP 404"):
                mod.pull(
                    connection_id="",
                    date_from="2026-07-01",
                    date_to="2026-07-01",
                    project_id="proj-test",
                    pull_id="pull_TEST_404",
                    connection_config={
                        "source": "url",
                        "url": "https://example.com/broken.csv",
                    },
                )


# ---------------------------------------------------------------------------
# SSRF protection -- Fix [HIGH #6]
# ---------------------------------------------------------------------------


class TestSSRFProtection:
    """Tests for the SSRF guard in _validate_url_ssrf (review-epic-8 #6)."""

    def _connector(self):
        return _import_connector()

    def test_private_ip_loopback_rejected(self):
        """127.x.x.x (loopback) is rejected before any HTTP request."""
        mod = self._connector()
        with pytest.raises(ValueError, match="privee|loopback|locale"):
            mod._validate_url_ssrf("http://127.0.0.1/data.csv")

    def test_private_ip_rfc1918_10_rejected(self):
        """10.x.x.x (RFC-1918) is rejected."""
        mod = self._connector()
        with pytest.raises(ValueError, match="privee|loopback|locale"):
            mod._validate_url_ssrf("http://10.0.0.1/data.csv")

    def test_private_ip_rfc1918_172_rejected(self):
        """172.16.x.x (RFC-1918) is rejected."""
        mod = self._connector()
        with pytest.raises(ValueError, match="privee|loopback|locale"):
            mod._validate_url_ssrf("http://172.16.0.1/data.csv")

    def test_private_ip_rfc1918_192_rejected(self):
        """192.168.x.x (RFC-1918) is rejected."""
        mod = self._connector()
        with pytest.raises(ValueError, match="privee|loopback|locale"):
            mod._validate_url_ssrf("http://192.168.1.1/data.csv")

    def test_metadata_ip_rejected(self):
        """169.254.169.254 (cloud metadata endpoint) is rejected."""
        mod = self._connector()
        with pytest.raises(ValueError, match="privee|loopback|locale"):
            mod._validate_url_ssrf("http://169.254.169.254/latest/meta-data/")

    def test_non_http_scheme_rejected(self):
        """file:// scheme is rejected."""
        mod = self._connector()
        with pytest.raises(ValueError, match="Schema.*non autorise"):
            mod._validate_url_ssrf("file:///etc/passwd")

    def test_ftp_scheme_rejected(self):
        """ftp:// scheme is rejected."""
        mod = self._connector()
        with pytest.raises(ValueError, match="Schema.*non autorise"):
            mod._validate_url_ssrf("ftp://example.com/data.csv")

    def test_public_url_passes_validation(self):
        """A real public HTTPS URL clears the SSRF guard (DNS resolves to public IP)."""
        import socket
        from unittest.mock import patch

        mod = self._connector()
        # Mock getaddrinfo to return a clearly public IP (8.8.8.8)
        mock_addr = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]
        with patch("socket.getaddrinfo", return_value=mock_addr):
            # Should not raise
            mod._validate_url_ssrf("https://example.com/data.csv")

    def test_pull_metadata_url_refused(self):
        """pull() with source='url' pointing to metadata IP returns an error, no fetch."""
        mod = self._connector()
        import os as _os
        _os.environ.pop("TOOROW_DUCKDB_PATH", None)

        with pytest.raises(ValueError, match="privee|loopback|locale"):
            mod.pull(
                connection_id="",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="proj-test",
                pull_id="pull_SSRF_TEST",
                connection_config={
                    "source": "url",
                    "url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
                },
            )


# ---------------------------------------------------------------------------
# R3 date normalization E2E (non-ISO format with timezone)
# ---------------------------------------------------------------------------


def test_r3_date_normalization_europe_paris():
    """R3: dates in DD/MM/YYYY Europe/Paris normalized to UTC ISO."""
    mod = _import_connector()
    os.environ.pop("TOOROW_DUCKDB_PATH", None)

    result = mod.pull(
        connection_id="",
        date_from="2026-07-12",
        date_to="2026-07-13",
        project_id="proj-r3",
        pull_id="pull_R3_TEST",
        connection_config={
            "source": "csv_inline",
            "date_format": "%d/%m/%Y",
            "source_timezone": "Europe/Paris",
            "rows": [
                # 13/07/2026 00:00 Paris = 12/07/2026 22:00 UTC -> date=2026-07-12
                {"date": "13/07/2026", "cout": 120.0, "revenu": 350.0},
            ],
        },
    )
    # Rows land at 2026-07-12 (UTC normalization from Paris midnight)
    # Both metrics written: 2 rows
    assert result["rows_written"] == 2
    assert result["rejected_rows"] == 0


# ---------------------------------------------------------------------------
# E2E test (live Postgres + DuckDB) -- BLOCKED
# ---------------------------------------------------------------------------


@pytest.mark.live_postgres
def test_e2e_generic_via_flows(tmp_path):
    """E2E: create generic datastream via flows.upsert_flow, run pull, verify rows.

    # BLOCKED: live Postgres + real DuckDB required.
    # Annotated BLOCKED per AI-01 pattern.
    #
    # Verification command (orchestrator runs this):
    #   TOOROW_DUCKDB_PATH=<path> uv run pytest
    #       server/tests/modules/generic/test_pull_generic.py::test_e2e_generic_via_flows
    #       -m live_postgres -v
    #
    # What this test does:
    #   1. Creates a generic datastream via core.flows.upsert_flow (the MCP path).
    #   2. Calls pull() directly with csv_inline rows (day > cost/revenue).
    #      Date format: DD/MM/YYYY, timezone: Europe/Paris (R3 proof).
    #   3. Asserts that raw_generic_daily has rows with canonical ISO dates
    #      (2026-07-12 for 13/07/2026 Paris midnight).
    #   4. dbt staging run command (orchestrator):
    #      cd dbt && dbt run --select stg_generic_daily --profiles-dir profiles
    """
    pytest.skip(
        "BLOCKED: live Postgres + DuckDB required. "
        "See test docstring for orchestrator commands."
    )
