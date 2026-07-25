"""Generic datastream connector -- Story 8.10, Epic 8.

Proves the extensibility contract R4: any day>measure tabular payload grafts
onto the landing contract with zero core change.

This module:
  * module_kind='kpi' -- lands rows in raw_generic_daily (DuckDB), long-format.
  * auth_type='none' -- no Nango token required.
  * Accepts two sources via connection_config:
      {source: 'csv_inline', rows: [{date, metric_col, ...}, ...]}
      {source: 'url', url: 'https://...'}  -- fetched via httpx.
  * Date normalization at landing (R3): parses the date column using
    connection_config['date_format'] (strptime pattern, default '%Y-%m-%d')
    and connection_config['source_timezone'] (IANA tz, default 'UTC');
    normalizes to canonical ISO 'YYYY-MM-DD'.
  * Rows with unparseable dates -> rejected_rows count (not written).
  * Column mapping via connection_config['mappings']:
      [{'source_field': 'cout', 'target_field': 'cost'}, ...]
    Unmapped columns become the metric name as-is.
  * Landing: raw_generic_daily (DuckDB) -- long-format:
      project_id, date, metric, value DOUBLE, breakdown_dimension,
      breakdown_value, pull_id, loaded_at
    One row per (date x measure x dimension combo).

AD-2: no core-specific strings hardcoded; all config passed via connection_config.
AD-7: pull_id received from core scheduler; never minted here.
HG-2: this module writes ONLY raw_generic_daily, never fact_daily_kpi.

ASCII stdout (AI-03).
"""

from __future__ import annotations

import csv
import io
import ipaddress
import logging
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance (AD-2: loader mounts under 'generic' namespace).
mcp_app = FastMCP("generic")

# Raw DuckDB table name (registered at import time for verification.py / dq_monitors).
RAW_TABLE = "raw_generic_daily"

# Register the raw table name with verification.py (provider-aware, AD-2).
try:
    from core.verification import register_raw_table_name  # noqa: PLC0415

    register_raw_table_name(RAW_TABLE, provider="generic")
except Exception:
    pass  # verification module may not be loaded in all test contexts


# ---------------------------------------------------------------------------
# Date normalization (R3)
# ---------------------------------------------------------------------------


def _normalize_date(
    raw_date: str,
    date_format: str = "%Y-%m-%d",
    source_timezone: str = "UTC",
) -> str | None:
    """Parse *raw_date* with *date_format* + *source_timezone*, return ISO date.

    Returns None when the date cannot be parsed (-> rejected_rows).
    The canonical ISO date is always 'YYYY-MM-DD' (UTC boundary).
    """
    if not raw_date:
        return None
    try:
        # Parse the date string with the declared format.
        dt = datetime.strptime(raw_date.strip(), date_format)  # noqa: DTZ007
        # Attach source timezone so we have a timezone-aware datetime.
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415

            tz = ZoneInfo(source_timezone)
        except Exception:
            tz = timezone.utc
        dt_aware = dt.replace(tzinfo=tz)
        # Convert to UTC and extract the date.
        dt_utc = dt_aware.astimezone(timezone.utc)
        return dt_utc.date().isoformat()
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Column mapping helpers
# ---------------------------------------------------------------------------


def _build_target_map(mappings: list[dict] | None) -> dict[str, str | None]:
    """Build {source_field: target_field} from the datastream mappings list.

    target_field may be None if unmapped (source field used verbatim as metric name).
    """
    if not mappings:
        return {}
    result: dict[str, str | None] = {}
    for m in mappings:
        sf = m.get("source_field", "")
        tf = m.get("target_field")
        if sf:
            result[sf] = tf or None
    return result


def _detect_date_column(row: dict) -> str | None:
    """Return the first column name that looks like a date column.

    Heuristic: column named 'date', 'jour', 'day', or 'Date' (case-insensitive).
    Falls back to None if not found.
    """
    for key in row:
        if key.lower() in ("date", "jour", "day", "datum", "fecha"):
            return key
    return None


