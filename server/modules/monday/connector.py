"""monday.com GraphQL connector: board snapshots and change events."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

# Import au niveau module (et non paresseux comme les appels a `core` dans les
# fonctions) : les classes d'exception ci-dessous en HERITENT, donc il doit etre
# resolu au moment ou le fichier est lu.
from core import pull_errors
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("monday")
API_URL = "https://api.monday.com/v2"
API_VERSION = "2026-07"
PLAN_BUDGETS = {
    "free": {"daily": 1000, "minute": 1000, "concurrency": 40, "ip_10_seconds": 5000},
    "basic": {"daily": 1000, "minute": 1000, "concurrency": 40, "ip_10_seconds": 5000},
    "pro": {"daily": 10000, "minute": 2500, "concurrency": 100, "ip_10_seconds": 5000},
    "enterprise": {"daily": 25000, "minute": 5000, "concurrency": 250, "ip_10_seconds": 5000},
}
KNOWN_COLUMN_TYPES = {
    "board-relation",
    "button",
    "checkbox",
    "color-picker",
    "country",
    "date",
    "dependency",
    "dropdown",
    "email",
    "file",
    "hour",
    "item-id",
    "last-updated",
    "link",
    "location",
    "long-text",
    "mirror",
    "name",
    "numbers",
    "people",
    "phone",
    "progress",
    "rating",
    "status",
    "subtasks",
    "tags",
    "team",
    "text",
    "timeline",
    "time-tracking",
    "vote",
    "week",
    "world-clock",
}


class ApiVersionMismatch(pull_errors.InvalidRequestError):
    """L'API a repondu sous une autre version que celle sur laquelle on est epingle.

    `invalid_request` : c'est LA definition de la derive de catalogue, et cette
    classe est la seule a emettre `pull_invalid_request_drift`. Rejouer ne change
    rien tant que le connecteur n'a pas ete releve sur la nouvelle version.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message=message)


class MondayGraphQLError(pull_errors.ConnectorError):
    """monday repond HTTP 200 et met l'echec dans `errors[].extensions.code`.

    CE QUE CETTE CLASSE SAVAIT DEJA, ET PERDAIT. Ses sites de levee nomment la
    bonne reponse en toutes lettres -- `MondayGraphQLError("auth_expired", ...)`
    -- mais elle heritait d'un `RuntimeError` nu, donc le worker ne voyait qu'une
    exception generique : `unclassified`, rejouee jusqu'au `dead_letter` contre un
    token qui ne marchera jamais, et aucun << reconnecte-toi >> a l'ecran.

    `code` reste ce que monday dit ; `error_class` devient ce que le worker lit.
    La correspondance est ci-dessous, et la taxonomie n'est PAS retranscrite ici :
    on nomme une classe canonique et `core.pull_errors.error_for_class` la
    construit, pour qu'`ERROR_CLASSES` reste le seul endroit ou elle est ecrite.

    `rate_limited` n'y figure pas volontairement : une limite de debit doit lever
    `core.quota.RateLimitError`, la seule que le disjoncteur de quota reconnait.
    """

    #: code monday -> classe canonique. `resource_not_found` est `invalid_request`
    #: (la selection designe un tableau qui n'existe plus : rejouer redonne la
    #: meme reponse) ; `invalid_response` est transitoire (une reponse non-objet
    #: est un incident de passerelle, pas une requete illegale).
    _CANONICAL = {
        "auth_expired": pull_errors.AUTH_EXPIRED,
        "permission_denied": pull_errors.PERMISSION_DENIED,
        "invalid_request": pull_errors.INVALID_REQUEST,
        "resource_not_found": pull_errors.INVALID_REQUEST,
        "invalid_response": pull_errors.PROVIDER_TRANSIENT,
        "provider_error": pull_errors.PROVIDER_TRANSIENT,
    }

    def __init__(self, code: str, message: str, *, retry_after: int | None = None, raw=None):
        super().__init__(provider_payload=raw, message=message)
        self.code = code
        self.retry_after = retry_after
        self.raw = raw
        canonical = pull_errors.error_for_class(
            self._CANONICAL.get(code, pull_errors.UNCLASSIFIED)
        )
        self.error_class = canonical.error_class
        self.user_action = canonical.user_action
        self.retryable = canonical.retryable


