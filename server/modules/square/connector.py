"""Square connector — payments/POS revenue source of record.

Pendant POS/omnichannel de Stripe (15.7) et Shopify (15.4) : expose une instance
``mcp_app: FastMCP`` que le loader du core monte sous le namespace ``square`` (AD-2).
Meme patron "drop-a-folder" que meta-ads / stripe / tiktok-ads : zero edit sous
server/core, un bloc UNION additif dans le mart.

# AD-12: le MCP server lit UNIQUEMENT le mart fact_daily_kpi -- pas de raw_*, pas de CSV.
# AD-3: l'access token vient de Nango juste avant usage -- jamais stocke ni logge (AD-3,
#        PAS le flux Google direct AD-21 qui est reserve au stack Google).
# AD-7: pull_id frappe par le scheduler du core, passe dans pull().
# AD-14: parametre identity/project_id.
# AI-10: toutes les metriques Square sont additives au grain jour ; pas d'aggregation_rule.

Decisions (parallelisme strict avec Stripe 15.7) :
  * MONTANTS EN CENTIMES -> UNITES (CONVERSION EXPLICITE) : les objets Money de Square
    (amount_money, refunded_money, processing_fee[].amount_money) portent amount dans la
    plus PETITE unite de la devise. Le connecteur DIVISE par 100 a l'ingestion
    (_amount_to_units) -- jamais silencieusement -- SAUF pour les devises zero-decimal
    (JPY, KRW, ...). La normalisation FX projet (pattern 4.2) est un etage SEPARE en dbt.
  * REVENUE / REFUNDS / FEES = COLONNES DEDIEES positives (refunds jamais soustrait de
    revenue). fees = SOMME de processing_fee[].amount_money.amount (Square peut renvoyer
    plusieurs lignes de frais par paiement).
  * REGLE DE DEDUP REVENUE (CRITIQUE, AD-4) : revenue Square, Shopify et Stripe peuvent
    mesurer la MEME vente. Jamais sommes dans un total croise. Regle declarative :
    metric_source_priority.csv (revenue -> shopify 1, stripe 2, square 3) + la vue
    cross_source_revenue. Voir manifest + staging + bloc mart.
  * TOPOLOGIE (AI-53) : un access token seller reach TOUS ses emplacements. discover_accounts()
    appelle GET /v2/locations et retourne la liste plate [{id,label}] que le flux d'onboarding
    du core consomme (selection_level='location'). Le location_id selectionne est passe comme
    filtre a /v2/payments. JAMAIS de location en variable d'environnement.

# AI-53: tous les champs/endpoints/quota API Square sont declares d'apres la doc officielle
# (verifie le 2026-07-21) et annotes "a confirmer en passe live" (AI-53/AI-13). Rien de
# non verifiable n'est affirme. Aucun compte de test Square disponible (no-connector-test-accounts)
# -> verification: blocked, ratification live differee.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance — the public surface the loader mounts.
mcp_app = FastMCP("square")

# ---------------------------------------------------------------------------
# Database connection helpers — env-var driven (no hardcoded paths).
# Same dual-backend pattern as stripe / meta-ads / shopify / tiktok-ads / gsc.
#   TOOROW_DB_MODE     = "duckdb" (default) | "bigquery"
#   TOOROW_DUCKDB_PATH = path to local .duckdb file
#   GCP_PROJECT        = GCP project ID (bigquery mode only)
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


def _query_duckdb(sql: str, params: list, duckdb_path: str) -> list[dict]:
    """Execute parameterized *sql* against the local DuckDB warehouse (F-01)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _query_bigquery(sql: str, params: dict) -> list[dict]:
    """Execute parameterized *sql* against BigQuery (F-01: @named parameters)."""
    from google.cloud import bigquery  # noqa: PLC0415

    project = os.environ.get("GCP_PROJECT", "")
    client = bigquery.Client(project=project or None)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(name, "STRING", value)
            for name, value in params.items()
        ]
    )
    result = client.query(sql, job_config=job_config).result()
    cols = [f.name for f in result.schema]
    return [dict(zip(cols, row)) for row in result]


def _get_mart_table(db_mode: str) -> str:
    """Fully-qualified mart table reference per engine (mirrors stripe)."""
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


