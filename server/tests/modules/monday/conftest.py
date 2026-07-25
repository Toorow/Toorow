from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "monday"


def _load(name: str):
    path = MODULE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"monday_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def connector():
    return _load("connector")


@pytest.fixture(scope="session")
def oauth():
    return _load("oauth")


@pytest.fixture(scope="session")
def catalog():
    return json.loads((MODULE_DIR / "api_catalog.json").read_text(encoding="utf-8"))
