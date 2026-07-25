"""Shared fixtures for the microsoft-ads module tests (Story 26.4).

Loads the connector by file path (the module directory is kebab-case, not a
package) and pins the platform developer-token env so header construction
never depends on the runner's environment. Ships an in-memory ZIP CSV builder
mirroring the provider download format (utf-8-sig CSV inside a ZIP).
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "microsoft-ads"
CONNECTOR_PATH = MODULE_DIR / "connector.py"

# The pinned version lives in ONE place (manifest) -- read it so test URLs can
# never drift from the pin (story 26.4).
MANIFEST = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
API_VERSION = MANIFEST["provider_api_version"]
REPORTING_BASE = f"{MANIFEST['provider_api_base']}/Reporting/{API_VERSION}"
CUSTOMER_BASE = (
    f"{MANIFEST['provider_customer_api_base']}/CustomerManagement/{API_VERSION}"
)
SUBMIT_URL = f"{REPORTING_BASE}/GenerateReport/Submit"
POLL_URL = f"{REPORTING_BASE}/GenerateReport/Poll"
DOWNLOAD_URL = "https://bingadsreports.test/download/report.zip"

# Composite topology account id '<account_id>@<customer_id>' (opaque to core).
ACCOUNT_ID = "135792468"
CUSTOMER_ID = "250012345"
COMPOSITE_ACCOUNT = f"{ACCOUNT_ID}@{CUSTOMER_ID}"


def import_connector():
    spec = importlib.util.spec_from_file_location(
        "connector_microsoft_ads", CONNECTOR_PATH
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


@pytest.fixture(autouse=True)
def _developer_token(monkeypatch):
    monkeypatch.setenv("MICROSOFT_ADS_DEVELOPER_TOKEN", "dev-token-test")


def make_report_zip(headers: list[str], rows: list[list[str]]) -> bytes:
    """Provider-shaped download artifact: ZIP holding one utf-8-sig CSV."""
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.csv", "﻿" + text.getvalue())
    return buffer.getvalue()


def fault_body(code: int, error_code: str = "MockError") -> dict:
    """ApiFaultDetail envelope with a numeric body Code (dossier section 10)."""
    return {
        "ApiFaultDetail": {
            "TrackingId": "trace-123",
            "Errors": [
                {"Code": code, "ErrorCode": error_code, "Message": f"mock {error_code}"}
            ],
        }
    }


class FakeClock:
    """Monotonic fake clock; the injected sleeper advances it (26.1 pattern)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
