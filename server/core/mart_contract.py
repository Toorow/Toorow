"""The contract a published mart offers to a reader outside the product.

Story 62.3. A dashboard bound to a mart yesterday must still read it today, so
the mart has to publish something stable: the dataset it lives in, the table
name, the columns, and a version that MOVES when a column moves.

WHAT IS DERIVED AND WHAT IS STORED. The columns are not retyped here and they
are not stored in Postgres: they are already declared, with the product, in
``dbt/models/marts/*.yml`` -- the same files dbt itself reads. A derivable value
is not a stored one, so this module reads that catalogue and computes a
fingerprint from it. The ONLY thing worth storing is what cannot be derived: the
version an outside dashboard was told to expect, and the moment the previous one
stops being readable. That is one small table, ``app.mart_contract_versions``.

WHY THE VERSION IS PINNED BY A WRITE AND NOT BY A READ. A read never writes. If
reading the contract bumped the version, two dashboards polling it would race
each other to a new number nobody published. The rebuild publishes; the read
reports what was published, and says plainly when the built shape has drifted
away from it.

WHICH MARTS ARE "PUBLISHED". Those declared in the marts schema files with
columns, excluding the ``int_`` intermediates -- dbt's own naming convention for
a model that exists to feed another one. Nothing is added to the catalogue by
hand here; a new published mart is a new declaration in the dbt yml.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import declare_action, insert_audit_row

logger = logging.getLogger("core.admin_api")

ACTION_MART_CONTRACT_PUBLISHED = declare_action("mart_contract.version_published")

#: The dbt marts declarations, read as DATA -- never imported, never executed.
_MARTS_MODELS_DIR = Path(__file__).parents[2] / "dbt" / "models" / "marts"

#: dbt's own convention: an ``int_`` model feeds another model. It is built, but
#: it is not a table an outside dashboard is invited to bind to.
_INTERMEDIATE_PREFIX = "int_"

#: How long the version an external dashboard already reads stays readable after
#: a rebuild replaced it. A DEFAULT, not a platform constant: the publish call
#: carries ``previous_readable_days`` when a project needs another window.
DEFAULT_PREVIOUS_READABLE_DAYS = 30
_MAX_PREVIOUS_READABLE_DAYS = 365


class MartContractUnavailable(RuntimeError):
    """The published-mart catalogue could not be read."""


@dataclass(frozen=True)
class MartShape:
    """One published mart, as the dbt catalogue declares it."""

    table: str
    columns: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        """The identity of this shape: order-sensitive, name-only."""
        payload = json.dumps({"table": self.table, "columns": list(self.columns)})
        return hashlib.sha256(payload.encode()).hexdigest()


async def _check_auth(*args, **kwargs):
    from core.admin_api import _check_auth as impl  # noqa: PLC0415

    return await impl(*args, **kwargs)


def _refuse_unless_project_allowed(*args, **kwargs):
    from core.admin_api import _refuse_unless_project_allowed as impl  # noqa: PLC0415

    return impl(*args, **kwargs)


def _read_yaml(path: Path) -> dict:
    import yaml  # noqa: PLC0415 -- only on the catalogue path

    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def published_marts(models_dir: Path | None = None) -> tuple[MartShape, ...]:
    """Return every published mart declared by the dbt marts catalogue.

    Deterministic: sorted by table name, columns in declaration order. A read;
    nothing is written and no database is touched.
    """
    directory = models_dir or _MARTS_MODELS_DIR
    if not directory.is_dir():
        raise MartContractUnavailable(
            f"the published-mart catalogue is missing at {directory}"
        )
    shapes: dict[str, MartShape] = {}
    for path in sorted(directory.glob("*.yml")):
        try:
            document = _read_yaml(path)
        except Exception as exc:  # noqa: BLE001 -- one bad file must not hide the rest
            raise MartContractUnavailable(
                f"the published-mart catalogue {path.name} could not be read: {exc}"
            ) from exc
        for model in document.get("models") or ():
            name = str(model.get("name") or "").strip()
            if not name or name.startswith(_INTERMEDIATE_PREFIX):
                continue
            columns = tuple(
                str(column.get("name")).strip()
                for column in (model.get("columns") or ())
                if str(column.get("name") or "").strip()
            )
            if not columns:
                continue
            shapes[name] = MartShape(table=name, columns=columns)
    if not shapes:
        raise MartContractUnavailable(
            f"the published-mart catalogue at {directory} declares no mart with columns"
        )
    return tuple(shapes[name] for name in sorted(shapes))


def published_mart(table: str, models_dir: Path | None = None) -> MartShape | None:
    """Return the declared shape of ONE published mart, or None."""
    wanted = (table or "").strip()
    return next((shape for shape in published_marts(models_dir) if shape.table == wanted), None)


def _pinned_rows(conn, project_id: str) -> dict[str, list[dict]]:
    """Read every pinned version of *project_id*, newest first per table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mart_table, schema_version, columns_fingerprint, published_at,
                   published_by, superseded_at, readable_until
            FROM app.mart_contract_versions
            WHERE project_id = %s
            ORDER BY mart_table ASC, schema_version DESC
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    pinned: dict[str, list[dict]] = {}
    for row in rows:
        pinned.setdefault(row[0], []).append(
            {
                "schema_version": int(row[1]),
                "columns_fingerprint": row[2],
                "published_at": row[3].isoformat() if row[3] else None,
                "published_by": row[4],
                "superseded_at": row[5].isoformat() if row[5] else None,
                "readable_until": row[6].isoformat() if row[6] else None,
            }
        )
    return pinned


