"""Conformance suite configuration — Story 1.8 (T1.1, T1.2).

Provides the ``module_path`` and ``manifest`` fixtures consumed by every
conformance layer.

**Every connector is swept by default.** ``module_path`` is parametrized over
``server/modules/*/`` (every folder carrying a ``manifest.json``), so a
non-conformant connector reddens the repository suite without anyone typing its
name. Each test id carries the module name, so a failure names the connector
immediately::

    tests/conformance/test_golden_pull.py::test_golden_pull[meta-ads]

``--module-path modules/<name>`` is a FILTER, not a prerequisite: it restricts
the parametrization to a single module for debugging. Before this change the
option was mandatory and six layers skipped outright without it — 37 connectors
of 38 were therefore never swept, which is how a ``pull()`` signature breach
reached the first real call with the contract already written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# CLI option registration
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--module-path",
        action="store",
        default=None,
        help=(
            "Optional FILTER: restrict the conformance sweep to a single module "
            "folder (e.g. --module-path modules/gsc). Omit it to sweep every "
            "connector in server/modules/."
        ),
    )


# ---------------------------------------------------------------------------
# Module discovery + parametrization
# ---------------------------------------------------------------------------

#: server/tests/conformance/conftest.py -> server/ -> server/modules/
_MODULES_DIR = Path(__file__).resolve().parents[2] / "modules"


def _all_module_dirs() -> list[Path]:
    """Every connector folder: a directory under server/modules/ with a manifest."""
    if not _MODULES_DIR.is_dir():  # pragma: no cover - repository layout guard
        raise pytest.UsageError(f"connector root not found: {_MODULES_DIR}")
    return sorted(
        (d for d in _MODULES_DIR.iterdir() if d.is_dir() and (d / "manifest.json").is_file()),
        key=lambda d: d.name,
    )


def _selected_module_dirs(config: pytest.Config) -> list[Path]:
    """Modules to sweep: all of them, or the single one --module-path names."""
    raw = config.getoption("--module-path")
    if raw is None:
        return _all_module_dirs()
    p = Path(raw).resolve()
    if not p.is_dir():
        raise pytest.UsageError(f"--module-path does not exist: {p}")
    return [p]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize every module-aware test over the selected connectors.

    ``module_path`` is parametrized indirectly, so any test reaching it through
    the closure (``manifest``, ``_layer1_status``, …) is swept too, without those
    tests declaring anything. The id is the module folder name.
    """
    if "module_path" not in metafunc.fixturenames:
        return
    selected = _selected_module_dirs(metafunc.config)
    metafunc.parametrize(
        "module_path",
        selected,
        ids=[d.name for d in selected],
        indirect=True,
        scope="session",
    )


# ---------------------------------------------------------------------------
# Enforce layer execution order: manifest → envelope → bundle → golden_pull
# ---------------------------------------------------------------------------

_LAYER_ORDER = [
    "test_manifest",
    "test_envelope",
    "test_bundle",
    "test_golden_pull",
    "test_context_events",  # Layer 5 (Story 4.5): context module golden fixture
    "test_reports",  # Layer 6 (Story 6.1): report-pack definition validation
]


def _parametrized_module_name(item: pytest.Item) -> str:
    """Connector name this item was parametrized on ('' when module-agnostic)."""
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return ""
    value = callspec.params.get("module_path")
    if value is None:
        return ""
    return Path(value).name


