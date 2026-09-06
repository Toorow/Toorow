"""The scheduled work must leave a trace -- story 59.4 side-finding, 2026-08-08.

`uvicorn.run(log_level="info")` configures uvicorn's own three loggers, each with
`propagate: False`. It attaches NOTHING to the root logger, so every
`logger.info(...)` in `core.*` was handled by `logging.lastResort`, which emits at
WARNING. Measured on the deployed service: zero `scheduler: ` or `dq_monitors: `
lines over two days, while the */15 tick answered 200 throughout.

These tests pin the repair at the level that decides it -- whether an INFO record
from a `core.*` logger reaches a handler -- rather than at the call that happens
to set it up.
"""

from __future__ import annotations

import io
import logging

import pytest


@pytest.fixture
def _restore_root_logging():
    """Give each test the root logger back exactly as it found it."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def test_core_logger_info_is_dropped_without_configuration(_restore_root_logging):
    """The defect itself: no root handler means INFO never reaches stdout.

    This is what production did, and it is why "did the nightly sweep run?" could
    not be answered from the logs.
    """
    root = logging.getLogger()
    root.handlers[:] = []
    root.setLevel(logging.WARNING)

    assert logging.getLogger("core.dq_monitors").isEnabledFor(logging.INFO) is False


def test_configure_logging_lets_core_info_through(_restore_root_logging, monkeypatch):
    from core.main import configure_logging

    monkeypatch.delenv("TOOROW_LOG_LEVEL", raising=False)
    level = configure_logging()

    assert level == logging.INFO
    root = logging.getLogger()
    assert root.handlers, "configure_logging must attach a handler to the root logger"

    captured = io.StringIO()
    root.handlers[0].setStream(captured)
    logging.getLogger("core.dq_monitors").info("dq_monitors: starting: datastreams=%d", 7)

    assert "dq_monitors: starting: datastreams=7" in captured.getvalue()


def test_configure_logging_honours_the_env_level(_restore_root_logging, monkeypatch):
    from core.main import configure_logging

    monkeypatch.setenv("TOOROW_LOG_LEVEL", "warning")
    assert configure_logging() == logging.WARNING
    assert logging.getLogger("core.scheduler").isEnabledFor(logging.INFO) is False


def test_unknown_level_falls_back_to_info_and_never_raises(
    _restore_root_logging, monkeypatch
):
    """A malformed level must not stop the server from booting."""
    from core.main import configure_logging

    monkeypatch.setenv("TOOROW_LOG_LEVEL", "LOUD")
    assert configure_logging() == logging.INFO
    assert logging.getLogger("core.scheduler").isEnabledFor(logging.INFO) is True