def _detect_dimension_columns(
    row: dict,
    date_col: str,
    measure_cols: set[str],
) -> list[str]:
    """Return column names that are neither date nor measures (= dimension columns)."""
    return [k for k in row if k != date_col and k not in measure_cols]


def _is_numeric(value: Any) -> bool:
    """Return True when value is numeric or a numeric string."""
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.replace(",", "."))
            return True
        except (ValueError, TypeError):
            return False
    return False


# ---------------------------------------------------------------------------
# Row exploding (wide -> long format)
# ---------------------------------------------------------------------------


def _explode_row(
    row: dict,
    date_col: str,
    canonical_date: str,
    target_map: dict[str, str | None],
    project_id: str,
    pull_id: str,
    loaded_at: str | None = None,
) -> list[dict]:
    """Expand one wide input row into N long-format landing rows.

    For each numeric column (excluding date and dimension columns), emit one row:
      project_id, date, metric, value, breakdown_dimension, breakdown_value,
      pull_id, (loaded_at when provided).
    Dimension columns generate a (breakdown_dimension, breakdown_value) pair on
    each metric row (first dimension wins for v1; R5 sub-dimension splits in 8.11).
    """
    # Identify measure columns (numeric, non-date).
    non_date_cols = {k: v for k, v in row.items() if k != date_col}
    measure_cols: set[str] = set()
    for k, v in non_date_cols.items():
        if _is_numeric(v):
            measure_cols.add(k)

    dim_cols = _detect_dimension_columns(row, date_col, measure_cols)

    # Resolve breakdown (v1: first dimension column only).
    breakdown_dimension: str | None = dim_cols[0] if dim_cols else None
    breakdown_value: str | None = str(row[dim_cols[0]]) if dim_cols else None

    result: list[dict] = []
    for col in sorted(measure_cols):  # sorted for deterministic order
        # Resolve canonical metric name via mappings.
        canonical_metric = target_map.get(col, col)
        if canonical_metric is None:
            canonical_metric = col  # unmapped -> verbatim

        try:
            value = float(str(row[col]).replace(",", "."))
        except (ValueError, TypeError):
            continue  # skip non-numeric

        landing_row: dict = {
            "project_id": project_id,
            "date": canonical_date,
            "metric": canonical_metric,
            "value": value,
            "breakdown_dimension": breakdown_dimension,
            "breakdown_value": breakdown_value,
            "pull_id": pull_id,
        }
        if loaded_at is not None:
            landing_row["loaded_at"] = loaded_at
        result.append(landing_row)
    return result


# ---------------------------------------------------------------------------
# transform() — conformance Layer 4 contract
# ---------------------------------------------------------------------------


def transform(raw_rows: list[dict], connection_config: dict | None = None) -> list[dict]:
    """Convert wide tabular rows into long-format canonical rows.

    This is the conformance Layer 4 entry point: called with raw_rows (list of
    dicts), returns canonical long-format rows suitable for raw_generic_daily.

    Parameters
    ----------
    raw_rows:
        Wide-format input rows, each a dict with a date column + N measure columns.
    connection_config:
        Optional config dict with 'date_format', 'source_timezone', 'mappings',
        'date_column'. Defaults apply when absent.

    Returns
    -------
    list[dict]
        Long-format rows: {project_id, date, metric, value, breakdown_dimension,
        breakdown_value, pull_id, loaded_at}.
    """
    cfg = connection_config or {}
    date_format: str = cfg.get("date_format", "%Y-%m-%d")
    source_tz: str = cfg.get("source_timezone", "UTC")
    mappings: list[dict] = cfg.get("mappings") or []
    target_map = _build_target_map(mappings)
    date_col_override: str | None = cfg.get("date_column")

    project_id: str = cfg.get("project_id", "conformance-test")
    pull_id: str = cfg.get("pull_id", "pull_GENERIC_TRANSFORM")
    # loaded_at is NOT included in transform() output (it is a landing-time field
    # added only in pull()). This keeps the expected_facts fixture deterministic.

    result: list[dict] = []
    for row in raw_rows:
        date_col = date_col_override or _detect_date_column(row)
        if not date_col or date_col not in row:
            continue
        canonical_date = _normalize_date(str(row[date_col]), date_format, source_tz)
        if canonical_date is None:
            continue
        long_rows = _explode_row(
            row, date_col, canonical_date, target_map, project_id, pull_id
            # loaded_at omitted intentionally (transform() = pure, no side effects)
        )
        result.extend(long_rows)
    return result


