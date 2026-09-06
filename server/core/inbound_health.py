"""Story 38.14 -- Connector health and import outcomes, as one read model.

WHY THIS MODULE EXISTS, stated plainly because it is the whole point. Before it,
inbound evidence was written and read by nobody: `app.inbound_receipts` had no
route in `admin_api`, `get_receipt`/`list_receipts` had no caller outside the
worker, and the only reader anywhere was a `LEFT JOIN LATERAL` on the Imports
page that showed the latest receipt OF A LEDGER ROW. A delivery that never
produced a ledger row -- denied, rejected, failed, which is every delivery an
operator actually needs to see -- was invisible on every surface.

THE LAYERS ARE SEPARATE ON PURPOSE (AC1). "The connector is broken" is six
different sentences with three different owners, and collapsing them into one
status is what makes an operator restart the wrong thing:

    installation  -- environment capability. Owner: platform administrator.
    domain        -- provider route + DNS + signature. Owner: platform administrator.
    activation    -- the tenant switched it on. Owner: organization owner.
    delivery      -- this Datastream's channels and credentials. Owner: Datastream operator.
    queue         -- the durable scan work behind each attachment. Owner: Datastream operator.
    data          -- the latest import, and the last-known-good publication.

`queue` was added on 2026-08-10. AC1 had named six states since the first draft
and this module assembled five: `app.inbound_scan_jobs` was read by the inbox and
the timeline, and by no LAYER -- so a job in `DEAD_LETTER` behind a published
ledger row answered `overall: "healthy"`.

Each layer reports its own state, its own blocking cause, and the AUTHORITY
required to repair it (AC4). A layer that cannot be read says so; it never
reports "healthy" for want of evidence.

LAST-KNOWN-GOOD IS ALWAYS SEPARATE FROM LATEST (AC5). `latest_import` and
`last_known_good_publication` are two fields, never one. A failed candidate must
not make a serving publication look absent -- that confusion is what turns a
recoverable failure into an incident.

NO SECRET, NO SAMPLE, NO HIGH-CARDINALITY IDENTIFIER (AC3, E38-NFR03). Counts,
states, timestamps and stable codes only. No token, no signing secret, no
recipient address, no row content. The read model is byte-identical whether it
is served over REST or through MCP, so the two cannot drift into disagreeing
about what happened.

Pure reads throughout: nothing here writes, commits or rolls back.
ASCII-only source (AI-03).
"""

from __future__ import annotations

import json
import logging
from typing import Any

