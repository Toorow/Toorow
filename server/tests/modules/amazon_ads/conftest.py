"""Shared fixtures for the amazon-ads module tests (Story 26.5).

Loads the connector by file path (the module directory is kebab-case, not a
package) and pins the platform LwA client-id env so header construction never
depends on the runner's environment. The async-report socle is driven with the
26.1 fakes (InMemoryReportRefStore + fake clock) -- no real sleeps, no DB.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "amazon-ads"
CONNECTOR_PATH = MODULE_DIR / "connector.py"

MANIFEST = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
HOSTS = MANIFEST["provider_api_hosts"]
API_VERSION = MANIFEST["provider_api_version"]


def import_connector():
    spec = importlib.util.spec_from_file_location("connector_amazon_ads", CONNECTOR_PATH)
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
def _client_id_env(monkeypatch):
    monkeypatch.setenv("AMAZON_ADS_CLIENT_ID", "amzn1.application-oa2-client.test")


class FakeClock:
    """Monotonic fake clock; the sleeper advances it (no real sleeps)."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def fake_clock():
    return FakeClock()


@pytest.fixture()
def ref_store(fake_clock):
    from core.async_reports import InMemoryReportRefStore

    return InMemoryReportRefStore(clock=fake_clock)


@pytest.fixture()
def async_hooks(fake_clock, ref_store):
    """The private test hooks _pull/pull_catalog_daily forward to the socle."""
    return {
        "_store": ref_store,
        "_clock": fake_clock,
        "_sleeper": fake_clock.sleep,
        "_deadline_seconds": 100000,
    }