# ---------------------------------------------------------------------------
# DuckDB table bootstrap
# ---------------------------------------------------------------------------


def _ensure_raw_table(duck_conn) -> None:
    """Create raw_generic_daily if it does not exist.

    Long-format: project_id, date, metric, value DOUBLE, breakdown_dimension,
    breakdown_value, pull_id, loaded_at.
    AD-7: append-only; no UPDATE/DELETE.
    """
    duck_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_generic_daily (
            project_id         VARCHAR,
            date               VARCHAR,
            metric             VARCHAR,
            value              DOUBLE,
            breakdown_dimension VARCHAR,
            breakdown_value    VARCHAR,
            pull_id            VARCHAR,
            loaded_at          VARCHAR
        )
        """
    )


def _land_rows(duck_conn, rows: list[dict], default_loaded_at: str = "") -> int:
    """Append long-format rows to raw_generic_daily. Returns count written."""
    if not rows:
        return 0
    duck_conn.executemany(
        """
        INSERT INTO raw_generic_daily
            (project_id, date, metric, value, breakdown_dimension,
             breakdown_value, pull_id, loaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r["project_id"],
                r["date"],
                r["metric"],
                r["value"],
                r.get("breakdown_dimension"),
                r.get("breakdown_value"),
                r["pull_id"],
                r.get("loaded_at", default_loaded_at),
            )
            for r in rows
        ],
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Source readers
# ---------------------------------------------------------------------------


def _read_csv_inline(source_rows: list[dict]) -> list[dict]:
    """Return source_rows as-is (already parsed by the caller)."""
    return list(source_rows)


