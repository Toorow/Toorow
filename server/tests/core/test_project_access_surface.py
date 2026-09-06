from __future__ import annotations

from datetime import datetime, timezone

import core.project_access_surface as project_access
import pytest
from core.operations import OperationResult
from core.project_access_api import _error
from core.project_access_surface import (
    ProjectAccessValidationError,
    _effective,
    prepare_access_handoff,
    read_project_access,
)


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.current = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))
        self.current = self.rows.pop(0)

    def fetchone(self):
        return self.current[0] if isinstance(self.current, list) else self.current

    def fetchall(self):
        return self.current if isinstance(self.current, list) else []


class Connection:
    def __init__(self, rows):
        self.cursor_instance = Cursor(rows)

    def cursor(self):
        return self.cursor_instance


def test_effective_access_preserves_only_the_owner_floor():
    assert _effective("owner", None) == ("manage", "owner_floor")
    assert _effective("member", None) == (None, "grant_required")
    assert _effective("viewer", "manage") == ("view", "explicit_grant")


def test_read_model_composes_membership_grant_and_handoff():
    now = datetime.now(timezone.utc)
    conn = Connection(
        [
            ("proj-1", "Demo", "org-1", "Acme"),
            [
                ("owner@example.com", "owner", 1, None),
                ("member@example.com", "member", 3, "edit"),
            ],
            [
                (
                    "handoff-1",
                    "invitee@example.com",
                    "pending",
                    now,
                    "/org/org-1/project/proj-1/access/people",
                )
            ],
        ]
    )

    result = read_project_access("proj-1", conn, actor="owner@example.com")

    assert result["caller_capability"] == "manage"
    assert result["people"][0]["grant_source"] == "owner_floor"
    assert result["people"][1]["effective_capability"] == "edit"
    assert result["handoffs"][0]["resume_ref"].endswith("/access/people")


def test_handoff_rejects_noncanonical_or_fragment_bearing_resume_refs():
    conn = Connection([])
    with pytest.raises(ProjectAccessValidationError):
        prepare_access_handoff(
            conn,
            project_id="proj-1",
            identity=None,
            actor="owner@example.com",
            # The fixed pre-46.4 onboarding return path: not a canonical route,
            # so a handoff must refuse to resume into it.
            resume_ref="/onboarding/responsibilities",
        )
    with pytest.raises(ProjectAccessValidationError):
        prepare_access_handoff(
            conn,
            project_id="proj-1",
            identity=None,
            actor="owner@example.com",
            resume_ref="/org/org-1/project/proj-1/access/people#bearer=secret",
        )


def test_handoff_uses_the_operation_seam_for_idempotent_replay(monkeypatch):
    captured = {}

    def fake_execute(_conn, spec, *, mutation):
        captured["spec"] = spec
        return OperationResult(
            operation_id="op-1",
            outcome="succeeded",
            result={
                "handoff_id": "handoff-1",
                "state": "pending",
                "expires_at": "2026-08-14T00:00:00+00:00",
                "resume_ref": "/org/org-1/project/proj-1/access/handoffs",
            },
            audit_event_id="audit-1",
            outbox_event_id="outbox-1",
            replayed=True,
        )

    monkeypatch.setattr(project_access, "execute_operation", fake_execute)
    result = prepare_access_handoff(
        Connection([("org-1",)]),
        project_id="proj-1",
        identity="member@example.com",
        actor="owner@example.com",
        resume_ref="/org/org-1/project/proj-1/access/handoffs",
        idempotency_key="stable-key",
    )

    assert captured["spec"].idempotency_key == "stable-key"
    assert captured["spec"].command_type == "project_access.handoff.prepare"
    assert result["replayed"] is True
    assert result["handoff_id"] == "handoff-1"


def test_handoff_reports_an_idempotency_payload_mismatch_as_conflict():
    from core.operations import OperationIdempotencyConflict

    response = _error(OperationIdempotencyConflict("key already bound"))

    assert response.status_code == 409