def pytest_collection_modifyitems(items: list) -> None:  # type: ignore[type-arg]
    """Group conformance items per connector, layer 1 first inside each group.

    Two invariants ride on this order:
      * layer 1 (manifest) runs before layers 2–6 **of the same module**, so the
        fail-fast flag is set before the layers that consult it;
      * all layers of one connector are contiguous, so the session-scoped
        ``module_path``/``manifest`` fixtures are built once per connector
        instead of thrashing on every parameter switch.

    Module-agnostic conformance tests (no ``module_path`` in their closure) keep
    the historical layer-then-nodeid order and run first.
    """
    def _layer_key(item: pytest.Item) -> tuple[str, int, str]:
        module_name = item.module.__name__.split(".")[-1] if hasattr(item, "module") else ""
        try:
            idx = _LAYER_ORDER.index(module_name)
        except ValueError:
            idx = len(_LAYER_ORDER)
        return (_parametrized_module_name(item), idx, item.nodeid)

    conformance_items = [i for i in items if "conformance" in i.nodeid]
    other_items = [i for i in items if "conformance" not in i.nodeid]

    conformance_items.sort(key=_layer_key)
    items[:] = other_items + conformance_items


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def module_path(request: pytest.FixtureRequest) -> Path:
    """Resolved path to the connector folder under test.

    Always parametrized by ``pytest_generate_tests`` above — one value per
    connector swept. There is no un-parametrized path any more: a test that
    reaches this fixture is a test that runs on every connector.
    """
    param = getattr(request, "param", None)
    if param is None:  # pragma: no cover - defensive: parametrization is unconditional
        pytest.fail(
            "module_path was requested without parametrization — "
            "pytest_generate_tests in tests/conformance/conftest.py must run"
        )
    p = Path(param).resolve()
    if not p.is_dir():
        pytest.fail(f"module folder does not exist: {p}")
    return p