def _validate_url_ssrf(url: str) -> None:
    """Raise ValueError if *url* targets a private/loopback/link-local address (SSRF guard).

    Fix [HIGH #6]: before any HTTP fetch we resolve the hostname and reject:
      - Non-http(s) schemes (file://, ftp://, etc.)
      - RFC-1918 private ranges: 10/8, 172.16/12, 192.168/16
      - Loopback: 127/8, ::1
      - Link-local / cloud metadata: 169.254/16 (incl. 169.254.169.254 AWS/GCP/Azure)
      - IPv6 link-local: fe80::/10
    Redirects are disabled to prevent SSRF via redirect chain.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Schema URL non autorise: {parsed.scheme!r}. "
            "Seuls 'http' et 'https' sont acceptes."
        )
    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("URL invalide: aucun hote detecte.")
    try:
        # getaddrinfo resolves the hostname to an IP (follows /etc/hosts too).
        addr_infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        raise ValueError(
            f"Impossible de resoudre l'hote {hostname!r}: {exc}"
        ) from exc

    _PRIVATE_NETWORKS = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),  # link-local + cloud metadata
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fe80::/10"),
    ]

    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        for net in _PRIVATE_NETWORKS:
            if ip in net:
                raise ValueError(
                    f"Acces refuse: l'URL {url!r} pointe vers une adresse privee/locale "
                    f"({ip_str} dans {net}). Les adresses RFC-1918, loopback et "
                    "link-local (dont metadonnees cloud 169.254.169.254) sont interdites."
                )


def _read_url(url: str) -> list[dict]:
    """Fetch CSV/JSON data from *url* and return a list of dicts.

    Supports:
      - Content-Type application/json or JSON extension -> json.loads
      - Otherwise -> csv.DictReader

    SSRF protection: validates URL before fetch; redirects disabled.
    """
    _validate_url_ssrf(url)

    import httpx  # noqa: PLC0415

    # follow_redirects=False prevents SSRF via redirect chain.
    resp = httpx.get(url, timeout=30.0, follow_redirects=False)
    if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
        raise ValueError(
            f"Redirection refusee depuis {url!r} vers {resp.headers.get('location', '?')!r}. "
            "Les redirections ne sont pas suivies pour des raisons de securite."
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Erreur HTTP {resp.status_code} lors de la recuperation de {url!r}"
        )
    content_type = resp.headers.get("content-type", "").lower()
    text = resp.text
    if "json" in content_type or url.lower().endswith(".json"):
        import json  # noqa: PLC0415

        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Try common wrappers: {rows: [...], data: [...], records: [...]}
            for key in ("rows", "data", "records", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
        raise ValueError(
            f"Format JSON non reconnu depuis {url!r}: "
            "attendu une liste ou un objet avec une cle 'rows'/'data'/'records'"
        )
    # Default: CSV
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


# ---------------------------------------------------------------------------
# pull() — AD-2 module contract
# ---------------------------------------------------------------------------


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile_id: str = "tabular_daily",
    connection_config: dict | None = None,
) -> dict:
    """Fetch tabular data and land it in raw_generic_daily (DuckDB).

    AD-2: receives everything it needs via connection_config.
    AD-7: pull_id received from core scheduler; never minted here.
    HG-2: writes ONLY raw_generic_daily, never fact_daily_kpi.

    connection_config keys
    ----------------------
    source         : 'csv_inline' (default) | 'url'
    rows           : list[dict] -- for source='csv_inline'
    url            : str        -- for source='url'
    date_format    : strptime pattern, default '%Y-%m-%d'
    source_timezone: IANA tz name, default 'UTC'
    date_column    : explicit date column name (auto-detected if absent)
    mappings       : [{source_field, target_field}, ...] from datastream_mappings

    Returns
    -------
    dict
        {profile_id, rows_written, rejected_rows, pull_id}
    """
    cfg = connection_config or {}
    source: str = cfg.get("source", "csv_inline")
    date_format: str = cfg.get("date_format", "%Y-%m-%d")
    source_tz: str = cfg.get("source_timezone", "UTC")
    date_col_override: str | None = cfg.get("date_column")
    mappings: list[dict] = cfg.get("mappings") or []
    target_map = _build_target_map(mappings)

    # ---- Fetch raw rows ----
    if source == "csv_inline":
        raw_rows = _read_csv_inline(cfg.get("rows") or [])
    elif source == "url":
        url = cfg.get("url", "")
        if not url:
            raise ValueError(
                "connection_config['url'] requis pour source='url'"
            )
        raw_rows = _read_url(url)
    else:
        raise ValueError(
            f"Source inconnue: {source!r}. Valeurs acceptees: 'csv_inline', 'url'"
        )

    # ---- Normalize dates + explode to long format ----
    loaded_at = datetime.now(tz=timezone.utc).isoformat()
    rejected_rows = 0
    long_rows: list[dict] = []

    for row in raw_rows:
        date_col = date_col_override or _detect_date_column(row)
        if not date_col or date_col not in row:
            rejected_rows += 1
            logger.warning(
                "generic_pull: pas de colonne date dans la ligne: %s", list(row.keys())
            )
            continue

        canonical_date = _normalize_date(str(row[date_col]), date_format, source_tz)
        if canonical_date is None:
            rejected_rows += 1
            logger.warning(
                "generic_pull: date non parseable: %r (format=%s)",
                row[date_col],
                date_format,
            )
            continue

        # Filter to requested date window.
        if canonical_date < date_from or canonical_date > date_to:
            continue

        exploded = _explode_row(
            row, date_col, canonical_date, target_map, project_id, pull_id,
            loaded_at=loaded_at,
        )
        long_rows.extend(exploded)

    # ---- Write to DuckDB ----
    rows_written = 0
    db_path = _duckdb_path()
    if db_path:
        try:
            from core import warehouse_write  # noqa: PLC0415

            with warehouse_write.open_raw_writer(db_path, project_id=project_id) as duck_conn:
                _ensure_raw_table(duck_conn)
                rows_written = _land_rows(duck_conn, long_rows, default_loaded_at=loaded_at)
        except Exception as exc:
            logger.error("generic_pull: erreur DuckDB: %s", exc)
            raise
    else:
        # No DuckDB path: return rows for testing without actually writing.
        rows_written = len(long_rows)
        logger.warning(
            "generic_pull: TOOROW_DUCKDB_PATH non defini -- "
            "lignes non persistees (mode test)"
        )

    logger.info(
        "generic_pull: pull_id=%s profile=%s rows_written=%d rejected=%d",
        pull_id,
        profile_id,
        rows_written,
        rejected_rows,
    )

    return {
        "profile_id": profile_id,
        "rows_written": rows_written,
        "rejected_rows": rejected_rows,
        "pull_id": pull_id,
    }


def _duckdb_path() -> str:
    import os  # noqa: PLC0415

    return os.environ.get("TOOROW_DUCKDB_PATH", "")


# ---------------------------------------------------------------------------
# MCP tool -- tabular_daily query (AD-1 envelope, conformance Layer 2)
# ---------------------------------------------------------------------------


@mcp_app.tool()
def tabular_daily(
    project_id: str = "default",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Query raw_generic_daily and return long-format rows in the AD-1 envelope.

    Returns metric rows from raw_generic_daily for the given project and date
    window. This is the MCP reporting surface for the generic module.

    Parameters:
        project_id: Project identifier.
        date_from:  Start date ISO-8601 (e.g. '2026-07-01').
        date_to:    End date ISO-8601 (e.g. '2026-07-10').

    Returns the AD-1 canonical envelope:
        {schema_version, meta: {freshness, provenance, alerts}, data: {rows, ...}}
    """
    from datetime import date, timedelta  # noqa: PLC0415

    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=7)).isoformat()

    rows: list[dict] = []
    error_msg: str | None = None

    db_path = _duckdb_path()
    if db_path:
        try:
            import duckdb  # noqa: PLC0415

            with duckdb.connect(db_path, read_only=True) as duck_conn:
                try:
                    result = duck_conn.execute(
                        """
                        SELECT project_id, date, metric, value,
                               breakdown_dimension, breakdown_value, pull_id, loaded_at
                        FROM raw_generic_daily
                        WHERE project_id = ?
                          AND date >= ?
                          AND date <= ?
                        ORDER BY date, metric
                        """,
                        [project_id, date_from, date_to],
                    )
                    cols = [d[0] for d in result.description]
                    rows = [dict(zip(cols, row)) for row in result.fetchall()]
                except Exception as exc:
                    error_msg = f"Erreur lecture raw_generic_daily: {exc}"
        except Exception as exc:
            error_msg = f"Erreur connexion DuckDB: {exc}"

    provenance = f"raw_generic_daily:{project_id}:{date_from}:{date_to}"
    alerts: list = []
    if error_msg:
        alerts.append({"type": "error", "message": error_msg})

    return {
        "schema_version": "1",
        "meta": {
            "freshness": "live",
            "provenance": provenance,
            "alerts": alerts,
        },
        "data": {
            "project_id": project_id,
            "date_from": date_from,
            "date_to": date_to,
            "rows": rows,
            "row_count": len(rows),
        },
    }
