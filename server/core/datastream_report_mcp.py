"""What a project collects, named by its Datastreams -- AD-42.

THE THIRTY-NINE TOOLS THIS REPLACES. Every connector declared a
``get_<provider>_report`` on its own ``mcp_app``; ``core.main`` mounted that app
under the connector's namespace, and FastMCP's namespace transform published
``google-ads_get_google_ads_report``, ``adobe-analytics_get_adobe_analytics_report``
and thirty-seven siblings into the catalog every host reads at session start.
Measured: 39 of the 92 tools a default host saw, 13 933 of the 45 106 bytes it
paid for before asking anything. The connector catalogue is bounded by nothing,
so that count grew with every folder dropped into ``server/modules/``.

Read side by side, the thirty-nine were the same function thirty-nine times: the
same ``fact_daily_kpi`` roll-up with a different connector literal in the WHERE
clause. The variable was never the tool -- it was the parameter.

WHY THE PARAMETER IS A DATASTREAM AND NOT A CONNECTOR. Substituting one generic
``get_report(connector=...)`` would have removed the thirty-eight duplicates and
kept the wrong question: an agent would still have to know that thirty-nine
connectors exist in order to name one. It needs to know what THIS project
collects, and that object has a name in the product -- the Datastream
(`glossary.md`: a Connector is what a project *could* collect; a Datastream is
what it *does*). So ``list_datastreams`` answers "what does this project
collect", and ``get_datastream_report`` answers "what do the numbers of one of
those say". Neither name contains a provider's, which is the whole of AD-42.

WHAT THIS MODULE REFUSES, AND WHY IT MUST. ``fact_daily_kpi`` is keyed
``(project, date, connector, metric, breakdown)`` (AD-4) and carries NO Datastream
discriminator. Two live Datastreams on one owner-project/connector therefore make
that mart slice unattributable to either. The console already fails closed on
exactly this, in ``datastream_sample_api.datastream_materialization_is_ambiguous``, and this
module calls THAT function rather than restating the rule: two copies of a
fail-closed rule eventually disagree, and the copy that drifted would be the one
that showed another stream's rows under this stream's name.

Conventions mirror the neighbouring ``*_mcp`` modules: ``from __future__ import
annotations``, module logger, lazy in-body imports (no cycle with ``core.main``),
ASCII-only, and registration through ``register_profiled`` so the tools carry a
capability declaration instead of arriving in the catalog by omission (AD-43).
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: Default reporting window when the caller names neither bound: yesterday back
#: ninety days. Same window the thirty-nine connector tools defaulted to, kept so
#: a caller that omitted the dates gets the same answer it used to get.
_DEFAULT_WINDOW_DAYS = 90

#: What a person does when the list is empty. Named here once so the empty list
#: and the refusal say the SAME thing -- a caller must not have to guess that the
#: two are the same situation.
_NO_DATASTREAM_NEXT_STEP = (
    "Create a Datastream in Data > Datastreams: pick a Connector, authorize a "
    "Source Account and choose what to collect."
)


def _tool_error(code: str, message: str, **extra: Any):
    """Return a ToolError carrying the canonical ``{code, message, ...}`` JSON."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    payload = {"code": code, "message": message}
    payload.update(extra)
    return ToolError(json.dumps(payload))


