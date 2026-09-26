"""Structural checks for `scripts/ci-local.sh`.

The script exists so a local run can match CI exactly instead of drifting from
it (image-reward key, single shared inference slot, etc.). The property this
file pins is the one that would otherwise rot silently: if
`python-validation.yml` changes its ruff/mypy/pytest-unit invocation, the local
script must change with it. Extracting the commands from the workflow text and
asserting they appear verbatim in the script catches that drift the moment
either file is edited on its own.

The e2e mode's safety behavior is pinned by running the script for real with
a fake `gh` and a fake `pytest` on an isolated PATH: the shared-slot guard
(three workflows, fail closed, `--force` override), argument forwarding, and
the exported environment. The real e2e suite never runs here.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

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
    assert os.access(_SCRIPT, os.X_OK), f"{_SCRIPT} is not executable"


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


def test_e2e_mode_runs_pytest_in_a_scrubbed_environment() -> None:
    """Image rewards must stay off locally, exactly as e2e.yml relies on.

    The behavioral tests below prove OPENAI_API_KEY never reaches pytest; this
    pins the mechanism so a refactor cannot quietly swap it for a denylist.
    """
    assert 'env -i "${run_env[@]}" pytest' in _script_text()


# --- Behavioral e2e-mode tests ------------------------------------------------

# Utilities the script itself needs. Linked into an isolated bin dir so the
# test controls whether `gh` exists at all (the real one may live in /usr/bin).
_SYSTEM_TOOLS = ("bash", "dirname", "mktemp", "mkdir", "cat", "env")

_SLOT_WORKFLOWS = ("e2e.yml", "nightly-evals.yml", "model-swap.yml")

# Fake gh: `gh run list --workflow=<wf> ...` prints FAKE_GH_BUSY_COUNT for the
# workflow named in FAKE_GH_BUSY_WORKFLOW and 0 otherwise, or exits
# FAKE_GH_EXIT when that is set, or prints FAKE_GH_OUTPUT verbatim.
_FAKE_GH = """#!{bash}
echo "$*" >> "$FAKE_LOG_DIR/gh_calls"
if [ -n "${{FAKE_GH_EXIT:-}}" ]; then
  echo "gh: not logged in" >&2
  exit "$FAKE_GH_EXIT"
fi
if [ -n "${{FAKE_GH_OUTPUT:-}}" ]; then
  echo "$FAKE_GH_OUTPUT"
  exit 0
fi
for arg in "$@"; do
  if [ "$arg" = "--workflow=${{FAKE_GH_BUSY_WORKFLOW:-none}}" ]; then
    echo "${{FAKE_GH_BUSY_COUNT:-1}}"
    exit 0
  fi
done
echo 0
"""

# Fake pytest: its log dir is baked in at write time, since e2e mode runs it
# under `env -i` and FAKE_LOG_DIR does not survive the scrub. Records its argv (one per line) and its environment for the
# latest call, and appends "<first arg>|<DATABASE_URL or <unset>>" per call.
# Exits 3 on the db-mode call when FAKE_PYTEST_STOP_AT_DB is set, so an `all`
# run stops before the docs step (which calls a real repo script).
_FAKE_PYTEST = """#!{bash}
printf '%s\\n' "$@" > "{log_dir}/pytest_args"
{env} > "{log_dir}/pytest_env"
echo "$1|${{DATABASE_URL-<unset>}}" >> "{log_dir}/pytest_calls"
if [ -n "${{FAKE_PYTEST_STOP_AT_DB:-}}" ] && [ "$1" = "tests/integration/" ]; then
  exit 3
