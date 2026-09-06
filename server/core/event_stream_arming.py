"""Chantier C -- arming an event stream is ONE gesture, on any domain.

WHAT THIS CLOSES. A governed Event Configuration reaches `active` through four
operations -- create the configuration, create a version, confirm it, activate it
-- and every one of them is an HTTP route nothing in the console calls. Measured
2026-08-14: `grep -rn "event-configurations" ui/admin/src` returns three hits, all
reads, none a write. So the object could only be created by someone who knew the
four addresses and could compose their payloads by hand, which is how the three
that exist in production got there.

AND NOTHING ABOUT IT WAS VIDEO. The payload those three carry is

    source_mapping    {"module": <connector>, "report_id": <profile>, "events": [...]}
    collection_policy {"cadence": <nightly|...>, "timezone": "UTC"}

and every field of it is already ON THE DATASTREAM (`module_name`,
`report_profile_id`, `schedule_mode`) or in the Connector manifest that Datastream
was built from. There is no question left to ask, so this module asks none: it
DERIVES the configuration and runs the ladder.

FIVE CONNECTORS, FIVE DOMAINS, one code path (measured the same day):
`google-business-profile:social_post`, `meta-ads:campaign_launch`,
`monday:board_events`, `shopify:product_launch`, `youtube-analytics:video_upload`.
Nothing below names any of them.

THE LADDER IS NOT SKIPPED, IT IS WALKED. `confirm_event_configuration_version`
declares `confirmation_mode="human"` and records the actor as its confirmation
reference: the person who asked for this IS the human confirming, and the audit
row says so. What is removed is the requirement to know that four operations
exist -- not the operations, not their evidence, and not their order.

Contract: docs/product-architecture/data.md, "Amendment, chantier C".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The one policy an armed stream starts with. `timezone` is `UTC` because the
#: date grain is a DATE everywhere (`date-grain-no-hourly`), and a stream armed
#: under a local zone would be the only object in the product claiming otherwise.
DEFAULT_TIMEZONE = "UTC"

#: What a Datastream's schedule means to a collection policy. An unknown mode
#: keeps its own name rather than being flattened to `nightly`: a policy that
#: says `nightly` about a stream that runs hourly is a wrong statement, and a
#: policy that repeats the schedule's own word is merely an unfamiliar one.
_DEFAULT_CADENCE = "nightly"


class EventStreamRefused(ValueError):
    """Arming was refused. ``message`` names the gesture that repairs it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def declared_events(module_name: str, report_profile_id: str) -> list[str]:
    """The event names a Connector's report profile declares, or an empty list.

    Read from the loaded module registry, exactly like `core.connectors_mcp`
    reads its profile summaries -- never from a table and never from a list of
    connector names in this file (AD-2: zero provider vocabulary here).
    """
    from core.main import get_loaded_modules  # noqa: PLC0415

    for loaded in get_loaded_modules():
        if getattr(loaded, "name", None) != module_name:
            continue
        manifest = getattr(loaded, "manifest", {}) or {}
        for profile in manifest.get("report_profiles") or ():
            if not isinstance(profile, dict) or profile.get("id") != report_profile_id:
                continue
            return [str(name) for name in (profile.get("events") or ()) if str(name)]
    return []


def plan_event_stream(conn, *, project_id: str, datastream_id: str) -> dict[str, Any]:
    """What arming this Datastream would create -- derived, nothing asked.

    Separated from the arming itself so a screen can show the exact payload
    before anyone commits to it. A gesture whose consequence cannot be read
    first is a gesture people click twice to find out what it does.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, module_name, report_profile_id, schedule_mode
            FROM app.datastreams
            WHERE id = %s AND project_id = %s AND archived_at IS NULL
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise EventStreamRefused(
            "datastream_not_found",
            "This Datastream is not in this Project, or has been archived.",
        )
    name, module_name, report_profile_id, schedule_mode = (str(value or "") for value in row)
    if not report_profile_id:
        raise EventStreamRefused(
            "no_report_profile",
            "This Datastream collects no declared report yet. Finish its setup, "
            "then arm its events.",
        )
    events = declared_events(module_name, report_profile_id)
    if not events:
        raise EventStreamRefused(
            "profile_declares_no_event",
            "What this Datastream collects is measurements, not events. Add a "
            "Datastream on a report that declares events, and arm that one.",
        )
    return {
        "datastream_id": datastream_id,
        "datastream_name": name,
        "configuration_name": events[0],
        "source_mapping": {
            "module": module_name,
            "report_id": report_profile_id,
            "events": events,
        },
        "collection_policy": {
            "cadence": schedule_mode or _DEFAULT_CADENCE,
            "timezone": DEFAULT_TIMEZONE,
        },
    }


def existing_configuration(conn, *, project_id: str, datastream_id: str) -> dict[str, Any] | None:
    """The live Event Configuration of this Datastream, when it already has one.

    Arming twice must not mint a second configuration for the same stream: the
    second would collect the same events under a second identity, and every
    later count of them would be double. The caller is told which one exists
    rather than being given a fresh one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.name, c.lifecycle_state,
                   v.id, v.version_number, v.review_state
            FROM app.event_configurations c
            LEFT JOIN LATERAL (
                SELECT id, version_number, review_state
                FROM app.event_configuration_versions
                WHERE event_configuration_id = c.id
                ORDER BY version_number DESC LIMIT 1
            ) v ON TRUE
            WHERE c.datastream_id = %s AND c.project_id = %s
              AND c.lifecycle_state <> 'archived'
            ORDER BY c.created_at
            LIMIT 1
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "event_configuration_id": str(row[0]),
        "name": str(row[1]),
        "lifecycle_state": str(row[2]),
        "version_id": str(row[3]) if row[3] else None,
        "version_number": row[4],
        "review_state": str(row[5]) if row[5] else None,
    }


