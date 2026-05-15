"""Tests for app.main entry point.

Verifies LangSmith guard, skeleton mode, and feature-flag behavior.
"""
from __future__ import annotations

import subprocess
import sys


def _run_main(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run app.main in a subprocess with the given environment."""
    import os
    full_env = {**os.environ, **env}
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


def test_default_is_skeleton_mode() -> None:
    """Without ENABLE_LANGGRAPH_PATH set, default is false (skeleton mode)."""
    import os
    env = {k: v for k, v in os.environ.items() if k != "ENABLE_LANGGRAPH_PATH"}
    result = subprocess.run(
        [sys.executable, "-m", "app.main"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(__import__("pathlib").Path(__file__).parent.parent.parent),
    )
    assert result.returncode == 0
    assert "skeleton" in result.stdout


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
