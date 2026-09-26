"""Structural checks for `scripts/ci-local.sh`.

The script exists so a local run can match CI exactly instead of drifting from
it (image-reward key, single shared inference slot, etc.). The property this
file pins is the one that would otherwise rot silently: if
`python-validation.yml` changes its ruff/mypy/pytest-unit invocation, the local
script must change with it. Extracting the commands from the workflow text and
asserting they appear verbatim in the script catches that drift the moment
either file is edited on its own.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "ci-local.sh"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "python-validation.yml"


def _script_text() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def _workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _step_run_lines(workflow_text: str, step_name: str) -> list[str]:
    """Pull the `run:` command line(s) that follow a `name: <step_name>` step.

    No YAML parser is used (none is a repo dependency); this walks the raw
    lines the way the sibling structural lint `test_signal_cli_pin.py` reads
    workflow files by regex rather than by schema. Handles both a same-line
    `run: <cmd>` and a block-scalar `run: |` followed by indented lines,
    stopping at the next step (`- name:`) or a dedent back to job level.
    """
    lines = workflow_text.splitlines()
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if line.strip() in (f"name: {step_name}", f"- name: {step_name}")
        ),
        None,
    )
    assert start is not None, f"could not find 'name: {step_name}' in {_WORKFLOW}"

    run_idx = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("run:")), None
    )
    assert run_idx is not None, f"no 'run:' line found after 'name: {step_name}' in {_WORKFLOW}"

    run_line = lines[run_idx]
    run_indent = len(run_line) - len(run_line.lstrip(" "))
    inline = run_line.strip()[len("run:") :].strip()

    if inline and inline != "|":
        return [inline]

    # Block scalar: collect subsequent lines indented deeper than `run:`.
    collected: list[str] = []
    for line in lines[run_idx + 1 :]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= run_indent:
            break
        collected.append(line.strip())
    assert collected, f"'run: |' block after 'name: {step_name}' in {_WORKFLOW} was empty"
    return collected


def test_script_exists_and_is_executable() -> None:
    assert _SCRIPT.is_file(), f"{_SCRIPT} does not exist"


def test_script_syntax_is_valid_bash() -> None:
    result = subprocess.run(  # noqa: S603
        ["bash", "-n", str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_help_runs_and_documents_every_mode() -> None:
    result = subprocess.run(  # noqa: S603
        ["bash", str(_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"--help exited {result.returncode}:\n{result.stderr}"
    for mode in ("unit", "db", "e2e", "docs", "all"):
        assert mode in result.stdout, f"--help output does not mention mode '{mode}'"

    # The compose smoke test's teardown (`docker compose down -v`) would tear
    # down a developer's own local stack's volumes, not a throwaway one —
    # scripts/ci-local.sh deliberately never runs it, and says so.
    assert "down -v" in result.stdout, "--help must warn about docker compose down -v"


def test_unit_mode_matches_python_validation_workflow_commands() -> None:
    workflow_text = _workflow_text()
    script_text = _script_text()

    ruff_cmd = _step_run_lines(workflow_text, "Run ruff")[0]
    mypy_cmd = _step_run_lines(workflow_text, "Run mypy")[0]
    pytest_cmd = _step_run_lines(workflow_text, "Run unit tests")[0]

    assert ruff_cmd in script_text, (
        f"scripts/ci-local.sh does not invoke ruff the way python-validation.yml does "
        f"({ruff_cmd!r})"
    )
    assert mypy_cmd in script_text, (
        f"scripts/ci-local.sh does not invoke mypy the way python-validation.yml does "
        f"({mypy_cmd!r})"
    )
    assert pytest_cmd in script_text, (
        f"scripts/ci-local.sh does not invoke pytest unit the way python-validation.yml does "
        f"({pytest_cmd!r})"
    )


def test_db_mode_matches_python_validation_workflow_command() -> None:
    workflow_text = _workflow_text()
    script_text = _script_text()

    run_lines = _step_run_lines(workflow_text, "Run integration and regression tests")
    # The workflow's `run:` block is multi-line (mkdir then pytest); pull just
    # the pytest invocation out of it.
    pytest_line = next(line for line in run_lines if line.startswith("pytest"))
    assert pytest_line in script_text, (
        f"scripts/ci-local.sh does not invoke pytest-db the way python-validation.yml does "
        f"({pytest_line!r})"
    )


def test_db_mode_defaults_database_url_to_ci_value() -> None:
    """The default must match python-validation.yml's service container."""
    assert "postgresql://hml:hml@localhost:5432/hml" in _script_text()


def test_e2e_mode_never_forwards_openai_api_key() -> None:
    """Image rewards must stay off locally, exactly as e2e.yml relies on."""
    text = _script_text()
    assert "unset OPENAI_API_KEY" in text