# Le vocabulaire des issues appartient au registre qui les ecrit, pas a ce
# lecteur : le recopier est precisement ce qui a laisse deux valeurs impossibles
# dans la liste saine.
from core.managed_feed_ledger import (
    OUTCOME_NOOP,
    OUTCOME_OPENED,
    OUTCOME_PUBLISHED,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Authority classes. Which human can repair which layer (AC4).
#
# These are CLASSES, not identities: naming a person here would be both a
# privacy leak and wrong the moment they change role.
# ---------------------------------------------------------------------------

AUTHORITY_PLATFORM_ADMIN = "platform_administrator"
AUTHORITY_ORG_OWNER = "organization_owner"
AUTHORITY_DATASTREAM_OPERATOR = "datastream_operator"
AUTHORITY_NONE = "none"

#: A layer whose evidence could not be read. Distinct from "unhealthy": an
#: unreadable layer is an unknown, and reporting it as healthy or as broken are
#: both lies, in opposite directions.
STATE_UNKNOWN = "unknown"


def _dt2iso(ts: Any) -> str | None:
    if ts is None:
        return None
    try:
        return ts.isoformat()
    except AttributeError:
        return str(ts)


#: THE TABS THE CONSOLE ACTUALLY DECLARES for a `datastream` object, copied from
#: the navigation registry and pinned to it by
#: `test_the_console_tab_list_is_the_one_the_console_declares`. A `tab` outside
#: this list is dropped by `ContentRouter` IN SILENCE, so the reference still
#: renders as a link and leads nowhere.
#:
#: `placements` JOINED THE CONSOLE ON 2026-08-11 AND NOT THIS LIST, and the test
#: that should have caught it had gone blind at the same time: it read
#: `shell/navigation.ts` by path, AD-42 moved every workspace's sections into
#: `shell/navigation/<workspace>.ts`, and the guard found no `datastream`
#: contract at all. It now reads the registry through
#: `tests.support.navigation_source`, which is why this line could finally be
#: written. The order matters too -- the assertion compares LISTS, because a set
#: comparison would let the two vocabularies drift in order and still pass.
#:
#: `deliveries` was exactly that, on the only two layers that offer a repair
#: (AC4), for the fourth time in this class -- `inbound_mcp` tells the first
#: three at its own `_console_reference`. The health panel is mounted on
#: `WorkbenchOverviewPage`, so `overview` is where an alert must land.
DATASTREAM_CONSOLE_TABS: tuple[str, ...] = (
    "overview",
    "mapping",
    "data",
    "cost",
    "placements",
    "processing",
    "runs",
    "outputs",
)


def console_reference(datastream_id: str | None, *, tab: str) -> dict[str, Any]:
    """A semantic console destination, never a hard-coded URL.

    This shape was proved against `ContentRouter` in `inbound_mcp` and lives
    here now because BOTH surfaces need it: the console renders it as an href
    and a noninteractive host receives it as its AD-27 destination. Two copies
    would be two chances to drift, and a reference the console silently drops
    is worse than none -- it reads like a link.

    An undeclared `tab` raises rather than shipping a dead link: the router's
    silence is the whole defect, and a loud failure here is the only thing that
    cannot be missed.
    """
    if tab not in DATASTREAM_CONSOLE_TABS:
        raise ValueError(
            f"{tab!r} is not a declared tab of the `datastream` navigation "
            f"contract {DATASTREAM_CONSOLE_TABS}. ContentRouter drops an "
            "unknown tab without a word, so the reference would render as a "
            "link and go nowhere."
        )
    return {
        "kind": "console",
        "requires_authenticated_session": True,
        "datastream_id": datastream_id,
        "owner_reference": {
            "surface": "project",
            "workspace": "data",
            "section": "datastreams",
            "global_surface": None,
            "global_section": None,
            "object_type": "datastream",
            "object_id": datastream_id,
            "tab": tab,
            "action": None,
        },
    }


def _recovery(
    *,
    api: str | None = None,
    method: str = "POST",
    mcp_tool: str | None = None,
    console_tab: str | None = None,
    datastream_id: str | None = None,
) -> dict[str, Any]:
    """The repair an alert points AT, named rather than described.

    AC4 asks that an alert << link to an authorized console/MCP recovery
    action >>. `next_action` was prose: it told a human what ought to happen and
    gave neither a console a destination to open nor a host a tool to call. A
    sentence is not a link.

    Every field here names something that EXISTS -- the routes are the ones
    declared in `connector_installation_api`, `connector_verification_api`,
    `connector_activation_api` and `inbound_health_api`, and the tool names are
    the ones in `inbound_mcp.INBOUND_MCP_TOOLS`. Naming a repair that is not
    mounted would be the same defect one layer up.
    """
    return {
        "api": api,
        "method": method if api else None,
        "mcp_tool": mcp_tool,
        "console": console_reference(datastream_id, tab=console_tab)
        if console_tab
        else None,
    }


#: The repairs each authority can actually invoke. Declared once so the same
#: alert reads identically in the console and over MCP (38.15 AC2).
_RECOVERY_INSTALL = _recovery(api="/api/connectors/{connector_name}/installation")
_RECOVERY_VERIFY = _recovery(api="/api/connectors/{connector_name}/verify")
_RECOVERY_ACTIVATE = _recovery(api="/api/connectors/{connector_name}/activation")


def _layer(
    name: str,
    *,
    state: str,
    healthy: bool | None,
    authority: str,
    blocking_cause: str | None = None,
    next_action: str | None = None,
    recovery: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One layer of the health answer, in the one shape every layer uses.

    ``healthy`` is deliberately three-valued (True / False / None): None means
    "not determined", and it is what an unreadable layer reports.

    ``recovery`` is None on a HEALTHY layer -- there is nothing to repair -- and
    on a layer whose repair does not exist as an invocable action. That second
    case is deliberate honesty: an unreadable database is not fixed by calling
    something, and offering a button there would invite an operator to keep
    pressing it. `authority` still says who owns the problem.
    """
    out = {
        "layer": name,
        "state": state,
        "healthy": healthy,
        "authority": authority,
        "blocking_cause": blocking_cause,
        "next_action": next_action,
        "recovery": None if healthy else recovery,
    }
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Layer readers.
# ---------------------------------------------------------------------------


def _read_installation(conn, *, connector_name: str, environment: str) -> dict:
    """Environment capability: is the connector installed here at all?"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, blocking_cause, responsible_actor, last_verified_at "
                "FROM app.connector_installations "
                "WHERE connector_name = %s AND environment = %s",
                (connector_name, environment),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- unreadable is not unhealthy
        logger.warning("inbound_health: installation unreadable: %s", type(exc).__name__)
        return _layer(
            "installation",
            state=STATE_UNKNOWN,
            healthy=None,
            authority=AUTHORITY_PLATFORM_ADMIN,
            blocking_cause="installation_state_unreadable",
            next_action="Retry; if it persists the platform database is unreachable.",
        )

    if row is None:
        return _layer(
            "installation",
            state="NOT_INSTALLED",
            healthy=False,
            authority=AUTHORITY_PLATFORM_ADMIN,
            blocking_cause="capability_not_installed",
            next_action="A platform administrator installs the inbound capability.",
            recovery=_RECOVERY_INSTALL,
            last_verified_at=None,
        )

    state, blocking_cause, responsible, last_verified = row
    healthy = state == "READY"
    return _layer(
        "installation",
        state=state,
        healthy=healthy,
        authority=AUTHORITY_PLATFORM_ADMIN,
        blocking_cause=None if healthy else (blocking_cause or state.lower()),
        next_action=(
            None if healthy else "A platform administrator completes installation and verification."
        ),
        recovery=_RECOVERY_INSTALL,
        responsible_actor=responsible,
        last_verified_at=_dt2iso(last_verified),
    )


def _read_domain(conn, *, connector_name: str, environment: str) -> dict:
    """Provider route and domain. Reports the ROUTE, never the signing secret.

    ``signing_secret_ref`` exists in the table and is deliberately not selected:
    even a reference is an infrastructure detail with no operator value, and the
    cheapest way to never leak a secret is to never read the column.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT domain, webhook_endpoint_version, dns_evidence_class, "
                "       config_version, superseded_by, updated_at "
                "FROM app.connector_domain_configs "
                "WHERE connector_name = %s AND environment = %s "
                "  AND superseded_by IS NULL "
                "ORDER BY config_version DESC LIMIT 1",
                (connector_name, environment),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbound_health: domain unreadable: %s", type(exc).__name__)
        return _layer(
            "domain",
            state=STATE_UNKNOWN,
            healthy=None,
            authority=AUTHORITY_PLATFORM_ADMIN,
            blocking_cause="domain_state_unreadable",
        )

    if row is None:
        return _layer(
            "domain",
            state="NOT_CONFIGURED",
            healthy=False,
            authority=AUTHORITY_PLATFORM_ADMIN,
            blocking_cause="no_verified_domain",
            next_action="A platform administrator configures and verifies a domain.",
            recovery=_RECOVERY_VERIFY,
        )

    domain, endpoint_version, evidence_class, config_version, _superseded, updated = row
    return _layer(
        "domain",
        state="CONFIGURED",
        healthy=True,
        authority=AUTHORITY_PLATFORM_ADMIN,
        domain=domain,
        webhook_endpoint_version=endpoint_version,
        dns_evidence_class=evidence_class,
        config_version=config_version,
        updated_at=_dt2iso(updated),
    )


def _read_activation(conn, *, connector_name: str, environment: str, org_id: str) -> dict:
    """Did this tenant switch the connector on? Separate from installing it."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, activated_at, deactivated_at "
                "FROM app.connector_activations "
                "WHERE connector_name = %s AND environment = %s AND org_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (connector_name, environment, org_id),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbound_health: activation unreadable: %s", type(exc).__name__)
        return _layer(
            "activation",
            state=STATE_UNKNOWN,
            healthy=None,
            authority=AUTHORITY_ORG_OWNER,
            blocking_cause="activation_state_unreadable",
        )

    if row is None:
        return _layer(
            "activation",
            state="NOT_ACTIVATED",
            healthy=False,
            authority=AUTHORITY_ORG_OWNER,
            blocking_cause="connector_not_activated_for_this_organization",
            next_action="An organization owner activates the connector.",
            recovery=_RECOVERY_ACTIVATE,
        )

    state, activated_at, deactivated_at = row
    healthy = state == "ACTIVE"
    return _layer(
        "activation",
        state=state,
        healthy=healthy,
        authority=AUTHORITY_ORG_OWNER,
        blocking_cause=None if healthy else state.lower(),
        activated_at=_dt2iso(activated_at),
        deactivated_at=_dt2iso(deactivated_at),
    )


def _read_delivery(conn, *, datastream_id: str) -> dict:
    """This Datastream's own delivery posture: enabled, channels, credentials."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT d.enabled, d.lifecycle_state, d.source_kind, d.config, "
                "       d.current_plan_version_id, d.current_mapping_version_id "
                "FROM app.datastreams d WHERE d.id = %s",
                (datastream_id,),
            )
            row = cur.fetchone()
            credential_count = 0
            if row is not None:
                cur.execute(
                    "SELECT count(*) FROM app.datastream_inbound_credentials "
                    "WHERE datastream_id = %s AND state IN ('ACTIVE', 'ROTATING')",
                    (datastream_id,),
                )
                credential_count = cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbound_health: delivery unreadable: %s", type(exc).__name__)
        return _layer(
            "delivery",
            state=STATE_UNKNOWN,
            healthy=None,
            authority=AUTHORITY_DATASTREAM_OPERATOR,
            blocking_cause="delivery_state_unreadable",
        )

    if row is None:
        return _layer(
            "delivery",
            state="NOT_FOUND",
            healthy=False,
            authority=AUTHORITY_DATASTREAM_OPERATOR,
            blocking_cause="datastream_not_found",
        )

    enabled, lifecycle, source_kind, config, plan_v, mapping_v = row
    config = config if isinstance(config, dict) else {}
    channels = sorted({str(c).lower() for c in (config.get("channels") or [])})

    # The order of these checks is the order an operator can act on them.
    blocking = None
    next_action = None
    if not enabled:
        blocking = "datastream_disabled"
        next_action = "Re-enable the Datastream."
    elif not channels:
        blocking = "no_delivery_channel_enabled"
        next_action = "Enable Email or Webhook delivery on the Datastream."
    elif credential_count == 0:
        blocking = "no_active_delivery_credential"
        next_action = "Issue a delivery credential."
    elif not plan_v or not mapping_v:
        # Unattended ingestion needs a locked plan + mapping; without them
        # `ingest_inbound_file` refuses, and it refuses at DELIVERY time, which
        # is far too late to be a surprise.
        blocking = "datastream_not_fully_configured"
        next_action = "Finish configuring the Datastream (plan and mapping)."

    return _layer(
        "delivery",
        state="READY" if blocking is None else "BLOCKED",
        healthy=blocking is None,
        authority=AUTHORITY_DATASTREAM_OPERATOR,
        blocking_cause=blocking,
        next_action=next_action,
        # UN OPERATEUR DE DATASTREAM PEUT AGIR ICI, et c'est ce qui distingue
        # cette couche des trois precedentes : la reprise d'un travail d'analyse
        # est a sa portee, l'installation ne l'est pas.
        recovery=_recovery(
            api=(
                "/api/connectors/{connector_name}/datastreams/{datastream_id}"
                "/scan-jobs/{job_id}/recover"
            ),
            mcp_tool="list_inbound_attachments",
            console_tab="overview",
            datastream_id=datastream_id,
        ),
        enabled=bool(enabled),
        lifecycle_state=lifecycle,
        source_kind=source_kind,
        channels=channels,
        active_credentials=credential_count,
    )


#: The states `app.inbound_scan_jobs` may hold (migration 186's own CHECK).
#: `DEAD_LETTER` is the terminal failure: attempts exhausted, evidence attached,
#: and nothing will retry it without an operator.
_QUEUE_IN_FLIGHT = ("QUEUED", "RUNNING", "RETRY_WAIT")

#: What "parser failure" MEANS, spelled with the codes the inbound path writes.
#: `parser_review_required` is the operator-gated one; the other two are hard
#: refusals. Quarantine and integrity errors are deliberately absent -- they are
#: storage failures, and folding them in would send an operator to fix a file
#: format that was never the problem.
_PARSER_FAILURE_CODES: tuple[str, ...] = (
    "parser_review_required",
    "file_not_parseable",
    "delivery_not_parseable",
)


def _read_queue(conn, *, datastream_id: str) -> dict:
    """The SIXTH layer AC1 names, and the one nothing assembled.

    AC1 separates << installation, domain/provider route, tenant activation,
    Datastream delivery channel, QUEUE and latest import/publication >>. Five
    layers were built. `app.inbound_scan_jobs` -- the durable work queue behind
    every attachment (migration 186) -- was read by no layer at all, while the
    inbox and the timeline each read it for their own row-level purposes.

    THE MEASURABLE CONSEQUENCE, and the reason this is not tidying: a scan job
    sitting in `DEAD_LETTER` with a last ledger row at `published` made the whole
    connector answer `overall: "healthy"`. The weakest-layer fold only walks
    `layers`, so work that will never run again was invisible to the one field an
    operator reads first.

    A dead-lettered job is the Datastream operator's to repair -- the recovery
    route is the one `_read_delivery` already names -- so the authority and the
    repair are the same, and only the cause differs.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, count(*) FROM app.inbound_scan_jobs "
                "WHERE datastream_id = %s GROUP BY state",
                (datastream_id,),
            )
            by_state = {row[0]: int(row[1]) for row in cur.fetchall()}
            cur.execute(
                "SELECT min(queued_at) FROM app.inbound_scan_jobs "
                "WHERE datastream_id = %s AND state = ANY(%s)",
                (datastream_id, list(_QUEUE_IN_FLIGHT)),
            )
            oldest_in_flight = cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001 -- unreadable is not unhealthy
        logger.warning("inbound_health: queue unreadable: %s", type(exc).__name__)
        return _layer(
            "queue",
            state=STATE_UNKNOWN,
            healthy=None,
            authority=AUTHORITY_DATASTREAM_OPERATOR,
            blocking_cause="queue_state_unreadable",
        )

    dead_letter = by_state.get("DEAD_LETTER", 0)
    in_flight = sum(by_state.get(state, 0) for state in _QUEUE_IN_FLIGHT)
    total = sum(by_state.values())

    if dead_letter:
        state, healthy, cause = "DEAD_LETTER", False, "scan_jobs_in_dead_letter"
        next_action = (
            "Inspect the attachment inbox and recover the dead-lettered scan jobs."
        )
    elif in_flight:
        # Work in the queue is the NORMAL state of a queue, not a fault. The age
        # of the oldest waiting job is reported so a reader can decide; no
        # threshold is invented here.
        state, healthy, cause, next_action = "DRAINING", True, None, None
    elif total:
        state, healthy, cause, next_action = "IDLE", True, None, None
    else:
        # NEVER A ZERO FOR A THING THAT WAS NEVER TRIED. No job at all cannot be
        # told apart from "no delivery has ever arrived", and reporting that as a
        # drained queue would be a green light nobody earned.
        state, healthy, cause, next_action = (
            "NO_JOB_YET",
            None,
            "no_scan_job_recorded_yet",
            None,
        )

    return _layer(
        "queue",
        state=state,
        healthy=healthy,
        authority=AUTHORITY_DATASTREAM_OPERATOR,
        blocking_cause=cause,
        next_action=next_action,
        recovery=_recovery(
            api=(
                "/api/connectors/{connector_name}/datastreams/{datastream_id}"
                "/scan-jobs/{job_id}/recover"
            ),
            mcp_tool="list_inbound_attachments",
            console_tab="overview",
            datastream_id=datastream_id,
        ),
        jobs_by_state=by_state,
        jobs_total=total,
        dead_letter_count=dead_letter,
        in_flight_count=in_flight,
        oldest_in_flight_queued_at=_dt2iso(oldest_in_flight),
    )


