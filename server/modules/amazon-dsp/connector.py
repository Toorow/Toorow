"""Standalone Amazon DSP Reporting v3 connector."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

# Import au niveau module (et non paresseux comme les appels a `core` dans les
# fonctions) : les classes d'exception ci-dessous en HERITENT, donc il doit etre
# resolu au moment ou le fichier est lu.
from core import pull_errors
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("amazon-dsp")

PROVIDER_API_VERSION = "reporting-v3"
REGIONAL_HOSTS = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}
REFETCH_DAYS = (3, 14, 45)

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")

_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'amazon-dsp'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


class AmazonDspOnboardingError(pull_errors.PermissionDeniedError):
    """The connection has no usable DSP seat/account/advertiser.

    `permission_denied` : le credential est authentifie et n'atteint rien. L'action
    juste est de se reconnecter avec les bons droits, et c'est `permission_denied`
    qui la fait remonter a l'ecran (`user_action="reconnect"`).

    Avant le 2026-08-01 cette classe heritait d'un `RuntimeError` nu : le worker la
    voyait `unclassified`, la rejouait jusqu'au `dead_letter` contre un credential
    qui ne marchera jamais, et n'affichait aucune action.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message=message)


class AmazonDspNotConfiguredError(AmazonDspOnboardingError):
    """Le credential va bien -- c'est la requete qui ne peut pas etre formee.

    Derive de l'erreur d'onboarding pour qu'un `except AmazonDspOnboardingError`
    existant continue de l'attraper, mais porte `invalid_request` : dire
    << reconnecte-toi >> enverrait l'operateur au mauvais ecran, puisque le compte se
    choisit dans l'assistant Datastream et pas sur la connexion.
    """

    error_class = pull_errors.INVALID_REQUEST
    user_action = pull_errors.SELECT_SOURCE_ACCOUNT


class AmazonDspCompatibilityError(pull_errors.InvalidRequestError, ValueError):
    """The explicit report shape is not legal for the DSP report type.

    `invalid_request` : rejouer la meme requete redonne la meme reponse, et c'est
    aussi le signal `pull_invalid_request_drift` -- une forme devenue illegale est
    une derive du catalogue. `ValueError` reste dans les bases, des
    appelants et des tests l'attrapent sous ce nom.
    """

    #: -> `Mapping` : le plan demande ce que la source ne rend plus
    #: (datastream-workbench-and-wizard.md:107). L'operateur a un endroit
    #: ou aller, contrairement a une derive de version d'API.
    user_action = pull_errors.REVIEW_MAPPING

    def __init__(self, message: str) -> None:
        super().__init__(message=message)


def _manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def _sources() -> dict:
    return json.loads(
        (Path(__file__).parent / "catalog_sources" / "catalog_sources.json").read_text(
            encoding="utf-8"
        )
    )


def _columns() -> dict[str, list[str]]:
    return json.loads(
        (Path(__file__).parent / "catalog_sources" / "report_type_columns.json").read_text(
            encoding="utf-8"
        )
    )


def _client_id(value: str | None = None) -> str:
    client_id = value or os.environ.get("AMAZON_ADS_CLIENT_ID", "")
    if not client_id:
        raise AmazonDspNotConfiguredError("Amazon DSP requires the platform Amazon-Ads-ClientId")
    return client_id


def _host(region: str) -> str:
    try:
        return REGIONAL_HOSTS[region.upper()]
    except KeyError as exc:
        raise AmazonDspNotConfiguredError(f"Unsupported Amazon DSP region: {region}") from exc


def _provider_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    return str(payload.get("code") or payload.get("error") or "") or None


def _raise_response(response) -> None:
    if response.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        raw = response.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError("amazon-dsp", retry_after)
    try:
        original = response.json()
    except Exception:
        original = response.text
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and (code := _provider_code(original)):
        normalized["code"] = code
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _headers(token: str, client_id: str, ads_account_id: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Amazon-Ads-ClientId": client_id,
        "Content-Type": "application/vnd.createasyncreportrequest.v3+json",
    }
    if ads_account_id:
        headers["Amazon-Ads-AccountId"] = ads_account_id
    return headers


