"""Layer 5: Context-event golden fixture — Story 4.5 (AC5, HG-5) / Epic 31.2.

Epic 31.2: routing is per-PROFILE by ``landing``, not by ``module_kind``. This
Layer runs when AT LEAST ONE profile lands in ``context_events`` (resolve_landing).
That covers BOTH a pure-context connector (github: all profiles context_events)
AND a MIXED connector (youtube: kpi profiles + a ``video_upload`` event profile).
A mixed connector therefore runs this Layer AND Layer 4 (per-profile routing).

Two fixture mechanisms are supported (a module may carry either or both):

  (A) transform_events replay (symmetric to Layer 4's golden_pull/expected_facts):
      ``tests/fixtures/golden_events.json`` (raw source rows) is fed through
      ``connector.transform_events()`` and compared field-by-field against
      ``tests/fixtures/expected_events.json`` (canonical event dicts). Pure,
      no I/O. This is the youtube video_upload contract.

  (B) pull() golden fixture (Story 4.5): ``golden_context_pull.json`` drives
      ``connector.pull()`` with mocked I/O; asserts rows_written / event types /
      project_id / platform / source. This is the github contract. Epic 31.2/H2:
      it mocks ``persist_context_event`` (the canonical sink that receives
      platform/source) so the platform/source STAMPING is asserted and
      ``validate_event_type`` is exercised in conformance.

HG-8: all skip/adapt conditions are generic (resolve_landing over each profile);
no module-specific strings appear in this file.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

pytestmark = pytest.mark.conformance_layer_5


def _load_connector(module_path: Path):
    """Import connector.py from module_path via importlib."""
    connector_file = module_path / "connector.py"
    if not connector_file.exists():
        pytest.fail(f"[context_events] connector.py not found at {connector_file}")

    module_name = f"connector_{module_path.name}_ctx"
    sys.modules.pop(module_name, None)

    try:
        spec = importlib.util.spec_from_file_location(module_name, connector_file)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as exc:
        pytest.fail(f"[context_events] connector.py could not be loaded: {exc}")

    return mod


# ---------------------------------------------------------------------------
# Layer 5 test
# ---------------------------------------------------------------------------


def test_context_events_golden_pull(
    manifest: dict,
    module_path: Path,
    _layer1_status: list[bool],
) -> None:
    """Layer 5: context module pull() writes expected context_events rows (AC5).

    Skips for kpi modules (module_kind != 'context').

    Fixture contract:
      <module_path>/tests/fixtures/golden_context_pull.json
      {
        "pull_inputs": {
          "connection_id": "...",
          "date_from": "YYYY-MM-DD",
          "date_to": "YYYY-MM-DD",
          "project_id": "...",
          "pull_id": "...",
          "profile_id": "...",
          "connection_config": { "owner": "...", "repo": "..." }
        },
        "api_responses": {
          "<url>": { "status_code": 200, "json": [...] }
        },
        "expected_rows_written": N,
        "expected_event_types": ["release", ...]
      }

    HG-8: routing is generic (resolve_landing over each profile).
    """
    if not _layer1_status[0]:
        pytest.skip("Layer 1 (manifest) failed — skipping context events layer")

    from core.context_events import resolve_landing  # noqa: PLC0415

    module_kind = manifest.get("module_kind", "kpi")
    profiles = manifest.get("report_profiles", [])
    has_event_profile = any(
        resolve_landing(p, module_kind) == "context_events" for p in profiles
    )
    # Fallback for a manifest with no report_profiles: honour the legacy hint.
    if not profiles:
        has_event_profile = module_kind == "context"
    if not has_event_profile:
        pytest.skip(
            "[context_events] no context_events-landing profile — Layer 5 is for "
            "modules with an event profile only; kpi profiles are validated by Layers 2–4"
        )

    fixtures_dir = module_path / "tests" / "fixtures"
    connector_mod = _load_connector(module_path)

    failures: list[str] = []
    ran_any = False

    # --- (A) transform_events replay (golden_events -> expected_events) ---------
    golden_events_path = fixtures_dir / "golden_events.json"
    expected_events_path = fixtures_dir / "expected_events.json"
    if golden_events_path.exists() or expected_events_path.exists():
        ran_any = True
        failures.extend(
            _check_transform_events_replay(
                connector_mod, golden_events_path, expected_events_path
            )
        )

    # --- (B) pull() golden fixture (rows_written / types / project_id / stamps) -
    fixture_path = fixtures_dir / "golden_context_pull.json"
    if fixture_path.exists():
        ran_any = True
        failures.extend(
            _check_pull_golden_fixture(connector_mod, fixture_path)
        )

    if not ran_any:
        pytest.fail(
            "[context_events] a context_events-landing profile is declared but no "
            "fixture found — create tests/fixtures/golden_events.json + "
            "expected_events.json (transform_events replay) and/or "
            "golden_context_pull.json (pull replay) to satisfy Layer 5"
        )

    if failures:
        pytest.fail("\n".join(failures))


# ---------------------------------------------------------------------------
# (A) transform_events replay — symmetric to Layer 4 golden_pull/expected_facts
# ---------------------------------------------------------------------------


def _check_transform_events_replay(
    connector_mod, golden_events_path: Path, expected_events_path: Path
) -> list[str]:
    """Assert connector.transform_events(golden_events) == expected_events."""
    failures: list[str] = []

    if not golden_events_path.exists():
        return [f"[context_events] golden_events.json missing at {golden_events_path}"]
    if not expected_events_path.exists():
        return [f"[context_events] expected_events.json missing at {expected_events_path}"]
    if not hasattr(connector_mod, "transform_events"):
        return [
            "[context_events] connector.py declares a context_events event profile "
            "but does not expose a transform_events(raw_rows) function"
        ]

    raw_rows: list[dict] = json.loads(golden_events_path.read_text(encoding="utf-8"))
    expected_rows: list[dict] = json.loads(expected_events_path.read_text(encoding="utf-8"))

    try:
        # Golden replay contract: NO date window (both bounds default None) so the
        # mapping is a pure 1:1, exactly like transform() in Layer 4.
        actual_rows: list[dict] = connector_mod.transform_events(raw_rows)
    except Exception as exc:
        return [f"[context_events] transform_events() raised an exception: {exc}"]

    if len(actual_rows) != len(expected_rows):
        failures.append(
            f"[context_events] event row count mismatch: expected {len(expected_rows)}, "
            f"got {len(actual_rows)}"
        )

    for i, (actual, expected) in enumerate(zip(actual_rows, expected_rows)):
        all_keys = set(expected.keys()) | set(actual.keys())
        for field in sorted(all_keys):
            exp_val = expected.get(field, "<missing>")
            act_val = actual.get(field, "<missing>")
            if exp_val != act_val:
                failures.append(
                    f"[context_events] event row {i} field {field}: "
                    f"expected {exp_val!r}, got {act_val!r}"
                )
    return failures


# ---------------------------------------------------------------------------
# (B) pull() golden fixture — Story 4.5 + Epic 31.2/H2 (platform/source stamps)
# ---------------------------------------------------------------------------


def _check_pull_golden_fixture(connector_mod, fixture_path: Path) -> list[str]:
    """Drive connector.pull() with mocked I/O; assert counts + stamping.

    Epic 31.2 / H2: patches ``core.context_events.persist_context_event`` (the
    canonical sink that receives platform/source) rather than the module-private
    ``_write_context_event``, so the platform/source stamping is captured and
    asserted, and ``validate_event_type`` runs in conformance.
    """
    fixture: dict = json.loads(fixture_path.read_text(encoding="utf-8"))

    pull_inputs: dict = fixture.get("pull_inputs", {})
    api_responses: dict = fixture.get("api_responses", {})
    expected_rows_written: int = fixture.get("expected_rows_written", 0)
    expected_event_types: list[str] = fixture.get("expected_event_types", [])
    expected_platform = fixture.get("expected_platform")
    expected_source = fixture.get("expected_source")

    if not hasattr(connector_mod, "pull"):
        return ["[context_events] connector.py does not expose a pull() function"]

    # Capture events at the canonical sink (persist_context_event): its kwargs
    # carry type -> event_type, plus platform and source (H2).
    written_rows: list[dict] = []

    import httpx as httpx_mod  # noqa: PLC0415
    from core import context_events as _ce  # noqa: PLC0415

    def _capture_persist(**kwargs) -> str:
        row = dict(kwargs)
        # Normalise 'type' -> 'event_type' so downstream assertions are uniform.
        if "type" in row and "event_type" not in row:
            row["event_type"] = row["type"]
        # H2: exercise the real canonical-vocabulary guard in conformance
        # (fail-closed: a connector event with an unknown type raises here,
        # exactly as the real persist_context_event would).
        _ce.validate_event_type(row.get("type", ""), row.get("source", "manual"))
        written_rows.append(row)
        return "evt_conformance_stub"

    def _resolve_mock_response(url: str, method: str = "get") -> httpx_mod.Response:
        """Return mocked response for URLs in api_responses; 404 for others."""
        for url_key, mock_resp in api_responses.items():
            if url_key in url or url == url_key:
                resp_headers = mock_resp.get("headers", {})
                return httpx_mod.Response(
                    mock_resp.get("status_code", 200),
                    json=mock_resp.get("json", []),
                    headers=resp_headers,
                )
        return httpx_mod.Response(404, json={"message": "Not found (test mock)"})

    def _fake_httpx_get(url: str, headers=None, timeout=None, **kwargs):
        """Return mocked response for GET requests (REST-style connectors)."""
        return _resolve_mock_response(url, method="get")

    class _FakeHttpxClient:
        """Minimal httpx.Client stub for GraphQL POST connectors (e.g. monday).

        Supports the context-manager protocol used by ``with httpx.Client() as c:``.
        The ``post`` method returns mock responses keyed from api_responses; headers
        from the fixture are forwarded so connectors that inspect response headers
        (e.g. API-Version, RateLimit-Policy) receive fixture-controlled values.
        """

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url: str, **kwargs) -> httpx_mod.Response:  # noqa: ARG002
            return _resolve_mock_response(url, method="post")

    patches = [
        mock.patch.object(_ce, "persist_context_event", side_effect=_capture_persist),
        # Also stub delete_connector_events_in_window: connectors that implement
        # idempotence (Epic 31.3) call this before re-inserting; the conformance
        # harness has no DB, so we stub it to a no-op returning 0.
        mock.patch.object(_ce, "delete_connector_events_in_window", return_value=0),
        mock.patch("httpx.get", side_effect=_fake_httpx_get),
        mock.patch("httpx.Client", _FakeHttpxClient),
    ]
    # github resolves its token via a module-private helper; patch it if present.
    if hasattr(connector_mod, "_get_github_token"):
        patches.append(
            mock.patch.object(
                connector_mod, "_get_github_token", return_value="fake-token-layer5"
            )
        )
    # GraphQL connectors resolve tokens via a module-private helper (_token);
    # patch it if present so nango is not called during conformance.
    if hasattr(connector_mod, "_token"):
        patches.append(
            mock.patch.object(
                connector_mod, "_token", return_value="fake-token-layer5"
            )
        )

    from contextlib import ExitStack  # noqa: PLC0415

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        try:
            result = connector_mod.pull(**pull_inputs)
        except Exception as exc:
            return [f"[context_events] pull() raised an unexpected exception: {exc}"]

    failures: list[str] = []

    # 1. rows_written count
    actual_rows_written = result.get("rows_written", -1)
    if actual_rows_written != expected_rows_written:
        failures.append(
            f"[context_events] rows_written mismatch: expected {expected_rows_written}, "
            f"got {actual_rows_written}"
        )

    # 2. pull_id propagated correctly
    if result.get("pull_id") != pull_inputs.get("pull_id"):
        failures.append(
            f"[context_events] pull_id in result ({result.get('pull_id')!r}) "
            f"does not match input ({pull_inputs.get('pull_id')!r})"
        )

    # 3. event types match
    if expected_event_types and written_rows:
        actual_types = [r.get("event_type") for r in written_rows]
        for expected_type in expected_event_types:
            if expected_type not in actual_types:
                failures.append(
                    f"[context_events] expected event_type {expected_type!r} "
                    f"not found in written rows (found: {actual_types})"
                )

    # 4. project_id binding on all written rows
    for i, row in enumerate(written_rows):
        if row.get("project_id") != pull_inputs.get("project_id"):
            failures.append(
                f"[context_events] row {i}: project_id {row.get('project_id')!r} "
                f"does not match input project_id {pull_inputs.get('project_id')!r}"
            )

    # 5. created_by follows '<connector>_pull:<pull_id>' convention.
    # Accept any non-empty value that ends with ':<pull_id>' so the check is
    # connector-agnostic (github_pull:X, monday_pull:X, etc. all pass).
    pull_id = pull_inputs.get("pull_id", "")
    for i, row in enumerate(written_rows):
        actual_created_by = row.get("created_by") or ""
        if actual_created_by and not actual_created_by.endswith(f":{pull_id}"):
            failures.append(
                f"[context_events] row {i}: created_by {actual_created_by!r} "
                f"does not follow the '<connector>_pull:{pull_id}' convention"
            )

    # 6. Epic 31.2 / H2: platform + source stamping (from the canonical sink).
    if expected_platform is not None:
        for i, row in enumerate(written_rows):
            if row.get("platform") != expected_platform:
                failures.append(
                    f"[context_events] row {i}: platform {row.get('platform')!r} "
                    f"does not match expected_platform {expected_platform!r}"
                )
    if expected_source is not None:
        for i, row in enumerate(written_rows):
            if row.get("source") != expected_source:
                failures.append(
                    f"[context_events] row {i}: source {row.get('source')!r} "
                    f"does not match expected_source {expected_source!r}"
                )

    return failures
