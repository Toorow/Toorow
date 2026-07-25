"""Opt-in integration test: parse a real facebook-ads-fields.md snapshot.

Skipped unless CATALOG_GEN_REAL_SOURCES env var points to the real file:
    CATALOG_GEN_REAL_SOURCES=/path/to/facebook-ads-fields.md pytest \\
        server/tests/tooling/test_catalog_gen_integration.py -v

Verified counts (2026-07-21, source: docs.supermetrics.com/docs/facebook-ads-fields.md,
~791 KB): exactly 537 metrics and 254 dimensions. Clean-line grep reproduces both.

Never runs in CI. The CATALOG_GEN_REAL_SOURCES env var must not be set in CI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_SERVER_DIR = _REPO_ROOT / "server"

for _p in (_SCRIPTS_DIR, _SERVER_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from catalog_gen.supermetrics_parser import parse_supermetrics_markdown  # noqa: E402

_REAL_SOURCES_ENV = "CATALOG_GEN_REAL_SOURCES"
_EXPECTED_METRICS = 537
_EXPECTED_DIMENSIONS = 254


@pytest.mark.skipif(
    not os.environ.get(_REAL_SOURCES_ENV),
    reason=f"{_REAL_SOURCES_ENV} env var not set — real-file test skipped",
)
def test_facebook_ads_fields_real_parse() -> None:
    """Parse the real facebook-ads-fields.md and assert exact metric/dimension counts.

    This is the canonical smoke test that verifies the parser handles the
    full Supermetrics markdown structure correctly.
    Expected: 537 metrics, 254 dimensions (verified 2026-07-21).
    """
    source_path = Path(os.environ[_REAL_SOURCES_ENV])
    assert source_path.exists(), f"CATALOG_GEN_REAL_SOURCES file not found: {source_path}"

    text = source_path.read_text(encoding="utf-8")
    entries = parse_supermetrics_markdown(text)

    metrics = [e for e in entries if e.kind == "metric"]
    dimensions = [e for e in entries if e.kind == "dimension"]

    assert len(metrics) == _EXPECTED_METRICS, (
        f"Expected {_EXPECTED_METRICS} metrics, got {len(metrics)}. "
        "Check supermetrics_parser clean-line logic."
    )
    assert len(dimensions) == _EXPECTED_DIMENSIONS, (
        f"Expected {_EXPECTED_DIMENSIONS} dimensions, got {len(dimensions)}. "
        "Check supermetrics_parser clean-line logic."
    )