# SQL body shared by both engines; only the table reference and the parameter
# placeholder style differ. User-supplied values NEVER enter the string (F-01).
_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'square'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _query_mart(date_from: str, date_to: str, project_id: str = "default") -> list[dict]:
    """Query fact_daily_kpi mart — DuckDB or BigQuery depending on env.

    # AD-12: MCP server reads marts only — never raw_* tables or CSV.
    """
    db_mode = _get_db_mode()
    table = _get_mart_table(db_mode)

    if db_mode == "duckdb":
        sql = _MART_QUERY.format(table=table, p_project="?", p_from="?", p_to="?")
        return _query_duckdb(sql, [project_id, date_from, date_to], _get_duckdb_path())
    elif db_mode == "bigquery":
        sql = _MART_QUERY.format(
            table=table, p_project="@project_id", p_from="@date_from", p_to="@date_to"
        )
        return _query_bigquery(
            sql, {"project_id": project_id, "date_from": date_from, "date_to": date_to}
        )
    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {db_mode!r}")


def _build_envelope(
    rows: list[dict],
    report_profile: str,
    date_from: str,
    date_to: str,
    project_id: str,
) -> dict:
    """Build the canonical AD-1 envelope from mart rows."""
    pull_ids = {r["pull_id"] for r in rows if r.get("pull_id")}
    freshness_values = [r["freshness"] for r in rows if r.get("freshness")]

    latest_pull_id = max(pull_ids) if pull_ids else None
    latest_freshness = max(freshness_values) if freshness_values else None

    data_by_metric: dict[str, list[dict]] = {}
    for r in rows:
        metric = r["metric"]
        data_by_metric.setdefault(metric, []).append(
            {
                "breakdown_dimension": r["breakdown_dimension"],
                "breakdown_value": r["breakdown_value"],
                "value": r["value"],
            }
        )

    # NFR8: provenance is a full dict, not a scalar pull_id string.
    provenance = (
        {
            "source_system": "square",
            "source_field": "fact_daily_kpi",
            "pull_id": latest_pull_id,
        }
        if latest_pull_id is not None
        else None
    )

    return {
        "schema_version": "1",
        "meta": {
            "freshness": latest_freshness,
            "provenance": provenance,
            "alerts": [],
        },
        "data": {
            "project_id": project_id,
            "report_profile": report_profile,
            "date_from": date_from,
            "date_to": date_to,
            "metrics": data_by_metric,
        },
    }


@mcp_app.tool()
def get_square_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "payments_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Square Payments Report — revenue source of record — reads from fact_daily_kpi mart.

    Report profiles: payments_daily
    Metrics: revenue, refunds, fees, transaction_count, order_count (all additive at day
        grain, AD-4). Montants EN UNITES devise (Square renvoie des centimes ; le connecteur
        divise par 100 a l'ingestion -- conversion explicite, jamais silencieuse). refunds et
        fees sont des COLONNES DEDIEES positives -- JAMAIS soustraites silencieusement de
        revenue ; le net (revenue - refunds - fees) se calcule explicitement au niveau
        semantique.
    Breakdown: day-total only (breakdown_dimension = 'day_total'). payment_id / order_id /
        location_id sont des dimensions de DETAIL du raw -- PAS des partitions du mart.

    REGLE DE DEDUP REVENUE (AD-4) : revenue Square, Shopify et Stripe peuvent mesurer la MEME
    vente. Ils ne sont JAMAIS sommes dans un total croise (cross_source_revenue choisit UNE
    source gagnante par jour).

    Returns the canonical AD-1 envelope via structuredContent.

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: payments_daily
        date_from: Start date ISO-8601 (e.g. '2026-04-01'). Defaults to 90 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
    # AD-12: MCP server reads fact_daily_kpi mart only — never raw_* tables or CSV.
    from datetime import date, timedelta  # noqa: PLC0415

    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=90)).isoformat()

    try:
        rows = _query_mart(date_from, date_to, project_id)
    except Exception as exc:
        return {
            "schema_version": "1",
            "meta": {
                "freshness": None,
                "provenance": None,
                "alerts": [{"level": "error", "message": str(exc)}],
            },
            "data": {
                "project_id": project_id,
                "report_profile": report_profile,
                "date_from": date_from,
                "date_to": date_to,
                "metrics": {},
            },
        }

    return _build_envelope(rows, report_profile, date_from, date_to, project_id)


