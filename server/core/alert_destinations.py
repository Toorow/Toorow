"""Where an alert goes once it has been written -- story 59.6.

THE GAP THIS MODULE CLOSES. Before it, what was PERSISTED was never SENT and
what was SENT was never PERSISTED. `app.alert_firings` holds every finding of
the nine DQ monitors and of the business, anomaly and mediaplan evaluators, and
its only reader was a SELECT. The in-memory signals of
`infra_alerts.evaluate_alerts` did reach a channel -- and they are computed in
memory, stored nowhere, and carry no `project_id` at all.

WHAT THIS MODULE ROUTES, AND WHAT IT DELIBERATELY DOES NOT.

  * It routes the **persisted firings** of `app.alert_firings`, per project.
  * It does NOT route the four `evaluate_alerts` signals -- `dead_letter_count`,
    `datastream_empty_streak`, `health_poller_staleness_seconds`,
    `mirror_sync_lag_seconds`. `AlertSignal` has no `project_id`
    (`infra_alerts.py:77`) and destinations are project-scoped, so those four
    stay Console-only. That is stated on the screen rather than hidden: a
    surface that listed destinations without saying which alerts cannot reach
    them would lie by omission.

ROUTING IS ON THE TYPE, NEVER ON THE SEVERITY. Eight DQ monitors out of eight
write `severity='warning'` (`dq_monitors.py:471,785,1003,1140,1321,1473,1695,1992`),
so a severity rule would put all of them in one bag and could not tell a schema
drift from a missed delivery. The discriminant is `alert_firings.type`, which is
`DqMonitor.alert_type` for every DQ finding -- and a rule naming a type the
registry of story 59.5 does not know is refused, `unknown_alert_type`.

A DESTINATION WITHOUT A RULE RECEIVES EVERYTHING. That is the only way the
non-DQ emitters (`business_threshold`, `anomaly`, `mediaplan_*`) reach a
destination at all, since the routable vocabulary is the DQ registry's.

THE SECRET LIVES IN THE ROW. Sealed by the project's tenant key with Fernet --
the exact shape `google_token_store` seals a Google token, which is the pattern
`app.connection_ref` already carries in production. A deployment-wide secret
could not be per-project, and this table is per-project by construction. An
e-mail destination carries **an address and no secret**; only a webhook carries
a key. The database says so too (`alert_destinations_email_has_no_secret`).

THE TRANSPORT IS HONEST ABOUT NOT BEING DEPLOYED. `infra/scripts/deploy.sh:148`
pushes neither `SMTP_HOST` nor any `ALERT_*` variable (`grep -c` -> 0), so the
e-mail transport answers `transport_unavailable` and NAMES the variable that is
missing. That is a first-class answer, not a workaround: a test button that
reported success against an absent transport would be the worst outcome of this
story.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from core import dq_monitor_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The word is `Alert destination`, once, everywhere (story 59.6, arbitrage 1).
#: `Channel` and `Webhook` are already ratified for the INPUT side of a
#: Datastream (`glossary.md:161-163`, `:355`), and "one word, one meaning" is the
#: rule story 59.5 spent itself enforcing.
KIND_EMAIL = "email"
KIND_SLACK = "slack_webhook"
KIND_WEBHOOK = "webhook"

KINDS: tuple[str, ...] = (KIND_EMAIL, KIND_SLACK, KIND_WEBHOOK)

#: The kinds whose target is a URL and which may therefore carry a key.
WEBHOOK_KINDS: tuple[str, ...] = (KIND_SLACK, KIND_WEBHOOK)

#: What a routing rule may name. IS the registry of story 59.5 -- not a copy of
#: it, so a monitor added there becomes routable without touching this module.
ROUTABLE_ALERT_TYPES: tuple[str, ...] = dq_monitor_registry.FIRING_ALERT_TYPES

#: The signals that cannot be routed, and why. Rendered by the screen.
#: It said FOUR until AI-101 split the platform-wide verification count into a
#: per-Datastream streak plus its blind spot -- a count in a comment is a
#: second source of truth for the tuple right below it.
CONSOLE_ONLY_SIGNALS: tuple[str, ...] = (
    "dead_letter_count",
    "datastream_empty_streak",
    "unattributed_empty_pulls",
    "health_poller_staleness_seconds",
    "mirror_sync_lag_seconds",
)

_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

_MAX_LABEL = 160
_MAX_TARGET = 2048
_DEFAULT_LOOKBACK_HOURS = 24
_DEFAULT_BATCH = 200
_HTTP_TIMEOUT_SECONDS = 10.0


class AlertDestinationError(Exception):
    """A refusal that carries the code the API and the screen both name."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def normalize_kind(raw: Any) -> str:
    kind = str(raw or "").strip().lower()
    if kind not in KINDS:
        raise AlertDestinationError(
            "unknown_kind",
            f"kind must be one of {list(KINDS)}, got {kind!r}",
        )
    return kind