def _request(client, method: str, host: str, path: str, headers: dict, **kwargs):
    response = client.request(method, f"{host}{path}", headers=headers, timeout=60, **kwargs)
    if response.status_code not in (200, 202, 425):
        _raise_response(response)
    return response


def _token(connection_id: str) -> str:
    from core import nango_client  # noqa: PLC0415

    return nango_client.get_fresh_token(connection_id, provider="amazon-dsp")


#: Le separateur du triplet de routage porte par l'identifiant de compte. Meme
#: convention que le connecteur Sponsored Ads frere, dont les identifiants de
#: compte sont deja composites ('<region>:<profileId>'). Ni la region (NA/EU/FE),
#: ni un adsAccountId, ni un advertiserId Amazon ne contiennent de ':'.
_ROUTING_SEPARATOR = ":"


def routed_advertiser_id(region: str, ads_account_id: str, advertiser_id: str) -> str:
    """Le triplet minimal qui permet d'adresser un advertiser DSP, en une chaine.

    Un appel DSP Reporting v3 exige TROIS choses que l'advertiser seul ne donne
    pas : l'hote regional, l'en-tete `Amazon-Ads-AccountId`, et l'advertiser
    lui-meme. Le canal qui relie l'ecran de selection au pull n'en transporte
    qu'une (une chaine opaque). D'ou ce triplet encode.
    """
    return _ROUTING_SEPARATOR.join((region.upper(), ads_account_id, advertiser_id))


def parse_routed_advertiser_id(advertiser_id: str | None) -> tuple[str, str, str]:
    """(region, ads_account_id, advertiser_id) -- ou une erreur typee qui dit quoi faire.

    Aucun repli d'environnement ici, et c'est deliberé : `core/account_topology.py`
    les declare deprecies, et une variable est unique pour tout le deploiement --
    tous les Datastreams de tous les projets tireraient le meme advertiser, et
    aucun quand elle n'est pas posee. C'est un defaut d'isolement, pas une
    commodite.
    """
    if not advertiser_id:
        raise AmazonDspNotConfiguredError(
            "Amazon DSP requires a selected advertiser: the operator picks one in "
            "the Datastream wizard (discover_accounts lists the advertisers each "
            "regional DSP seat exposes) and the worker passes it as `advertiser_id` "
            "(manifest account_topology.pull_parameter). No advertiser was selected "
            "for this connection, and there is no deployment-wide default."
        )
    parts = advertiser_id.split(_ROUTING_SEPARATOR)
    if len(parts) != 3 or not all(parts):
        raise AmazonDspNotConfiguredError(
            f"Amazon DSP advertiser selection {advertiser_id!r} is not routable: a DSP "
            "call needs the region (regional API host), the adsAccountId "
            "(Amazon-Ads-AccountId header) and the advertiserId. `discover_accounts` "
            "mints that triple as the account id "
            "('<region>:<adsAccountId>:<advertiserId>'); re-run account discovery for "
            "this connection to refresh the selection."
        )
    region, ads_account_id, advertiser = parts
    _host(region)  # refuse an unknown region before any network call
    return region.upper(), ads_account_id, advertiser