#: LES ISSUES QUI VALENT SANTE, LUES CHEZ CELUI QUI LES DEFINIT. Cette liste
#: nommait `succeeded` et `ok` : deux valeurs que la CHECK de la migration 077
#: n'autorise pas et qui ne pouvaient donc JAMAIS apparaitre. En echange `noop`
#: et `opened` en etaient absents et tombaient dans NO_PUBLICATION -- un doublon
#: deliberement saute et un import EN VOL faisaient lire le connecteur comme
#: bloque, et le pli par la couche la plus faible propageait cela a `overall`.
#:
#: `noop` EST sain : la migration 077 le decrit comme la lignee de fraicheur --
#: << ce run a confirme que l'instantane de la ligne X est a jour >>. Rien de
#: neuf a publier est le contraire d'un probleme.
#: `written` reste un defaut de publication : ecrit et non publie, le lecteur
#: n'est pas servi.
_HEALTHY_OUTCOMES: frozenset[str] = frozenset({OUTCOME_PUBLISHED, OUTCOME_NOOP})

#: L'import commence et sans issue. Nomme a part parce qu'il n'est ni sain ni
#: casse, et que les confondre est exactement ce que cette story doit empecher.
_OUTCOME_IN_FLIGHT = OUTCOME_OPENED


def _read_data(conn, *, datastream_id: str) -> dict:
    """Latest import AND last-known-good publication -- two fields, never one."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, outcome, row_count, rejected_row_count, error_code, "
                "       created_at "
                "FROM app.managed_feed_import_ledger "
                "WHERE datastream_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (datastream_id,),
            )
            latest = cur.fetchone()
            cur.execute(
                "SELECT current_published_execution_id FROM app.datastreams WHERE id = %s",
                (datastream_id,),
            )
            published_row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbound_health: data unreadable: %s", type(exc).__name__)
        return _layer(
            "data",
            state=STATE_UNKNOWN,
            healthy=None,
            authority=AUTHORITY_DATASTREAM_OPERATOR,
            blocking_cause="import_state_unreadable",
        )

    published = published_row[0] if published_row else None

    latest_import = None
    if latest is not None:
        ledger_id, outcome, rows, rejected, error_code, created = latest
        latest_import = {
            "import_ledger_id": ledger_id,
            "outcome": outcome,
            "row_count": rows,
            "rejected_row_count": rejected,
            "error_code": error_code,
            "created_at": _dt2iso(created),
        }

    # A failed latest import with a serving publication is DEGRADED, not down:
    # readers are still being answered correctly from the last known good.
    if latest_import is None:
        state, healthy, cause = "NO_IMPORT_YET", None, "no_delivery_received_yet"
    elif latest_import["outcome"] in _HEALTHY_OUTCOMES:
        state, healthy, cause = "OK", True, None
    elif latest_import["outcome"] == _OUTCOME_IN_FLIGHT:
        # LA SEULE ISSUE NON TERMINALE (migration 077, son propre commentaire).
        # Un import en vol n'a pas d'issue : le dire sain mentirait, le dire casse
        # accuserait le connecteur d'une panne qui n'existe pas. Inconnu, donc --
        # la meme reponse que pour une couche illisible, et pour la meme raison.
        state, healthy, cause = "IMPORT_IN_FLIGHT", None, "import_in_flight"
    elif published:
        state, healthy, cause = (
            "DEGRADED_LAST_GOOD_SERVING",
            False,
            (latest_import.get("error_code") or "latest_import_did_not_publish"),
        )
    else:
        state, healthy, cause = (
            "NO_PUBLICATION",
            False,
            (latest_import.get("error_code") or "latest_import_did_not_publish"),
        )

    return _layer(
        "data",
        state=state,
        healthy=healthy,
        authority=AUTHORITY_DATASTREAM_OPERATOR,
        blocking_cause=cause,
        recovery=_recovery(
            api=(
                "/api/connectors/{connector_name}/datastreams/{datastream_id}"
                "/raw-imports/{raw_import_id}/reprocess"
            ),
            mcp_tool="prepare_inbound_reprocess",
            console_tab="overview",
            datastream_id=datastream_id,
        ),
        next_action=(
            None
            if healthy is not False
            else "Inspect the import inbox and repair the mapping, or reprocess."
        ),
        latest_import=latest_import,
        # Deliberately its own field: a failed candidate must never make a
        # serving publication look absent.
        last_known_good_publication=published,
    )


# ---------------------------------------------------------------------------
# Metrics (AC3) -- counts and states, never a secret or a row.
# ---------------------------------------------------------------------------


def _read_metrics(conn, *, datastream_id: str, window_days: int = 30) -> dict:
    """Delivery and attachment counters over a bounded window."""
    metrics: dict[str, Any] = {"window_days": window_days}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, count(*) FROM app.inbound_receipts "
                "WHERE datastream_id = %s "
                "  AND created_at > NOW() - make_interval(days => %s) "
                "GROUP BY state",
                (datastream_id, window_days),
            )
            by_state = {row[0]: row[1] for row in cur.fetchall()}
            metrics["deliveries_by_state"] = by_state
            metrics["deliveries_total"] = sum(by_state.values())

            cur.execute(
                "SELECT state, count(*) FROM app.inbound_raw_imports "
                "WHERE datastream_id = %s "
                "  AND created_at > NOW() - make_interval(days => %s) "
                "GROUP BY state",
                (datastream_id, window_days),
            )
            att_state = {row[0]: row[1] for row in cur.fetchall()}
            metrics["attachments_by_state"] = att_state
            metrics["attachments_total"] = sum(att_state.values())

            # DUPLICATE CONTENT: the same bytes arriving more than once.
            #
            # A COUNT AND A RATE ARE TWO THINGS, and this block used to emit one
            # under a comment promising the other -- "A rate, not a verdict"
            # above a `count(*)`. Both are produced now, and each is named for
            # what it is:
            #   repeated_content_hashes -- how many distinct files arrived twice
            #   duplicate_attachments   -- how many arrivals were repeats
            #   duplicate_rate          -- repeats as a share of all arrivals
            #
            # Whether a repeat is wrong remains policy; none of the three says.
            cur.execute(
                "SELECT count(*), COALESCE(sum(repeats - 1), 0) FROM ("
                "  SELECT content_hash, count(*) AS repeats "
                "  FROM app.inbound_raw_imports "
                "  WHERE datastream_id = %s "
                "    AND created_at > NOW() - make_interval(days => %s) "
                "  GROUP BY content_hash HAVING count(*) > 1"
                ") repeated",
                (datastream_id, window_days),
            )
            repeated_hashes, duplicate_attachments = cur.fetchone()
            metrics["repeated_content_hashes"] = int(repeated_hashes or 0)
            metrics["duplicate_attachments"] = int(duplicate_attachments or 0)
            # HONEST-NULL, NOT ZERO. No arrival at all means the rate has no
            # denominator; 0.0 would read as "nothing was duplicated", which is
            # a measurement nobody made.
            attachments_total = metrics.get("attachments_total") or 0
            metrics["duplicate_rate"] = (
                round(metrics["duplicate_attachments"] / attachments_total, 4)
                if attachments_total
                else None
            )

            # PARSER FAILURES -- named by AC3 and present nowhere until now.
            # The codes are the ones the inbound path actually writes; counting
            # "any failure" instead would fold quarantine and integrity errors
            # into a number an operator would read as a file-format problem.
            cur.execute(
                "SELECT count(*) FROM app.inbound_raw_imports "
                "WHERE datastream_id = %s "
                "  AND created_at > NOW() - make_interval(days => %s) "
                "  AND error_code = ANY(%s)",
                (datastream_id, window_days, list(_PARSER_FAILURE_CODES)),
            )
            metrics["parser_failures"] = int(cur.fetchone()[0] or 0)

            # LAST SUCCESSFUL PUBLICATION -- the other AC3 reading that did not
            # exist. `_read_data` reports the CURRENT pointer; this says when a
            # publication last succeeded, which is the question an operator asks
            # when the latest import failed.
            cur.execute(
                "SELECT id, created_at FROM app.managed_feed_import_ledger "
                "WHERE datastream_id = %s AND outcome = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (datastream_id, OUTCOME_PUBLISHED),
            )
            published_row = cur.fetchone()
            metrics["last_successful_publication"] = (
                {
                    "import_ledger_id": published_row[0],
                    "published_at": _dt2iso(published_row[1]),
                }
                if published_row
                else None
            )

            # DENIED DELIVERIES -- asked for by AC3, and IMPOSSIBLE to count.
            #
            # A delivery refused at the credential never creates a receipt, and
            # `app.inbound_receipts.state` admits only
            # RECEIVED|PROCESSING|LANDED|REJECTED|FAILED -- there is no row to
            # count and no state to count it in. Emitting 0 here would be the
            # worst available answer: it reads as "nothing was denied" when the
            # truth is "this surface cannot see denials at all".
            metrics["denied_deliveries"] = {
                "available": False,
                "reason": "not_applicable",
                "detail": (
                    "A delivery denied at the credential creates no receipt, so "
                    "denials are not observable from inbound evidence. Rate-limit "
                    "and resolution events carry that history."
                ),
            }

            cur.execute(
                "SELECT max(created_at) FROM app.inbound_receipts WHERE datastream_id = %s",
                (datastream_id,),
            )
            metrics["last_receipt_at"] = _dt2iso(cur.fetchone()[0])

            # Age of the oldest delivery still mid-flight: the one number that
            # says "the worker is not draining" without needing a queue probe.
            cur.execute(
                "SELECT min(created_at) FROM app.inbound_receipts "
                "WHERE datastream_id = %s AND state IN ('RECEIVED', 'PROCESSING')",
                (datastream_id,),
            )
            metrics["oldest_in_flight_at"] = _dt2iso(cur.fetchone()[0])

            # LA LATENCE DE TRAITEMENT -- l autre moitie d AC3.
            #
            # `oldest_in_flight_at` dit qu une livraison ATTEND ; il ne dit pas
            # combien de temps prend une livraison qui aboutit. Les deux
            # repondent a des questions differentes : la premiere << le worker
            # draine-t-il ? >>, la seconde << a quoi ressemble un traitement
            # normal ? >>. Sans la seconde, personne ne peut dire si la premiere
            # est anormale.
            #
            # Mesuree sur les recus TERMINAUX seulement : pour eux, `updated_at`
            # est l instant ou l etat final a ete pose. Sur un recu en vol la
            # duree court encore, et l inclure ferait baisser la mediane a chaque
            # fois que la file s allonge -- exactement le contraire du signal.
            #
            # L ECHANTILLON VOYAGE AVEC LA MESURE. Une mediane sur deux recus
            # n est pas une mediane, et un lecteur qui ne voit pas le compte la
            # traite comme si elle en etait une. Aucun seuil n est invente ici :
            # le compte est rendu, et qui lit decide.
            cur.execute(
                "SELECT count(*), "
                "       percentile_cont(0.5) WITHIN GROUP ("
                "         ORDER BY EXTRACT(EPOCH FROM (updated_at - created_at))), "
                "       max(EXTRACT(EPOCH FROM (updated_at - created_at))) "
                "FROM app.inbound_receipts "
                "WHERE datastream_id = %s "
                "  AND created_at > NOW() - make_interval(days => %s) "
                "  AND state IN ('LANDED', 'REJECTED', 'FAILED')",
                (datastream_id, window_days),
            )
            settled, median_seconds, max_seconds = cur.fetchone()
            metrics["settled_receipts"] = int(settled or 0)
            metrics["processing_latency_median_seconds"] = (
                round(float(median_seconds), 3) if median_seconds is not None else None
            )
            metrics["processing_latency_max_seconds"] = (
                round(float(max_seconds), 3) if max_seconds is not None else None
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbound_health: metrics unreadable: %s", type(exc).__name__)
        metrics["unavailable"] = True
    return metrics


# ---------------------------------------------------------------------------
# Public surface.
# ---------------------------------------------------------------------------


def get_inbound_health(
    conn,
    *,
    connector_name: str,
    environment: str,
    datastream_id: str | None = None,
    org_id: str | None = None,
    window_days: int = 30,
) -> dict[str, Any]:
    """The layered health answer. One backend, one shape, console and MCP alike.

    Datastream-scoped layers are omitted rather than faked when no
    ``datastream_id`` is given: an absent layer is honest, an empty one is not.

    The overall verdict is the WEAKEST layer, and it carries the authority of
    that layer -- so "who fixes this" is answered by the same call that says
    something is wrong (AC4).
    """
    layers: list[dict[str, Any]] = [
        _read_installation(conn, connector_name=connector_name, environment=environment),
        _read_domain(conn, connector_name=connector_name, environment=environment),
    ]
    if org_id:
        layers.append(
            _read_activation(
                conn,
                connector_name=connector_name,
                environment=environment,
                org_id=org_id,
            )
        )
    metrics = None
    if datastream_id:
        layers.append(_read_delivery(conn, datastream_id=datastream_id))
        # BETWEEN DELIVERY AND DATA, because that is where the work sits: a file
        # has arrived and has not yet become an import. AC1 orders the six states
        # this way, and the fold below walks the list in order.
        layers.append(_read_queue(conn, datastream_id=datastream_id))
        layers.append(_read_data(conn, datastream_id=datastream_id))
        metrics = _read_metrics(conn, datastream_id=datastream_id, window_days=window_days)

    # The weakest layer decides. Order matters: a false beats an unknown, because
    # a known break is more actionable than an unread one -- but an unknown still
    # beats healthy, so nothing is ever reported green on missing evidence.
    overall = "healthy"
    authority = AUTHORITY_NONE
    blocking = None
    for layer in layers:
        if layer["healthy"] is False:
            overall, authority, blocking = (
                "blocked",
                layer["authority"],
                layer["blocking_cause"],
            )
            break
        if layer["healthy"] is None and overall == "healthy":
            overall, authority, blocking = (
                "unknown",
                layer["authority"],
                layer["blocking_cause"],
            )

    return {
        "connector_name": connector_name,
        "environment": environment,
        "datastream_id": datastream_id,
        "overall": overall,
        "authority": authority,
        "blocking_cause": blocking,
        "layers": layers,
        "metrics": metrics,
    }


_SCAN_POLICY_KEYS = frozenset(
    {
        "max_bytes",
        "max_uncompressed_bytes",
        "max_compression_ratio",
        "max_archive_entries",
        "max_rows",
        "max_columns",
        "max_scan_seconds",
        "max_memory_bytes",
    }
)
_SCAN_EVIDENCE_KEYS = frozenset(
    {
        "size_bytes",
        "size_limit",
        "row_estimate",
        "row_limit",
        "spreadsheet_rows",
        "spreadsheet_columns",
        "column_estimate",
        "column_limit",
        "archive_entries",
        "archive_central_directory_limit",
        "archive_entry_limit",
        "archive_central_directory_bytes",
        "archive_uncompressed_bytes",
        "archive_uncompressed_limit",
        "archive_worst_ratio",
        "archive_ratio_limit",
        "archive_readable",
        "archive_traversal_entry",
        "archive_encrypted_entry",
        "header_inspection_bytes",
        "header_terminated",
    }
)


def _safe_evidence_code(value: Any, *, maximum: int = 120) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        return None
    if not all(char.isalnum() or char in "._:/+-" for char in value):
        return None
    return value


#: CE QU'AUCUNE SURFACE NE REND, quelle que soit la porte. `quarantine_uri` est
#: l'adresse d'un objet retenu et `error_detail` le detail brut d'un parseur,
#: qui peut porter des valeurs de lignes -- l'inbox les retirait deja, la
#: chronologie les laissait passer. Une seule liste, pour que les deux ne puissent
#: plus decrire la meme livraison differemment.
_WITHHELD_FROM_EVERY_SURFACE = frozenset({"quarantine_uri", "error_detail"})


def redact_scan_verdict(value: Any) -> dict[str, Any] | None:
    """Allowlist content-free scan evidence before console or MCP exposure."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    if isinstance(value.get("accepted"), bool):
        out["accepted"] = value["accepted"]
    for key in ("reason", "detected_type", "declared_type", "malware_engine"):
        safe = _safe_evidence_code(value.get(key))
        if safe is not None:
            out[key] = safe
    if isinstance(value.get("size_bytes"), int):
        out["size_bytes"] = value["size_bytes"]
    malware = value.get("malware")
    if malware in {"clean", "infected", "unavailable", "not_run"}:
        out["malware"] = malware
    detail = value.get("malware_detail")
    if detail in {None, "scanner_clean", "signature_detected"}:
        out["malware_detail"] = detail
    policy = value.get("policy")
    if isinstance(policy, dict):
        safe_policy = {
            key: item
            for key, item in policy.items()
            if key in _SCAN_POLICY_KEYS and isinstance(item, (int, float))
        }
        version = _safe_evidence_code(policy.get("version"))
        if version is not None:
            safe_policy["version"] = version
        out["policy"] = safe_policy
    evidence = value.get("evidence")
    if isinstance(evidence, dict):
        out["evidence"] = {
            key: item
            for key, item in evidence.items()
            if key in _SCAN_EVIDENCE_KEYS
            and (isinstance(item, (int, float, bool)) or item == "infinite")
        }
        encoding = _safe_evidence_code(evidence.get("encoding"))
        if encoding is not None:
            out["evidence"]["encoding"] = encoding
    return out