# ---------------------------------------------------------------------------
# Square API constants — Story (Epic connecteurs).
# AI-53 verifie le 2026-07-21 (doc officielle developer.squareup.com): l'API REST Square
# est versionnee par un header (Square-Version), PAS par le chemin. La base est stable.
# ---------------------------------------------------------------------------
SQUARE_API_BASE = os.environ.get("SQUARE_API_BASE", "https://connect.squareup.com/v2")
# AI-53: version d'API epinglee pour la stabilite des champs (a confirmer en passe live).
SQUARE_API_VERSION = os.environ.get("SQUARE_API_VERSION", "2026-05-20")

_PAYMENTS_PATH = "/payments"
_LOCATIONS_PATH = "/locations"

# Square pagination: opaque `cursor`. La reponse porte `cursor` (vide sur la derniere page).
# Garde anti-boucle (pattern shopify F-2 / stripe) : borne dure _MAX_PAGES + set de curseurs
# deja vus (curseur non-progressant / mock casse). 100 objets/page x 1000 pages tres au-dessus
# de tout pull reel.
_MAX_PAGES = 1000
_PAGE_LIMIT = 100

# Square "zero-decimal currencies" : montants deja en unite (pas de centimes).
# AI-53 (doc officielle developer.squareup.com/reference/square/objects/Money + ISO 4217).
# Liste declaree ici ; a confirmer exhaustive en passe live. Alignee sur Stripe 15.7.
_ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf",
        "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
    }
)

# Story 25.7: le provider error map vit dans manifest.json (AD-2 — core ne hardcode jamais
# de codes provider). Charge une fois et cache, miroir du pattern meta-ads / stripe.
_ERROR_MAP: dict[str, str] | None = None


def _load_error_map() -> dict[str, str]:
    """Return the manifest's ``error_map`` (status:code -> canonical class), cached."""
    global _ERROR_MAP
    if _ERROR_MAP is None:
        manifest_path = Path(__file__).parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _ERROR_MAP = {
            k: v for k, v in manifest.get("error_map", {}).items()
            if not k.startswith("_")
        }
    return _ERROR_MAP


def _square_error_payload(resp: httpx.Response):
    """Normalise a Square error body so core._extract_provider_code can read a code.

    Square error responses use a LIST shape: ``{"errors": [{"category", "code",
    "detail", "field"}]}``. core._extract_provider_code recognises ``{"error":{"code"}}``
    and top-level ``{"code"}`` shapes, NOT the list — so without normalisation the
    manifest error_map would never refine.

    This helper surfaces the FIRST error's code at the top level while PRESERVING the
    original errors array as evidence:

        {"code": "ACCESS_TOKEN_REVOKED", "category": "AUTHENTICATION_ERROR",
         "errors": [ ...originals... ]}

    core then extracts ``code`` via its generic top-level branch and the error_map key
    ``"<status>:<code>"`` matches. If the body is not JSON or not the expected shape,
    the raw text/body is returned unchanged (still preserved as evidence).
    """
    try:
        body = resp.json()
    except Exception:
        return resp.text
    if not isinstance(body, dict):
        return body
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        first = errors[0]
        return {
            "code": first.get("code"),
            "category": first.get("category"),
            "errors": errors,  # originals preserved as evidence
        }
    return body


# ---------------------------------------------------------------------------
# Amount / date parsing helpers
# ---------------------------------------------------------------------------


def _amount_to_units(amount_minor, currency: str) -> float:
    """Convert a Square minor-unit amount (centimes) to the currency's major unit.

    Square renvoie TOUS les montants Money dans la plus petite unite de la devise
    (centimes pour EUR/USD -> diviser par 100). Les "zero-decimal currencies"
    (JPY, KRW, ...) sont deja en unite -> pas de division. CONVERSION EXPLICITE :
    jamais silencieuse, commentee ici et testee. La normalisation FX projet
    (pattern 4.2) est un ETAGE SEPARE en staging dbt.
    """
    try:
        minor = float(amount_minor or 0)
    except (ValueError, TypeError):
        minor = 0.0
    if (currency or "").lower() in _ZERO_DECIMAL_CURRENCIES:
        return round(minor, 2)
    return round(minor / 100.0, 2)


def _money_amount(money) -> int:
    """Extract the integer minor-unit amount from a Square Money object ({amount, currency})."""
    if isinstance(money, dict):
        try:
            return int(money.get("amount", 0) or 0)
        except (ValueError, TypeError):
            return 0
    return 0


def _money_currency(money, default: str = "USD") -> str:
    """Extract the ISO currency code from a Square Money object."""
    if isinstance(money, dict) and money.get("currency"):
        return str(money["currency"])
    return default