def discover_accounts(
    connection_id: str,
    *,
    regions: tuple[str, ...] = ("NA", "EU", "FE"),
    _client=None,
    _token_value: str | None = None,
    _client_id_value: str | None = None,
) -> list[dict]:
    """Probe regional DSP account APIs; an empty seat is a typed failure."""
    token = _token_value or _token(connection_id)
    client_id = _client_id(_client_id_value)
    client = _client or httpx.Client()
    selections: list[dict] = []
    for region in regions:
        payload = _request(
            client,
            "GET",
            _host(region),
            "/accounts",
            _headers(token, client_id),
        ).json()
        for account in payload.get("accounts") or []:
            account_id = str(account.get("adsAccountId") or account.get("id") or "")
            for advertiser in account.get("advertisers") or []:
                advertiser_id = str(advertiser.get("advertiserId") or advertiser.get("id") or "")
                selections.append(
                    {
                        # L'`id` est la SEULE chose que le coeur persiste : il
                        # stocke une chaine opaque dans
                        # `app.connection_account_scope.account_id` et la rend
                        # telle quelle au pull. Les cles voisines (region,
                        # ads_account_id...) servent l'ecran de choix et
                        # s'arretent la. Un `id` indexe -- ce qu'il etait --
                        # ne routait donc rien, et designait un AUTRE advertiser
                        # des que la decouverte reordonnait la liste.
                        # La recherche l'exigeait deja nommement :
                        # << identifiant opaque portant assez d'information de
                        # region pour router sans redecouvrir a chaque pull >>.
                        "id": routed_advertiser_id(region, account_id, advertiser_id),
                        "region": region,
                        "ads_account_id": account_id,
                        "advertiser_id": advertiser_id,
                        "display_name": advertiser.get("name") or advertiser_id,
                        "timezone": advertiser.get("timezone") or "UTC",
                        "currency": advertiser.get("currency") or "",
                    }
                )
    if not selections:
        raise AmazonDspOnboardingError(
            "No Amazon DSP seat/advertiser is accessible; Sponsored Ads profiles are not used"
        )
    return selections


def validate_selection(report_type: str, group_by: str, time_unit: str, columns: list[str]) -> dict:
    spec = _sources()["report_type_compatibility"]["report_types"].get(report_type)
    if spec is None:
        raise AmazonDspCompatibilityError(f"Unknown Amazon DSP report type: {report_type}")
    invalid = sorted(set(columns) - set(_columns().get(report_type, [])))
    if group_by not in spec["group_by"] or time_unit not in spec["time_units"] or invalid:
        raise AmazonDspCompatibilityError(
            f"Illegal DSP shape report_type={report_type} group_by={group_by} "
            f"time_unit={time_unit} fields={invalid}"
        )
    if report_type == "dspBenchmarks":
        raise AmazonDspCompatibilityError(
            "dspBenchmarks requires a dedicated non-additive benchmark grain"
        )
    return spec


def split_date_windows(date_from: str, date_to: str, max_days: int) -> list[tuple[str, str]]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("date_to must not precede date_from")
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=max_days - 1))
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + timedelta(days=1)
    return windows


def build_report_request(
    report_type: str,
    group_by: str,
    columns: list[str],
    date_from: str,
    date_to: str,
    advertiser_id: str,
    *,
    time_unit: str = "DAILY",
    region: str,
    ads_account_id: str,
) -> dict:
    validate_selection(report_type, group_by, time_unit, columns)
    return {
        "startDate": date_from,
        "endDate": date_to,
        "configuration": {
            "adProduct": "AMAZON_DSP",
            "reportTypeId": report_type,
            "groupBy": [group_by],
            "columns": columns,
            "timeUnit": time_unit,
            "format": "GZIP_JSON",
            "filters": [{"field": "advertiserId", "values": [advertiser_id]}],
        },
        "routing": {
            "region": region.upper(),
            "adsAccountId": ads_account_id,
            "advertiserId": advertiser_id,
        },
    }


