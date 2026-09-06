from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from core.entry_confirmations import (
    HOSTED_ENTRY_COMMAND,
    INSTANCE_CLAIM_COMMAND,
    ConsumedEntryConfirmation,
    EntryConfirmationRefused,
    bind_entry_confirmation_operation,
    canonical_payload_hash,
    consume_entry_confirmation,
    issue_entry_confirmation,
)

from tests.conftest import REPO_ROOT


def _payload(name: str = "Acme") -> dict:
    return {
        "organization_name": name,
        "organization_slug": name.lower(),
        "project_name": "First project",
        "project_slug": "first-project",
        "currency": "EUR",
        "timezone": "Europe/Paris",
    }


def _cursor_with_confirmation(
    *,
    command: str = HOSTED_ENTRY_COMMAND,
    actor: str = "person_1",
    payload: dict | None = None,
    idempotency_key: str = "request-1",
    context_reference: str = "hosted.entry_scope.create:console",
    secret: str = "ecfs_server_secret",
    expires_at: datetime | None = None,
    consumed_at=None,
    operation_id=None,
):
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (
            command,
            actor,
            canonical_payload_hash(command, payload or _payload()),
            hashlib.sha256(idempotency_key.encode()).hexdigest(),
            hashlib.sha256(context_reference.encode()).hexdigest(),
            hashlib.sha256(secret.encode()).hexdigest(),
            expires_at or datetime.now(timezone.utc) + timedelta(minutes=5),
            consumed_at,
            operation_id,
        )
    ]
    cur.rowcount = 1
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def test_issue_mints_secret_but_persists_only_its_digest():
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    issued = issue_entry_confirmation(
        conn,
        actor_person_id="person_1",
        command_type=HOSTED_ENTRY_COMMAND,
        request_payload=_payload(),
        idempotency_key="request-1",
        context_reference="hosted.entry_scope.create:console",
    )

    insert_params = cur.execute.call_args.args[1]
    assert issued.confirmation_secret.startswith("ecfs_")
    assert issued.confirmation_secret not in insert_params
    assert hashlib.sha256(issued.confirmation_secret.encode()).hexdigest() in insert_params
    assert issued.expires_at <= datetime.now(timezone.utc) + timedelta(minutes=15)

def test_confirmation_consumes_only_for_exact_bindings():
    conn, cur = _cursor_with_confirmation()

    result = consume_entry_confirmation(
        conn,
        confirmation_id="econf_1",
        confirmation_secret="ecfs_server_secret",
        actor_person_id="person_1",
        command_type=HOSTED_ENTRY_COMMAND,
        request_payload=_payload(),
        idempotency_key="request-1",
        context_reference="hosted.entry_scope.create:console",
    )

    assert result.replayed is False
    assert result.operation_id is None
    assert any(
        "SET consumed_at = NOW()" in call.args[0]
        for call in cur.execute.call_args_list
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"actor_person_id": "person_2"},
        {"command_type": INSTANCE_CLAIM_COMMAND},
        {"request_payload": _payload("Other")},
        {"idempotency_key": "request-2"},
        {"context_reference": "hosted.entry_scope.create:other-workspace"},
        {"confirmation_secret": "ecfs_wrong_secret"},
    ],
)
def test_confirmation_refuses_another_person_command_payload_or_context(overrides):
    conn, cur = _cursor_with_confirmation()
    supplied = {
        "confirmation_id": "econf_1",
        "confirmation_secret": "ecfs_server_secret",
        "actor_person_id": "person_1",
        "command_type": HOSTED_ENTRY_COMMAND,
        "request_payload": _payload(),
        "idempotency_key": "request-1",
        "context_reference": "hosted.entry_scope.create:console",
    }
    supplied.update(overrides)

    with pytest.raises(EntryConfirmationRefused):
        consume_entry_confirmation(conn, **supplied)

    assert not any(
        "SET consumed_at = NOW()" in call.args[0]
        for call in cur.execute.call_args_list
    )