fi
exit 0
"""

_FAKE_NOOP = """#!{bash}
exit 0
"""


def _write_exe(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_env(tmp_path: Path) -> dict[str, str]:
    bash = shutil.which("bash")
    env_bin = shutil.which("env")
    assert bash and env_bin
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in _SYSTEM_TOOLS:
        real = shutil.which(tool)
        assert real, f"{tool} not found on the test host"
        (bin_dir / tool).symlink_to(real)
    _write_exe(bin_dir / "gh", _FAKE_GH.format(bash=bash))
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    _write_exe(bin_dir / "pytest", _FAKE_PYTEST.format(bash=bash, env=env_bin, log_dir=log_dir))
    for tool in ("ruff", "mypy"):
        _write_exe(bin_dir / tool, _FAKE_NOOP.format(bash=bash))
    return {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "FAKE_LOG_DIR": str(log_dir),
        "REWARD_ARTIFACTS_DIR": str(tmp_path / "rewards"),
    }


def _run_mode(env: dict[str, str], mode: str, *args: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash
    return subprocess.run(  # noqa: S603
        [bash, str(_SCRIPT), mode, *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _run_e2e(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return _run_mode(env, "e2e", *args)


def _pytest_ran(env: dict[str, str]) -> bool:
    return (Path(env["FAKE_LOG_DIR"]) / "pytest_args").exists()


def _pytest_env(env: dict[str, str]) -> dict[str, str]:
    lines = (Path(env["FAKE_LOG_DIR"]) / "pytest_env").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def _pytest_calls(env: dict[str, str]) -> list[str]:
    path = Path(env["FAKE_LOG_DIR"]) / "pytest_calls"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.mark.parametrize("workflow", _SLOT_WORKFLOWS)
def test_e2e_refuses_when_any_slot_workflow_is_busy(
    fake_env: dict[str, str], workflow: str
) -> None:
    env = {**fake_env, "FAKE_GH_BUSY_WORKFLOW": workflow}
    result = _run_e2e(env)
    assert result.returncode != 0
    assert workflow in result.stderr
    assert not _pytest_ran(env), "pytest must not start while the shared slot is busy"


def test_e2e_checks_all_three_slot_workflows(fake_env: dict[str, str]) -> None:
    result = _run_e2e(fake_env)
    assert result.returncode == 0, result.stderr
    calls = (Path(fake_env["FAKE_LOG_DIR"]) / "gh_calls").read_text(encoding="utf-8")
    for workflow in _SLOT_WORKFLOWS:
        assert f"--workflow={workflow}" in calls


def test_e2e_force_bypasses_busy_slot(fake_env: dict[str, str]) -> None:
    env = {**fake_env, "FAKE_GH_BUSY_WORKFLOW": "e2e.yml"}
    result = _run_e2e(env, "--force")
    assert result.returncode == 0, result.stderr
    assert _pytest_ran(env)


def test_e2e_fails_closed_when_gh_is_absent(fake_env: dict[str, str]) -> None:
    (Path(fake_env["PATH"]) / "gh").unlink()
    result = _run_e2e(fake_env)
    assert result.returncode != 0
    assert "gh" in result.stderr
    assert not _pytest_ran(fake_env)


def test_e2e_fails_closed_when_gh_errors(fake_env: dict[str, str]) -> None:
    env = {**fake_env, "FAKE_GH_EXIT": "4"}
    result = _run_e2e(env)
    assert result.returncode != 0
    assert not _pytest_ran(env)


def test_e2e_fails_closed_on_unparseable_gh_output(fake_env: dict[str, str]) -> None:
    env = {**fake_env, "FAKE_GH_OUTPUT": "not-a-number"}
    result = _run_e2e(env)
    assert result.returncode != 0
    assert not _pytest_ran(env)


def test_e2e_force_bypasses_missing_gh(fake_env: dict[str, str]) -> None:
    (Path(fake_env["PATH"]) / "gh").unlink()
    result = _run_e2e(fake_env, "--force")
    assert result.returncode == 0, result.stderr
    assert _pytest_ran(fake_env)


def test_e2e_forwards_files_and_exports_ci_env(fake_env: dict[str, str]) -> None:
    env = {
        **fake_env,
        "OPENAI_API_KEY": "sk-placeholder",
        "E2E_TURN_TIMEOUT_SECONDS": "5",
        "LANGSMITH_TRACING": "true",
        "LLM_MAX_RETRIES": "9",
        "LANG": "C.UTF-8",
    }
    result = _run_e2e(env, "tests/e2e/test_a.py", "tests/e2e/test_b.py")
    assert result.returncode == 0, result.stderr

    args = (Path(env["FAKE_LOG_DIR"]) / "pytest_args").read_text(encoding="utf-8").splitlines()
    assert args == ["tests/e2e/test_a.py", "tests/e2e/test_b.py", "-q", "-rs"]

    recorded = _pytest_env(env)
    for scrubbed in (
        "OPENAI_API_KEY",
        "E2E_TURN_TIMEOUT_SECONDS",
        "LANGSMITH_TRACING",
        "LLM_MAX_RETRIES",
        "FAKE_LOG_DIR",
    ):
        assert scrubbed not in recorded, f"{scrubbed} leaked into the e2e environment"
    assert recorded["ENABLE_E2E_CONVERSATIONS"] == "true"
    assert recorded["E2E_DEBUG_TURNS"] == "true"
    assert recorded["E2E_MAX_LLM_CALLS"] == "120"
    assert recorded["LANG"] == "C.UTF-8"
    assert recorded["PATH"] == env["PATH"]


def test_e2e_defaults_to_whole_suite_and_honors_overrides(fake_env: dict[str, str]) -> None:
    env = {**fake_env, "E2E_MAX_LLM_CALLS": "7", "ENABLE_E2E_CONVERSATIONS": "false"}
    result = _run_e2e(env)
    assert result.returncode == 0, result.stderr

    args = (Path(env["FAKE_LOG_DIR"]) / "pytest_args").read_text(encoding="utf-8").splitlines()
    assert args == ["tests/e2e/", "-q", "-rs"]

    recorded = _pytest_env(env)
    assert recorded["E2E_MAX_LLM_CALLS"] == "7"
    assert recorded["ENABLE_E2E_CONVERSATIONS"] == "true"


def test_e2e_passes_only_the_allowlisted_variables(fake_env: dict[str, str]) -> None:
    """The scrubbed env holds the passthrough set plus the e2e variables, nothing else."""
    env = {**fake_env, "USER_TZ": "UTC", "SOME_UNRELATED_VAR": "x"}
    result = _run_e2e(env)
    assert result.returncode == 0, result.stderr

    allowed = {
        "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV", "PYTHONPATH", "TERM",
        "ENABLE_E2E_CONVERSATIONS", "DATABASE_URL", "LLM_PROXY_BASE_URL",
        "LLM_PROXY_API_KEY", "E2E_MAX_LLM_CALLS", "E2E_DEBUG_TURNS",
        "AUTHORIZED_PEERS", "SIGNAL_ACCOUNT", "REWARD_ARTIFACTS_DIR",
    }  # fmt: skip
    # bash adds PWD/SHLVL/_ to the fake pytest's own environment when it starts.
    shell_added = {"PWD", "SHLVL", "_", "OLDPWD"}
    assert set(_pytest_env(env)) - shell_added <= allowed


@pytest.mark.parametrize("mode", ["unit", "db", "docs", "all"])
def test_force_is_rejected_outside_e2e(fake_env: dict[str, str], mode: str) -> None:
    result = _run_mode(fake_env, mode, "--force")
    assert result.returncode == 2
    assert "--force" in result.stderr
    assert not _pytest_ran(fake_env)


def test_all_mode_keeps_database_url_for_db_step(fake_env: dict[str, str]) -> None:
    """The unit step unsets DATABASE_URL; that must not leak into the db step."""
    custom = "postgresql://hml:hml@db.invalid:6543/custom"
    env = {**fake_env, "DATABASE_URL": custom, "FAKE_PYTEST_STOP_AT_DB": "1"}
    result = _run_mode(env, "all")
    # 3 is the fake pytest's exit on the db step, so docs never runs here.
    assert result.returncode == 3, result.stderr
    assert _pytest_calls(env) == ["tests/unit/|<unset>", f"tests/integration/|{custom}"]


@pytest.mark.parametrize("mode", ["db", "e2e"])
def test_database_password_is_never_printed(fake_env: dict[str, str], mode: str) -> None:
    password = "pw-placeholder-7d1c"
    env = {
        **fake_env,
        "DATABASE_URL": f"postgresql://hml:{password}@db.invalid:6543/hml?sslmode=require",
    }
    result = _run_mode(env, mode)
    assert result.returncode == 0, result.stderr
    assert password not in result.stdout
    assert password not in result.stderr
    assert "db.invalid:6543/hml" in result.stdout


def test_e2e_workflow_uploads_a_run_unique_pytest_log() -> None:
    """The homelab runner is persistent: a fixed log path could upload a stale run's output."""
    text = (_REPO_ROOT / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    run_unique = 'log_file="$RUNNER_TEMP/e2e-pytest-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.log"'
    assert run_unique in text
    assert 'rm -f "$log_file"' in text
    assert text.index('rm -f "$log_file"') < text.index('| tee "$log_file"')
    assert "set -o pipefail" in text
    upload = text[text.index("- name: Upload pytest output") :]
    upload = upload[: upload.index("- name:", 1)]
    assert "if: always()" in upload
    assert "name: e2e-pytest-log" in upload
    assert "e2e-pytest-${{ github.run_id }}-${{ github.run_attempt }}.log" in upload
    assert "if-no-files-found: ignore" in upload