def canonical_request_hash(request: dict) -> str:
    stable = {key: value for key, value in request.items() if key not in {"name", "reportName"}}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class _DspReportFlow:
    def __init__(self, client, host, headers, request):
        self.client = client
        self.host = host
        self.headers = headers
        self.request = request
        self.download_url: str | None = None
        self.poll_401_count = 0

    def submit(self) -> str:
        body = {key: value for key, value in self.request.items() if key != "routing"}
        body["name"] = f"toorow-{canonical_request_hash(self.request)[:20]}"
        response = _request(
            self.client, "POST", self.host, "/reporting/reports", self.headers, json=body
        )
        payload = response.json()
        report_id = payload.get("reportId") or payload.get("existingReportId")
        if not report_id:
            raise AmazonDspCompatibilityError("Amazon DSP report creation returned no reportId")
        return str(report_id)

    def poll(self, report_id: str) -> str:
        response = self.client.request(
            "GET",
            f"{self.host}/reporting/reports/{report_id}",
            headers=self.headers,
            timeout=60,
        )
        if response.status_code == 401 and self.poll_401_count < 2:
            self.poll_401_count += 1
            return "pending"
        if response.status_code not in (200, 202):
            _raise_response(response)
        payload = response.json()
        status = str(payload.get("status") or "").upper()
        self.download_url = payload.get("url") or payload.get("location")
        return {
            "PENDING": "pending",
            "PROCESSING": "processing",
            "COMPLETED": "completed",
            "SUCCESS": "completed",
            "FAILED": "failed",
            "EXPIRED": "expired",
        }.get(status, "pending")

    def download(self, report_id: str) -> list[dict]:
        if not self.download_url:
            self.poll(report_id)
        response = httpx.get(self.download_url or "", timeout=60)
        if response.status_code in (401, 403, 404):
            self.download_url = None
            self.poll(report_id)
            response = httpx.get(self.download_url or "", timeout=60)
        response.raise_for_status()
        raw = response.content
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8-sig")
        content_type = response.headers.get("Content-Type", "")
        if "csv" in content_type or (text and text[0] not in "[{"):
            return list(csv.DictReader(io.StringIO(text)))
        payload = json.loads(text or "[]")
        return payload.get("rows") or payload if isinstance(payload, dict) else payload


def run_dsp_report(
    connection_id: str,
    request: dict,
    *,
    _client=None,
    _token_value: str | None = None,
    _client_id_value: str | None = None,
    **driver_kwargs,
) -> dict:
    from core.async_reports import AsyncReportFlow, run_async_report  # noqa: PLC0415

    routing = request["routing"]
    token = _token_value or _token(connection_id)
    headers = _headers(token, _client_id(_client_id_value), routing["adsAccountId"])
    implementation = _DspReportFlow(
        _client or httpx.Client(), _host(routing["region"]), headers, request
    )
    flow = AsyncReportFlow(
        submit=implementation.submit,
        poll=implementation.poll,
        download=implementation.download,
        request_hash=canonical_request_hash(request),
    )
    return run_async_report(flow, ledger_ref=connection_id, **driver_kwargs)