@pytest.fixture(scope="session")
def manifest(module_path: Path) -> dict:
    """Parsed manifest.json for the module under test."""
    manifest_path = module_path / "manifest.json"
    if not manifest_path.exists():
        pytest.fail(f"[manifest] manifest.json not found at {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Layer-1 pass/fail flag — PER CONNECTOR
# ---------------------------------------------------------------------------
# test_manifest.py flips ``_layer1_status[0] = False`` so the later layers of the
# SAME connector skip instead of cascading noise. Once the suite sweeps 38
# connectors, a single shared list[bool] would silence layers 2–6 of every other
# connector as soon as one manifest is broken. The flag is therefore stored in a
# session-level dict keyed by connector name, and handed to the test through a
# tiny proxy that keeps the historical ``status[0]`` read/write shape.
#
# The proxy (rather than a parametrized list fixture) also survives pytest
# re-instantiating session-scoped fixtures when the parameter cycles: the truth
# lives in the dict, which is created once for the whole session.


@pytest.fixture(scope="session")
def _layer1_failed_modules() -> set[str]:
    """Names of connectors whose layer 1 (manifest) failed during this session."""
    return set()


class _Layer1Status:
    """``status[0]`` reads/writes the layer-1 verdict of ONE connector."""

    __slots__ = ("_failed", "_module")

    def __init__(self, failed: set[str], module: str) -> None:
        self._failed = failed
        self._module = module

    def __getitem__(self, index: int) -> bool:
        return self._module not in self._failed

    def __setitem__(self, index: int, value: bool) -> None:
        if value:
            self._failed.discard(self._module)
        else:
            self._failed.add(self._module)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<layer1 {self._module}: {'passed' if self[0] else 'FAILED'}>"


@pytest.fixture
def _layer1_status(module_path: Path, _layer1_failed_modules: set[str]) -> _Layer1Status:
    """Per-connector layer-1 verdict; [True] = passed, [False] = failed."""
    return _Layer1Status(_layer1_failed_modules, module_path.name)


# ---------------------------------------------------------------------------
# Story 6.3 — additive fixture: multi-connector golden DuckDB extension.
#
# For the organic_crossview report (gsc module), Layer 6 needs BOTH gsc and
# google-analytics rows in the golden DuckDB mart, plus a semantic view for
# the non-additive average_position routing (AD-4).
#
# This fixture patches test_reports._build_golden_duckdb to post-process the
# freshly created golden DuckDB and add:
#   1. main_marts.semantic_avg_position view (from fact_daily_kpi gsc rows)
#   2. google-analytics connector rows for organic_crossview cross-source join
#
# Additive only — no existing fixture or test logic is changed.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _extend_golden_duckdb_for_gsc(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch _build_golden_duckdb to add semantic view + GA4 rows for gsc module.

    Runs for every test in the conformance suite but only applies the patch to
    the gsc parameter of the sweep (safe no-op for the 37 other connectors).
    """
    # Module-agnostic tests (Story 37.7's country-vocabulary data tests, the
    # registry/capability gates, ...) never reach a connector: asking for
    # module_path there would raise, and there is nothing to patch anyway.
    # Testing the CLOSURE rather than a hard-coded file list keeps this correct
    # when a new module-agnostic file lands in the directory.
    if "module_path" not in request.fixturenames:
        return

    module_path = request.getfixturevalue("module_path")
    if module_path.name != "gsc":
        return

    try:
        from tests.conformance import test_reports as _tr  # noqa: PLC0415
    except ImportError:
        return

    original_build = _tr._build_golden_duckdb

    def _patched_build_golden_duckdb(
        module_path_arg: Path, module_name: str, db_path: Path
    ) -> bool:
        result = original_build(module_path_arg, module_name, db_path)
        if not result:
            return result
        try:
            import duckdb  # noqa: PLC0415

            con = duckdb.connect(str(db_path))
            try:
                # 1. Create semantic_avg_position view (AD-4: non-additive routing).
                #    View computes impression-weighted average position from fact_daily_kpi.
                #    Mirrors dbt/models/marts/semantic_avg_position.sql. The name is the
                #    canonical one after the 19.4 debt resolution (average_position lives
                #    in semantic_avg_position, NOT the pre-19.4 semantic_average_position
                #    which warehouse.py treats as inexistent).
                con.execute(
                    """
                    CREATE VIEW IF NOT EXISTS main_marts.semantic_avg_position AS
                    SELECT
                        project_id,
                        date,
                        connector,
                        breakdown_dimension,
                        breakdown_value,
                        CASE
                            WHEN SUM(
                                CASE WHEN metric = 'impressions' THEN value ELSE 0 END
                            ) > 0
                            THEN SUM(
                                CASE WHEN metric = 'average_position' THEN value ELSE 0 END
                                * CASE WHEN metric = 'impressions' THEN value ELSE 0 END
                            ) / NULLIF(SUM(
                                CASE WHEN metric = 'impressions' THEN value ELSE 0 END
                            ), 0)
                            ELSE AVG(CASE WHEN metric = 'average_position' THEN value END)
                        END AS average_position,
                        -- Colonnes de preuve (AD-4): la couche semantique
                        -- geographique re-agrege les lignes pays apres regroupement
                        -- par marche a partir du poids, jamais en moyennant un
                        -- ratio deja calcule. warehouse.py les exige via
                        -- _SEMANTIC_EVIDENCE_BY_METRIC -- sans elles la vue de
                        -- fixture divergeait du modele dbt et la requete cassait
                        -- sur "semantic_weight not found".
                        SUM(
                            CASE WHEN metric = 'impressions' THEN value ELSE 0 END
                        ) AS impressions_weight,
                        SUM(
                            CASE WHEN metric = 'impressions' THEN value ELSE 0 END
                        ) AS semantic_weight,
                        MAX(pull_id) AS pull_id,
                        MAX(loaded_at) AS loaded_at
                    FROM main_marts.fact_daily_kpi
                    WHERE metric IN ('average_position', 'impressions')
                      AND connector = 'gsc'
                    GROUP BY project_id, date, connector, breakdown_dimension, breakdown_value
                    """
                )
                # 2. Seed google-analytics sessions rows for organic_crossview.
                #    Multi-connector fixture: gsc + google-analytics on same dates (Story 6.3 AC7).
                sessions_rows = [
                    ("conformance-test", "2026-07-01", "google-analytics", "sessions",
                     "date", "2026-07-01", 320.0, "pull_ga4_golden", "2026-07-01T00:00:00"),
                    ("conformance-test", "2026-07-02", "google-analytics", "sessions",
                     "date", "2026-07-02", 295.0, "pull_ga4_golden", "2026-07-01T00:00:00"),
                    ("conformance-test", "2026-07-03", "google-analytics", "sessions",
                     "date", "2026-07-03", 310.0, "pull_ga4_golden", "2026-07-01T00:00:00"),
                ]
                for row in sessions_rows:
                    con.execute(
                        "INSERT INTO main_marts.fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)",
                        list(row),
                    )
            finally:
                con.close()
        except Exception:
            pass  # best-effort; the main fixture succeeded
        return result

    monkeypatch.setattr(_tr, "_build_golden_duckdb", _patched_build_golden_duckdb)