def read_contracts(project_id: str, conn, models_dir: Path | None = None) -> list[dict]:
    """Return the contract of every published mart of *project_id*. A READ.

    Two reads in a row with no rebuild between them return the same payload,
    because everything in it is either derived from files or read from rows --
    nothing is minted on the way out.
    """
    from core.warehouse_tenancy import bigquery_marts_dataset  # noqa: PLC0415

    dataset = bigquery_marts_dataset(project_id, conn=conn)
    pinned = _pinned_rows(conn, project_id)
    contracts: list[dict] = []
    for shape in published_marts(models_dir):
        history = pinned.get(shape.table) or []
        current = next((row for row in history if row["superseded_at"] is None), None)
        previous = next((row for row in history if row["superseded_at"] is not None), None)
        if current is None:
            state = "unpublished"
        elif current["columns_fingerprint"] == shape.fingerprint:
            state = "published"
        else:
            state = "rebuild_pending"
        contracts.append(
            {
                "project_id": project_id,
                "dataset": dataset,
                "table": shape.table,
                "columns": list(shape.columns),
                "columns_fingerprint": shape.fingerprint,
                "schema_version": current["schema_version"] if current else None,
                "state": state,
                "published_at": current["published_at"] if current else None,
                "next_schema_version": (
                    current["schema_version"] + 1 if state == "rebuild_pending" else None
                ),
                "previous": (
                    {
                        "schema_version": previous["schema_version"],
                        "readable_until": previous["readable_until"],
                    }
                    if previous
                    else None
                ),
            }
        )
    return contracts


def publish_contract_version(
    project_id: str,
    table: str,
    conn,
    *,
    actor: str,
    previous_readable_days: int = DEFAULT_PREVIOUS_READABLE_DAYS,
    models_dir: Path | None = None,
) -> dict:
    """Pin the built shape of *table* and return the contract that now holds.

    Idempotent on the SHAPE: publishing a shape that is already the current one
    returns it unchanged, with no new row and no audit line. A shape whose
    columns differ supersedes it -- version + 1 -- and the superseded version
    stays readable for the declared window, so a dashboard bound yesterday keeps
    reading while its owner is told to move.
    """
    from ulid import ULID  # noqa: PLC0415

    shape = published_mart(table, models_dir)
    if shape is None:
        raise LookupError(table)
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        owner = cur.fetchone()
        if owner is None or not owner[0]:
            raise LookupError(project_id)
        org_id = owner[0]
        cur.execute(
            """
            SELECT id, schema_version, columns_fingerprint
            FROM app.mart_contract_versions
            WHERE project_id = %s AND mart_table = %s AND superseded_at IS NULL
            FOR UPDATE
            """,
            (project_id, shape.table),
        )
        current = cur.fetchone()
        if current is not None and current[2] == shape.fingerprint:
            return {"changed": False, "schema_version": int(current[1])}
        version = int(current[1]) + 1 if current is not None else 1
        if current is not None:
            cur.execute(
                """
                UPDATE app.mart_contract_versions
                SET superseded_at = NOW(),
                    readable_until = NOW() + make_interval(days => %s)
                WHERE id = %s
                """,
                (previous_readable_days, current[0]),
            )
        cur.execute(
            """
            INSERT INTO app.mart_contract_versions
                (id, org_id, project_id, mart_table, schema_version,
                 columns_fingerprint, published_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                f"martver_{ULID()}",
                org_id,
                project_id,
                shape.table,
                version,
                shape.fingerprint,
                actor,
            ),
        )
        insert_audit_row(
            conn,
            identity=actor,
            action=ACTION_MART_CONTRACT_PUBLISHED,
            provider_account="",
            connection_ref="",
            metadata={
                "project_id": project_id,
                "mart_table": shape.table,
                "schema_version": version,
                "columns_fingerprint": shape.fingerprint,
                "previous_readable_days": previous_readable_days,
            },
        )
    return {"changed": True, "schema_version": version}


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _project_scope_refusal(project_id: str) -> Response | None:
    """409 when the physical topology cannot represent project scope.

    A contract names the dataset an outside reader binds to. Under
    `TOOROW_ORG_SCHEMAS` the marts dataset is per ORGANIZATION, so a contract read
    at project scope would name -- and hand an external reader -- the dataset of
    every project of the org, without the refusal the grant route already opposes
    (`dataset_access_api._resolve_project_target`). The same limit is named here,
    so the two routes of this capability answer one way.
    """
    from core.warehouse_tenancy import project_marts_scope  # noqa: PLC0415

    try:
        target, refusal = project_marts_scope(project_id)
    except Exception:  # noqa: BLE001 -- an unresolvable name is a refusal, not a 500
        refusal = "The project's marts dataset could not be named."
        target = None
    if target is None:
        return JSONResponse(
            {"code": "project_scope_unavailable", "message": refusal},
            409,
        )
    return None


async def _list_mart_contracts(request: Request) -> Response:
    """GET /api/projects/{project_id}/marts/contracts -- what an outside reader binds to."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Bearer token required"}, 401)
    project_id = request.path_params["project_id"]
    denied = _refuse_unless_project_allowed(
        identity or "anonymous", project_id, "view", "mart-contracts"
    )
    if denied is not None:
        return denied
    scope_refusal = _project_scope_refusal(project_id)
    if scope_refusal is not None:
        return scope_refusal
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            contracts = read_contracts(project_id, conn)
    except MartContractUnavailable as exc:
        return JSONResponse(
            {
                "code": "catalogue_unavailable",
                "message": (
                    "The published-mart catalogue could not be read, so no contract "
                    f"can be named. Restore the dbt marts declarations and retry. ({exc})"
                ),
            },
            503,
        )
    except Exception:
        logger.exception("mart contract list failed project=%s", project_id)
        return JSONResponse({"code": "db_error", "message": "Database error"}, 500)
    return JSONResponse({"contracts": contracts}, 200)


