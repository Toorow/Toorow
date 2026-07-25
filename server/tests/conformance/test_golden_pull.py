"""Layer 4: Golden pull fixture — Story 1.8 (T5.1–T5.6).

Replays ``tests/fixtures/golden_pull.json`` through the module's
``transform()`` function and compares the result field-by-field against
``tests/fixtures/expected_facts.json``.

Fixture contract (mandatory from this story onward):
  <module_path>/
    tests/
      fixtures/
        golden_pull.json      # raw input rows (list[dict])
        expected_facts.json   # canonical fact rows after transform() (list[dict])

Skips if layer 1 failed (_layer1_status[0] is False).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.conformance_layer_4


def _load_connector(module_path: Path) -> ModuleType:
    """Import connector.py from module_path via importlib."""
    connector_file = module_path / "connector.py"
    if not connector_file.exists():
        pytest.fail(f"[golden_pull] connector.py not found at {connector_file}")

    module_name = f"connector_{module_path.name}_gp"
    sys.modules.pop(module_name, None)

    try:
        spec = importlib.util.spec_from_file_location(module_name, connector_file)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as exc:
        pytest.fail(f"[golden_pull] connector.py could not be loaded: {exc}")

    return mod


# ---------------------------------------------------------------------------
# Layer 4 test
# ---------------------------------------------------------------------------


def test_golden_pull(
    manifest: dict, module_path: Path, _layer1_status: list[bool]
) -> None:
    """Replay golden fixture through transform() and compare to expected facts (AC5).

    Epic 31.2: routing is now per-PROFILE by landing, not by module_kind. This
    Layer runs when AT LEAST ONE profile lands in 'fact_daily_kpi'
    (resolve_landing). A pure context connector (all profiles -> context_events,
    e.g. github) skips this Layer (validated by Layer 5). A MIXED connector
    (youtube: kpi profiles + a context_events event profile) still runs BOTH
    Layers -- here it validates its transform()/expected_facts contract.

    HG-8: the skip condition is generic (resolve_landing over each profile) —
    no module-specific name appears here (Story 4.5, AC5; Epic 31.2).
    """
    if not _layer1_status[0]:
        pytest.skip("Layer 1 (manifest) failed — skipping golden pull layer")

    from core.context_events import resolve_landing  # noqa: PLC0415

    module_kind = manifest.get("module_kind", "kpi")
    profiles = manifest.get("report_profiles", [])
    has_kpi_profile = any(
        resolve_landing(p, module_kind) == "fact_daily_kpi" for p in profiles
    )
    # Epic 31.2: a module with no fact_daily_kpi-landing profile has no
    # transform()/expected_facts contract (all events -> Layer 5).
    if profiles and not has_kpi_profile:
        pytest.skip(
            "[golden_pull] no profile lands in fact_daily_kpi (all context_events) — "
            "Layer 4 (golden pull) is N/A; see Layer 5 (test_context_events.py)"
        )

    fixtures_dir = module_path / "tests" / "fixtures"
    if not fixtures_dir.exists():
        pytest.fail(
            f"[golden_pull] fixture directory missing — create "
            f"{module_path}/tests/fixtures/golden_pull.json and expected_facts.json"
        )

    golden_path = fixtures_dir / "golden_pull.json"
    expected_path = fixtures_dir / "expected_facts.json"

    if not golden_path.exists():
        pytest.fail(f"[golden_pull] golden_pull.json missing at {golden_path}")
    if not expected_path.exists():
        pytest.fail(f"[golden_pull] expected_facts.json missing at {expected_path}")

    raw_rows: list[dict] = json.loads(golden_path.read_text(encoding="utf-8"))
    expected_rows: list[dict] = json.loads(expected_path.read_text(encoding="utf-8"))

    connector_mod = _load_connector(module_path)

    if not hasattr(connector_mod, "transform"):
        pytest.fail(
            "[golden_pull] connector.py does not expose a transform(raw_rows) function"
        )

    try:
        actual_rows: list[dict] = connector_mod.transform(raw_rows)
    except Exception as exc:
        pytest.fail(f"[golden_pull] transform() raised an exception: {exc}")

    failures: list[str] = []

    if len(actual_rows) != len(expected_rows):
        failures.append(
            f"[golden_pull] row count mismatch: expected {len(expected_rows)}, "
            f"got {len(actual_rows)}"
        )

    for i, (actual, expected) in enumerate(zip(actual_rows, expected_rows)):
        all_keys = set(expected.keys()) | set(actual.keys())
        for field in sorted(all_keys):
            exp_val = expected.get(field, "<missing>")
            act_val = actual.get(field, "<missing>")
            if exp_val != act_val:
                failures.append(
                    f"[golden_pull] row {i} field {field}: expected {exp_val!r}, got {act_val!r}"
                )

    if failures:
        pytest.fail("\n".join(failures))