def normalize_label(raw: Any) -> str:
    label = str(raw or "").strip()
    if not label or len(label) > _MAX_LABEL:
        raise AlertDestinationError(
            "invalid_label",
            f"label is required and must be at most {_MAX_LABEL} characters",
        )
    return label


def normalize_target(kind: str, raw: Any) -> str:
    """The address or the URL, refused rather than coerced.

    An e-mail destination that is not an address, or a webhook that is not an
    https URL, would fail at send time -- hours later, in a scheduler step
    nobody is watching. It is refused at the door instead.
    """
    target = str(raw or "").strip()
    if not target or len(target) > _MAX_TARGET:
        raise AlertDestinationError(
            "invalid_target",
            f"target is required and must be at most {_MAX_TARGET} characters",
        )
    if kind == KIND_EMAIL:
        if not _EMAIL_SHAPE.match(target):
            raise AlertDestinationError(
                "invalid_target", "an email destination takes one address"
            )
        return target
    parsed = urlparse(target)
    if parsed.scheme != "https" or not parsed.netloc:
        raise AlertDestinationError(
            "invalid_target",
            "a webhook destination takes one https URL -- a key sent over http "
            "is a key disclosed",
        )
    return target


def normalize_alert_types(raw: Any) -> list[str]:
    """The routing rule. Empty means every type; an unknown type is refused."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raise AlertDestinationError(
            "invalid_alert_types", "alert_types is a list of types, not a string"
        )
    if not isinstance(raw, Iterable):
        raise AlertDestinationError(
            "invalid_alert_types", "alert_types is a list of types"
        )
    seen: list[str] = []
    for entry in raw:
        alert_type = str(entry or "").strip()
        if not alert_type:
            continue
        if alert_type not in ROUTABLE_ALERT_TYPES:
            raise AlertDestinationError(
                "unknown_alert_type",
                f"{alert_type!r} is not a monitor type this build knows. "
                f"Known types: {list(ROUTABLE_ALERT_TYPES)}",
            )
        if alert_type not in seen:
            seen.append(alert_type)
    return seen


def normalize_secret(kind: str, raw: Any) -> str | None:
    """An e-mail destination carries no secret -- refused, not ignored."""
    if raw is None:
        return None
    secret = str(raw)
    if not secret.strip():
        return None
    if kind == KIND_EMAIL:
        raise AlertDestinationError(
            "email_carries_no_secret",
            "an email destination carries an address and no secret",
        )
    return secret


def mask_target(kind: str, target: str) -> str:
    """What a read route may show. Never the whole thing.

    A Slack incoming-webhook URL IS a credential: echoing it back on a list
    screen would publish the secret this table exists to protect.
    """
    value = str(target or "").strip()
    if not value:
        return ""
    if kind == KIND_EMAIL:
        local, at, domain = value.partition("@")
        if not at:
            return f"{value[:1]}…"
        return f"{local[:1]}…@{domain}"
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        tail = parsed.path[-4:] if len(parsed.path) > 4 else parsed.path
        return f"{parsed.scheme}://{parsed.netloc}/…{tail}"
    return f"{value[:4]}…"


# ---------------------------------------------------------------------------
# The secret, sealed in its row
# ---------------------------------------------------------------------------


def encrypt_secret(secret: str, project_id: str) -> bytes:
    """Seal a destination secret with the project's tenant key.

    Same primitive and same key class as `google_token_store.encrypt_token_payload`:
    Fernet over `tenant_keys.get_or_create_key(project_id)`. The error never
    echoes the plaintext.
    """
    from cryptography.fernet import Fernet  # noqa: PLC0415

    from core.tenant_keys import get_tenant_key_backend  # noqa: PLC0415

    try:
        key = get_tenant_key_backend().get_or_create_key(project_id)
        fernet = Fernet(base64.urlsafe_b64encode(key))
        return fernet.encrypt(json.dumps({"v": 1, "secret": secret}).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- never let the plaintext escape
        raise AlertDestinationError(
            "secret_seal_failed",
            f"destination secret could not be sealed (secret redacted): {type(exc).__name__}",
        ) from None


def decrypt_secret(blob: bytes | memoryview | None, project_id: str) -> str | None:
    """Unseal a destination secret, or None when the row carries none.

    Reads keys with `decryption_keys`, never `get_or_create_key` (AI-278): an
    unseal that mints its own key unseals nothing and calls the intact ciphertext
    corrupt. It also tries the rotation predecessors, so a key rotation stops
    invalidating every destination secret written before it.
    """
    if not blob:
        return None
    from cryptography.fernet import Fernet, InvalidToken  # noqa: PLC0415

    from core.tenant_keys import get_tenant_key_backend  # noqa: PLC0415

    try:
        keys = get_tenant_key_backend().decryption_keys(project_id)
    except Exception as exc:  # noqa: BLE001
        raise AlertDestinationError(
            "secret_unseal_failed",
            f"destination secret could not be unsealed (secret redacted): {type(exc).__name__}",
        ) from None
    if not keys:
        raise AlertDestinationError(
            "secret_key_missing",
            "the project has no tenant key -- this destination secret can no "
            "longer be unsealed and must be set again (secret redacted)",
        )
    for key in keys:  # newest first
        try:
            document = json.loads(
                Fernet(base64.urlsafe_b64encode(key)).decrypt(bytes(blob)).decode("utf-8")
            )
        except InvalidToken:
            continue
        except Exception as exc:  # noqa: BLE001
            raise AlertDestinationError(
                "secret_unseal_failed",
                f"destination secret could not be unsealed (secret redacted): "
                f"{type(exc).__name__}",
            ) from None
        value = document.get("secret")
        return str(value) if value is not None else None
    raise AlertDestinationError(
        "secret_unseal_failed",
        "destination secret could not be unsealed: wrong key or tampered (secret redacted)",
    )


# ---------------------------------------------------------------------------
# Reading and writing destinations
# ---------------------------------------------------------------------------

#: Every column a read may touch. `secret_blob` is ABSENT on purpose: the one
#: place it is selected is `_load_row_with_secret`, used by a send and by
#: nothing that answers a request.
_READ_COLUMN_NAMES = (
    "id",
    "project_id",
    "kind",
    "label",
    "target",
    "alert_types",
    "enabled",
    "created_by",
    "created_at",
    "updated_at",
)
_READ_COLUMNS = ", ".join(_READ_COLUMN_NAMES)
_READ_COLUMNS_D = ", ".join(f"d.{name}" for name in _READ_COLUMN_NAMES)


def _public_row(row: Sequence[Any], *, last_delivery: Mapping[str, Any] | None = None) -> dict:
    """One destination as every read route renders it -- masked, never sealed."""
    kind = row[2]
    record = {
        "id": row[0],
        "project_id": row[1],
        "kind": kind,
        "label": row[3],
        "target_masked": mask_target(kind, row[4]),
        "alert_types": list(row[5] or []),
        "enabled": bool(row[6]),
        "created_by": row[7],
        "created_at": _iso(row[8]),
        "updated_at": _iso(row[9]),
        # A destination that carries a key says THAT it does, never the key.
        "has_secret": kind in WEBHOOK_KINDS and bool(row[10]) if len(row) > 10 else None,
    }
    if record["has_secret"] is None:
        record.pop("has_secret")
    record["last_delivery"] = dict(last_delivery) if last_delivery else None
    return record


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def create_destination(
    conn,
    *,
    project_id: str,
    kind: Any,
    label: Any,
    target: Any,
    alert_types: Any = None,
    secret: Any = None,
    created_by: str = "system",
) -> dict:
    """Insert one destination. Returns the PUBLIC record -- never the secret."""
    from ulid import ULID  # noqa: PLC0415

    resolved_kind = normalize_kind(kind)
    resolved_label = normalize_label(label)
    resolved_target = normalize_target(resolved_kind, target)
    resolved_types = normalize_alert_types(alert_types)
    resolved_secret = normalize_secret(resolved_kind, secret)
    blob = encrypt_secret(resolved_secret, project_id) if resolved_secret else None

    destination_id = f"adest_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.alert_destinations
                (id, project_id, kind, label, target, secret_blob, alert_types,
                 enabled, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
            RETURNING {_READ_COLUMNS}, secret_blob IS NOT NULL
            """,
            (
                destination_id,
                project_id,
                resolved_kind,
                resolved_label,
                resolved_target,
                blob,
                resolved_types,
                created_by,
            ),
        )
        row = cur.fetchone()
    if row is None:  # pragma: no cover -- INSERT ... RETURNING always returns
        raise AlertDestinationError("write_failed", "the destination was not written")
    return _public_row(row)