def test_confirmation_refuses_expired_secret():
    conn, _cur = _cursor_with_confirmation(
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    with pytest.raises(EntryConfirmationRefused) as caught:
        consume_entry_confirmation(
            conn,
            confirmation_id="econf_1",
            confirmation_secret="ecfs_server_secret",
            actor_person_id="person_1",
            command_type=HOSTED_ENTRY_COMMAND,
            request_payload=_payload(),
            idempotency_key="request-1",
            context_reference="hosted.entry_scope.create:console",
        )

    assert caught.value.code == "confirmation_expired"


def test_consumed_confirmation_only_replays_its_original_operation():
    conn, _cur = _cursor_with_confirmation(
        consumed_at=datetime.now(timezone.utc), operation_id="op_original"
    )

    result = consume_entry_confirmation(
        conn,
        confirmation_id="econf_1",
        confirmation_secret="ecfs_server_secret",
        actor_person_id="person_1",
        command_type=HOSTED_ENTRY_COMMAND,
        request_payload=_payload(),
        idempotency_key="request-1",
        context_reference="hosted.entry_scope.create:console",
    )

    assert result.replayed is True
    assert result.operation_id == "op_original"


def test_binding_cannot_replace_the_original_operation():
    confirmation_conn, _ = _cursor_with_confirmation()
    confirmation = consume_entry_confirmation(
        confirmation_conn,
        confirmation_id="econf_1",
        confirmation_secret="ecfs_server_secret",
        actor_person_id="person_1",
        command_type=HOSTED_ENTRY_COMMAND,
        request_payload=_payload(),
        idempotency_key="request-1",
        context_reference="hosted.entry_scope.create:console",
    )
    bind_cur = MagicMock()
    bind_cur.fetchone.return_value = None
    bind_conn = MagicMock()
    bind_conn.cursor.return_value.__enter__.return_value = bind_cur

    with pytest.raises(EntryConfirmationRefused):
        bind_entry_confirmation_operation(
            bind_conn, confirmation=confirmation, operation_id="op_other"
        )


def test_lost_issue_response_can_bind_a_new_confirmation_to_same_operation():
    """A second exact issue may recover by replaying the original operation."""
    for confirmation_id in ("econf_lost", "econf_retry"):
        confirmation = ConsumedEntryConfirmation(
            confirmation_id=confirmation_id,
            command_type=HOSTED_ENTRY_COMMAND,
            actor_person_id="person_1",
            payload_hash=canonical_payload_hash(HOSTED_ENTRY_COMMAND, _payload()),
            idempotency_key_hash=hashlib.sha256(b"request-1").hexdigest(),
            operation_id=None,
            replayed=False,
        )
        cur = MagicMock()
        cur.fetchone.return_value = ("op_original",)
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur

        bind_entry_confirmation_operation(
            conn, confirmation=confirmation, operation_id="op_original"
        )

    migration = (REPO_ROOT / "infra/nango/migrations/116_entry_confirmations.sql").read_text(
        encoding="utf-8"
    )
    assert "operation_id             TEXT\n" in migration
    assert "operation_id             TEXT UNIQUE" not in migration


def test_every_command_the_code_issues_is_in_the_database_vocabulary():
    """A constant added at the write site, a CHECK left behind, a 503 that names nothing.

    `entry_confirmations_command_type_check` carried four values from migration
    131. Three commands were added to this module afterwards and none reached the
    constraint, so each raised `CheckViolation` at issue time under a catch-all
    503. Measured live 2026-08-07: `POST .../draft-confirmations` -> 503
    `preconfiguration_unavailable`, and no Datastream setup confirmation could
    EVER be issued, in any of the three modes.

    The list lives in two places by necessity -- Python and a CHECK. This test is
    what keeps them one list: it reads the constants and the migration text, so a
    command added here without its migration fails in CI rather than in a log
    nobody is reading.
    """
    import re
    from pathlib import Path

    from core import entry_confirmations

    issued = {
        value
        for name, value in vars(entry_confirmations).items()
        if name.endswith("_COMMAND") and isinstance(value, str)
    }
    migrations = Path(__file__).resolve().parents[3] / "infra" / "nango" / "migrations"
    latest = max(
        (path for path in migrations.glob("*.sql") if "entry_confirmations" in path.read_text(
            encoding="utf-8"
        ) and "command_type_check" in path.read_text(encoding="utf-8")),
        key=lambda path: int(path.name.split("_", 1)[0]),
    )
    allowed = set(re.findall(r"'([a-z_]+(?:\.[a-z_]+)+)'", latest.read_text(encoding="utf-8")))
    assert issued <= allowed, (
        f"{sorted(issued - allowed)} would raise CheckViolation at issue time; "
        f"add them to {latest.name}"
    )