def _project_datastreams(conn, project_id: str) -> list[dict]:
    """Every live Datastream bound to *project_id*, with its owner project.

    The join is `app.datastreams` -> `app.project_flux`, the AD-5 binding edge: a
    Datastream is organization-owned and attaches to a project explicitly, so a
    multi-project Datastream never acquires a second identity. `archived_at IS
    NULL` is what "live" means (migration 256 -- an archived Datastream stops
    holding its name).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ds.id, ds.name, ds.module_name, ds.project_id,
                   ds.enabled, ds.schedule_mode, ds.report_profile_id
            FROM app.datastreams ds
            JOIN app.project_flux pf
              ON pf.flux_id = ds.id AND pf.org_id = ds.org_id
            WHERE pf.project_id = %s AND ds.archived_at IS NULL
            ORDER BY ds.name
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "connector": row[2] or "",
            "data_project_id": row[3],
            "enabled": bool(row[4]),
            "schedule_mode": row[5],
            "report_profile": row[6],
        }
        for row in rows
    ]


def _match_datastream(streams: list[dict], reference: str) -> dict | None:
    """Resolve *reference* against a Datastream id first, then its name.

    A caller that just read ``list_datastreams`` holds both, and refusing the one
    it happens to quote would make the two tools disagree about what names a
    Datastream. The id wins on a collision -- it is the identity, the name is a
    label the owner may change.
    """
    wanted = (reference or "").strip()
    if not wanted:
        return None
    for stream in streams:
        if stream["id"] == wanted:
            return stream
    lowered = wanted.casefold()
    for stream in streams:
        if (stream["name"] or "").casefold() == lowered:
            return stream
    return None


def _resolve_window(date_from: str, date_to: str) -> tuple[str, str]:
    """Fill an omitted bound: yesterday, back ``_DEFAULT_WINDOW_DAYS``."""
    resolved_to = (date_to or "").strip() or (date.today() - timedelta(days=1)).isoformat()
    resolved_from = (date_from or "").strip() or (
        date.today() - timedelta(days=_DEFAULT_WINDOW_DAYS)
    ).isoformat()
    return resolved_from, resolved_to


def list_datastreams(project_id: str = "") -> dict:
    """What this project actually collects -- one line per Datastream.

    A DATASTREAM is a running collection: one Connector, one authorized Source
    Account, one thing collected on a schedule. It is what this project DOES
    collect, as opposed to a Connector, which is what it COULD (see
    docs/product-architecture/glossary.md).

    Start here. There is no tool named after a provider: to read numbers, take an
    id or a name from this list and pass it to ``get_datastream_report``.

    An empty list is an answer, not a failure: it means this project collects
    nothing yet, and it says what fills it.

    Parameters:
        project_id: Project identifier. Empty binds the caller's default project.
    """
    from core import db as _core_db  # noqa: PLC0415
    from core.main import _envelope, _resolve_project  # noqa: PLC0415
    from core.mcp_scope import caller_identity, refuse_unless_project_scope  # noqa: PLC0415

    identity = caller_identity()
    resolved_project_id = _resolve_project(project_id, identity)
    refuse_unless_project_scope(resolved_project_id, identity)

    # The connection that READS carries the access context (core/db.py): the guard
    # above opened, resolved and closed its own, and a fresh unarmed one would put
    # the floor back to zero.
    with _core_db.request_connection(identity) as conn:
        streams = _project_datastreams(conn, resolved_project_id)
        for stream in streams:
            stream["report_is_attributable"] = not _is_ambiguous(
                conn, stream["data_project_id"], stream["connector"]
            )

    published = [
        {
            "id": stream["id"],
            "name": stream["name"],
            "connector": stream["connector"],
            "enabled": stream["enabled"],
            "schedule_mode": stream["schedule_mode"],
            "report_profile": stream["report_profile"],
            # Told here rather than discovered at the next call: a caller learns
            # which of these it can actually read numbers for before it asks.
            "report_is_attributable": stream["report_is_attributable"],
        }
        for stream in streams
    ]
    data: dict[str, Any] = {
        "project_id": resolved_project_id,
        "datastreams": published,
        "count": len(published),
    }
    if not published:
        data["empty_because"] = "this project has no Datastream yet"
        data["next_step"] = _NO_DATASTREAM_NEXT_STEP
    return _envelope(
        data,
        provenance={
            "source_system": "connector-core",
            "source_field": "list_datastreams",
            "pull_id": None,
        },
        freshness="live",
    )


def _is_ambiguous(conn, data_project_id: str, connector: str) -> bool:
    """Delegate to the console's fail-closed rule -- never a second copy of it."""
    from core.datastream_sample_api import datastream_materialization_is_ambiguous  # noqa: PLC0415

    return datastream_materialization_is_ambiguous(
        conn, data_project_id=data_project_id, connector=connector
    )