def list_destinations(conn, project_id: str) -> list[dict]:
    """Every destination of one project, each with its last delivery.

    "Ce que chacun reçoit" has exactly one honest answer, and it is this join:
    when a destination last received something, and what the last attempt did.
    Before `app.alert_deliveries` there was no such date at all.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_READ_COLUMNS_D},
                   d.secret_blob IS NOT NULL,
                   last.delivered_at, last.state, last.last_error_class,
                   COALESCE(counts.delivered_count, 0),
                   COALESCE(counts.failed_count, 0)
            FROM app.alert_destinations d
            LEFT JOIN LATERAL (
                SELECT dl.delivered_at, dl.state, dl.last_error_class
                FROM app.alert_deliveries dl
                WHERE dl.destination_id = d.id
                ORDER BY dl.created_at DESC
                LIMIT 1
            ) last ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*) FILTER (WHERE dl.state = 'delivered') AS delivered_count,
                       COUNT(*) FILTER (WHERE dl.state <> 'delivered') AS failed_count
                FROM app.alert_deliveries dl
                WHERE dl.destination_id = d.id
            ) counts ON TRUE
            WHERE d.project_id = %s
            ORDER BY d.created_at DESC
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    records: list[dict] = []
    for row in rows:
        last = {
            "delivered_at": _iso(row[11]),
            "state": row[12],
            "last_error_class": row[13],
            "delivered_count": int(row[14] or 0),
            "failed_count": int(row[15] or 0),
        }
        if last["state"] is None and last["delivered_count"] == 0 and last["failed_count"] == 0:
            last = None
        records.append(_public_row(row[:11], last_delivery=last))
    return records


def get_destination(conn, destination_id: str, project_id: str) -> dict | None:
    """One destination of one project, public shape, or None."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_READ_COLUMNS}, secret_blob IS NOT NULL
            FROM app.alert_destinations
            WHERE id = %s AND project_id = %s
            """,
            (destination_id, project_id),
        )
        row = cur.fetchone()
    return _public_row(row) if row else None


def update_destination(
    conn,
    destination_id: str,
    project_id: str,
    *,
    fields: Mapping[str, Any],
) -> dict | None:
    """Patch label, target, alert_types, enabled or secret. Scoped to a project.

    The `project_id` is part of the WHERE, not of the body: an id belonging to
    another tenant answers "not found" rather than being updated.
    """
    current = _load_row_with_secret(conn, destination_id, project_id)
    if current is None:
        return None
    kind = current["kind"]

    assignments: list[str] = []
    params: list[Any] = []
    if "label" in fields:
        assignments.append("label = %s")
        params.append(normalize_label(fields["label"]))
    if "target" in fields:
        assignments.append("target = %s")
        params.append(normalize_target(kind, fields["target"]))
    if "alert_types" in fields:
        assignments.append("alert_types = %s")
        params.append(normalize_alert_types(fields["alert_types"]))
    if "enabled" in fields:
        assignments.append("enabled = %s")
        params.append(bool(fields["enabled"]))
    if "secret" in fields:
        raw = fields["secret"]
        if raw is None or not str(raw).strip():
            assignments.append("secret_blob = NULL")
        else:
            assignments.append("secret_blob = %s")
            params.append(encrypt_secret(normalize_secret(kind, raw) or "", project_id))
    if not assignments:
        raise AlertDestinationError(
            "no_update_fields",
            "provide label, target, alert_types, enabled or secret to update",
        )
    assignments.append("updated_at = NOW()")
    params.extend([destination_id, project_id])

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.alert_destinations SET "
            + ", ".join(assignments)
            + f" WHERE id = %s AND project_id = %s RETURNING {_READ_COLUMNS},"
            " secret_blob IS NOT NULL",
            params,
        )
        row = cur.fetchone()
    return _public_row(row) if row else None


def delete_destination(conn, destination_id: str, project_id: str) -> dict | None:
    """Remove a destination. Its deliveries go with it (ON DELETE CASCADE).

    A hard delete, unlike `alert_definitions`: nothing references a destination
    the way a firing references its definition, and a disabled row left behind
    would keep answering the confirmation's count.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM app.alert_destinations
            WHERE id = %s AND project_id = %s
            RETURNING id, kind, label
            """,
            (destination_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "kind": row[1], "label": row[2], "deleted": True}


def _load_row_with_secret(conn, destination_id: str, project_id: str) -> dict | None:
    """The only read that touches `secret_blob`, and it never answers a request."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, kind, label, target, secret_blob, alert_types, enabled
            FROM app.alert_destinations
            WHERE id = %s AND project_id = %s
            """,
            (destination_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "project_id": row[1],
        "kind": row[2],
        "label": row[3],
        "target": row[4],
        "secret_blob": row[5],
        "alert_types": list(row[6] or []),
        "enabled": bool(row[7]),
    }


def destinations_for_alert(conn, project_id: str, alert_type: str) -> list[dict]:
    """Which destinations a firing of this type reaches. The routing itself.

    Empty `alert_types` receives everything -- and that is the only route the
    non-DQ emitters have, because the rule vocabulary is the DQ registry's.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, kind, label, target, secret_blob, alert_types, enabled
            FROM app.alert_destinations
            WHERE project_id = %s
              AND enabled
              AND (cardinality(alert_types) = 0 OR %s = ANY(alert_types))
            ORDER BY created_at
            """,
            (project_id, str(alert_type or "")),
        )
        rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "project_id": row[1],
            "kind": row[2],
            "label": row[3],
            "target": row[4],
            "secret_blob": row[5],
            "alert_types": list(row[6] or []),
            "enabled": bool(row[7]),
        }
        for row in rows
    ]


def firing_count_since(conn, project_id: str, hours: int = _DEFAULT_LOOKBACK_HOURS) -> int:
    """How many firings this project wrote in the window -- the empty state's number.

    The empty screen says "alerts stay in the console" and then states HOW MANY
    stayed there. A `0` standing in for an absence is exactly what this count
    refuses to be: it is measured, and it is measured on the project asked for.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM app.alert_firings
            WHERE project_id = %s
              AND fired_at >= NOW() - make_interval(hours => %s)
            """,
            (project_id, int(hours)),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# The transports
# ---------------------------------------------------------------------------


def alerts_enabled() -> bool:
    """The off-switch that already governs the console channel governs this too."""
    return os.environ.get("ALERTS_ENABLED", "true").lower() == "true"


def email_transport_status() -> tuple[bool, str | None]:
    """Is the SMTP transport deployed, and if not, WHICH variable is missing.

    `infra/scripts/deploy.sh:148` pushes seventeen variables and not one is
    `SMTP_*` or `ALERT_*`, so today this answers `(False, "SMTP_HOST")` on every
    deployment. Naming the variable is the whole point: "it did not send" is
    not actionable, "SMTP_HOST is not set on this deployment" is.
    """
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return False, "SMTP_HOST"
    return True, None


def _email_channel(recipient: str):
    """The EXISTING `EmailChannel`, per destination address.

    Story 59.6, arbitrage 9: no new transport and no new API key. The channel
    was written for story 5.2 and has been unreachable ever since, because
    `build_channels()` only ever builds it from a deployment-wide address.
    """
    from core.infra_alerts import EmailChannel, _validated_link_base_url  # noqa: PLC0415

    return EmailChannel(
        smtp_host=os.environ.get("SMTP_HOST", "").strip(),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=os.environ.get("SMTP_USER") or None,
        smtp_password=os.environ.get("SMTP_PASSWORD") or None,
        email_to=recipient,
        email_from=os.environ.get("ALERT_EMAIL_FROM", "toorow@example.com"),
        link_base_url=_validated_link_base_url(
            os.environ.get("ALERT_LINK_BASE_URL", "http://localhost:5173")
        ),
    )


def _signal_for(payload: Mapping[str, Any]):
    """A firing (or a test) as the `AlertSignal` every channel already takes."""
    from core.infra_alerts import AlertSignal  # noqa: PLC0415

    return AlertSignal(
        signal=str(payload.get("alert_type") or "alert"),
        value=float(payload.get("observed_value") or 0),
        threshold=float(payload.get("threshold") or 0),
        severity=str(payload.get("severity") or "warning"),
        connector=payload.get("datastream_id"),
        timestamp=str(payload.get("fired_at") or datetime.now(tz=timezone.utc).isoformat()),
        message=payload.get("message"),
    )


def _webhook_body(destination: Mapping[str, Any], payload: Mapping[str, Any]) -> dict:
    """One JSON shape for both webhook kinds.

    `text` is what Slack renders; the structured fields are what a generic
    receiver reads. One body, because two would drift.
    """
    summary = payload.get("message") or (
        f"{payload.get('alert_type')} on project {payload.get('project_id')}"
    )
    return {
        "event": payload.get("event") or "alert_firing",
        "text": f"[toorow] {summary}",
        "destination": {"id": destination.get("id"), "label": destination.get("label")},
        "alert": {
            "firing_id": payload.get("firing_id"),
            "type": payload.get("alert_type"),
            "project_id": payload.get("project_id"),
            "datastream_id": payload.get("datastream_id"),
            "severity": payload.get("severity"),
            "observed_value": payload.get("observed_value"),
            "threshold": payload.get("threshold"),
            "window_date": _iso(payload.get("window_date")),
            "fired_at": _iso(payload.get("fired_at")),
            "message": payload.get("message"),
        },
    }


def send_to_destination(destination: Mapping[str, Any], payload: Mapping[str, Any]) -> dict:
    """Attempt ONE delivery and report what happened. Never raises.

    Returns `{"delivered": bool, "error_class": str | None, "detail": str}`.
    The error class is the vocabulary the screen and the delivery row share:
    `transport_unavailable`, `alerts_disabled`, `http_error`, `unreachable`,
    `send_failed`.
    """
    if not alerts_enabled():
        return {
            "delivered": False,
            "error_class": "alerts_disabled",
            "detail": "ALERTS_ENABLED is false on this deployment, so nothing is sent.",
        }

    kind = destination.get("kind")
    if kind == KIND_EMAIL:
        available, missing = email_transport_status()
        if not available:
            return {
                "delivered": False,
                "error_class": "transport_unavailable",
                "detail": (
                    f"{missing} is not set on this deployment, so no email can leave it. "
                    "The alert stays in the console and in app.alert_firings."
                ),
            }
        try:
            _email_channel(str(destination.get("target") or "")).send(_signal_for(payload))
        except Exception as exc:  # noqa: BLE001
            return {
                "delivered": False,
                "error_class": "send_failed",
                "detail": f"{type(exc).__name__}",
            }
        return {"delivered": True, "error_class": None, "detail": "sent"}

    if kind in WEBHOOK_KINDS:
        import httpx  # noqa: PLC0415

        headers = {"Content-Type": "application/json"}
        secret = destination.get("secret")
        if secret is None and destination.get("secret_blob") is not None:
            try:
                secret = decrypt_secret(
                    destination.get("secret_blob"), str(destination.get("project_id") or "")
                )
            except AlertDestinationError as exc:
                return {"delivered": False, "error_class": exc.code, "detail": exc.message}
        if secret:
            # The key never travels in the body, and never in a log line.
            headers["Authorization"] = f"Bearer {secret}"
        try:
            response = httpx.post(
                str(destination.get("target") or ""),
                json=_webhook_body(destination, payload),
                headers=headers,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 -- DNS, TLS, timeout
            return {
                "delivered": False,
                "error_class": "unreachable",
                "detail": f"{type(exc).__name__}",
            }
        if response.status_code >= 400:
            return {
                "delivered": False,
                "error_class": "http_error",
                "detail": f"HTTP {response.status_code}",
            }
        return {"delivered": True, "error_class": None, "detail": f"HTTP {response.status_code}"}

    return {
        "delivered": False,
        "error_class": "unknown_kind",
        "detail": f"no transport for kind {kind!r}",
    }


def send_test(conn, destination_id: str, project_id: str) -> dict:
    """"Send a test" -- the real transport, and NO firing written.

    It depends on neither the scheduler nor `app.alert_firings`: a person who
    has just typed a webhook URL needs the verdict now, and a test that wrote a
    firing would put a fabricated finding in the evidence of a project.
    """
    row = _load_row_with_secret(conn, destination_id, project_id)
    if row is None:
        return {"code": "not_found", "delivered": False}
    payload = {
        "event": "alert_delivery_test",
        "alert_type": "delivery_test",
        "firing_id": None,
        "project_id": project_id,
        "datastream_id": None,
        "severity": "info",
        "observed_value": 0,
        "threshold": 0,
        "window_date": None,
        "fired_at": datetime.now(tz=timezone.utc).isoformat(),
        "message": (
            "Test from toorow: this destination is reachable. "
            "No alert fired and nothing was recorded."
        ),
    }
    verdict = send_to_destination(row, payload)
    return {
        "code": "delivered" if verdict["delivered"] else verdict["error_class"],
        "delivered": verdict["delivered"],
        "detail": verdict["detail"],
        "destination_id": destination_id,
        "kind": row["kind"],
    }


# ---------------------------------------------------------------------------
# The bridge: persisted firings reach their destinations
# ---------------------------------------------------------------------------


def deliver_pending_firings(
    conn,
    *,
    project_id: str | None = None,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
    limit: int = _DEFAULT_BATCH,
) -> dict:
    """Send every firing of the window that a destination has not received yet.

    ONE function, called from the nightly step that already calls `notify_alert`
    (`scheduler.py:2107-2109`). The pairing is driven by the DESTINATIONS: with
    none configured the join returns nothing, which is why 3596 rows on the
    disposable base and 522 on preprod cost exactly one query.

    Idempotent by `UNIQUE (destination_id, firing_id)`: a night that runs twice
    does not send twice, and neither does a lookback that overlaps yesterday's.
    """
    if not alerts_enabled():
        return {"attempted": 0, "delivered": 0, "failed": 0, "skipped": "alerts_disabled"}

    from ulid import ULID  # noqa: PLC0415

    clauses = ["f.fired_at >= NOW() - make_interval(hours => %s)", "dl.id IS NULL"]
    params: list[Any] = [int(lookback_hours)]
    if project_id:
        clauses.append("f.project_id = %s")
        params.append(project_id)
    params.append(int(limit))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.id, f.type, f.project_id, f.message, f.severity, f.fired_at,
                   f.observed_value, f.threshold, f.window_date, f.datastream_id,
                   d.id, d.kind, d.label, d.target, d.secret_blob
            FROM app.alert_firings f
            JOIN app.alert_destinations d
              ON d.project_id = f.project_id
             AND d.enabled
             AND (cardinality(d.alert_types) = 0 OR f.type = ANY(d.alert_types))
            LEFT JOIN app.alert_deliveries dl
              ON dl.destination_id = d.id AND dl.firing_id = f.id
            WHERE """
            + " AND ".join(clauses)
            + """
            ORDER BY f.fired_at
            LIMIT %s
            """,
            params,
        )
        pairs = cur.fetchall()

    delivered = 0
    failed = 0
    error_classes: dict[str, int] = {}
    for pair in pairs:
        destination = {
            "id": pair[10],
            "kind": pair[11],
            "label": pair[12],
            "target": pair[13],
            "secret_blob": pair[14],
            "project_id": pair[2],
        }
        payload = {
            "event": "alert_firing",
            "firing_id": pair[0],
            "alert_type": pair[1],
            "project_id": pair[2],
            "message": pair[3],
            "severity": pair[4],
            "fired_at": pair[5],
            "observed_value": float(pair[6]) if pair[6] is not None else 0.0,
            "threshold": float(pair[7]) if pair[7] is not None else 0.0,
            "window_date": pair[8],
            "datastream_id": pair[9],
        }
        delivery_id = f"adlv_{ULID()}"
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.alert_deliveries
                    (id, destination_id, firing_id, alert_type, state, attempts)
                VALUES (%s, %s, %s, %s, 'processing', 1)
                ON CONFLICT (destination_id, firing_id) DO NOTHING
                RETURNING id
                """,
                (delivery_id, destination["id"], payload["firing_id"], payload["alert_type"]),
            )
            claimed = cur.fetchone()
        conn.commit()
        if claimed is None:
            # Another worker claimed the pair between the SELECT and here.
            continue

        verdict = send_to_destination(destination, payload)
        with conn.cursor() as cur:
            if verdict["delivered"]:
                cur.execute(
                    """
                    UPDATE app.alert_deliveries
                    SET state = 'delivered', delivered_at = NOW(),
                        last_error_class = NULL, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (delivery_id,),
                )
            else:
                cur.execute(
                    """
                    UPDATE app.alert_deliveries
                    SET state = 'pending', last_error_class = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (verdict["error_class"], delivery_id),
                )
        conn.commit()
        if verdict["delivered"]:
            delivered += 1
        else:
            failed += 1
            code = str(verdict["error_class"])
            error_classes[code] = error_classes.get(code, 0) + 1

    if pairs:
        logger.info(
            "alert_destinations: deliver_pending_firings: attempted=%d delivered=%d failed=%d %s",
            len(pairs),
            delivered,
            failed,
            error_classes or "",
        )
    return {
        "attempted": len(pairs),
        "delivered": delivered,
        "failed": failed,
        "error_classes": error_classes,
    }
