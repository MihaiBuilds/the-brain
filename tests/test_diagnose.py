"""Tests for ``src/diagnose.py``.

Locks the bundle contract:

- The zip is named ``brain-diagnostic-YYYY-MM-DD-HHMMSS.zip``.
- A ``MODE.txt`` file documents whether Docker was detected.
- A ``REDACTED_FIELDS.txt`` file lists every env var the bundle
  intentionally omits.
- ``DB_PASSWORD`` (and friends) never appear as a value anywhere in
  the bundle, even when set in the environment.
- Allow-listed env vars do appear with their actual values.
- OS info and Brain version land in their own files.
"""

import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.diagnose import build_bundle


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 6, 15, 14, 30, 45, tzinfo=UTC)


def _bundle_files(zip_path: Path) -> dict[str, str]:
    with zipfile.ZipFile(zip_path) as zf:
        return {name: zf.read(name).decode("utf-8") for name in zf.namelist()}


def test_bundle_filename_matches_locked_pattern(tmp_path, fixed_now):
    zip_path = build_bundle(target_dir=tmp_path, now=fixed_now)
    assert zip_path.parent == tmp_path
    assert zip_path.name == "brain-diagnostic-2026-06-15-143045.zip"
    # The on-disk file actually exists.
    assert zip_path.exists()


def test_bundle_contains_mode_marker(tmp_path, fixed_now):
    zip_path = build_bundle(target_dir=tmp_path, now=fixed_now)
    files = _bundle_files(zip_path)
    assert "MODE.txt" in files
    # MODE is either docker or no-docker depending on the runner; both
    # are valid — we only lock that the value uses the locked vocabulary.
    assert files["MODE.txt"].startswith("MODE: ")
    assert files["MODE.txt"].strip().endswith(("docker", "no-docker"))


def test_bundle_contains_redacted_fields_doc(tmp_path, fixed_now):
    zip_path = build_bundle(target_dir=tmp_path, now=fixed_now)
    files = _bundle_files(zip_path)
    assert "REDACTED_FIELDS.txt" in files
    doc = files["REDACTED_FIELDS.txt"]
    # Every presence-only env var must be named in the explainer so the
    # bug-reporter knows what was filtered.
    for name in ("DB_PASSWORD", "LLM_API_KEY", "MEMORY_VAULT_TOKEN", "THE_BRAIN_API_TOKEN"):
        assert name in doc


def test_db_password_value_is_never_written_to_bundle(tmp_path, fixed_now, monkeypatch):
    secret = "super-secret-db-password-do-not-leak"
    monkeypatch.setenv("DB_PASSWORD", secret)
    # Make sure the rest of the allow-listed env is also present so the
    # environment.txt file actually gets built — exercises the full path.
    monkeypatch.setenv("DB_HOST", "localhost")

    zip_path = build_bundle(target_dir=tmp_path, now=fixed_now)
    files = _bundle_files(zip_path)

    # The bundle's environment.txt records DB_HOST's value but NOT the
    # password's value. Scan every file in the bundle for the secret.
    for name, content in files.items():
        assert secret not in content, f"DB_PASSWORD leaked into {name}"


def test_allowlisted_env_vars_appear_with_their_values(tmp_path, fixed_now, monkeypatch):
    monkeypatch.setenv("DB_HOST", "test-host")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LLM_MODEL", "mistralai/ministral-3-3b")

    zip_path = build_bundle(target_dir=tmp_path, now=fixed_now)
    env_file = _bundle_files(zip_path)["environment.txt"]
    assert "DB_HOST=test-host" in env_file
    assert "LOG_LEVEL=DEBUG" in env_file
    assert "LLM_MODEL=mistralai/ministral-3-3b" in env_file


def test_presence_only_env_vars_recorded_without_value(tmp_path, fixed_now, monkeypatch):
    monkeypatch.setenv("THE_BRAIN_API_TOKEN", "secret-token-xyz")

    zip_path = build_bundle(target_dir=tmp_path, now=fixed_now)
    env_file = _bundle_files(zip_path)["environment.txt"]

    # The secret value is not anywhere in the env file.
    assert "secret-token-xyz" not in env_file
    # But the name IS, with the redaction marker.
    assert "THE_BRAIN_API_TOKEN" in env_file
    assert "redacted" in env_file.lower()


def test_bundle_includes_os_info_and_brain_version(tmp_path, fixed_now):
    zip_path = build_bundle(target_dir=tmp_path, now=fixed_now)
    files = _bundle_files(zip_path)
    assert "os_info.txt" in files
    assert "python=" in files["os_info.txt"]
    assert "brain_version.txt" in files
    # The version is either a semver-ish digit string or the explicit
    # "package not installed" marker.
    version_text = files["brain_version.txt"].strip()
    assert version_text  # non-empty
    assert re.match(r"^\d+\.\d+\.\d+", version_text) or "not installed" in version_text


def test_no_docker_mode_skips_docker_files_with_marker(tmp_path, fixed_now, monkeypatch):
    # Force the no-docker branch by hiding ``docker`` from the resolved PATH.
    monkeypatch.setenv("PATH", "/nonexistent")
    zip_path = build_bundle(target_dir=tmp_path, now=fixed_now)
    files = _bundle_files(zip_path)

    assert files["MODE.txt"].strip() == "MODE: no-docker"
    assert "docker_skipped.txt" in files
    assert "docker_compose_ps.txt" not in files
    assert "docker_logs_brain.txt" not in files
    assert "docker_logs_db.txt" not in files