def _redact_scan_job(
    state: Any,
    attempts: Any,
    maximum: Any,
    error: Any,
    recovery: Any,
    recovery_count: Any,
    updated_at: Any,
) -> dict[str, Any] | None:
    if state is None:
        return None
    out = {
        "state": state
        if state
        in {
            "QUEUED",
            "RUNNING",
            "RETRY_WAIT",
            "SUCCEEDED",
            "REJECTED",
            "DEAD_LETTER",
        }
        else "UNKNOWN",
        "attempt_count": attempts if isinstance(attempts, int) else None,
        "max_attempts": maximum if isinstance(maximum, int) else None,
        "error_code": _safe_evidence_code(error),
        "recovery_count": recovery_count if isinstance(recovery_count, int) else 0,
        "updated_at": _dt2iso(updated_at),
    }
    if isinstance(recovery, dict):
        out["recovery"] = {
            "version": _safe_evidence_code(recovery.get("version")),
            "command": _safe_evidence_code(recovery.get("command")),
            "job_id": _safe_evidence_code(recovery.get("job_id")),
            "requires_authorization": recovery.get("requires_authorization") is True,
        }
    return out


def _redact_parser_review_detail(error_code: str | None, detail: Any) -> dict[str, Any] | None:
    """Expose only bounded parser issue codes/field labels from operator evidence."""
    if error_code != "parser_review_required" or not isinstance(detail, str):
        return None
    try:
        payload = json.loads(detail)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != "parser-review-v1":
        return None
    from core.inbound_scan import neutralise_untrusted  # noqa: PLC0415

    issues = []
    for raw in payload.get("issues", [])[:20]:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "")[:64]
        if not code or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in code):
            continue
        issues.append(
            {
                "code": code,
                "field": neutralise_untrusted(
                    str(raw["field"]) if raw.get("field") is not None else None,
                    max_len=120,
                ),
            }
        )
    return {"schema": "parser-review-v1", "issues": issues}