def _parse_created_date(created_at) -> str:
    """Map a Square RFC 3339 created_at timestamp to a UTC day-grain ISO date string.

    AI-53: politique tz = jour UTC (bornage source accepte au grain jour, documente en
    staging -- meme politique que meta/ga4/stripe). A confirmer en passe live.
    Square emits RFC 3339 like '2026-07-01T09:15:00Z' (sometimes with a numeric offset or
    fractional seconds). Returns '' if unparseable.
    """
    if not created_at:
        return ""
    text = str(created_at).strip()
    # datetime.fromisoformat accepts 'Z' from 3.11+, but normalise defensively.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


def _extract_fee_total(payment: dict) -> int:
    """Sum Square processing_fee[].amount_money.amount for one payment, in MINOR units.

    AI-53 verifie le 2026-07-21: processing_fee est un TABLEAU (read-only) ou chaque
    element porte {amount_money{amount,currency}, effective_at, type}. Square peut renvoyer
    PLUSIEURS lignes de frais par paiement (ex: frais initial + ajustement) -> on SOMME.
    Absent (paiement non regle/settled) -> 0. A confirmer en passe live (disponibilite avant
    settlement). Retour EN CENTIMES (converti plus loin).
    """
    fees = payment.get("processing_fee")
    if not isinstance(fees, list):
        return 0
    total = 0
    for fee in fees:
        if isinstance(fee, dict):
            total += _money_amount(fee.get("amount_money"))
    return total


def _parse_payment(payment: dict) -> dict:
    """Map one Square Payment to the canonical raw record shape (one row per payment).

    AI-53 A CONFIRMER EN PASSE LIVE: field names d'apres la doc officielle
    developer.squareup.com/reference/square/objects/Payment (verifie 2026-07-21):
      - created_at (RFC 3339) -> date (day grain, UTC date extracted)
      - id -> payment_id
      - order_id -> order_id (dimension de detail, joignable Orders API quand present)
      - location_id -> location_id (emplacement du paiement ; topologie + dimension de detail)
      - amount_money.amount (centimes) -> revenue (additif, converti en unites)
      - refunded_money.amount (centimes) -> refunds (COLONNE DEDIEE positive)
      - SUM(processing_fee[].amount_money.amount) (centimes) -> fees (COLONNE DEDIEE positive)
      - amount_money.currency -> revenue_source_currency (normalise devise projet en staging)
      - transaction_count / order_count = 1 par payment (agreges au jour dans le mart)

    review F-1: parse est une extraction STRUCTURELLE avec conversion EXPLICITE des montants
    (centimes -> unites). Les noms de champs metriques gardent leurs noms SOURCE (amount,
    refunded, fee_amount) ; transform() applique le rename canonique (-> revenue, refunds, fees)
    depuis le manifest, donc le rename map est exerce sur CHAQUE pull ET sur la golden fixture.
    """
    currency = _money_currency(payment.get("amount_money"), default="USD")

    return {
        "date": _parse_created_date(payment.get("created_at")),
        "payment_id": str(payment.get("id", "")),
        "order_id": (str(payment["order_id"]) if payment.get("order_id") else None),
        "location_id": (str(payment["location_id"]) if payment.get("location_id") else None),
        # SOURCE metric names kept here; transform() renames them to canonical (manifest).
        # CONVERSION EXPLICITE centimes -> unites (jamais silencieuse).
        "amount": _amount_to_units(_money_amount(payment.get("amount_money")), currency),
        "refunded": _amount_to_units(_money_amount(payment.get("refunded_money")), currency),
        "fee_amount": _amount_to_units(_extract_fee_total(payment), currency),
        "transaction_count": 1,
        "order_count": 1,
        "revenue_source_currency": (currency or "USD").upper(),
    }


