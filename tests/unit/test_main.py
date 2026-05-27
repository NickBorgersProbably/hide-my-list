"""Tests for app.main entry point.

Verifies LangSmith guard, skeleton mode, and feature-flag behavior.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_main(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run app.main in a subprocess with the given environment."""
    full_env = {**os.environ, **env}
    if "LOG_FILE" not in env:
        full_env.pop("LOG_FILE", None)
    return subprocess.run(
        [sys.executable, "-m", "app.main"],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=str(__import__("pathlib").Path(__file__).parent.parent.parent),
    )


def test_skeleton_mode_prints_skeleton() -> None:
    """With ENABLE_LANGGRAPH_PATH=false, app prints 'skeleton' and exits 0."""
    result = _run_main({"ENABLE_LANGGRAPH_PATH": "false"})
    assert result.returncode == 0
    assert "skeleton" in result.stdout


def test_log_file_writes_structured_logs_and_preserves_stdout(tmp_path: Path) -> None:
    """LOG_FILE writes JSON logs to disk while stdout logging remains active."""
    log_file = tmp_path / "logs" / "app.log"
    result = _run_main({"ENABLE_LANGGRAPH_PATH": "false", "LOG_FILE": str(log_file)})

    assert result.returncode == 0
    assert "skeleton" in result.stdout
    assert '"event": "app.skeleton_mode"' in result.stdout

    log_lines = log_file.read_text().splitlines()
    assert len(log_lines) == 1
    payload = json.loads(log_lines[0])
    assert payload["event"] == "app.skeleton_mode"
    assert payload["level"] == "warning"


def test_log_file_redacts_private_fields_from_file_and_stdout(tmp_path: Path) -> None:
    """Private log fields are redacted before any configured sink receives them."""
    log_file = tmp_path / "logs" / "app.log"
    code = f"""
import os
import structlog

from app.main import _configure_logging

os.environ["LOG_FILE"] = {str(log_file)!r}
_configure_logging()
structlog.get_logger("test").info(
    "redaction.check",
    peer="raw-peer-token",
    recipient="raw-recipient-token",
    message="raw-message-token",
)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent.parent),
    )

    assert result.returncode == 0
    assert "raw-peer-token" not in result.stdout
    assert "raw-recipient-token" not in result.stdout
    assert "raw-message-token" not in result.stdout

    stdout_payload = json.loads(result.stdout)
    assert stdout_payload["peer"] == "<recipient>"
    assert stdout_payload["recipient"] == "<recipient>"
    assert stdout_payload["message"] == "<private>"

    log_text = log_file.read_text()
    assert "raw-peer-token" not in log_text
    assert "raw-recipient-token" not in log_text
    assert "raw-message-token" not in log_text

    file_payload = json.loads(log_text)
    assert file_payload["peer"] == "<recipient>"
    assert file_payload["recipient"] == "<recipient>"
    assert file_payload["message"] == "<private>"


def test_langsmith_guard_blocks_boot() -> None:
    """LANGSMITH_TRACING=true without ALLOW_PRIVATE_TRACE_EXPORT=true must exit 1."""
    result = _run_main({
        "LANGSMITH_TRACING": "true",
        "ALLOW_PRIVATE_TRACE_EXPORT": "false",
        "ENABLE_LANGGRAPH_PATH": "false",
    })
    assert result.returncode == 1
    assert "ALLOW_PRIVATE_TRACE_EXPORT" in result.stderr


def test_langsmith_guard_allows_boot_with_consent() -> None:
    """LANGSMITH_TRACING=true WITH ALLOW_PRIVATE_TRACE_EXPORT=true allows boot."""
    result = _run_main({
        "LANGSMITH_TRACING": "true",
        "ALLOW_PRIVATE_TRACE_EXPORT": "true",
        "ENABLE_LANGGRAPH_PATH": "false",
    })
    # With flag off, still exits 0 in skeleton mode
    assert result.returncode == 0
    assert "skeleton" in result.stdout