def get_attachment_inbox(
    conn,
    *,
    datastream_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """One row PER ATTACHMENT, with its delivery beside it (E38-UX06).

    This is the surface that did not exist. The previous reader was a join from
    the import ledger, so it could only ever show attachments that had already
    produced a ledger row -- that is, the successes. A denied, rejected or
    failed delivery appeared nowhere, which is the opposite of what an inbox is
    for.

    The scan verdict travels with each row so an operator sees WHY a file was
    refused without downloading a byte of it (38.10 AC5).
    """
    if not isinstance(limit, int) or limit <= 0 or limit > 200:
        raise ValueError("limit must be an integer in 1..200")
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.id, COALESCE(r.ordinal,j.attachment_ordinal), "
            "       r.filename, r.media_type_declared, r.media_type_detected, "
            "       r.size_bytes, r.content_hash, COALESCE(r.state,j.state), "
            "       r.scan_verdict, COALESCE(r.error_code,j.error_code), "
            "       r.error_detail, r.import_ledger_id, COALESCE(r.created_at,j.queued_at), "
            "       rx.id, rx.channel, rx.state, rx.provider_event_id, "
            "       rx.created_at, j.state, j.attempt_count, j.max_attempts, "
            "       j.error_code, j.recovery_evidence, j.recovery_count, "
            "       j.updated_at "
            "FROM app.inbound_scan_jobs j "
            "FULL OUTER JOIN app.inbound_raw_imports r "
            " ON r.receipt_id=j.receipt_id AND r.ordinal=j.attachment_ordinal "
            "JOIN app.inbound_receipts rx "
            " ON rx.id=COALESCE(j.receipt_id,r.receipt_id) "
            "WHERE COALESCE(j.datastream_id,r.datastream_id) = %s "
            "ORDER BY COALESCE(r.created_at,j.queued_at) DESC, "
            "         COALESCE(r.ordinal,j.attachment_ordinal) ASC "
            "LIMIT %s OFFSET %s",
            (datastream_id, limit, offset),
        )
        rows = cur.fetchall()

    from core.inbound_scan import neutralise_untrusted  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for row in rows:
        (
            raw_id,
            ordinal,
            filename,
            declared,
            detected,
            size,
            digest,
            state,
            verdict,
            error_code,
            error_detail,
            ledger_id,
            created,
            receipt_id,
            channel,
            receipt_state,
            provider_event_id,
            receipt_created,
            job_state,
            attempts,
            max_attempts,
            job_error,
            recovery,
            recovery_count,
            job_updated,
        ) = row
        out.append(
            {
                "raw_import_id": raw_id,
                "ordinal": ordinal,
                # Bounded and defused: this is the sender's string, and it is shown.
                "filename": neutralise_untrusted(filename),
                "media_type_declared": declared,
                "media_type_detected": detected,
                "size_bytes": size,
                "content_hash": digest,
                "state": state,
                "scan_verdict": redact_scan_verdict(verdict),
                "scan_job": _redact_scan_job(
                    job_state,
                    attempts,
                    max_attempts,
                    job_error,
                    recovery,
                    recovery_count,
                    job_updated,
                ),
                "error_code": error_code,
                "parser_review": _redact_parser_review_detail(error_code, error_detail),
                "import_ledger_id": ledger_id,
                "created_at": _dt2iso(created),
                "receipt": {
                    "receipt_id": receipt_id,
                    "channel": channel,
                    "state": receipt_state,
                    # An opaque provider key, not a secret: it is what an operator
                    # quotes when asking why a delivery did not arrive.
                    "provider_event_id": provider_event_id,
                    "created_at": _dt2iso(receipt_created),
                },
            }
        )
    return out


