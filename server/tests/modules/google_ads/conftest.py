"""Shared fixtures for the google-ads module tests (Story 26.2).

Loads the connector by file path (the module directory is kebab-case, not a
package) and pins the platform developer-token env so header construction
never depends on the runner's environment.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "google-ads"
CONNECTOR_PATH = MODULE_DIR / "connector.py"

# The pinned version lives in ONE place (manifest) -- read it so test URLs can
# never drift from the pin (story 26.2).
MANIFEST = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
API_VERSION = MANIFEST["provider_api_version"]
API_BASE = f"{MANIFEST['provider_api_base']}/{API_VERSION}"


def import_connector():
    spec = importlib.util.spec_from_file_location("connector_google_ads", CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def connector():
    return import_connector()


@pytest.fixture(scope="session")
def catalog():
    return json.loads((MODULE_DIR / "api_catalog.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _developer_token(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "dev-token-test")
