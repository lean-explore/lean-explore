"""Tests for logging setup."""

import logging
import sys

import pytest

from lean_explore.util.logging import setup_logging


@pytest.fixture
def clean_root_logger(monkeypatch):
    """Swap in a fresh root logger for basicConfig; restore state afterwards.

    pytest attaches its capture handlers to whatever ``logging.getLogger()``
    returns when the test body starts, which would turn basicConfig into a
    no-op. Tests therefore call the yielded function to drop those handlers
    right before exercising setup_logging.
    """
    root = logging.RootLogger(logging.WARNING)
    monkeypatch.setattr(logging, "root", root)
    noisy = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}

    def reset() -> logging.RootLogger:
        root.handlers.clear()
        return root

    yield reset
    for handler in root.handlers:
        handler.close()
    for name, level in noisy.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize(
    ("verbose", "level"), [(False, logging.INFO), (True, logging.DEBUG)]
)
def test_setup_logging_configures_root(clean_root_logger, verbose, level):
    """Root logger gets the requested level and one stdout handler."""
    root = clean_root_logger()
    setup_logging(verbose=verbose)

    assert root.level == level
    (handler,) = root.handlers
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stdout
    assert "%(levelname)s" in handler.formatter._fmt


def test_setup_logging_quiets_http_libraries(clean_root_logger):
    """httpx/httpcore are raised to WARNING even in verbose mode."""
    clean_root_logger()
    setup_logging(verbose=True)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_setup_logging_is_noop_when_already_configured(clean_root_logger):
    """An already-configured root logger is left untouched (basicConfig)."""
    root = clean_root_logger()
    existing = logging.NullHandler()
    root.addHandler(existing)
    root.setLevel(logging.ERROR)

    setup_logging(verbose=True)

    assert root.handlers == [existing]
    assert root.level == logging.ERROR