def _downstream_stages(conn, ledger_ids: list[str], datastream_id: str) -> dict[str, dict]:
    """The MAPPING, DQ and PUBLICATION stages of the spine, per import-ledger row.

    AC2 asks for one trace spine across << receipt, quarantine, scan, parse,
    MAPPING, DQ and PUBLICATION >>. The timeline stopped at parse. Everything
    after it existed and was simply never read: `app.managed_feed_import_ledger`
    carries the mapping/plan versions, the accepted and rejected row counts, and
    a terminal `outcome` that says whether anything was published at all.

    C EST LA MOITIE QUI FAISAIT MENTIR LA PREMIERE. `state = 'LANDED'` on an
    attachment means << it produced an import-ledger row >>, not << it was
    published >> -- the ledger row can sit at `written` (a candidate exists and
    nothing is live), `rejected` (the DQ gate refused it) or `failed`. Reading
    LANDED as publication told an operator their data had changed when the only
    thing that had changed was a candidate nobody promoted.

    Withheld deliberately, matching the rest of this surface:

    * `landing_relation` -- the address of an internal raw relation. An operator
      acts on the Datastream, never on the relation, and naming it invites a
      query nobody should be writing by hand.
    * `error_detail` verbatim -- `_WITHHELD_FROM_EVERY_SURFACE` already excludes
      it on the attachment, and letting it back in through a second route is
      exactly the two-surfaces-one-redaction defect this module fought before.
      The bounded `error_code` is kept: it is what an operator acts on.
    """
    if not ledger_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, mapping_version_id, plan_version_id, write_mode, "
            "       row_count, rejected_row_count, outcome, error_code, "
            "       execution_id, superseded_ledger_id, snapshot_observed_at "
            "FROM app.managed_feed_import_ledger "
            "WHERE id = ANY(%s) AND datastream_id = %s",
            (list(ledger_ids), datastream_id),
        )
        rows = cur.fetchall()

    stages: dict[str, dict] = {}
    for (
        ledger_id, mapping_version_id, plan_version_id, write_mode,
        row_count, rejected_row_count, outcome, error_code,
        execution_id, superseded_ledger_id, observed_at,
    ) in rows:
        accepted = int(row_count) if row_count is not None else None
        rejected = int(rejected_row_count or 0)
        # LE TAUX N EST RENDU QUE QUAND IL EN EST UN. row_count is honest-NULL
        # until measured (AD-9), and 0 rejected out of an unknown total is not
        # << 0% >> -- it is << nothing counted yet >>. Inventing a denominator
        # here would produce a clean rate for a run that never read a row.
        total = (accepted + rejected) if accepted is not None else None
        stages[ledger_id] = {
            "mapping": {
                "mapping_version_id": mapping_version_id,
                "plan_version_id": plan_version_id,
                "write_mode": write_mode,
            },
            "dq": {
                "accepted_row_count": accepted,
                "rejected_row_count": rejected,
                "rejected_row_pct": (
                    round(100.0 * rejected / total, 3) if total else None
                ),
            },
            "publication": {
                "outcome": outcome,
                # The reader must not have to know that 'written' means << a
                # candidate exists and nothing is live >>. Stated, not deduced.
                "published": outcome == "published",
                "execution_id": execution_id,
                "superseded_ledger_id": superseded_ledger_id,
                "error_code": _safe_evidence_code(error_code),
                "snapshot_observed_at": _dt2iso(observed_at),
            },
        }
    return stages