def get_datastream_report(
    datastream: str,
    project_id: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Daily metric roll-up for ONE Datastream of this project.

    Reads the ``fact_daily_kpi`` mart only (AD-12) and returns the canonical AD-1
    envelope: metric, breakdown dimension, breakdown value and the summed value
    over the window, with the pull that produced the freshest row.

    Only additive metrics are summed here (AD-4); ratios are computed at view
    time from the semantic layer and non-additive source metrics route through
    their declared aggregation rule -- neither is re-derived in this tool.

    REFUSES rather than guesses: the mart carries no Datastream discriminator, so
    if this project runs two live Datastreams on the same Connector, no row can be
    attributed to one of them and this returns ``ambiguous_materialization``.
    ``list_datastreams`` reports that per stream up front, as
    ``report_is_attributable``.

    Parameters:
        datastream: Datastream id or name, from ``list_datastreams``.
        project_id: Project identifier. Empty binds the caller's default project.
        date_from:  ISO-8601 start; empty means 90 days back.
        date_to:    ISO-8601 end; empty means yesterday.
    """
    from core import db as _core_db  # noqa: PLC0415
    from core import warehouse  # noqa: PLC0415
    from core.main import _envelope, _resolve_project  # noqa: PLC0415
    from core.mcp_scope import caller_identity, refuse_unless_project_scope  # noqa: PLC0415

    identity = caller_identity()
    resolved_project_id = _resolve_project(project_id, identity)
    refuse_unless_project_scope(resolved_project_id, identity)
    resolved_from, resolved_to = _resolve_window(date_from, date_to)

    with _core_db.request_connection(identity) as conn:
        streams = _project_datastreams(conn, resolved_project_id)
        if not streams:
            raise _tool_error(
                "no_datastream",
                "This project collects nothing yet, so there is no report to read.",
                next_step=_NO_DATASTREAM_NEXT_STEP,
            )
        stream = _match_datastream(streams, datastream)
        if stream is None:
            raise _tool_error(
                "datastream_not_found",
                f"No Datastream named {datastream!r} in this project.",
                next_step="Call list_datastreams and use an id or name it returns.",
            )
        if not stream["connector"]:
            raise _tool_error(
                "datastream_has_no_connector",
                f"Datastream {stream['name']!r} names no Connector, so it lands no "
                "rows in the warehouse.",
                next_step="Finish the Datastream setup in Data > Datastreams.",
            )
        if _is_ambiguous(conn, stream["data_project_id"], stream["connector"]):
            raise _tool_error(
                "ambiguous_materialization",
                f"This project runs more than one live Datastream on "
                f"{stream['connector']!r}, and the warehouse cannot tell their rows "
                "apart, so no figure can be attributed to this one.",
                next_step=(
                    "Archive the duplicate Datastream, or read the connector-wide "
                    "figures with get_report."
                ),
            )

    rows = warehouse.query_daily_report(
        project_id=stream["data_project_id"],
        start_date=resolved_from,
        end_date=resolved_to,
        connectors=[stream["connector"]],
        include_prior_period=False,
    )

    metrics: dict[str, list[dict]] = {}
    pull_ids = {row["pull_id"] for row in rows if row.get("pull_id")}
    freshness_values = [row["loaded_at"] for row in rows if row.get("loaded_at")]
    for row in rows:
        metrics.setdefault(row["metric"], []).append(
            {
                "date": str(row["date"]),
                "breakdown_dimension": row.get("breakdown_dimension"),
                "breakdown_value": row.get("breakdown_value"),
                "value": row["value"],
            }
        )

    data = {
        "project_id": resolved_project_id,
        "datastream": {
            "id": stream["id"],
            "name": stream["name"],
            "connector": stream["connector"],
        },
        "date_from": resolved_from,
        "date_to": resolved_to,
        "metrics": metrics,
    }
    alerts = []
    if not rows:
        # An honest empty state: the window held nothing. Never an empty dict that
        # reads as "the numbers are zero".
        data["empty_because"] = "no row landed in this window"
        alerts.append(
            {
                "level": "info",
                "message": (
                    "This Datastream landed no row between "
                    f"{resolved_from} and {resolved_to}. Check its Runs tab, or "
                    "widen the window."
                ),
            }
        )
    envelope = _envelope(
        data,
        provenance={
            "source_system": stream["connector"],
            "source_field": "fact_daily_kpi",
            "pull_id": max(pull_ids) if pull_ids else None,
        },
        freshness=str(max(freshness_values)) if freshness_values else None,
        alerts=alerts,
    )
    # AD-18, measured here since 2026-09-01 (`analyze-and-test.md`, amendment of
    # that date): this is the `fact_daily_kpi` roll-up handed to the model, the
    # same figures the 39 per-connector report tools used to hand it, and the
    # gate measured those. Recorded, never enforced, never raising. This tool
    # returns a plain envelope and has no `_meta` channel to echo the verdict on:
    # the row and the span attribute are written, and `get_context_adherence`
    # reads them.
    try:
        from core import adherence as _adherence  # noqa: PLC0415
        from core.main import _current_identity  # noqa: PLC0415

        _adherence.record_data_query(
            "get_datastream_report",
            project_id=resolved_project_id,
            trace_id=_adherence.current_exchange_trace_id(),
            identity=_current_identity(),
        )
    except Exception as _gate_exc:  # noqa: BLE001 -- the gate must never break the tool
        logger.debug("get_datastream_report: pre_query_gate_skipped: %s", _gate_exc)
    return envelope


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the two source-agnostic collection tools (AD-42), both declared (AD-43)."""
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        list_datastreams,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_datastream_report,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