# ---------------------------------------------------------------------------
# Raw table DDL (long-ish per-payment format, mirrors raw_stripe_payments).
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_square_payments (
    date                     VARCHAR,
    payment_id               VARCHAR,
    order_id                 VARCHAR,
    location_id              VARCHAR,
    revenue                  DOUBLE,
    refunds                  DOUBLE,
    fees                     DOUBLE,
    transaction_count        INTEGER,
    order_count              INTEGER,
    revenue_source_currency  VARCHAR DEFAULT 'USD',
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_square_payments
    (date, payment_id, order_id, location_id, revenue, refunds, fees,
     transaction_count, order_count, revenue_source_currency, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical rows into raw_square_payments (DuckDB only at P-dev).

    Same self-contained pattern as stripe/meta-ads/shopify _insert_raw_rows: the connector
    owns its raw table DDL and never imports from a non-package seeds/ folder.

    # refunds and fees are stored in their OWN columns (dedicated, positive). They are NEVER
    # subtracted from revenue here (decision de story, meme discipline que Stripe 15.7).
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL)
        # Additive/idempotent guards for tables created before newer columns existed.
        con.execute(
            "ALTER TABLE raw_square_payments ADD COLUMN IF NOT EXISTS "
            "revenue_source_currency VARCHAR DEFAULT 'USD'"
        )
        con.execute(
            "ALTER TABLE raw_square_payments ADD COLUMN IF NOT EXISTS order_id VARCHAR"
        )
        values = [
            (
                r.get("date", ""),
                r.get("payment_id", ""),
                (r.get("order_id") or None),
                (r.get("location_id") or None),
                float(r.get("revenue", 0) or 0),
                float(r.get("refunds", 0) or 0),
                float(r.get("fees", 0) or 0),
                int(r.get("transaction_count", 1) or 0),
                int(r.get("order_count", 1) or 0),
                r.get("revenue_source_currency", "USD"),
                pull_id,
                loaded_at,
                project_id,
            )
            for r in rows
        ]
        if values:
            con.executemany(_RAW_INSERT_SQL, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev "
            "(BigQuery path not yet implemented)"
        )


# ---------------------------------------------------------------------------
# HTTP paging helper — shared by pull() and pull_catalog_daily().
# ---------------------------------------------------------------------------


def _iso_to_rfc3339(date_str: str, end_of_day: bool) -> str:
    """Convert a YYYY-MM-DD date to a Square RFC 3339 timestamp (UTC boundary)."""
    from datetime import date as _date  # noqa: PLC0415

    d = _date.fromisoformat(date_str)
    if end_of_day:
        return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc).isoformat()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()