def get_delivery_timeline(conn, *, receipt_id: str, datastream_id: str) -> dict[str, Any] | None:
    """One delivery, with every attachment's stage -- the trace spine (AC2).

    Returns None when the receipt does not belong to this Datastream, which is
    indistinguishable from "no such receipt" on purpose (E38-NFR01).

    ``published_data_changed`` is stated EXPLICITLY rather than inferred by the
    reader: AC2 requires the interface to say whether published data changed,
    and leaving that to be deduced from an outcome string is how two surfaces
    end up disagreeing about it.
    """
    from core.inbound_raw_imports import list_raw_imports_for_receipt  # noqa: PLC0415
    from core.inbound_receipts import get_receipt  # noqa: PLC0415

    receipt = get_receipt(conn, receipt_id=receipt_id, datastream_id=datastream_id)
    if receipt is None:
        return None

    from core.inbound_scan import neutralise_untrusted  # noqa: PLC0415

    attachments = list_raw_imports_for_receipt(
        conn, receipt_id=receipt_id, datastream_id=datastream_id
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attachment_ordinal, state, attempt_count, max_attempts, "
            "       error_code, recovery_evidence, recovery_count, updated_at "
            "FROM app.inbound_scan_jobs "
            "WHERE receipt_id=%s AND datastream_id=%s",
            (receipt_id, datastream_id),
        )
        scan_jobs = {
            row[0]: _redact_scan_job(
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
            )
            for row in cur.fetchall()
        }
    # DEUX SURFACES, UNE SEULE REDACTION. Cette liste etendait le read-model brut
    # (`inbound_raw_imports._safe_read_model`), qui porte `quarantine_uri` et le
    # `error_detail` VERBATIM -- deux choses que l'inbox retire deliberement au
    # profit d'un `parser_review` redige. La meme piece jointe se lisait donc
    # differemment selon la route empruntee, ce que l'en-tete de
    # `inbound_health_api` interdit explicitement : << a handler that reshapes is
    # a handler that can leak something the read model was careful to exclude >>.
    # L'adresse d'un objet en quarantaine et le detail brut d'un parseur sont
    # exactement ce genre de chose.
    attachments = [
        {
            **{
                key: value
                for key, value in attachment.items()
                if key not in _WITHHELD_FROM_EVERY_SURFACE
            },
            "filename": neutralise_untrusted(attachment.get("filename")),
            "scan_verdict": redact_scan_verdict(attachment.get("scan_verdict")),
            "parser_review": _redact_parser_review_detail(
                attachment.get("error_code"), attachment.get("error_detail")
            ),
            "scan_job": scan_jobs.get(attachment.get("ordinal")),
        }
        for attachment in attachments
    ]
    raw_ordinals = {attachment.get("ordinal") for attachment in attachments}
    attachments.extend(
        {
            "raw_import_id": None,
            "ordinal": ordinal,
            "filename": None,
            "media_type_declared": None,
            "media_type_detected": None,
            "size_bytes": None,
            "content_hash": None,
            "state": job["state"],
            "scan_verdict": None,
            "scan_job": job,
            "error_code": job.get("error_code"),
            "import_ledger_id": None,
            "created_at": job.get("updated_at"),
        }
        for ordinal, job in scan_jobs.items()
        if ordinal not in raw_ordinals and job is not None
    )
    landed = [a for a in attachments if a.get("state") == "LANDED"]

    # La seconde moitie de la colonne vertebrale : mapping, DQ, publication.
    downstream = _downstream_stages(
        conn,
        [a["import_ledger_id"] for a in attachments if a.get("import_ledger_id")],
        datastream_id,
    )
    for attachment in attachments:
        attachment["downstream"] = downstream.get(attachment.get("import_ledger_id") or "")
    published = [
        a
        for a in attachments
        if (a.get("downstream") or {}).get("publication", {}).get("published")
    ]

    return {
        "receipt": receipt,
        "attachments": attachments,
        "attachment_count": len(attachments),
        "landed_count": len(landed),
        "published_count": len(published),
        # MESURE, PLUS DEDUCTION. Ceci valait `bool(landed)`, et LANDED ne veut
        # pas dire publie : il veut dire << une ligne de ledger existe >>. Une
        # livraison arretee au candidat, refusee par la porte DQ ou en echec
        # annoncait donc a l operateur que ses donnees publiees avaient change.
        "published_data_changed": bool(published),
    }