async def _publish_mart_contract_version(request: Request) -> Response:
    """POST /api/projects/{project_id}/marts/contracts/{mart_table}/versions."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Bearer token required"}, 401)
    project_id = request.path_params["project_id"]
    mart_table = request.path_params["mart_table"]
    denied = _refuse_unless_project_allowed(
        identity or "anonymous", project_id, "manage", "mart-contracts"
    )
    if denied is not None:
        return denied
    scope_refusal = _project_scope_refusal(project_id)
    if scope_refusal is not None:
        return scope_refusal
    try:
        raw = await request.body()
        body = json.loads(raw) if raw else {}
    except Exception:
        return JSONResponse({"code": "invalid_body", "message": "Invalid JSON body"}, 400)
    days = body.get("previous_readable_days", DEFAULT_PREVIOUS_READABLE_DAYS)
    if not isinstance(days, int) or isinstance(days, bool) or not (
        0 < days <= _MAX_PREVIOUS_READABLE_DAYS
    ):
        return JSONResponse(
            {
                "code": "invalid_input",
                "message": (
                    "Say how many days the version being replaced stays readable, "
                    f"between 1 and {_MAX_PREVIOUS_READABLE_DAYS}."
                ),
            },
            422,
        )
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            result = publish_contract_version(
                project_id,
                mart_table,
                conn,
                actor=identity or "anonymous",
                previous_readable_days=days,
            )
            contracts = read_contracts(project_id, conn)
            conn.commit()
    except LookupError:
        return JSONResponse(
            {
                "code": "not_found",
                "message": (
                    f"{mart_table!r} is not a published mart. Publish one of the marts "
                    "the product builds, listed by this project's contracts."
                ),
            },
            404,
        )
    except MartContractUnavailable as exc:
        return JSONResponse(
            {
                "code": "catalogue_unavailable",
                "message": (
                    "The published-mart catalogue could not be read, so no version can "
                    f"be pinned. Restore the dbt marts declarations and retry. ({exc})"
                ),
            },
            503,
        )
    except Exception:
        logger.exception(
            "mart contract publish failed project=%s table=%s", project_id, mart_table
        )
        return JSONResponse({"code": "db_error", "message": "Database error"}, 500)
    contract = next((item for item in contracts if item["table"] == mart_table), None)
    return JSONResponse({**(contract or {}), "changed": result["changed"]}, 200)


MART_CONTRACT_ROUTES = [
    Route(
        "/api/projects/{project_id}/marts/contracts/{mart_table}/versions",
        endpoint=_publish_mart_contract_version,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/marts/contracts",
        endpoint=_list_mart_contracts,
        methods=["GET"],
    ),
]
