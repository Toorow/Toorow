"""Stripe connector — Story 15.7 (Epic 15, connecteurs vague 1).

Source de verite REVENUS SaaS/services (pendant de Shopify pour les business sans
boutique e-commerce). Expose une instance ``mcp_app: FastMCP`` que le loader du core
monte sous le namespace ``stripe`` (AD-2). Meme patron "drop-a-folder" que meta-ads /
shopify / tiktok-ads : zero edit sous server/core, un bloc UNION additif dans le mart.

# AD-12: le MCP server lit UNIQUEMENT le mart fact_daily_kpi -- pas de raw_*, pas de CSV.
# AD-3: le secret vient de Nango juste avant usage -- jamais stocke ni logge (AD-3, PAS
#        le flux Google direct AD-21 qui est reserve au stack Google).
# AD-7: pull_id frappe par le scheduler du core, passe dans pull().
# AD-14: parametre identity/project_id.
# AI-10: toutes les metriques Stripe sont additives au grain jour ; pas d'aggregation_rule.

Decisions de story (Dev Agent Record 15-7-connecteur-stripe.md) :
  * MONTANTS EN CENTIMES -> UNITES (CONVERSION EXPLICITE) : Stripe renvoie tous les
    montants (charge.amount, refunded, fee) dans la plus PETITE unite de la devise
    (centimes pour EUR/USD ; les devises "zero-decimal" comme JPY sont deja en unite).
    Le connecteur DIVISE par 100 a l'ingestion (_amount_to_units) -- JAMAIS de conversion
    silencieuse cachee : la division est explicite, commentee et testee. La normalisation
    FX projet (pattern 4.2) se fait ENSUITE en staging dbt via fx_rates.
  * REVENUE = COLONNE DEDIEE, REFUNDS EN COLONNE DEDIEE (positifs, jamais soustraits),
    FEES EN COLONNE DEDIEE -- meme discipline que Shopify 15.4 (refund_amount dedie).
    Le net (revenue - refunds - fees) se calcule EXPLICITEMENT au niveau semantique.
  * REGLE DE DEDUP REVENUE (CRITIQUE, AD-4) : un paiement Stripe qui regle une commande
    Shopify mesure la MEME vente. revenue Stripe et revenue Shopify ne sont JAMAIS sommes
    dans un total croise. Regle declarative : metric_source_priority.csv (revenue -> shopify
    priorite 1, stripe priorite 2) + la vue cross_source_revenue (miroir de
    cross_source_conversions 3.7). Voir manifest + staging + bloc mart.
  * CLE DE RECONCILIATION GA4 (AI-53, verifie le 2026-07-19, doc officielle Stripe) :
    - charge.payment_intent -> PaymentIntent, dont metadata peut porter un identifiant
      client (mais N'EST PAS joignable GA4 par defaut) ;
    - checkout.Session.client_reference_id : champ PREVU pour transporter un identifiant
      cote marchand (ex: le client_id GA4 / un order id) -- c'est la SEULE cle
      officiellement joignable GA4 cote Stripe, MAIS elle n'existe QUE pour les paiements
      passes par Stripe Checkout ET seulement si le marchand l'a rempli (souvent absent).
    - Les Charges brutes (paiements API/Elements hors Checkout) N'ONT PAS de
      client_reference_id.
    CONCLUSION HONNETE : aucune cle de jointure GA4 FIABLE et universelle cote Stripe.
    On expose donc client_reference_id comme DIMENSION DE DETAIL du raw (nullable, joignable
    GA4 quand present) et charge_id / payment_intent_id comme identifiants de transaction.
    La reconciliation Epic 17 cote Stripe reste au niveau AGREGE (verified = revenue jour),
    pas au niveau transaction (contrairement a Shopify x GA4 via transaction_id, 17.4).

# AI-53: tous les champs/endpoints/quota API Stripe sont declares d'apres la doc officielle
# (verifie le 2026-07-19) et annotes "a confirmer en passe live" (AI-53/AI-13). Rien de
# non verifiable n'est affirme.
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
mcp_app = FastMCP("stripe")

# ---------------------------------------------------------------------------
# Database connection helpers — env-var driven (no hardcoded paths).
# Same dual-backend pattern as meta-ads / shopify / tiktok-ads / gsc connectors.
#   TOOROW_DB_MODE     = "duckdb" (default) | "bigquery"
#   TOOROW_DUCKDB_PATH = path to local .duckdb file
#   GCP_PROJECT           = GCP project ID (bigquery mode only)
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
    """Fully-qualified mart table reference per engine.

    DuckDB: dbt materialises marts into the main_marts schema.
    BigQuery (F-02): a dataset qualifier is mandatory — BQ_MARTS_DATASET
    (default "marts"), optionally project-qualified via GCP_PROJECT.
    """
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
    WHERE connector = 'stripe'
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
        if metric not in data_by_metric:
            data_by_metric[metric] = []
        data_by_metric[metric].append(
            {
                "breakdown_dimension": r["breakdown_dimension"],
                "breakdown_value": r["breakdown_value"],
                "value": r["value"],
            }
        )

    # NFR8: provenance is a full dict, not a scalar pull_id string.
    provenance = (
        {
            "source_system": "stripe",
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
def get_stripe_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "payments_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Stripe Payments Report — source de verite revenus — reads from fact_daily_kpi mart.

    Report profiles: payments_daily
    Metrics: revenue, refunds, fees, transaction_count, order_count (all additive at
        day grain, AD-4). Montants EN UNITES devise (Stripe renvoie des centimes ; le
        connecteur divise par 100 a l'ingestion -- conversion explicite, jamais silencieuse).
        refunds et fees sont des COLONNES DEDIEES positives -- JAMAIS soustraites
        silencieusement de revenue ; le net (revenue - refunds - fees) se calcule
        explicitement au niveau semantique.
    Breakdown: day-total only (breakdown_dimension = 'day_total'). charge_id /
        payment_intent_id / client_reference_id sont des dimensions de DETAIL du raw --
        PAS des partitions du mart (le mart agrege au jour).

    REGLE DE DEDUP REVENUE (AD-4) : le revenue Stripe et le revenue Shopify peuvent mesurer
    la MEME vente. Ils ne sont JAMAIS sommes dans un total croise (cross_source_revenue
    choisit UNE source gagnante par jour, comme cross_source_conversions).

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

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
# Stripe API pull — Story 15.7
# ---------------------------------------------------------------------------

# AI-53 verifie le 2026-07-19 (doc officielle stripe.com/docs/api): l'API REST Stripe
# est versionnee par un header (Stripe-Version), PAS par le chemin. La base est stable.
STRIPE_API_BASE = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com/v1")
# AI-53: version d'API epinglee pour la stabilite des champs (a confirmer en passe live).
STRIPE_API_VERSION = os.environ.get("STRIPE_API_VERSION", "2024-06-20")

# Story 25.7: the Stripe provider error map lives in manifest.json (AD-2 —
# core never hardcodes provider codes). Loaded once and cached, mirroring the
# meta-ads pattern.
_ERROR_MAP: dict[str, str] | None = None


def _load_error_map() -> dict[str, str]:
    """Return the manifest's ``error_map`` (status:code -> canonical class), cached.

    Keys are ``"<http_status>:<provider_code>"``. Stripe error responses carry
    {error:{code: "api_key_expired", type: "invalid_request_error", ...}}.
    core._extract_provider_code reads error.code (shape {error:{code:X}}), so
    Stripe codes such as "api_key_expired" are extracted and matched here.
    """
    global _ERROR_MAP
    if _ERROR_MAP is None:
        manifest_path = Path(__file__).parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _ERROR_MAP = {
            k: v for k, v in manifest.get("error_map", {}).items()
            if not k.startswith("_")
        }
    return _ERROR_MAP
_CHARGES_PATH = "/charges"

# Stripe pagination: curseur "starting_after" (id du dernier objet). has_more indique
# s'il reste des pages. Garde anti-boucle (pattern shopify F-2 / tiktok) : borne dure
# _MAX_PAGES + set d'ids de curseur deja vus (curseur non-progressant / mock casse).
# 100 objets/page x 1000 pages = 100k charges/fenetre, tres au-dessus de tout pull reel.
_MAX_PAGES = 1000
_PAGE_LIMIT = 100

# Stripe "zero-decimal currencies" : les montants sont deja en unite (pas de centimes).
# AI-53 verifie le 2026-07-19 (doc officielle stripe.com/docs/currencies#zero-decimal).
# La liste est declaree ici d'apres la doc ; a confirmer exhaustive en passe live.
_ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf",
        "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
    }
)


def _amount_to_units(amount_minor, currency: str) -> float:
    """Convert a Stripe minor-unit amount (centimes) to the currency's major unit.

    Stripe renvoie TOUS les montants dans la plus petite unite de la devise
    (centimes pour EUR/USD -> diviser par 100). Les "zero-decimal currencies"
    (JPY, KRW, ...) sont deja en unite -> pas de division. CONVERSION EXPLICITE :
    jamais silencieuse, commentee ici et testee (test_amount_conversion_*).
    La normalisation FX projet (pattern 4.2) est un ETAGE SEPARE en staging dbt.
    """
    try:
        minor = float(amount_minor or 0)
    except (ValueError, TypeError):
        minor = 0.0
    if (currency or "").lower() in _ZERO_DECIMAL_CURRENCIES:
        return round(minor, 2)
    return round(minor / 100.0, 2)


_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_stripe_payments (
    date                     VARCHAR,
    charge_id                VARCHAR,
    payment_intent_id        VARCHAR,
    client_reference_id      VARCHAR,
    revenue                  DOUBLE,
    refunds                  DOUBLE,
    fees                     DOUBLE,
    transaction_count        INTEGER,
    order_count              INTEGER,
    revenue_source_currency  VARCHAR DEFAULT 'EUR',
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_stripe_payments
    (date, charge_id, payment_intent_id, client_reference_id, revenue, refunds, fees,
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
    """Insert canonical rows into raw_stripe_payments (DuckDB only at P-dev).

    Same self-contained pattern as meta-ads/shopify/tiktok _insert_raw_rows: the connector
    owns its raw table DDL and never imports from a non-package seeds/ folder.

    # refunds and fees are stored in their OWN columns (dedicated, positive). They are NEVER
    # subtracted from revenue here (decision de story 15.7, meme discipline que Shopify 15.4).
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL)
        # Additive/idempotent guards for tables created before newer columns existed.
        con.execute(
            "ALTER TABLE raw_stripe_payments ADD COLUMN IF NOT EXISTS "
            "revenue_source_currency VARCHAR DEFAULT 'EUR'"
        )
        con.execute(
            "ALTER TABLE raw_stripe_payments ADD COLUMN IF NOT EXISTS "
            "client_reference_id VARCHAR"
        )
        values = [
            (
                r.get("date", ""),
                r.get("charge_id", ""),
                (r.get("payment_intent_id") or None),
                # client_reference_id : cle GA4 joignable QUAND presente (souvent absente).
                (r.get("client_reference_id") or None),
                float(r.get("revenue", 0) or 0),
                float(r.get("refunds", 0) or 0),
                float(r.get("fees", 0) or 0),
                int(r.get("transaction_count", 1) or 0),
                int(r.get("order_count", 1) or 0),
                r.get("revenue_source_currency", "EUR"),
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


def _extract_fees(api_charge: dict) -> float:
    """Derive the Stripe processing fee for one charge, in MINOR units (centimes).

    AI-53 A CONFIRMER EN STORY (live): la fee Stripe vit sur le BalanceTransaction lie
    (charge.balance_transaction.fee), pas directement sur la Charge. Avec expand=
    ['data.balance_transaction'] la Charge porte l'objet balance_transaction {fee, ...}.
    On lit charge.balance_transaction.fee ; si absent (non expand / non regle), 0.
    Champ/shape a confirmer en passe live. Retour EN CENTIMES (converti plus loin).
    """
    bt = api_charge.get("balance_transaction")
    if isinstance(bt, dict):
        try:
            return float(bt.get("fee", 0) or 0)
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def _extract_client_reference_id(api_charge: dict) -> str | None:
    """Derive the GA4-joinable client_reference_id when present (AI-53, souvent absent).

    AI-53 verifie le 2026-07-19 : client_reference_id est un champ de checkout.Session,
    PAS de Charge. Il n'est joignable GA4 que si (a) le paiement passe par Stripe Checkout
    ET (b) le marchand l'a rempli. Sur une Charge brute il n'existe pas -> None.
    On accepte deux emplacements possibles selon le shape d'expansion :
      - charge.metadata.client_reference_id (si le marchand l'a copie en metadata) ;
      - charge.payment_intent.metadata.client_reference_id (idem cote PaymentIntent).
    Aucun de ces deux n'est GARANTI -> nullable, mesure honnetement en qualite.
    """
    meta = api_charge.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("client_reference_id"):
        return str(meta["client_reference_id"])
    pi = api_charge.get("payment_intent")
    if isinstance(pi, dict):
        pi_meta = pi.get("metadata") or {}
        if isinstance(pi_meta, dict) and pi_meta.get("client_reference_id"):
            return str(pi_meta["client_reference_id"])
    return None


def _payment_intent_id(api_charge: dict) -> str | None:
    """Extract the PaymentIntent id (charge.payment_intent may be a string id or an object)."""
    pi = api_charge.get("payment_intent")
    if isinstance(pi, dict):
        return str(pi.get("id")) if pi.get("id") else None
    if pi in (None, ""):
        return None
    return str(pi)


def _parse_charge(api_charge: dict) -> dict:
    """Map one Stripe Charge to the canonical raw record shape (one row per charge).

    AI-53 A CONFIRMER EN STORY (live): field names supposed d'apres la doc officielle
    stripe.com/docs/api/charges (verifie 2026-07-19):
      - created (unix epoch seconds) -> date (day grain, UTC date extracted)
      - id -> charge_id
      - payment_intent -> payment_intent_id (dimension de detail, identifiant de transaction)
      - metadata / payment_intent.metadata client_reference_id -> client_reference_id
        (cle GA4 joignable QUAND presente, souvent absente -- documente honnetement)
      - amount (centimes) -> revenue (additif, converti en unites EN CENTIMES ici puis /100)
      - amount_refunded (centimes) -> refunds (COLONNE DEDIEE positive)
      - balance_transaction.fee (centimes) -> fees (COLONNE DEDIEE positive)
      - currency -> revenue_source_currency (normalise devise projet en staging via fx_rates)
      - transaction_count / order_count = 1 par charge (agreges au jour dans le mart)

    review-15-7 F-1: parse est une extraction STRUCTURELLE avec conversion EXPLICITE des
    montants (centimes -> unites). Les noms de champs metriques gardent leurs noms SOURCE
    (amount, amount_refunded, fee_amount) ; transform() applique le rename canonique
    (-> revenue, refunds, fees) depuis le manifest, donc le rename map est exerce sur CHAQUE
    pull ET sur la golden fixture (AI-54).
    """
    currency = api_charge.get("currency", "eur") or "eur"

    # created est un timestamp epoch (secondes). Grain jour = la date UTC.
    # AI-53: politique tz = jour UTC (bornage source accepte au grain jour, documente en
    # staging -- meme politique que meta/ga4). A confirmer en passe live.
    created = api_charge.get("created")
    date_str = ""
    if created not in (None, ""):
        try:
            date_str = datetime.fromtimestamp(
                int(created), tz=timezone.utc
            ).date().isoformat()
        except (ValueError, TypeError, OSError):
            date_str = ""

    # CONVERSION EXPLICITE centimes -> unites (jamais silencieuse). amount/amount_refunded/fee
    # sont EN CENTIMES cote API ; _amount_to_units divise par 100 (sauf zero-decimal).
    return {
        "date": date_str,
        "charge_id": str(api_charge.get("id", "")),
        "payment_intent_id": _payment_intent_id(api_charge),
        "client_reference_id": _extract_client_reference_id(api_charge),
        # SOURCE metric names kept here; transform() renames them to canonical (manifest).
        "amount": _amount_to_units(api_charge.get("amount", 0), currency),
        "amount_refunded": _amount_to_units(api_charge.get("amount_refunded", 0), currency),
        "fee_amount": _amount_to_units(_extract_fees(api_charge), currency),
        "transaction_count": 1,
        "order_count": 1,
        "revenue_source_currency": (currency or "EUR").upper(),
    }


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
) -> dict:
    """Fetch Stripe charges and land rows in raw_stripe_payments.

    # AD-12: called by the queue worker only — no synchronous API call during an LLM tool call.
    # AD-3: the secret key is used immediately as a Bearer header then discarded —
    #        it is NEVER stored, logged, or passed further (AD-3, NOT the Google flow AD-21).
    # AD-7: pull_id is minted by the caller; this function receives it and never mints its own.

    Parameters
    ----------
    connection_id:
        Nango connection identifier passed to get_fresh_token (provider='stripe').
    date_from, date_to:
        ISO-8601 date strings (YYYY-MM-DD) for the created[gte] / created[lte] window.
    project_id:
        Project binding for the raw rows.
    pull_id:
        Core-minted pull ULID (AD-7). Passed in; not generated here.

    Returns
    -------
    dict
        {"pull_id": str, "row_count": int, "date_from": str, "date_to": str}

    Raises
    ------
    RuntimeError
        If the Stripe API returns a non-200 (non-429) response.
    RateLimitError
        If the Stripe API returns HTTP 429 (rate limit exceeded).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # AD-3: secret obtained immediately before use; falls out of scope after the call.
    secret_key = nango_client.get_fresh_token(connection_id, provider="stripe")

    url = f"{STRIPE_API_BASE}{_CHARGES_PATH}"
    headers = {
        # AD-3: Stripe uses a Bearer secret key (restricted read-only key recommended).
        "Authorization": f"Bearer {secret_key}",
        "Stripe-Version": STRIPE_API_VERSION,
    }
    # AI-53 verifie 2026-07-19: window par created[gte]/created[lte] (epoch seconds),
    # limit<=100, expand[]=data.balance_transaction pour porter la fee. A confirmer live.
    from datetime import date as _date  # noqa: PLC0415

    gte = int(
        datetime(
            *_date.fromisoformat(date_from).timetuple()[:3], tzinfo=timezone.utc
        ).timestamp()
    )
    # created[lte] = fin de journee inclusive (23:59:59 UTC du date_to).
    lte = int(
        datetime(
            *_date.fromisoformat(date_to).timetuple()[:3], 23, 59, 59, tzinfo=timezone.utc
        ).timestamp()
    )
    params: dict = {
        "created[gte]": gte,
        "created[lte]": lte,
        "limit": _PAGE_LIMIT,
        "expand[]": "data.balance_transaction",
    }

    all_charges: list[dict] = []
    seen_cursors: set[str] = set()
    pages = 0
    starting_after: str | None = None
    while True:
        # review-15-7 (pattern shopify F-2 / tiktok): boucle BORNEE. Un serveur/mock
        # renvoyant un curseur non-progressant ou has_more toujours vrai ne doit JAMAIS
        # bloquer le worker. Borne _MAX_PAGES + garde sur curseur deja vu.
        if starting_after is not None and starting_after in seen_cursors:
            logger.warning(
                "stripe_pull: non-progressing cursor detected, stopping "
                "(pull_id=%s pages=%d)", pull_id, pages,
            )
            break
        if starting_after is not None:
            seen_cursors.add(starting_after)
        if pages >= _MAX_PAGES:
            logger.warning(
                "stripe_pull: page cap %d reached, stopping (pull_id=%s)",
                _MAX_PAGES, pull_id,
            )
            break
        pages += 1

        page_params = dict(params)
        if starting_after is not None:
            page_params["starting_after"] = starting_after
        resp = httpx.get(url, params=page_params, headers=headers, timeout=30.0)

        if resp.status_code == 429:
            # AI-53 verifie 2026-07-19: Stripe renvoie HTTP 429 (code 'rate_limit') au
            # depassement. "stripe" vit dans le MODULE (pas dans core) -- AD-2 compliant.
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(float(retry_after_raw)) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("stripe", retry_after)

        if resp.status_code != 200:
            # Story 25.2/25.7: canonical typed error; provider payload preserved.
            # Pass the manifest error_map so Stripe codes (e.g. api_key_expired ->
            # auth_expired, invalid_api_key -> auth_revoked) refine the HTTP class.
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body, _load_error_map())

        payload = resp.json()
        page_charges = payload.get("data") or []
        all_charges.extend(page_charges)

        # Curseur : Stripe pagine via starting_after=<id du dernier objet> tant que
        # has_more est vrai. Pas d'objets -> stop.
        if not payload.get("has_more") or not page_charges:
            break
        last_id = page_charges[-1].get("id")
        if not last_id:
            break
        starting_after = str(last_id)

    # review-15-7 F-1: parse (source keys + conversion montants) -> transform (manifest
    # rename) -> insert. Le canonical_metric_mapping est exerce sur CHAQUE pull.
    raw_rows: list[dict] = [_parse_charge(c) for c in all_charges]
    canonical_rows: list[dict] = transform(raw_rows)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no secret in log — only pull_id and row_count (safe metadata).
    logger.info("stripe_pull_completed: pull_id=%s row_count=%d", pull_id, row_count)

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
    """True if *row* is a raw Stripe Charge ({object:'charge'} or has 'created'+'amount')."""
    if row.get("object") == "charge":
        return True
    # A parsed row already carries source metric names (amount + date already extracted);
    # an API charge carries the epoch 'created' AND the raw 'amount' without a 'date' key.
    return "created" in row and "date" not in row


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map source fields to canonical names using manifest mappings.

    # AD-4: canonical field mapping driven by manifest — do not hardcode source names.

    review-15-7 F-2 (AI-54 honest, pattern tiktok): transform() is the module's
    canonicalization entrypoint (the shared conformance Layer 4 harness calls transform()
    directly on golden_pull.json). golden_pull.json is the REAL Stripe Charge API shape
    ({object:'charge', created, amount(centimes), balance_transaction.fee, ...}), so
    transform() first routes API-shaped rows through _parse_charge() (structural extraction
    + EXPLICIT centimes->units conversion, keeping SOURCE names like amount) THEN applies
    the canonical rename (amount -> revenue, amount_refunded -> refunds, fee_amount -> fees)
    from the manifest. This genuinely exercises _parse_charge on the golden fixture, not just
    the respx mocks. Already-parsed rows (source-name dicts, e.g. from the live pull path)
    pass straight to the rename step. Structural fields pass through unchanged.
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

    # F-2: parse API-shaped Charge rows to the canonical source-name record first
    # (centimes->units conversion happens in _parse_charge). Already-parsed rows pass through.
    parsed_rows = [
        _parse_charge(row) if _is_api_shaped(row) else row for row in raw_rows
    ]

    result: list[dict] = []
    for row in parsed_rows:
        canonical_row: dict = {}
        for key, value in row.items():
            canonical_row[rename_map.get(key, key)] = value
        result.append(canonical_row)
    return result


# ---------------------------------------------------------------------------
# Story 25.9: catalog_driven execution — pull_catalog_daily (PROJECTION style).
# Fetches /v1/charges + expand[]=data.balance_transaction (same as pull()).
# Selection controls which catalog fields are projected into landed rows.
# Fetchable: CHARGE, BALANCE TRANSACTION, PLATFORM CONTRACT.
# Excluded: CUSTOMER (id only), INVOICE, SUBSCRIPTION, REFUND, PAYOUT.
# ---------------------------------------------------------------------------


def _extract_dotted(obj: dict, path: str):
    """Extract from *obj* via a dotted *path* (e.g. 'outcome.risk_score'). Returns None if absent."""
    parts = path.split(".")
    current = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _project_charge(api_charge: dict, selection: dict) -> list[dict]:
    """Project one charge into catalog-driven rows (one per selected field).

    Sections: PLATFORM CONTRACT fields from _parse_charge (revenue/refunds/fees);
    BALANCE TRANSACTION fields from charge.balance_transaction; CHARGE fields
    via parsed dict + dotted-path fallback on raw api_charge.
    """
    catalog: dict[str, dict] = selection.get("_catalog_fields", {})
    source_fields: dict[str, str] = selection.get("source_fields", {})
    selected_metrics: list[str] = selection.get("metrics", [])
    selected_dimensions: list[str] = selection.get("dimensions", [])

    parsed = _parse_charge(api_charge)
    charge_date = parsed.get("date", "")
    charge_id = parsed.get("charge_id", "")

    bt = api_charge.get("balance_transaction")
    if not isinstance(bt, dict):
        bt = {}

    rows: list[dict] = []

    for field_id in selected_metrics:
        src = source_fields.get(field_id, field_id)
        section = catalog.get(field_id, {}).get("section", "")
        if section == "PLATFORM CONTRACT":
            if field_id == "revenue":
                value = parsed.get("amount")
            elif field_id == "refunds":
                value = parsed.get("amount_refunded")
            elif field_id == "fees":
                value = parsed.get("fee_amount")
            else:
                value = parsed.get(src)
        elif section == "BALANCE TRANSACTION":
            value = _extract_dotted(bt, src)
        else:
            value = parsed.get(src)
            if value is None:
                value = _extract_dotted(api_charge, src)
        rows.append({"row_type": "metric", "field_id": field_id, "value": value, "date": charge_date, "charge_id": charge_id})  # noqa: E501

    for field_id in selected_dimensions:
        if field_id == "date":
            continue
        src = source_fields.get(field_id, field_id)
        section = catalog.get(field_id, {}).get("section", "")
        if section == "BALANCE TRANSACTION":
            value = _extract_dotted(bt, src)
        else:
            value = parsed.get(src)
            if value is None and src != field_id:
                value = _extract_dotted(api_charge, src)
            if value is None:
                value = parsed.get(field_id)
            if value is None:
                value = _extract_dotted(api_charge, src)
        rows.append({"row_type": "dimension", "field_id": field_id, "value": value, "date": charge_date, "charge_id": charge_id})  # noqa: E501

    return rows


_CATALOG_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_stripe_catalog_daily (
    date VARCHAR, charge_id VARCHAR, field_id VARCHAR,
    row_type VARCHAR, value VARCHAR, pull_id VARCHAR,
    loaded_at VARCHAR, project_id VARCHAR
)
"""

_CATALOG_RAW_INSERT_SQL = """
INSERT INTO raw_stripe_catalog_daily
    (date, charge_id, field_id, row_type, value, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_catalog_rows(rows: list[dict], pull_id: str, loaded_at: str, project_id: str, db_mode: str, duckdb_path: str) -> int:  # noqa: E501
    """Land projected rows into raw_stripe_catalog_daily (DuckDB only at P-dev)."""
    if db_mode != "duckdb":
        raise ValueError(f"_insert_catalog_rows: unsupported db_mode {db_mode!r}")
    from core import warehouse_write  # noqa: PLC0415

    con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
    con.execute(_CATALOG_RAW_CREATE_DDL)
    values = [(r.get("date", ""), r.get("charge_id", ""), r.get("field_id", ""), r.get("row_type", ""), str(r["value"]) if r.get("value") is not None else None, pull_id, loaded_at, project_id) for r in rows]  # noqa: E501
    if values:
        con.executemany(_CATALOG_RAW_INSERT_SQL, values)
    con.close()
    return len(values)


def pull_catalog_daily(  # noqa: PLR0912
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
) -> dict:
    """Story 25.9: catalog_driven daily pull for Stripe (PROJECTION style).

    Fetches /v1/charges with expand[]=data.balance_transaction (same objects as
    pull()). Selection controls which catalog fields land in raw_stripe_catalog_daily.
    Excluded sections: CUSTOMER, INVOICE, SUBSCRIPTION, REFUND, PAYOUT (not fetched).
    """
    from core import nango_client  # noqa: PLC0415

    if selection is None:
        from core.catalog_contract import catalog_default_selection, validate_selection  # noqa: PLC0415

        _cat = json.loads((Path(__file__).parent / "api_catalog.json").read_text(encoding="utf-8"))
        selection, _ = validate_selection(_cat, catalog_default_selection(_cat))

    if "_catalog_fields" not in selection:
        _cat_raw = json.loads((Path(__file__).parent / "api_catalog.json").read_text(encoding="utf-8"))
        selection = dict(selection)
        selection["_catalog_fields"] = {f["field_id"]: f for f in _cat_raw.get("fields", [])}

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()
    secret_key = nango_client.get_fresh_token(connection_id, provider="stripe")
    url = f"{STRIPE_API_BASE}{_CHARGES_PATH}"
    headers = {"Authorization": f"Bearer {secret_key}", "Stripe-Version": STRIPE_API_VERSION}

    from datetime import date as _date  # noqa: PLC0415

    gte = int(datetime(*_date.fromisoformat(date_from).timetuple()[:3], tzinfo=timezone.utc).timestamp())
    lte = int(datetime(*_date.fromisoformat(date_to).timetuple()[:3], 23, 59, 59, tzinfo=timezone.utc).timestamp())
    params: dict = {"created[gte]": gte, "created[lte]": lte, "limit": _PAGE_LIMIT, "expand[]": "data.balance_transaction"}

    all_charges: list[dict] = []
    seen_cursors: set[str] = set()
    pages = 0
    starting_after: str | None = None
    while True:
        if starting_after is not None and starting_after in seen_cursors:
            logger.warning("stripe_catalog_pull: non-progressing cursor (pull_id=%s pages=%d)", pull_id, pages)
            break
        if starting_after is not None:
            seen_cursors.add(starting_after)
        if pages >= _MAX_PAGES:
            logger.warning("stripe_catalog_pull: page cap %d reached (pull_id=%s)", _MAX_PAGES, pull_id)
            break
        pages += 1
        page_params = dict(params)
        if starting_after is not None:
            page_params["starting_after"] = starting_after
        resp = httpx.get(url, params=page_params, headers=headers, timeout=30.0)
        if resp.status_code == 429:
            from core.quota import RateLimitError  # noqa: PLC0415

            ra = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(float(ra)) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("stripe", retry_after)
        if resp.status_code != 200:
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body, _load_error_map())
        payload = resp.json()
        page_charges = payload.get("data") or []
        all_charges.extend(page_charges)
        if not payload.get("has_more") or not page_charges:
            break
        last_id = page_charges[-1].get("id")
        if not last_id:
            break
        starting_after = str(last_id)

    projected_rows: list[dict] = []
    for api_charge in all_charges:
        projected_rows.extend(_project_charge(api_charge, selection))

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_catalog_rows(projected_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path)
    logger.info("stripe_catalog_pull_completed: pull_id=%s row_count=%d charges=%d metrics=%d dimensions=%d", pull_id, row_count, len(all_charges), len(selection.get("metrics", [])), len(selection.get("dimensions", [])))
    return {"pull_id": pull_id, "row_count": row_count, "date_from": date_from, "date_to": date_to}


# ---------------------------------------------------------------------------
# Story 15.7: register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# The raw table name never appears in core source code (AD-2).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_stripe_payments", provider="stripe")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
