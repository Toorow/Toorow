"""Pytest configuration for integration tests.

Sets anyio backend to asyncio for all async tests in this package.
"""

import pytest


@pytest.fixture
def anyio_backend():
    """Use asyncio as the anyio backend for integration tests."""
    return "asyncio"