def _fetch_all_payments(
    connection_id: str,
    date_from: str,
    date_to: str,
    pull_id: str,
    location_id: str | None,
    log_prefix: str,
) -> list[dict]:
    """Fetch every Square payment in the window, following the cursor (bounded).

    # AD-3: the access token is used immediately as a Bearer header then discarded.
    Raises core.quota.RateLimitError on 429; a typed ConnectorError on any other non-2xx.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    # AD-3: token obtained immediately before use; falls out of scope after the calls.
    token = nango_client.get_fresh_token(connection_id, provider="square")
    url = f"{SQUARE_API_BASE}{_PAYMENTS_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Square-Version": SQUARE_API_VERSION,
    }
    # AI-53 verifie 2026-07-21: window par begin_time/end_time (RFC 3339), sort_field=CREATED_AT,
    # sort_order=ASC, limit<=100, location_id filtre optionnel. A confirmer live.
    base_params: dict = {
        "begin_time": _iso_to_rfc3339(date_from, end_of_day=False),
        "end_time": _iso_to_rfc3339(date_to, end_of_day=True),
        "sort_field": "CREATED_AT",
        "sort_order": "ASC",
        "limit": _PAGE_LIMIT,
    }
    if location_id:
        base_params["location_id"] = location_id

    all_payments: list[dict] = []
    seen_cursors: set[str] = set()
    pages = 0
    cursor: str | None = None
    while True:
        # Boucle BORNEE (pattern shopify F-2 / stripe): un serveur/mock renvoyant un curseur
        # non-progressant ou toujours peuple ne doit JAMAIS bloquer le worker.
        if cursor is not None and cursor in seen_cursors:
            logger.warning(
                "%s: non-progressing cursor detected, stopping (pull_id=%s pages=%d)",
                log_prefix, pull_id, pages,
            )
            break
        if cursor is not None:
            seen_cursors.add(cursor)
        if pages >= _MAX_PAGES:
            logger.warning(
                "%s: page cap %d reached, stopping (pull_id=%s)", log_prefix, _MAX_PAGES, pull_id
            )
            break
        pages += 1

        page_params = dict(base_params)
        if cursor is not None:
            page_params["cursor"] = cursor
        resp = httpx.get(url, params=page_params, headers=headers, timeout=30.0)

        if resp.status_code == 429:
            # AI-53 verifie 2026-07-21: Square renvoie HTTP 429 (category RATE_LIMITED) au
            # depassement. "square" vit dans le MODULE (pas dans core) -- AD-2 compliant.
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(float(retry_after_raw)) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("square", retry_after)

        if resp.status_code != 200:
            # Taxonomie 25.2/25.7 : erreur typee canonique ; payload provider preserve.
            # Square a une forme LIST {errors:[{code}]} -> _square_error_payload surface le
            # premier code au top-level pour que l'error_map du manifest raffine.
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            raise classify_http_error(
                resp.status_code, _square_error_payload(resp), _load_error_map()
            )

        payload = resp.json()
        page_payments = payload.get("payments") or []
        all_payments.extend(page_payments)

        # Curseur : Square pagine via `cursor` tant qu'il est non-vide. Vide/absent -> stop.
        next_cursor = payload.get("cursor")
        if not next_cursor or not page_payments:
            break
        cursor = str(next_cursor)

    return all_payments


# ---------------------------------------------------------------------------
# pull() — called by the queue worker only (AD-12).
# ---------------------------------------------------------------------------


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    location_id: str | None = None,
) -> dict:
    """Fetch Square payments and land rows in raw_square_payments.

    # AD-12: called by the queue worker only — no synchronous API call during an LLM tool call.
    # AD-3: the access token is used immediately as a Bearer header then discarded —
    #        it is NEVER stored, logged, or passed further (AD-3, NOT the Google flow AD-21).
    # AD-7: pull_id is minted by the caller; this function receives it and never mints its own.

    Parameters
    ----------
    connection_id:
        Nango connection identifier passed to get_fresh_token (provider='square').
    date_from, date_to:
        ISO-8601 date strings (YYYY-MM-DD) for the begin_time / end_time window.
    project_id:
        Project binding for the raw rows.
    pull_id:
        Core-minted pull ULID (AD-7). Passed in; not generated here.
    location_id:
        Optional Square location filter (from account-topology selection, mirrors meta-ads
        ad_account_id). None fetches payments across all of the token's locations.

    Returns
    -------
    dict
        {"pull_id": str, "row_count": int, "date_from": str, "date_to": str}

    Raises
    ------
    ConnectorError
        Typed error on non-200 (non-429) response (refined by the manifest error_map).
    RateLimitError
        On HTTP 429 (RATE_LIMITED).
    """
    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    all_payments = _fetch_all_payments(
        connection_id, date_from, date_to, pull_id, location_id, "square_pull"
    )

    # review F-1: parse (source keys + conversion montants) -> transform (manifest rename) ->
    # insert. Le canonical_metric_mapping est exerce sur CHAQUE pull.
    raw_rows: list[dict] = [_parse_payment(p) for p in all_payments]
    canonical_rows: list[dict] = transform(raw_rows)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no token in log — only pull_id and row_count (safe metadata).
    logger.info("square_pull_completed: pull_id=%s row_count=%d", pull_id, row_count)

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# transform() — manifest-driven canonical field mapping
# ---------------------------------------------------------------------------


def _is_api_shaped(row: dict) -> bool:
    """True if *row* is a raw Square Payment (has 'amount_money', or 'created_at' unparsed)."""
    if isinstance(row.get("amount_money"), dict):
        return True
    # A parsed row already carries a 'date' key; an API payment carries RFC 3339 'created_at'
    # without a 'date' key.
    return "created_at" in row and "date" not in row


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map source fields to canonical names using manifest mappings.

    # AD-4: canonical field mapping driven by manifest — do not hardcode source names.

    review F-2 (honest, pattern stripe/tiktok): transform() is the module's canonicalization
    entrypoint (the shared conformance Layer 4 harness calls transform() directly on
    golden_pull.json). golden_pull.json is the REAL Square Payment API shape ({id, created_at,
    amount_money{amount,currency}, refunded_money, processing_fee[...], location_id, order_id}),
    so transform() first routes API-shaped rows through _parse_payment() (structural extraction
    + EXPLICIT centimes->units conversion, keeping SOURCE names like amount) THEN applies the
    canonical rename (amount -> revenue, refunded -> refunds, fee_amount -> fees) from the
    manifest. This genuinely exercises _parse_payment on the golden fixture, not just the respx
    mocks. Already-parsed rows (source-name dicts, e.g. from the live pull path) pass straight
    to the rename step. Structural fields pass through unchanged.
    """
    _manifest_path = Path(__file__).parent / "manifest.json"
    _manifest = json.loads(_manifest_path.read_text(encoding="utf-8"))

    rename_map: dict[str, str] = {}
    for src, val in _manifest.get("canonical_metric_mapping", {}).items():
        if isinstance(val, str):
            rename_map[src] = val
        elif isinstance(val, dict):
            rename_map[src] = val.get("canonical", src)
    rename_map.update(_manifest.get("canonical_dimension_mapping", {}))

    # F-2: parse API-shaped Payment rows to the canonical source-name record first
    # (centimes->units conversion happens in _parse_payment). Already-parsed rows pass through.
    parsed_rows = [
        _parse_payment(row) if _is_api_shaped(row) else row for row in raw_rows
    ]

    result: list[dict] = []
    for row in parsed_rows:
        canonical_row: dict = {}
        for key, value in row.items():
            canonical_row[rename_map.get(key, key)] = value
        result.append(canonical_row)
    return result


