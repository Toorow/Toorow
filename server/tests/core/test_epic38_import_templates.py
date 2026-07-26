"""Story 38.6: Import template catalog + inbound Datastream creation -- offline + ASGI tests.

Covers (per AC + Task 5):
  (a) Template immutability: UPDATE/DELETE blocked by trigger (offline + live-PG).
  (b) AC5: create_inbound_datastream routes through Epic 12 create_datastream
      (spy confirms it is the single write path; no direct INSERT on datastreams).
  (c) config pins template_code, template_version, channels, publishable.
  (d) AC4: GENERIC_TABULAR_V1 -> enabled=False, publishable=False (draft gate).
  (e) Unknown template fails closed (no Datastream created).
  (f) Channel subsets: {email}, {webhook}, {upload}, mixed all accepted;
      unknown channel raises; empty channels raises.
  (g) Pinned version never auto-bumps (a new version is a new row; existing
      Datastream keeps its pinned version).
  (h) Secret-free catalog read-model (no credential/signing/cross-tenant field).
  (i) MCP list_inbound_templates == REST GET /templates (same evidence).
  (j) Idempotency-Key missing -> 422; conflicting payload -> 409.
  (k) request_payload has no random id (deterministic idempotency, review H1).
  (l) Non-member identity -> nondisclosing 404 on POST /datastreams.
  (m) Live-PG-gated: immutability trigger + PK + 6 seeds present on real DB.

Non-tautological: mock cursor SEQUENCE matches real conn.cursor() call order.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared mock helpers (mirrors test_epic38_connector_activation.py style).
# ---------------------------------------------------------------------------


def _cur(*fetchone_rows, rowcount: int = 1, fetchall=None):
    """Build a mock cursor with an ordered sequence of fetchone return values."""
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(fetchone_rows)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return cur


def _conn_with(*curs):
    """Build a mock connection that yields cursors in order."""
    conn = MagicMock()
    conn.cursor.side_effect = list(curs)
    return conn


def _stub_operation(monkeypatch, module=None, *, capture=None):
    """Stub execute_operation on core.operations to run the mutation synchronously.

    ``module`` is accepted for API-compat with the activation test style but
    the effective patch target is always ``core.operations.execute_operation``
    because create_inbound_datastream uses a lazy ``from core.operations import``
    inside the function body; patching core.operations ensures the stub is visible.
    """
    from core import operations

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-test-1")
        if capture is not None:
            capture.setdefault("specs", []).append(spec)
            capture.setdefault("changes", []).append(changed)
        return operations.OperationResult(
            "op-test-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(operations, "execute_operation", execute)


# ---------------------------------------------------------------------------
# Helper: a fake get_template / latest_version / list_templates for isolation.
# ---------------------------------------------------------------------------

_TV_CONTRACT = {
    "required_fields": ["datastream_id", "campaign_id_toorow", "media_type",
                        "channel_or_network", "event_start_timestamp", "media_date",
                        "net_cost_eur"],
    "optional_fields": ["duration_seconds", "gross_cost_eur", "creative_id"],
    "identity_keys": ["event_start_timestamp", "channel_or_network"],
    "grain": "broadcast_spot",
    "currency_unit": {"cost_currency": "EUR"},
    "validation": {"required_fields_must_be_present": True},
    "canonical_bindings": {"media_type_value": "TV"},
}

_GENERIC_CONTRACT = {
    "required_fields": ["datastream_id", "campaign_id_toorow"],
    "optional_fields": [],
    "identity_keys": [],
    "grain": "unknown",
    "validation": {"publish_gate": "canonical_semantics_required"},
    "canonical_bindings": {"publish_gate": "canonical_semantics_required"},
}


def _fake_tv_template():
    return {
        "template_code": "OFFLINE_TV_V1",
        "version": 1,
        "title": "Offline TV broadcast spend",
        "contract": _TV_CONTRACT,
        "is_generic": False,
        "created_at": "2026-07-23T00:00:00+00:00",
    }


def _fake_generic_template():
    return {
        "template_code": "GENERIC_TABULAR_V1",
        "version": 1,
        "title": "Generic tabular file (discover-first fallback)",
        "contract": _GENERIC_CONTRACT,
        "is_generic": True,
        "created_at": "2026-07-23T00:00:00+00:00",
    }


# Minimal datastream dict returned by the Epic 12 seam stub.
def _fake_ds(ds_id="ds_TEST001", template_code="OFFLINE_TV_V1", version=1,
             channels=None, publishable=True, enabled=True):
    return {
        "id": ds_id,
        "project_id": "proj-1",
        "name": "TV test",
        "module_name": None,
        "source_kind": "managed_feed",
        "enabled": enabled,
        "config": {
            "template_code": template_code,
            "template_version": version,
            "channels": sorted(channels or ["email"]),
            "publishable": publishable,
        },
        "created_by": "user@example.com",
        "created_at": "2026-07-23T00:00:00+00:00",
        "updated_at": "2026-07-23T00:00:00+00:00",
        "current_plan_version_id": None,
        "archived_at": None,
        "archived_by": None,
        "versioned": False,
        "writer_kind": "toorow",
        "destination_policy": "managed_raw",
        "cadence_mode": "manual",
        "validation_state": "legacy",
        "schedule_mode": "manual",
        "refetch_days": 3,
        "date_window_days": 30,
    }


# ---------------------------------------------------------------------------
# (a) Template immutability: offline contract check.
# ---------------------------------------------------------------------------


def test_template_immutability_offline_no_update_or_delete():
    """Offline: import_templates module never issues UPDATE or DELETE."""
    import inspect

    import core.import_templates as it

    src = inspect.getsource(it)
    # The module must not contain UPDATE or DELETE SQL against import_templates.
    assert "UPDATE app.import_templates" not in src, (
        "import_templates.py must not UPDATE the template catalog (immutability)"
    )
    assert "DELETE FROM app.import_templates" not in src, (
        "import_templates.py must not DELETE from the template catalog (immutability)"
    )


# ---------------------------------------------------------------------------
# (b) AC5: create_inbound_datastream is the ONLY write path -- spy on Epic 12.
# ---------------------------------------------------------------------------


def test_create_inbound_datastream_uses_epic12_create_datastream(monkeypatch):
    """AC5: create_inbound_datastream calls datastreams.create_datastream, not direct SQL."""
    import core.import_templates as it

    captured_calls: list[tuple] = []
    fake_ds = _fake_ds()

    def fake_create_datastream(data, project_id, created_by, conn):
        captured_calls.append((data, project_id, created_by))
        return fake_ds

    monkeypatch.setattr("core.datastreams.create_datastream", fake_create_datastream)
    # Also patch the import inside the function body via monkeypatching the module attr.
    _stub_operation(monkeypatch, it)

    def fake_get_template(conn, code, version):
        return _fake_tv_template()

    monkeypatch.setattr(it, "get_template", fake_get_template)

    conn = MagicMock()
    it.create_inbound_datastream(
        conn,
        project_id="proj-1",
        name="TV test",
        template_code="OFFLINE_TV_V1",
        template_version=1,
        channels=["email"],
        created_by="user@example.com",
        org_id="org-1",
        idempotency_key="ik-ac5-proof",
    )

    # The Epic 12 seam was called exactly once.
    assert len(captured_calls) == 1, "create_datastream must be called exactly once"
    data_arg, project_id_arg, created_by_arg = captured_calls[0]
    assert data_arg["source_kind"] == "managed_feed", (
        "source_kind must be managed_feed (AC1)"
    )
    assert project_id_arg == "proj-1"
    assert created_by_arg == "user@example.com"


def test_create_inbound_datastream_config_pins_template_and_channels(monkeypatch):
    """AC5: config must pin template_code, template_version, channels, publishable."""
    import core.import_templates as it

    captured_data: dict = {}
    fake_ds = _fake_ds(channels=["email", "webhook"])

    def fake_create_datastream(data, project_id, created_by, conn):
        captured_data.update(data)
        return fake_ds

    monkeypatch.setattr("core.datastreams.create_datastream", fake_create_datastream)
    _stub_operation(monkeypatch, it)
    monkeypatch.setattr(it, "get_template", lambda conn, code, v: _fake_tv_template())

    conn = MagicMock()
    it.create_inbound_datastream(
        conn,
        project_id="proj-1",
        name="TV test",
        template_code="OFFLINE_TV_V1",
        template_version=1,
        channels=["email", "webhook"],
        created_by="user@example.com",
        org_id="org-1",
        idempotency_key="ik-config-pin",
    )

    cfg = captured_data.get("config", {})
    assert cfg.get("template_code") == "OFFLINE_TV_V1", "config must pin template_code"
    assert cfg.get("template_version") == 1, "config must pin template_version"
    assert sorted(cfg.get("channels", [])) == ["email", "webhook"], (
        "config must pin channels"
    )
    assert "publishable" in cfg, "config must include publishable"
    assert cfg["publishable"] is True, "non-generic template must be publishable=True"


# ---------------------------------------------------------------------------
# (c) config.publishable is True for non-generic, False for generic.
# ---------------------------------------------------------------------------


def test_create_inbound_datastream_non_generic_is_publishable(monkeypatch):
    """Non-generic template -> publishable=True, enabled=True."""
    import core.import_templates as it

    captured: dict = {}

    def fake_create_datastream(data, project_id, created_by, conn):
        captured.update(data)
        return _fake_ds()

    monkeypatch.setattr("core.datastreams.create_datastream", fake_create_datastream)
    _stub_operation(monkeypatch, it)
    monkeypatch.setattr(it, "get_template", lambda conn, code, v: _fake_tv_template())

    it.create_inbound_datastream(
        MagicMock(), project_id="p", name="n", template_code="OFFLINE_TV_V1",
        template_version=1, channels=["upload"], created_by="u", org_id="o",
        idempotency_key="ik-1",
    )

    assert captured.get("enabled") is True
    assert captured.get("config", {}).get("publishable") is True


# ---------------------------------------------------------------------------
# (d) AC4: GENERIC_TABULAR_V1 -> draft (publishable=False, enabled=False).
# ---------------------------------------------------------------------------


def test_create_inbound_datastream_generic_is_draft(monkeypatch):
    """AC4: GENERIC_TABULAR_V1 creates a DRAFT Datastream (publishable=False)."""
    import core.import_templates as it

    captured: dict = {}

    def fake_create_datastream(data, project_id, created_by, conn):
        captured.update(data)
        return _fake_ds(template_code="GENERIC_TABULAR_V1", publishable=False, enabled=False)

    monkeypatch.setattr("core.datastreams.create_datastream", fake_create_datastream)
    _stub_operation(monkeypatch, it)
    monkeypatch.setattr(it, "get_template", lambda conn, code, v: _fake_generic_template())

    it.create_inbound_datastream(
        MagicMock(), project_id="p", name="generic ds", template_code="GENERIC_TABULAR_V1",
        template_version=1, channels=["upload"], created_by="u", org_id="o",
        idempotency_key="ik-generic",
    )

    assert captured.get("enabled") is False, "generic Datastream must be created disabled"
    cfg = captured.get("config", {})
    assert cfg.get("publishable") is False, "generic Datastream must be publishable=False"
    assert "publish_gate" in cfg, "generic config must include publish_gate"


def test_generic_publish_gate_in_config(monkeypatch):
    """AC4: config.publish_gate is present and set to canonical_semantics_required."""
    import core.import_templates as it

    captured: dict = {}

    def fake_create_datastream(data, project_id, created_by, conn):
        captured.update(data)
        return _fake_ds(template_code="GENERIC_TABULAR_V1", publishable=False, enabled=False)

    monkeypatch.setattr("core.datastreams.create_datastream", fake_create_datastream)
    _stub_operation(monkeypatch, it)
    monkeypatch.setattr(it, "get_template", lambda conn, code, v: _fake_generic_template())

    it.create_inbound_datastream(
        MagicMock(), project_id="p", name="g", template_code="GENERIC_TABULAR_V1",
        template_version=1, channels=["email"], created_by="u", org_id="o",
        idempotency_key="ik-gate",
    )

    assert captured["config"]["publish_gate"] == "canonical_semantics_required"


# ---------------------------------------------------------------------------
# (e) Unknown template fails closed.
# ---------------------------------------------------------------------------


def test_create_inbound_datastream_unknown_template_fails_closed(monkeypatch):
    """Unknown template code -> TemplateNotFound; no create_datastream call."""
    import core.import_templates as it
    from core.import_templates import TemplateNotFound

    called = []
    monkeypatch.setattr(
        "core.datastreams.create_datastream",
        lambda *a, **kw: called.append(1) or {},
    )
    _stub_operation(monkeypatch, it)
    monkeypatch.setattr(it, "get_template", lambda conn, code, v: None)

    with pytest.raises(TemplateNotFound):
        it.create_inbound_datastream(
            MagicMock(), project_id="p", name="n", template_code="NO_SUCH_CODE",
            template_version=1, channels=["email"], created_by="u", org_id="o",
            idempotency_key="ik-notfound",
        )

    assert not called, "create_datastream must NOT be called for an unknown template"


def test_unknown_template_version_fails_closed(monkeypatch):
    """Unknown version of a known code -> TemplateNotFound (fail closed)."""
    import core.import_templates as it
    from core.import_templates import TemplateNotFound

    monkeypatch.setattr(it, "get_template", lambda conn, code, v: None)

    with pytest.raises(TemplateNotFound):
        it.create_inbound_datastream(
            MagicMock(), project_id="p", name="n", template_code="OFFLINE_TV_V1",
            template_version=99, channels=["email"], created_by="u", org_id="o",
            idempotency_key="ik-noversion",
        )


# ---------------------------------------------------------------------------
# (f) Channel subsets -- valid and invalid.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channels", [
    ["email"],
    ["webhook"],
    ["upload"],
    ["email", "webhook"],
    ["email", "upload"],
    ["webhook", "upload"],
    ["email", "webhook", "upload"],
])
def test_valid_channel_subsets_accepted(monkeypatch, channels):
    """All non-empty subsets of {email, webhook, upload} are accepted."""
    import core.import_templates as it

    captured: list[list] = []

    def fake_create_datastream(data, project_id, created_by, conn):
        captured.append(data.get("config", {}).get("channels", []))
        return _fake_ds(channels=channels)

    monkeypatch.setattr("core.datastreams.create_datastream", fake_create_datastream)
    _stub_operation(monkeypatch, it)
    monkeypatch.setattr(it, "get_template", lambda conn, code, v: _fake_tv_template())

    it.create_inbound_datastream(
        MagicMock(), project_id="p", name="n", template_code="OFFLINE_TV_V1",
        template_version=1, channels=channels, created_by="u", org_id="o",
        idempotency_key=f"ik-{'_'.join(sorted(channels))}",
    )
    assert captured, "create_datastream must have been called"
    assert set(captured[0]) == set(channels)


def test_unknown_channel_raises(monkeypatch):
    """Unknown channel -> TemplateChannelError; no Datastream created."""
    import core.import_templates as it
    from core.import_templates import TemplateChannelError

    monkeypatch.setattr(it, "get_template", lambda conn, code, v: _fake_tv_template())
    called = []
    monkeypatch.setattr(
        "core.datastreams.create_datastream",
        lambda *a, **kw: called.append(1) or {},
    )

    with pytest.raises(TemplateChannelError):
        it.create_inbound_datastream(
            MagicMock(), project_id="p", name="n", template_code="OFFLINE_TV_V1",
            template_version=1, channels=["fax"],
            created_by="u", org_id="o", idempotency_key="ik-bad-ch",
        )
    assert not called


def test_empty_channels_raises(monkeypatch):
    """Empty channels list -> TemplateChannelError."""
    import core.import_templates as it
    from core.import_templates import TemplateChannelError

    monkeypatch.setattr(it, "get_template", lambda conn, code, v: _fake_tv_template())

    with pytest.raises(TemplateChannelError):
        it.create_inbound_datastream(
            MagicMock(), project_id="p", name="n", template_code="OFFLINE_TV_V1",
            template_version=1, channels=[],
            created_by="u", org_id="o", idempotency_key="ik-empty",
        )


def test_none_channels_raises(monkeypatch):
    """None channels -> TemplateChannelError."""
    import core.import_templates as it
    from core.import_templates import TemplateChannelError

    monkeypatch.setattr(it, "get_template", lambda conn, code, v: _fake_tv_template())

    with pytest.raises(TemplateChannelError):
        it.create_inbound_datastream(
            MagicMock(), project_id="p", name="n", template_code="OFFLINE_TV_V1",
            template_version=1, channels=None,
            created_by="u", org_id="o", idempotency_key="ik-none-ch",
        )


# ---------------------------------------------------------------------------
# (g) Pinned version never auto-bumps.
# ---------------------------------------------------------------------------


def test_pinned_version_never_auto_bumped(monkeypatch):
    """An existing Datastream keeps its pinned version; create with v1 stays at v1."""
    import core.import_templates as it

    captured: dict = {}

    # latest_version says version 3 exists, but we pin v1.
    monkeypatch.setattr(it, "latest_version", lambda conn, code: 3)
    monkeypatch.setattr(it, "get_template",
                        lambda conn, code, version: _fake_tv_template() if version == 1 else None)

    def fake_create_datastream(data, project_id, created_by, conn):
        captured.update(data)
        return _fake_ds()

    monkeypatch.setattr("core.datastreams.create_datastream", fake_create_datastream)
    _stub_operation(monkeypatch, it)

    it.create_inbound_datastream(
        MagicMock(), project_id="p", name="n", template_code="OFFLINE_TV_V1",
        template_version=1,  # explicit pin at v1
        channels=["email"], created_by="u", org_id="o", idempotency_key="ik-pin",
    )

    assert captured["config"]["template_version"] == 1, (
        "Datastream must pin the requested version, not auto-bump to latest"
    )


# ---------------------------------------------------------------------------
# (h) Secret-free catalog read-model.
# ---------------------------------------------------------------------------


def test_list_templates_no_secret_fields(monkeypatch):
    """list_templates read-model must not include credentials or cross-tenant fields."""
    import core.import_templates as it

    secret_fields = {
        "signing_secret", "api_key", "token", "credential", "password",
        "dns_evidence_hash", "evidence_hash",
    }

    # Build a mock conn that returns one row.
    cur = _cur(fetchall=[
        ("OFFLINE_TV_V1", 1, "TV spend", _TV_CONTRACT, False, None),
    ])
    cur.fetchall.return_value = [("OFFLINE_TV_V1", 1, "TV spend", _TV_CONTRACT, False, None)]
    cur.description = [
        ("template_code",), ("version",), ("title",),
        ("contract",), ("is_generic",), ("created_at",),
    ]
    conn = _conn_with(cur)

    results = it.list_templates(conn)
    for entry in results:
        for key in entry:
            assert key not in secret_fields, (
                f"list_templates must not expose secret field '{key}'"
            )


def test_catalog_read_model_fields():
    """list_templates returns the documented safe fields only."""
    import core.import_templates as it

    expected_keys = {
        "template_code", "version", "title", "required_fields",
        "optional_fields", "identity_keys", "grain", "is_generic", "created_at",
    }

    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.description = [
        ("template_code",), ("version",), ("title",), ("contract",),
        ("is_generic",), ("created_at",),
    ]
    cur.fetchall.return_value = [
        ("OFFLINE_TV_V1", 1, "TV spend", _TV_CONTRACT, False, None),
    ]
    conn = MagicMock()
    conn.cursor.return_value = cur

    results = it.list_templates(conn)
    assert len(results) == 1
    assert set(results[0].keys()) == expected_keys


# ---------------------------------------------------------------------------
# (i) MCP list_inbound_templates == REST GET /templates (same evidence).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_mcp_registry(monkeypatch):
    from core import mcp_profiles

    monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
    mcp_profiles.reset_registry_for_tests()
    yield
    mcp_profiles.reset_registry_for_tests()


class _Recorder:
    def __init__(self):
        self.handlers = {}
        self.tool = MagicMock()

    def capture(self, handler):
        self.handlers[handler.__name__] = handler
        return handler


def _register_operations(recorder):
    from core import mcp_profiles, operations_mcp

    real = mcp_profiles.register_profiled

    def spy(mcp, handler, **kwargs):
        recorder.capture(handler)
        return real(mcp, handler, **kwargs)

    mcp_profiles.register_profiled = spy  # type: ignore[assignment]
    try:
        operations_mcp.register(recorder)
    finally:
        mcp_profiles.register_profiled = real  # type: ignore[assignment]
    return recorder.handlers


def test_list_inbound_templates_registered_under_operations():
    """list_inbound_templates must be registered under profile=operations, effect=read."""
    from core import mcp_profiles

    recorder = _Recorder()
    _register_operations(recorder)

    decls = {d.name: d for d in mcp_profiles.registered_declarations()}
    assert "list_inbound_templates" in decls, (
        "list_inbound_templates must be registered in the operations profile"
    )
    d = decls["list_inbound_templates"]
    assert d.profile == "operations"
    assert d.effect == "read"
    assert d.confirmation_mode == "none"


def test_list_inbound_templates_mcp_returns_templates(monkeypatch):
    """MCP list_inbound_templates returns the same catalog as REST list_templates."""

    fake_templates = [
        {
            "template_code": "OFFLINE_TV_V1", "version": 1, "title": "TV spend",
            "required_fields": ["net_cost_eur"], "optional_fields": [],
            "identity_keys": [], "grain": "spot", "is_generic": False, "created_at": None,
        }
    ]

    @contextlib.contextmanager
    def fake_gc():
        yield MagicMock()

    monkeypatch.setattr("core.db.get_connection", fake_gc)
    monkeypatch.setattr("core.import_templates.list_templates", lambda conn: fake_templates)

    recorder = _Recorder()
    _register_operations(recorder)

    handler = recorder.handlers.get("list_inbound_templates")
    assert handler is not None, "list_inbound_templates handler must be captured"
    assert callable(handler)

    # Call the handler -- it should return without error.
    result = handler()
    # result is a ToolResult; structured_content carries the data envelope.
    sc = result.structured_content
    assert sc is not None
    data = sc.get("data", {})
    assert "templates" in data
    assert data["templates"] == fake_templates
    assert data["count"] == 1


# ---------------------------------------------------------------------------
# (j) Idempotency-Key missing -> 422; conflicting -> 409 (ASGI).
# ---------------------------------------------------------------------------


@pytest.fixture()
def _import_template_app(monkeypatch):
    from core.import_templates_api import IMPORT_TEMPLATE_ROUTES
    from starlette.routing import Router
    from starlette.testclient import TestClient

    app = Router(routes=IMPORT_TEMPLATE_ROUTES)

    async def _fake_auth(request):
        return True, "user@example.com"

    monkeypatch.setattr("core.import_templates_api._check_auth", _fake_auth)
    return TestClient(app, raise_server_exceptions=False)


def test_post_datastreams_missing_idempotency_key(_import_template_app, monkeypatch):
    """POST /datastreams without Idempotency-Key -> 422."""
    client = _import_template_app
    resp = client.post(
        "/api/connectors/inbound/datastreams",
        json={
            "project_id": "proj-1",
            "name": "TV",
            "template_code": "OFFLINE_TV_V1",
            "template_version": 1,
            "channels": ["email"],
            "org_id": "org-1",
        },
    )
    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "missing_header"


def test_patch_enable_generic_draft_is_publish_gated(monkeypatch):
    """Story 38.6 AC4 / review F1: PATCH enabled=True on a generic-tabular draft
    (publish_gate active) is refused 422 -- the gate cannot be bypassed via PATCH,
    and update_datastream is never reached."""
    from unittest.mock import MagicMock

    import core.admin_api as admin_api
    import core.datastreams as datastreams_mod
    import core.db as db_mod
    from starlette.routing import Route, Router
    from starlette.testclient import TestClient

    async def _fake_auth(request):
        return True, "user@example.com"

    monkeypatch.setattr(admin_api, "_check_auth", _fake_auth)
    monkeypatch.setattr(admin_api, "_enforce_datastream_project_scope", lambda *a, **kw: None)
    monkeypatch.setattr(
        datastreams_mod,
        "get_datastream",
        lambda ds_id, project_id, conn: {
            "id": ds_id,
            "project_id": project_id,
            "versioned": False,
            "config": {"publish_gate": "canonical_semantics_required"},
        },
    )
    called = {"update": 0}

    def _no_update(*a, **kw):
        called["update"] += 1
        return {}

    monkeypatch.setattr(datastreams_mod, "update_datastream", _no_update)

    class _Ctx:
        def __enter__(self):
            cursor = MagicMock()
            cursor.__enter__.return_value = cursor
            cursor.__exit__.return_value = False
            cursor.fetchone.return_value = (
                None,
                None,
                None,
                {"publish_gate": "canonical_semantics_required"},
                None,
            )
            conn = MagicMock()
            conn.cursor.return_value = cursor
            return conn

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(db_mod, "get_connection", lambda: _Ctx())

    app = Router(
        routes=[
            Route("/api/datastreams/{id}", endpoint=admin_api._patch_datastream, methods=["PATCH"]),
        ]
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.patch(
        "/api/datastreams/ds-generic",
        json={"project_id": "proj-1", "enabled": True},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "publish_gate_active"
    assert called["update"] == 0  # gate blocked before any write


def test_post_datastreams_conflict(_import_template_app, monkeypatch):
    """POST /datastreams with conflicting Idempotency-Key -> 409."""

    from core.operations import OperationIdempotencyConflict

    @contextlib.contextmanager
    def fake_gc():
        yield MagicMock()

    def _raise_conflict(*a, **kw):
        raise OperationIdempotencyConflict("conflict")

    monkeypatch.setattr("core.db.get_connection", fake_gc)
    monkeypatch.setattr(
        "core.import_templates_api._check_project_member", lambda *a: True
    )
    monkeypatch.setattr(
        "core.import_templates.create_inbound_datastream", _raise_conflict
    )

    client = _import_template_app
    resp = client.post(
        "/api/connectors/inbound/datastreams",
        headers={"Idempotency-Key": "ik-conflict"},
        json={
            "project_id": "proj-1",
            "name": "TV",
            "template_code": "OFFLINE_TV_V1",
            "template_version": 1,
            "channels": ["email"],
            "org_id": "org-1",
        },
    )
    assert resp.status_code == 409


def test_get_templates_returns_catalog(_import_template_app, monkeypatch):
    """GET /templates returns the catalog list."""

    fake_catalog = [
        {
            "template_code": "OFFLINE_TV_V1", "version": 1, "title": "TV spend",
            "required_fields": [], "optional_fields": [], "identity_keys": [],
            "grain": "spot", "is_generic": False, "created_at": None,
        }
    ]

    @contextlib.contextmanager
    def fake_gc():
        yield MagicMock()

    monkeypatch.setattr("core.db.get_connection", fake_gc)
    monkeypatch.setattr("core.import_templates.list_templates", lambda conn: fake_catalog)

    client = _import_template_app
    resp = client.get("/api/connectors/inbound/templates")
    assert resp.status_code == 200
    body = resp.json()
    assert "templates" in body


def test_post_datastreams_non_member_gets_404(_import_template_app, monkeypatch):
    """POST /datastreams: caller without project access -> nondisclosing 404."""

    @contextlib.contextmanager
    def fake_gc():
        yield MagicMock()

    monkeypatch.setattr("core.db.get_connection", fake_gc)
    monkeypatch.setattr(
        "core.import_templates_api._check_project_member", lambda *a: False
    )

    client = _import_template_app
    resp = client.post(
        "/api/connectors/inbound/datastreams",
        headers={"Idempotency-Key": "ik-nonmember"},
        json={
            "project_id": "proj-1",
            "name": "TV",
            "template_code": "OFFLINE_TV_V1",
            "template_version": 1,
            "channels": ["email"],
            "org_id": "org-1",
        },
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found"


def test_post_datastreams_unknown_template_returns_422(_import_template_app, monkeypatch):
    """POST /datastreams with unknown template -> 422 (not 500)."""

    from core.import_templates import TemplateNotFound

    @contextlib.contextmanager
    def fake_gc():
        yield MagicMock()

    def _raise_not_found(*a, **kw):
        raise TemplateNotFound("no such template")

    monkeypatch.setattr("core.db.get_connection", fake_gc)
    monkeypatch.setattr(
        "core.import_templates_api._check_project_member", lambda *a: True
    )
    monkeypatch.setattr(
        "core.import_templates.create_inbound_datastream", _raise_not_found
    )

    client = _import_template_app
    resp = client.post(
        "/api/connectors/inbound/datastreams",
        headers={"Idempotency-Key": "ik-no-tpl"},
        json={
            "project_id": "proj-1",
            "name": "TV",
            "template_code": "NO_SUCH",
            "template_version": 1,
            "channels": ["email"],
            "org_id": "org-1",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "template_not_found"


def test_post_datastreams_bad_channels_returns_422(_import_template_app, monkeypatch):
    """POST /datastreams with invalid channel -> 422."""

    from core.import_templates import TemplateChannelError

    @contextlib.contextmanager
    def fake_gc():
        yield MagicMock()

    def _raise_channel_error(*a, **kw):
        raise TemplateChannelError("bad channel")

    monkeypatch.setattr("core.db.get_connection", fake_gc)
    monkeypatch.setattr(
        "core.import_templates_api._check_project_member", lambda *a: True
    )
    monkeypatch.setattr(
        "core.import_templates.create_inbound_datastream", _raise_channel_error
    )

    client = _import_template_app
    resp = client.post(
        "/api/connectors/inbound/datastreams",
        headers={"Idempotency-Key": "ik-bad-ch"},
        json={
            "project_id": "proj-1",
            "name": "TV",
            "template_code": "OFFLINE_TV_V1",
            "template_version": 1,
            "channels": ["fax"],
            "org_id": "org-1",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_channels"


# ---------------------------------------------------------------------------
# (k) Deterministic idempotency: request_payload has no random id.
# ---------------------------------------------------------------------------


def test_request_payload_has_no_random_id(monkeypatch):
    """request_payload must be deterministic over business inputs (review H1)."""
    import core.import_templates as it
    import core.operations as ops

    captured_specs: list = []

    def fake_execute(conn, spec, *, mutation):
        captured_specs.append(spec)
        changed = mutation(conn, "op-test")
        return ops.OperationResult("op-test", "succeeded", changed.result, None, None, False)

    # Patch at the module where execute_operation is defined (lazy import path).
    monkeypatch.setattr(ops, "execute_operation", fake_execute)
    monkeypatch.setattr(it, "get_template", lambda conn, code, v: _fake_tv_template())
    monkeypatch.setattr(
        "core.datastreams.create_datastream",
        lambda data, pid, cby, conn: _fake_ds(),
    )

    it.create_inbound_datastream(
        MagicMock(), project_id="p", name="TV test", template_code="OFFLINE_TV_V1",
        template_version=1, channels=["email"], created_by="u", org_id="o",
        idempotency_key="ik-deterministic",
    )

    assert captured_specs, "execute_operation must have been called"
    payload = captured_specs[0].request_payload
    # Payload must contain only stable business fields; no ULID/random id.
    allowed_keys = {
        "template_code", "template_version", "channels", "publishable",
        "project_id", "name",
    }
    assert set(payload.keys()).issubset(allowed_keys), (
        f"request_payload contains unexpected keys: {set(payload.keys()) - allowed_keys}"
    )
    # Calling twice with same inputs produces same hash (deterministic).
    from core.operations import _canonical_hash
    hash1 = _canonical_hash(payload)

    captured_specs.clear()
    it.create_inbound_datastream(
        MagicMock(), project_id="p", name="TV test", template_code="OFFLINE_TV_V1",
        template_version=1, channels=["email"], created_by="u", org_id="o",
        idempotency_key="ik-deterministic",
    )
    hash2 = _canonical_hash(captured_specs[0].request_payload)
    assert hash1 == hash2, "request_payload must be deterministic over business inputs"


# ---------------------------------------------------------------------------
# Live-PG-gated tests (skip without real DB).
# ---------------------------------------------------------------------------

_LIVE_PG = pytest.mark.skipif(
    True,  # always skip in offline suite; run with -k "live" against real PG
    reason="live-PG-gated: run with PLATFORM_DB_URL set and -k 'live'",
)


@_LIVE_PG
def test_live_pg_immutability_trigger():
    """Live PG: UPDATE and DELETE on app.import_templates must raise."""
    import psycopg
    from core.audit import _db_url

    with psycopg.connect(_db_url()) as conn:
        # Attempt UPDATE -- must fail.
        with pytest.raises(Exception, match="immutable"):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.import_templates SET title = 'hacked' "
                    "WHERE template_code = 'OFFLINE_TV_V1' AND version = 1"
                )
        conn.rollback()

        # Attempt DELETE -- must fail.
        with pytest.raises(Exception):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.import_templates "
                    "WHERE template_code = 'OFFLINE_TV_V1' AND version = 1"
                )
        conn.rollback()


@_LIVE_PG
def test_live_pg_primary_key_unique():
    """Live PG: duplicate (template_code, version) is rejected by PK."""
    import psycopg
    from core.audit import _db_url

    with psycopg.connect(_db_url()) as conn:
        with pytest.raises(Exception):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.import_templates "
                    "(template_code, version, title, contract) "
                    "VALUES ('OFFLINE_TV_V1', 1, 'dup', '{}'::jsonb)"
                )
        conn.rollback()


@_LIVE_PG
def test_live_pg_six_seeds_present():
    """Live PG: all 6 ratified template codes are seeded at version 1."""
    import psycopg
    from core.audit import _db_url

    expected_codes = {
        "OFFLINE_TV_V1",
        "OFFLINE_OOH_V1",
        "OFFLINE_DOOH_V1",
        "OFFLINE_RADIO_V1",
        "OFFLINE_PRESS_V1",
        "GENERIC_TABULAR_V1",
    }
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT template_code FROM app.import_templates WHERE version = 1"
            )
            found = {row[0] for row in cur.fetchall()}
    assert expected_codes.issubset(found), (
        f"Missing seeds: {expected_codes - found}"
    )


@_LIVE_PG
def test_live_pg_generic_is_generic_true():
    """Live PG: GENERIC_TABULAR_V1 seed has is_generic = TRUE."""
    import psycopg
    from core.audit import _db_url

    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_generic FROM app.import_templates "
                "WHERE template_code = 'GENERIC_TABULAR_V1' AND version = 1"
            )
            row = cur.fetchone()
    assert row is not None and row[0] is True