def check_account_access(
    connection_id: str,
    advertiser_id: str,
    *,
    _client=None,
    _token_value: str | None = None,
    _client_id_value: str | None = None,
) -> str:
    """Verifier l'acces a l'advertiser ROUTE ('<region>:<adsAccountId>:<advertiserId>').

    Meme entree que les connecteurs freres a compte composite (Sponsored Ads
    '<region>:<profileId>', Google Ads '<cid>@<login_cid>') : la chaine que le
    coeur persiste, pas un dictionnaire que rien ne lui transmet.
    """
    region, ads_account_id, advertiser = parse_routed_advertiser_id(advertiser_id)
    request = build_report_request(
        "dspCampaign",
        "campaign",
        ["date", "impressions"],
        date.today().isoformat(),
        date.today().isoformat(),
        advertiser,
        region=region,
        ads_account_id=ads_account_id,
    )
    flow = _DspReportFlow(
        _client or httpx.Client(),
        _host(region),
        _headers(
            _token_value or _token(connection_id),
            _client_id(_client_id_value),
            ads_account_id,
        ),
        request,
    )
    return flow.submit()


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_amazon_dsp_daily (
  region VARCHAR, ads_account_id VARCHAR, advertiser_id VARCHAR, report_type VARCHAR,
  group_by VARCHAR, date VARCHAR, dimensions_json VARCHAR, metric VARCHAR, value DOUBLE,
  provider_value VARCHAR, non_additive BOOLEAN, request_hash VARCHAR, pull_id VARCHAR,
  loaded_at VARCHAR, project_id VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_amazon_dsp_daily
    (region, ads_account_id, advertiser_id, report_type, group_by, date, dimensions_json,
    metric, value, provider_value, non_additive, request_hash, pull_id, loaded_at,
    project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _land(rows: list[dict], context: dict) -> int:
    if os.environ.get("TOOROW_DB_MODE", "duckdb") not in ("duckdb", "bigquery"):
        raise ValueError("amazon-dsp landing supports duckdb and bigquery")
    from core import warehouse_write  # noqa: PLC0415

    path = os.environ.get("TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "local.duckdb"))
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    selected_columns = set(context["columns"])
    non_additive_tokens = ("rate", "ratio", "average", "reach", "frequency", "roas")
    values = []
    for row in rows:
        dimensions = {
            key: value
            for key, value in row.items()
            if key in selected_columns and not isinstance(value, (int, float))
        }
        row_date = str(row.get("date") or row.get("intervalStart") or context["date_from"])
        for metric, provider_value in row.items():
            if metric not in selected_columns or metric in dimensions:
                continue
            try:
                value = float(provider_value)
            except (TypeError, ValueError):
                continue
            values.append(
                (
                    context["region"],
                    context["ads_account_id"],
                    context["advertiser_id"],
                    context["report_type"],
                    context["group_by"],
                    row_date,
                    json.dumps(dimensions, sort_keys=True, separators=(",", ":")),
                    metric,
                    value,
                    str(provider_value),
                    any(token in metric.lower() for token in non_additive_tokens),
                    context["request_hash"],
                    context["pull_id"],
                    loaded_at,
                    context["project_id"],
                )
            )
    connection = warehouse_write.open_raw_writer(path, project_id=context["project_id"])
    connection.execute(_RAW_DDL)
    if values:
        connection.executemany(_RAW_INSERT_SQL, values)
    connection.close()
    return len(values)


def _pull_profile(
    connection_id, date_from, date_to, project_id, pull_id, profile, selection, advertiser_id
):
    from core.pull_errors import ProviderTransientError  # noqa: PLC0415

    # Le COMPTE vient du parametre declare ; `selection` ne porte que la forme du
    # RAPPORT. Les deux objets s'appelaient pareil et ce n'etaient pas les memes :
    # celui que le plan fournit est ferme sur selection_mode / metrics /
    # dimensions / grain / filters (datastream-intent.schema.json,
    # additionalProperties: false), donc il ne pouvait ni porter un advertiser,
    # ni un adsAccountId, ni une region.
    region, ads_account_id, advertiser = parse_routed_advertiser_id(advertiser_id)
    selection = selection or {}
    report_type = selection.get("report_type", "dspCampaign")
    group_by = selection.get("group_by", "campaign")
    columns = selection.get("columns") or [
        "date",
        "advertiserId",
        "impressions",
        "clicks",
        "totalCost",
        "purchases",
        "sales",
    ]
    time_unit = selection.get("time_unit", "DAILY")
    spec = validate_selection(report_type, group_by, time_unit, columns)
    total = 0
    for window_from, window_to in split_date_windows(
        date_from, date_to, int(spec["max_range_days"])
    ):
        request = build_report_request(
            report_type,
            group_by,
            columns,
            window_from,
            window_to,
            advertiser,
            time_unit=time_unit,
            region=region,
            ads_account_id=ads_account_id,
        )
        outcome = run_dsp_report(connection_id, request)
        if outcome["status"] != "completed":
            raise ProviderTransientError(
                message=f"Amazon DSP report deferred for {outcome.get('report_ref')}"
            )
        context = {
            **selection,
            "region": region,
            "ads_account_id": ads_account_id,
            "advertiser_id": advertiser,
            "report_type": report_type,
            "group_by": group_by,
            "columns": columns,
            "date_from": window_from,
            "request_hash": canonical_request_hash(request),
            "pull_id": pull_id,
            "project_id": project_id,
        }
        total += _land(outcome["rows"], context)
    return {
        "pull_id": pull_id,
        "row_count": total,
        "date_from": date_from,
        "date_to": date_to,
        "refetch_days": list(REFETCH_DAYS),
        "profile": profile,
    }


def pull(
    connection_id,
    date_from,
    date_to,
    project_id,
    pull_id,
    selection=None,
    # OPTIONNEL, jamais requis. Le worker ne passe le compte que si une selection
    # existe (core/queue.py) : en positionnel requis, un Datastream sans selection
    # leverait un `TypeError` nu, hors de toute taxonomie, au lieu de l'erreur
    # typee qui nomme ce qui manque.
    advertiser_id=None,
):
    """Pull par defaut -- profil `campaign_daily`.

    `advertiser_id` porte le choix de l'operateur sous la forme routee que
    `discover_accounts` a mintee ('<region>/<adsAccountId>/<advertiserId>') :
    un advertiserId nu ne suffit pas a adresser l'API DSP.
    """
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "campaign_daily",
        selection,
        advertiser_id,
    )


def pull_campaign_daily(
    connection_id, date_from, date_to, project_id, pull_id, selection=None, advertiser_id=None
):
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "campaign_daily",
        selection,
        advertiser_id,
    )


def pull_catalog_daily(
    connection_id, date_from, date_to, project_id, pull_id, selection=None, advertiser_id=None
):
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "catalog_daily",
        selection,
        advertiser_id,
    )


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


