"""Tests for ``src/logging_config.py``.

Locks the two-renderer split (``keyvalue`` default, ``json`` opt-in via
``LOG_FORMAT``), the ``bind_run_id`` context manager's structured-field
injection, and the stdlib-via-structlog routing so existing
``logger = logging.getLogger(__name__)`` call sites Just Work.
"""

import json
import logging

import pytest

from src.logging_config import bind_run_id, configure_logging


@pytest.fixture(autouse=True)
def _restore_logging_state(monkeypatch):
    """Each test runs with a clean LOG_FORMAT env and restores the root
    logger's handlers + level after the test.

    Without this, configure_logging() permanently replaces the root
    handlers with a structlog.stdlib.ProcessorFormatter — which then
    leaks into every test that runs afterwards in the same pytest
    session. The leak surfaces as a logging traceback fragment ending
    up inside ``CliRunner().invoke(...).output`` of unrelated CLI tests.
    """
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_json_renderer_emits_valid_json_for_each_line(monkeypatch, caplog, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    configure_logging()

    logger = logging.getLogger("brain.test.json-renderer")
    logger.info("hello world")

    captured = capsys.readouterr().err or capsys.readouterr().out
    # Output goes to the root StreamHandler — which defaults to stderr.
    out = capsys.readouterr()
    line = (out.err + out.out + captured).strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["event"] == "hello world"
    assert parsed["level"] == "info"


def test_keyvalue_renderer_emits_console_format(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "keyvalue")
    configure_logging()

    logger = logging.getLogger("brain.test.kv-renderer")
    logger.info("hello console")

    out = capsys.readouterr()
    line = (out.err + out.out).strip().splitlines()[-1]
    # ConsoleRenderer output is not JSON — first sanity check is that it
    # is NOT parseable as JSON, and that the event text appears in the line.
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "hello console" in line


def test_default_renderer_is_keyvalue_when_log_format_unset(capsys):
    # No LOG_FORMAT in env → falls back to the human-readable renderer.
    configure_logging()

    logger = logging.getLogger("brain.test.default-renderer")
    logger.info("default")

    out = capsys.readouterr()
    line = (out.err + out.out).strip().splitlines()[-1]
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "default" in line


def test_bind_run_id_adds_field_to_json_log(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    configure_logging()

    logger = logging.getLogger("brain.test.bind")
    with bind_run_id("00000000-0000-0000-0000-000000000abc"):
        logger.info("inside the scope")
    logger.info("outside the scope")

    out = capsys.readouterr()
    lines = [ln for ln in (out.err + out.out).strip().splitlines() if ln]
    inside_line = next(ln for ln in lines if "inside the scope" in ln)
    outside_line = next(ln for ln in lines if "outside the scope" in ln)

    inside = json.loads(inside_line)
    outside = json.loads(outside_line)
    assert inside["run_id"] == "00000000-0000-0000-0000-000000000abc"
    assert "run_id" not in outside


def test_stdlib_logger_routes_through_structlog_formatter(monkeypatch, capsys):
    # Existing src/ modules use stdlib `logger = logging.getLogger(...)`.
    # The configure_logging() call must route those through the structlog
    # formatter so the new JSON renderer applies — otherwise the M1-M4
    # log call sites would keep emitting raw stdlib format.
    monkeypatch.setenv("LOG_FORMAT", "json")
    configure_logging()

    logger = logging.getLogger("brain.runner.fake_module")
    logger.warning("stdlib-call still routes")

    out = capsys.readouterr()
    line = (out.err + out.out).strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["event"] == "stdlib-call still routes"
    assert parsed["level"] == "warning"
