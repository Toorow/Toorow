"""Shared fixtures for the pinterest-ads module tests (Story 26.3).

Loads the connector by file path (the module directory is kebab-case, not a
package). The pinned API version lives in ONE place (manifest) -- test URLs
are built from it so they can never drift from the pin.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "pinterest-ads"
CONNECTOR_PATH = MODULE_DIR / "connector.py"

MANIFEST = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
API_VERSION = MANIFEST["provider_api_version"]
API_BASE = f"{MANIFEST['provider_api_base']}/{API_VERSION}"

AD_ACCOUNT = "549755885175"


def import_connector():
    spec = importlib.util.spec_from_file_location(
        "connector_pinterest_ads_tests", CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def connector():
    return import_connector()


@pytest.fixture(scope="session")
def catalog():
    return json.loads((MODULE_DIR / "api_catalog.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def catalog_sources():
    return json.loads(
        (MODULE_DIR / "catalog_sources" / "catalog_sources.json").read_text(
            encoding="utf-8"
        )
    )