def _get_mart_table(db_mode: str, project_id: str | None) -> str:
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(project_id)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


def _query_duckdb(sql: str, params: list, duckdb_path: str) -> list[dict]:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _query_bigquery(sql: str, params: dict) -> list[dict]:
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


def _query_mart_dsp(date_from: str, date_to: str, project_id: str = "default") -> list[dict]:
    """Query the mart for Amazon DSP data (AD-12: reads fact_daily_kpi only)."""
    db_mode = _get_db_mode()
    table = _get_mart_table(db_mode, project_id)
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


def _build_envelope_dsp(
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
        data_by_metric.setdefault(r["metric"], []).append(
            {
                "breakdown_dimension": r["breakdown_dimension"],
                "breakdown_value": r["breakdown_value"],
                "value": r["value"],
            }
        )

    provenance = (
        {
            "source_system": "amazon-dsp",
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


def _adapt_languages(raw_row: dict, canonical_row: dict, manifest: dict) -> None:
    """Resolve this row's language dimensions through the SHARED adapter.

    Story 27.8: `language`/`languageCode`-style pairs are two ENCODINGS of one
    dimension. The generic rename map above is a dict, so without this call the
    last field of manifest.json silently won and a display name such as 'English'
    could be published as a canonical value. core.language_dimensions owns the
    rule (governance.md, "an encoding is not a dimension"); core -> module is the
    direction AD-2 allows.
    """
    from core.language_dimensions import adapt_manifest_row_languages  # noqa: PLC0415

    adapt_manifest_row_languages(raw_row, canonical_row, manifest)


def transform(raw_rows: list[dict]) -> list[dict]:
    manifest = _manifest()
    mappings = manifest["canonical_metric_mapping"] | manifest["canonical_dimension_mapping"]
    result: list[dict] = []
    for row in raw_rows:
        canonical = {
            (
                mappings.get(key, key)
                if isinstance(mappings.get(key, key), str)
                else mappings[key]["canonical"]
            ): value
            for key, value in row.items()
        }
        _adapt_languages(row, canonical, manifest)
        result.append(canonical)
    return result


@mcp_app.tool()
def get_amazon_dsp_report(
    project_id: str = "default",
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed Amazon DSP report envelope -- reads from fact_daily_kpi mart.

    Report profiles: campaign_daily, catalog_daily
    Metrics: impressions, clicks, totalCost, purchases, sales (additive at day grain)
    Breakdown dimensions: advertiser_id

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    # AD-4: DSP conversion columns (purchases/sales) are restated by Amazon
    # after the conversion event (refetch ladder 3/14/45 days).

    Parameters:
        project_id: Project identifier (default: 'default')
        report_profile: One of the declared profiles (campaign_daily, catalog_daily)
        date_from: Start date ISO-8601 (e.g. '2026-06-01'). Defaults to 30 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=30)).isoformat()

    try:
        rows = _query_mart_dsp(date_from, date_to, project_id)
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

    return _build_envelope_dsp(rows, report_profile, date_from, date_to, project_id)


# ---------------------------------------------------------------------------
# Register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_amazon_dsp_daily", provider="amazon-dsp")
except Exception:
    pass  # best-effort; verification logs a warning if the table name is missing