_ERROR_CODES = {
    "COMPLEXITY_BUDGET_EXHAUSTED": "rate_limited",
    "DAILY_LIMIT_EXCEEDED": "rate_limited",
    "MINUTE_LIMIT_EXCEEDED": "rate_limited",
    "CONCURRENCY_LIMIT_EXCEEDED": "rate_limited",
    "IP_RATE_LIMIT_EXCEEDED": "rate_limited",
    "UNAUTHENTICATED": "auth_expired",
    "AUTHENTICATION_ERROR": "auth_expired",
    "FORBIDDEN": "permission_denied",
    "PERMISSION_DENIED": "permission_denied",
    "NOT_FOUND": "resource_not_found",
    "RESOURCE_PROTECTION": "rate_limited",
}


def _header(headers, name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value)
    return None


def _parse_rate_limit(headers) -> dict[str, Any]:
    result: dict[str, Any] = {
        "api_version": _header(headers, "API-Version"),
        "policy": _header(headers, "RateLimit-Policy"),
        "rate_limit": _header(headers, "RateLimit"),
    }
    retry = _header(headers, "Retry-After")
    if retry and retry.isdigit():
        result["retry_after"] = int(retry)
    for header_name in ("policy", "rate_limit"):
        raw = result.get(header_name) or ""
        parsed = {}
        for part in raw.replace(";", ",").split(","):
            key, separator, value = part.strip().partition("=")
            if separator and value.strip().isdigit():
                parsed[key.strip().lower()] = int(value.strip())
        result[f"{header_name}_values"] = parsed
    return result


def ensure_budget(quota: dict[str, Any]) -> None:
    """Stop pagination before known complexity or header budgets are exhausted.

    Leve `core.quota.RateLimitError` -- et non plus un `MondayGraphQLError` portant
    la chaine `"rate_limited"`. C'est le TYPE que `queue.py` attrape pour remettre
    le job en file et alimenter le disjoncteur ; une exception qui dit seulement
    << rate_limited >> dans un attribut tombait dans le filet generique, consommait
    une tentative et laissait le disjoncteur aveugle a toute limite monday.
    """
    from core.quota import RateLimitError  # noqa: PLC0415

    complexity = quota.get("complexity") or {}
    if complexity and int(complexity.get("after", 1)) <= 0:
        raise RateLimitError("monday", complexity.get("reset_in_x_seconds"))
    remaining = (quota.get("rate_limit_values") or {}).get("remaining")
    if remaining is not None and remaining <= 0:
        raise RateLimitError("monday", quota.get("retry_after"))


_ERROR_MAP: dict | None = None