# ---------------------------------------------------------------------------
# discover_accounts() — account topology (Story 25.5/25.7): list seller locations.
# ---------------------------------------------------------------------------


def discover_accounts(connection_id: str) -> list[dict]:
    """List the Square locations the connection's token can reach.

    Calls GET /v2/locations and returns the generic flat-level list core's topology flow
    consumes:

        [{"id": "L_ABC123", "label": "Downtown Store"}, ...]

    The selection_level is 'location' (single level, no parent hierarchy). Core uses the
    returned list to present a location-selection step during onboarding; the chosen id is
    handed back to pull() as location_id.

    AI-53 verifie le 2026-07-21 (doc officielle developer.squareup.com/reference/square/
    locations-api/list-locations): un access token seller donne acces a tous ses emplacements.
    Reponse: {locations:[{id, name, status, currency, country, ...}]}.

    Raises:
        core.quota.RateLimitError on 429 (breaker path, unchanged contract).
        A typed core.pull_errors.ConnectorError on any other non-2xx (refined by error_map).

    AD-3: the token is used immediately as a Bearer header, never stored or logged.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    # AD-3: token obtained immediately before use; falls out of scope after call.
    token = nango_client.get_fresh_token(connection_id, provider="square")

    resp = httpx.get(
        f"{SQUARE_API_BASE}{_LOCATIONS_PATH}",
        headers={
            "Authorization": f"Bearer {token}",
            "Square-Version": SQUARE_API_VERSION,
        },
        timeout=30.0,
    )

    if resp.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(float(retry_after_raw)) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("square", retry_after)

    if resp.status_code != 200:
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        raise classify_http_error(
            resp.status_code, _square_error_payload(resp), _load_error_map()
        )

    payload = resp.json()
    locations = payload.get("locations") or []

    # AD-3: no token in log — only safe counts.
    logger.info("square_discover_accounts: found %d location(s)", len(locations))

    result: list[dict] = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        loc_id = loc.get("id")
        if not loc_id:
            continue
        # Label = human name when present, else fall back to the id (core stores id verbatim).
        result.append({"id": str(loc_id), "label": str(loc.get("name") or loc_id)})
    return result


# ---------------------------------------------------------------------------
# Story 25.9: catalog_driven execution — pull_catalog_daily (PROJECTION style).
# Fetches /v2/payments (same objects as pull()). Selection controls which catalog fields
# are projected into landed rows.
# Fetchable sections: PAYMENT, PROCESSING FEE, CARD DETAILS, PLATFORM CONTRACT.
# Excluded: LOCATION (separate /v2/locations), ORDER (separate Orders API), REFUND
#           (separate /v2/refunds), CUSTOMER (id only on the payment).
# ---------------------------------------------------------------------------


def _extract_dotted(obj: dict, path: str):
    """Extract from *obj* via a dotted *path* (e.g. 'card_details.card.card_brand'); None if absent."""  # noqa: E501
    parts = path.split(".")
    current = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _project_payment(api_payment: dict, selection: dict) -> list[dict]:
    """Project one payment into catalog-driven rows (one per selected field).

    PLATFORM CONTRACT fields (revenue/refunds/fees) come from _parse_payment; all other
    sections read via dotted-path fallback on the raw api_payment.
    """
    catalog: dict[str, dict] = selection.get("_catalog_fields", {})
    source_fields: dict[str, str] = selection.get("source_fields", {})
    selected_metrics: list[str] = selection.get("metrics", [])
    selected_dimensions: list[str] = selection.get("dimensions", [])

    parsed = _parse_payment(api_payment)
    payment_date = parsed.get("date", "")
    payment_id = parsed.get("payment_id", "")

    rows: list[dict] = []

    for field_id in selected_metrics:
        src = source_fields.get(field_id, field_id)
        section = catalog.get(field_id, {}).get("section", "")
        if section == "PLATFORM CONTRACT":
            if field_id == "revenue":
                value = parsed.get("amount")
            elif field_id == "refunds":
                value = parsed.get("refunded")
            elif field_id == "fees":
                value = parsed.get("fee_amount")
            else:
                value = parsed.get(src)
        else:
            value = parsed.get(src)
            if value is None:
                value = _extract_dotted(api_payment, src)
        rows.append({"row_type": "metric", "field_id": field_id, "value": value, "date": payment_date, "payment_id": payment_id})  # noqa: E501

    for field_id in selected_dimensions:
        if field_id == "date":
            continue
        src = source_fields.get(field_id, field_id)
        value = parsed.get(src)
        if value is None and src != field_id:
            value = _extract_dotted(api_payment, src)
        if value is None:
            value = parsed.get(field_id)
        if value is None:
            value = _extract_dotted(api_payment, src)
        rows.append({"row_type": "dimension", "field_id": field_id, "value": value, "date": payment_date, "payment_id": payment_id})  # noqa: E501

    return rows


_CATALOG_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_square_catalog_daily (
    date VARCHAR, payment_id VARCHAR, field_id VARCHAR,
    row_type VARCHAR, value VARCHAR, pull_id VARCHAR,
    loaded_at VARCHAR, project_id VARCHAR
)
"""