def arm_event_stream(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    org_id: str,
    actor: str,
    name: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Create, version, confirm and activate -- one call, four recorded operations.

    Each step keeps its own operation, its own evidence and its own audit row.
    The idempotency keys share one stem, so a retried gesture replays the same
    four operations instead of minting a second configuration -- which is the
    same rule `publish-confirmations` and `publish-activate` already hold, and
    the one that costs a pass every time it is discovered by collision.
    """
    from core import event_configurations  # noqa: PLC0415

    live = existing_configuration(conn, project_id=project_id, datastream_id=datastream_id)
    if live is not None and live["lifecycle_state"] == "active":
        raise EventStreamRefused(
            "already_armed",
            f"This Datastream already collects events under `{live['name']}`. "
            "Open it to change what it collects.",
        )

    plan = plan_event_stream(conn, project_id=project_id, datastream_id=datastream_id)
    stem = f"arm-events-{datastream_id}"

    created = event_configurations.create_event_configuration(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        org_id=org_id,
        name=str(name or plan["configuration_name"]),
        actor=actor,
        idempotency_key=f"{stem}-create",
        trace_id=trace_id,
    )
    configuration_id = str(
        (created.get("result") or created).get("event_configuration_id") or ""
    )
    if not configuration_id:
        raise EventStreamRefused(
            "configuration_not_created",
            "The Event Configuration was not created. Nothing was armed; try again.",
        )

    versioned = event_configurations.create_event_configuration_version(
        conn,
        project_id=project_id,
        event_configuration_id=configuration_id,
        org_id=org_id,
        source_mapping=plan["source_mapping"],
        collection_policy=plan["collection_policy"],
        actor=actor,
        idempotency_key=f"{stem}-version",
        trace_id=trace_id,
    )
    version_id = str((versioned.get("result") or versioned).get("version_id") or "")
    if not version_id:
        raise EventStreamRefused(
            "version_not_created",
            "The Event Configuration exists but carries no version yet. Open it "
            "and add one.",
        )

    event_configurations.confirm_event_configuration_version(
        conn,
        project_id=project_id,
        event_configuration_id=configuration_id,
        version_id=version_id,
        org_id=org_id,
        actor=actor,
        idempotency_key=f"{stem}-confirm",
        trace_id=trace_id,
    )
    activated = event_configurations.activate_event_configuration_version(
        conn,
        project_id=project_id,
        event_configuration_id=configuration_id,
        version_id=version_id,
        org_id=org_id,
        actor=actor,
        idempotency_key=f"{stem}-activate",
        trace_id=trace_id,
    )
    return {
        "event_configuration_id": configuration_id,
        "version_id": version_id,
        "source_mapping": plan["source_mapping"],
        "collection_policy": plan["collection_policy"],
        "activation": activated.get("result") or activated,
    }


# ---------------------------------------------------------------------------
# Story 68.4: the same gesture for a stream that has NO Connector.
# ---------------------------------------------------------------------------


def plan_managed_feed_event_stream(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    event_types: list[str],
    mapping_version_id: str,
) -> dict[str, Any]:
    """What arming a MANAGED FEED's event stream would create -- derived too.

    `plan_event_stream` above derives everything from the Connector manifest,
    and a managed feed has no Connector: `module_name IS NULL`, no report
    profile, no declared events. Every field it needs is still declared
    somewhere -- in the pinned MAPPING, which names the event types its file
    carries (Story 68.4's roles). So this asks nothing either; it reads the
    other declaration.

    The anchor changes with it. A Connector stream anchors on its contract
    version; this one anchors on the pinned mapping version, which the store
    refuses to update just as firmly (migration 032). `module` is explicitly
    NULL rather than absent: "this stream has no Connector" is a fact, and an
    omitted key would read as "nobody said".
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, module_name, schedule_mode
            FROM app.datastreams
            WHERE id = %s AND project_id = %s AND archived_at IS NULL
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise EventStreamRefused(
            "datastream_not_found",
            "This Datastream is not in this Project, or has been archived.",
        )
    name, module_name, schedule_mode = row
    if module_name:
        raise EventStreamRefused(
            "datastream_has_a_connector",
            "This Datastream collects through a Connector. Arm its events from "
            "the Connector's declared report, not from a file mapping.",
        )
    types = [str(value).strip().lower() for value in event_types if str(value).strip()]
    if not types:
        raise EventStreamRefused(
            "file_declares_no_event_type",
            "The imported file names no event type. Declare the `type` column "
            "in the mapping, then import it again.",
        )
    if not str(mapping_version_id or "").strip():
        raise EventStreamRefused(
            "no_pinned_mapping",
            "This Datastream pins no mapping version, so nothing declares what "
            "its file carries. Publish its mapping, then import.",
        )
    return {
        "datastream_id": datastream_id,
        "datastream_name": str(name or ""),
        "configuration_name": types[0],
        "source_mapping": {
            "module": None,
            "mapping_version_id": str(mapping_version_id),
            "events": sorted(set(types)),
        },
        "collection_policy": {
            "cadence": str(schedule_mode or "") or _DEFAULT_CADENCE,
            "timezone": DEFAULT_TIMEZONE,
        },
    }


def ensure_managed_feed_event_stream(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    org_id: str,
    actor: str,
    event_types: list[str],
    mapping_version_id: str,
    trace_id: str | None = None,
) -> str:
    """The ACTIVE event configuration version of a managed feed, arming if needed.

    Idempotent by design: an already-active stream is READ, never re-armed --
    a second configuration would collect the same markers under a second
    identity and every later count of them would be double (the rule
    `existing_configuration` states). Returns the active version id, which is
    what the insert trigger demands every event carry.

    Runs on the caller's transaction: the arming, the events and the ledger row
    of one import land together or not at all.
    """
    from core import event_configurations  # noqa: PLC0415

    live = existing_configuration(conn, project_id=project_id, datastream_id=datastream_id)
    if live is not None and live["review_state"] == "active" and live["version_id"]:
        return str(live["version_id"])

    plan = plan_managed_feed_event_stream(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        event_types=event_types,
        mapping_version_id=mapping_version_id,
    )
    stem = f"arm-events-managed-{datastream_id}-{mapping_version_id}"

    configuration_id = str(live["event_configuration_id"]) if live is not None else ""
    if not configuration_id:
        created = event_configurations.create_event_configuration(
            conn,
            project_id=project_id,
            datastream_id=datastream_id,
            org_id=org_id,
            name=plan["configuration_name"],
            actor=actor,
            idempotency_key=f"{stem}-create",
            trace_id=trace_id,
        )
        configuration_id = str(
            (created.get("result") or created).get("event_configuration_id") or ""
        )
    if not configuration_id:
        raise EventStreamRefused(
            "configuration_not_created",
            "The Event Configuration was not created. Nothing was armed; try again.",
        )

    versioned = event_configurations.create_event_configuration_version(
        conn,
        project_id=project_id,
        event_configuration_id=configuration_id,
        org_id=org_id,
        source_mapping=plan["source_mapping"],
        collection_policy=plan["collection_policy"],
        actor=actor,
        idempotency_key=f"{stem}-version",
        trace_id=trace_id,
    )
    version_id = str((versioned.get("result") or versioned).get("version_id") or "")
    if not version_id:
        raise EventStreamRefused(
            "version_not_created",
            "The Event Configuration exists but carries no version yet.",
        )

    event_configurations.confirm_event_configuration_version(
        conn,
        project_id=project_id,
        event_configuration_id=configuration_id,
        version_id=version_id,
        org_id=org_id,
        actor=actor,
        idempotency_key=f"{stem}-confirm",
        trace_id=trace_id,
    )
    event_configurations.activate_event_configuration_version(
        conn,
        project_id=project_id,
        event_configuration_id=configuration_id,
        version_id=version_id,
        org_id=org_id,
        actor=actor,
        idempotency_key=f"{stem}-activate",
        trace_id=trace_id,
    )
    return version_id