def _load_error_map() -> dict:
    """L'`error_map` du manifeste (cle "<statut>:<error_code>"), en cache.

    monday publie un tableau de codes (developer.monday.com, << Error codes >>)
    et met le sien dans un `error_code` de PREMIER NIVEAU, que
    `core.pull_errors._extract_provider_codes` lit deja : aucune normalisation
    n'est necessaire ici. Seuls les codes portes par un statut NON-2xx sont
    declares -- ceux de la couche GraphQL voyagent sur un HTTP 200 et sont traites
    dans `graphql_request` / `_ERROR_CODES`, la ou ils s'appliquent.
    """
    global _ERROR_MAP
    if _ERROR_MAP is None:
        from pathlib import Path  # noqa: PLC0415

        manifest = json.loads(
            (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
        )
        _ERROR_MAP = manifest.get("error_map") or {}
    return _ERROR_MAP


def _response_json(response):
    payload = response.json()
    if not isinstance(payload, dict):
        raise MondayGraphQLError("invalid_response", "monday returned a non-object payload")
    return payload


def graphql_request(client, token: str, query: str, variables: dict | None = None):
    response = client.post(
        API_URL,
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
            "API-Version": API_VERSION,
        },
        json={"query": query, "variables": variables or {}},
        timeout=60.0,
    )
    quota = _parse_rate_limit(response.headers)
    actual_version = quota["api_version"]
    if actual_version != API_VERSION:
        raise ApiVersionMismatch(f"expected API-Version {API_VERSION}, got {actual_version!r}")
    payload = _response_json(response)
    if response.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        raise RateLimitError("monday", quota.get("retry_after"))
    if response.status_code >= 400:
        # Un echec purement HTTP n'a pas besoin du vocabulaire GraphQL de monday :
        # le classifieur de core le type deja (401/403/400/5xx), et l'`error_map`
        # du manifeste le raffine si le provider donne un code. Le fourre-tout
        # `provider_error` d'avant reclassait tout en transitoire, donc un 400
        # -- une derive de catalogue -- etait rejoue jusqu'au dead_letter.
        #
        # 401 ET 403 PASSENT ICI DEPUIS 67-21. Ils partaient avant en
        # `MondayGraphQLError("auth_expired" / "permission_denied")` AVANT que core
        # ne voie la reponse : le worker recevait une exception sans `error_class`
        # canonique, et surtout aucune cle `401:` ou `403:` de l'`error_map` ne
        # pouvait tirer. Les deux cas que monday distingue et que le pur HTTP ne
        # distingue pas -- un jeton REVOQUE (monday n'expire pas ses jetons OAuth)
        # et une restriction d'IP a laquelle se reconnecter ne changera rien --
        # n'atteignaient donc personne.
        raise pull_errors.classify_http_error(
            response.status_code, payload, _load_error_map()
        )
    errors = payload.get("errors") or []
    if errors:
        first = errors[0]
        extensions = first.get("extensions") or {}
        provider_code = str(extensions.get("code") or "GRAPHQL_ERROR").upper()
        retry_after = extensions.get("retry_in_seconds") or quota.get("retry_after")
        canonical = _ERROR_CODES.get(provider_code, "invalid_request")
        if canonical == "rate_limited":
            # C'est ICI que monday dit ses limites : HTTP 200, verdict dans le
            # corps. Cinq de ses onze codes sont des limites de debit, et sans ce
            # branchement aucune d'elles n'atteignait le disjoncteur.
            from core.quota import RateLimitError  # noqa: PLC0415

            raise RateLimitError(
                "monday", int(retry_after) if retry_after is not None else None
            )
        raise MondayGraphQLError(
            canonical,
            str(first.get("message") or provider_code),
            retry_after=int(retry_after) if retry_after is not None else None,
            raw=errors,
        )
    data = payload.get("data") or {}
    quota["complexity"] = data.pop("complexity", None)
    return data, quota


def _token(connection_id: str) -> str:
    from core import nango_client  # noqa: PLC0415

    return nango_client.get_fresh_token(connection_id, provider="monday")


_DISCOVERY_QUERY = """
query ToorowDiscovery {
  complexity { query before after reset_in_x_seconds }
  me { id name account { id name } }
  workspaces { id name kind }
  boards(limit: 500) { id name state board_kind workspace_id }
}
"""


def discover_accounts(connection_id: str, *, _token: str | None = None, _client=None):
    token = _token or globals()["_token"](connection_id)
    client = _client or httpx.Client()
    data, _ = graphql_request(client, token, _DISCOVERY_QUERY)
    account = (data.get("me") or {}).get("account") or {}
    workspaces = [dict(workspace) for workspace in data.get("workspaces") or []]
    boards = []
    for board in data.get("boards") or []:
        boards.append(
            {
                "id": str(board.get("id")),
                "name": board.get("name") or str(board.get("id")),
                "workspace_id": None
                if board.get("workspace_id") is None
                else str(board["workspace_id"]),
                "state": board.get("state"),
                "kind": board.get("board_kind"),
            }
        )
    return [
        {
            "account_id": str(account.get("id") or "token-account"),
            "name": account.get("name") or "monday account",
            "workspaces": workspaces,
            "boards": boards,
        }
    ]