_CATALOG_RAW_INSERT_SQL = """
INSERT INTO raw_square_catalog_daily
    (date, payment_id, field_id, row_type, value, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_catalog_rows(rows: list[dict], pull_id: str, loaded_at: str, project_id: str, db_mode: str, duckdb_path: str) -> int:  # noqa: E501
    """Land projected rows into raw_square_catalog_daily (DuckDB only at P-dev)."""
    if db_mode != "duckdb":
        raise ValueError(f"_insert_catalog_rows: unsupported db_mode {db_mode!r}")
    from core import warehouse_write  # noqa: PLC0415

    con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
    con.execute(_CATALOG_RAW_CREATE_DDL)
    values = [(r.get("date", ""), r.get("payment_id", ""), r.get("field_id", ""), r.get("row_type", ""), str(r["value"]) if r.get("value") is not None else None, pull_id, loaded_at, project_id) for r in rows]  # noqa: E501
    if values:
        con.executemany(_CATALOG_RAW_INSERT_SQL, values)
    con.close()
    return len(values)


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    location_id: str | None = None,
) -> dict:
    """Story 25.9: catalog_driven daily pull for Square (PROJECTION style).

    Fetches /v2/payments (same objects as pull()). Selection controls which catalog fields
    land in raw_square_catalog_daily. Excluded sections (LOCATION, ORDER, REFUND, CUSTOMER)
    require separate endpoints and are not fetched here.
    """
    if selection is None:
        from core.catalog_contract import (  # noqa: PLC0415
            catalog_default_selection,
            validate_selection,
        )

        _cat = json.loads((Path(__file__).parent / "api_catalog.json").read_text(encoding="utf-8"))
        selection, _ = validate_selection(_cat, catalog_default_selection(_cat))

    if "_catalog_fields" not in selection:
        _cat_raw = json.loads(
            (Path(__file__).parent / "api_catalog.json").read_text(encoding="utf-8")
        )
        selection = dict(selection)
        selection["_catalog_fields"] = {f["field_id"]: f for f in _cat_raw.get("fields", [])}

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    all_payments = _fetch_all_payments(
        connection_id, date_from, date_to, pull_id, location_id, "square_catalog_pull"
    )

    projected_rows: list[dict] = []
    for api_payment in all_payments:
        projected_rows.extend(_project_payment(api_payment, selection))

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_catalog_rows(
        projected_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )
    logger.info("square_catalog_pull_completed: pull_id=%s row_count=%d payments=%d metrics=%d dimensions=%d", pull_id, row_count, len(all_payments), len(selection.get("metrics", [])), len(selection.get("dimensions", [])))  # noqa: E501
    return {"pull_id": pull_id, "row_count": row_count, "date_from": date_from, "date_to": date_to}


# ---------------------------------------------------------------------------
# Register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# The raw table name never appears in core source code (AD-2).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_square_payments", provider="square")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