_SNAPSHOT_QUERY = """
query ToorowBoardSnapshot($boardIds: [ID!]!, $limit: Int!) {
  complexity { query before after reset_in_x_seconds }
  boards(ids: $boardIds) {
    id name
    columns { id title type settings_str }
    groups { id title }
    items_page(limit: $limit) {
      cursor
      items { id name updated_at group { id } parent_item { id }
        column_values { id type text value }
        subitems { id name updated_at group { id } parent_item { id }
          column_values { id type text value }
        }
      }
    }
  }
}
"""

_NEXT_ITEMS_QUERY = """
query ToorowNextItems($cursor: String!, $limit: Int!) {
  complexity { query before after reset_in_x_seconds }
  next_items_page(cursor: $cursor, limit: $limit) {
    cursor
    items { id name updated_at group { id } parent_item { id }
      column_values { id type text value }
      subitems { id name updated_at group { id } parent_item { id }
        column_values { id type text value }
      }
    }
  }
}
"""


def _json_value(value):
    if isinstance(value, (dict, list, int, float, bool)) or value is None:
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {"unparsed": value}


def build_board_catalog(board: dict) -> dict:
    columns = []
    for column in board.get("columns") or []:
        columns.append(
            {
                "id": str(column.get("id")),
                "title": column.get("title") or str(column.get("id")),
                "type": column.get("type") or "unknown",
                "settings": _json_value(column.get("settings_str")),
                "known_type": column.get("type") in KNOWN_COLUMN_TYPES,
            }
        )
    canonical = json.dumps(columns, sort_keys=True, separators=(",", ":"))
    return {
        "board_id": str(board.get("id")),
        "columns": columns,
        "fingerprint": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def _normalise_item(board_id: str, item: dict) -> dict:
    values = []
    for value in item.get("column_values") or []:
        values.append(
            {
                "id": str(value.get("id")),
                "type": value.get("type") or "unknown",
                "text": value.get("text"),
                "raw_value": _json_value(value.get("value")),
                "known_type": value.get("type") in KNOWN_COLUMN_TYPES,
            }
        )
    return {
        "board_id": str(board_id),
        "item_id": str(item.get("id")),
        "item_name": item.get("name"),
        "group_id": str((item.get("group") or {}).get("id") or ""),
        "parent_item_id": str((item.get("parent_item") or {}).get("id") or ""),
        "updated_at": item.get("updated_at"),
        "column_values": values,
    }


def _normalise_items(board_id: str, items: list[dict]) -> list[dict]:
    rows = []
    for item in items:
        rows.append(_normalise_item(board_id, item))
        rows.extend(_normalise_items(board_id, item.get("subitems") or []))
    return rows


def fetch_board_snapshot(client, token: str, board_id: str):
    data, quota = graphql_request(
        client, token, _SNAPSHOT_QUERY, {"boardIds": [str(board_id)], "limit": 500}
    )
    boards = data.get("boards") or []
    if not boards:
        raise MondayGraphQLError("resource_not_found", f"board {board_id!r} was not returned")
    board = boards[0]
    page = board.get("items_page") or {}
    rows = _normalise_items(str(board_id), page.get("items") or [])
    cursor = page.get("cursor")
    while cursor:
        ensure_budget(quota)
        next_data, quota = graphql_request(
            client, token, _NEXT_ITEMS_QUERY, {"cursor": cursor, "limit": 500}
        )
        page = next_data.get("next_items_page") or {}
        rows.extend(_normalise_items(str(board_id), page.get("items") or []))
        cursor = page.get("cursor")
    return rows, build_board_catalog(board)


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_monday_board_snapshot (
  board_id VARCHAR, item_id VARCHAR, item_name VARCHAR, group_id VARCHAR,
  parent_item_id VARCHAR, updated_at TIMESTAMP, column_id VARCHAR,
  column_type VARCHAR, column_text VARCHAR, column_raw_value JSON,
  known_type BOOLEAN, schema_fingerprint VARCHAR, pull_id VARCHAR,
  loaded_at TIMESTAMP, project_id VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_monday_board_snapshot
    (board_id, item_id, item_name, group_id, parent_item_id, updated_at, column_id,
    column_type, column_text, column_raw_value, known_type, schema_fingerprint, pull_id,
    loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _land_snapshot(rows, schema, *, pull_id, project_id, duckdb_path):
    from core import warehouse_write  # noqa: PLC0415

    con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
    try:
        con.execute(_RAW_DDL)
        now = datetime.now(UTC).isoformat()
        values = []
        for row in rows:
            by_column = {value["id"]: value for value in row["column_values"]}
            for column in schema["columns"]:
                value = by_column.get(column["id"]) or {}
                values.append(
                    (
                        row["board_id"],
                        row["item_id"],
                        row["item_name"],
                        row["group_id"],
                        row["parent_item_id"],
                        row["updated_at"],
                        column["id"],
                        column["type"],
                        value.get("text"),
                        json.dumps(value.get("raw_value"), sort_keys=True),
                        column["known_type"],
                        schema["fingerprint"],
                        pull_id,
                        now,
                        project_id,
                    )
                )
        if values:
            con.executemany(_RAW_INSERT_SQL, values)
    finally:
        con.close()
    return len(values)


def _board_ids(connection_config, board_id=None):
    """Le(s) tableau(x) a tirer : la SELECTION de l'operateur d'abord.

    `board_id` est le compte choisi dans l'assistant, transmis par le worker
    sous le nom que le manifeste declare (`account_topology.pull_parameter`).
    Avant, seul `connection_config` etait lu -- et le contrat de dispatch ne le
    passe pas davantage que `selection` : aucun tableau n'arrivait jamais, donc
    ce connecteur levait a chaque pull.
    """
    if board_id not in (None, ""):
        return [str(board_id)]
    config = connection_config or {}
    raw_ids = config.get("board_ids") or [config.get("board_id")]
    board_ids = list(dict.fromkeys(str(value) for value in raw_ids if value not in (None, "")))
    if not board_ids:
        raise ValueError(
            "monday: a board selection is required. The operator picks one in the "
            "Datastream wizard (discover_accounts lists the boards the token can "
            "reach) and the worker passes it as `board_id`. There is no "
            "deployment-wide default: one would pull the same board for every project."
        )
    return board_ids


def pull_board_snapshot(
    connection_id,
    date_from,
    date_to,
    project_id,
    pull_id,
    connection_config=None,
    board_id=None,
    *,
    _client=None,
    _token_value=None,
):
    token = _token_value or _token(connection_id)
    client = _client or httpx.Client()
    duckdb_path = (connection_config or {}).get("duckdb_path") or os.environ.get(
        "DUCKDB_PATH", "toorow.duckdb"
    )
    written = 0
    fingerprints = {}
    for board_id in _board_ids(connection_config, board_id):
        rows, schema = fetch_board_snapshot(client, token, board_id)
        written += _land_snapshot(
            rows,
            schema,
            pull_id=pull_id,
            project_id=project_id,
            duckdb_path=duckdb_path,
        )
        fingerprints[board_id] = schema["fingerprint"]
    return {
        "profile_id": "board_snapshot",
        "rows_written": written,
        "row_count": written,
        "pull_id": pull_id,
        "schema_fingerprints": fingerprints,
        "date_from": date_from,
        "date_to": date_to,
    }


_UPDATES_QUERY = """
query ToorowBoardUpdates($boardIds: [ID!]!, $limit: Int!) {
  complexity { query before after reset_in_x_seconds }
  boards(ids: $boardIds) {
    updates(limit: $limit) {
      id created_at updated_at text_body creator { id name }
    }
  }
}
"""


def _update_events(data, date_from: str, date_to: str):
    events = []
    for board in data.get("boards") or []:
        for update in board.get("updates") or []:
            date = str(update.get("updated_at") or update.get("created_at") or "")[:10]
            if len(date) != 10 or not (date_from <= date <= date_to):
                continue
            events.append(
                {
                    "date": date,
                    "label": f"monday update {update.get('id')}",
                    "description": update.get("text_body") or "",
                }
            )
    return events


def pull_board_events(
    connection_id,
    date_from,
    date_to,
    project_id,
    pull_id,
    connection_config=None,
    board_id=None,
    *,
    _client=None,
    _token_value=None,
):
    from core.context_events import (  # noqa: PLC0415
        delete_connector_events_in_window,
        persist_context_event,
    )

    token = _token_value or _token(connection_id)
    data, _ = graphql_request(
        _client or httpx.Client(),
        token,
        _UPDATES_QUERY,
        # The operator's selection, like every other pull of this module: this
        # one call omitted it and fell back to the deployment config.
        {"boardIds": _board_ids(connection_config, board_id), "limit": 100},
    )
    events = _update_events(data, date_from, date_to)
    delete_connector_events_in_window(
        project_id=project_id,
        source="monday",
        event_type="milestone",
        date_from=date_from,
        date_to=date_to,
    )
    for event in events:
        persist_context_event(
            project_id=project_id,
            event_date=event["date"],
            type="milestone",
            label=event["label"],
            description=event["description"],
            created_by=f"monday_pull:{pull_id}",
            platform="monday",
            source="monday",
        )
    return {
        "profile_id": "board_events",
        "rows_written": len(events),
        "row_count": len(events),
        "pull_id": pull_id,
        "date_from": date_from,
        "date_to": date_to,
    }


def handle_webhook(
    payload: dict,
    *,
    expected_secret: str | None = None,
    supplied_secret: str | None = None,
    seen_ids: set[str] | None = None,
):
    """Validate challenge/secret and deduplicate edit/delete/retry deliveries."""
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}
    if expected_secret and not hmac.compare_digest(expected_secret, supplied_secret or ""):
        raise PermissionError("invalid monday webhook secret")
    event = payload.get("event") or {}
    identity = hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if seen_ids is not None and identity in seen_ids:
        return {"duplicate": True, "event_id": identity}
    if seen_ids is not None:
        seen_ids.add(identity)
    return {"duplicate": False, "event_id": identity, "event": event}


def pull(
    connection_id,
    date_from,
    date_to,
    project_id,
    pull_id,
    profile_id="board_snapshot",
    connection_config=None,
    board_id=None,
    **kwargs,
):
    if profile_id == "board_snapshot":
        return pull_board_snapshot(
            connection_id, date_from, date_to, project_id, pull_id, connection_config,
            board_id, **kwargs
        )
    if profile_id == "board_events":
        return pull_board_events(
            connection_id, date_from, date_to, project_id, pull_id, connection_config,
            board_id, **kwargs
        )
    raise ValueError(f"unsupported monday profile: {profile_id}")


def transform(raw_rows: list[dict]) -> list[dict]:
    """Apply manifest-declared canonical renames without KPI projection."""
    from pathlib import Path

    manifest = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
    rename = dict(manifest.get("canonical_dimension_mapping") or {})
    for source, target in (manifest.get("canonical_metric_mapping") or {}).items():
        rename[source] = target if isinstance(target, str) else target["canonical"]
    return [
        {rename.get(source, source): value for source, value in row.items()} for row in raw_rows
    ]


def transform_events(
    raw_rows: list[dict],
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Map monday update rows to canonical, date-bounded context events."""
    events = []
    for update in raw_rows:
        event_date = str(update.get("updated_at") or update.get("created_at") or "")[:10]
        if len(event_date) != 10:
            continue
        if date_from is not None and event_date < date_from:
            continue
        if date_to is not None and event_date > date_to:
            continue
        events.append(
            {
                "event_type": "milestone",
                "event_date": event_date,
                "label": f"monday update {update.get('id')}",
                "description": update.get("text_body") or "",
                "platform": "monday",
                "source": "monday",
            }
        )
    return events


@mcp_app.tool()
def board_snapshot(project_id: str = "default") -> dict:
    """Describe the latest full-grain monday snapshot surface."""
    return {
        "schema_version": "1",
        "meta": {
            "freshness": None,
            "provenance": {"connector": "monday", "landing": "raw_snapshot"},
            "alerts": [
                {
                    "level": "info",
                    "message": "Snapshot state is not projected into fact_daily_kpi.",
                }
            ],
        },
        "data": {
            "project_id": project_id,
            "profile_id": "board_snapshot",
            "grain": ["board_id", "item_id"],
            "metrics": {},
        },
    }
