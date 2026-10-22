"""Structural lint: CI steps must work on the isolated homelab runners.

The homelab runners are moving to one rootless Docker daemon per repo,
with a fresh runner container per job (official actions-runner image).
That image has no sudo and no pip. A `sudo` step there exits 127, and
`python3 -m pip` fails. These tests stop either from coming back in the
paths that run on `self-hosted`.

They also pin the workflow_run fix in codex-diagnose-workflow-failure.yml.
A fork PR chooses its branch name, and git allows `$(...)` in ref names.
So the branch name must never be interpolated into a `run:` script, and
envsubst must not paste it into the agent's shell command.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_ACTIONS = _REPO_ROOT / ".github" / "actions"
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_RECLAIM = _REPO_ROOT / "scripts" / "reclaim-ownership.sh"

# Composite actions that self-hosted jobs call.
_SELF_HOSTED_ACTIONS = ("review-codex-run", "review-codex-resume")


def _code_lines(path: Path) -> list[str]:
    """Lines with YAML comments dropped, so prose about sudo is allowed."""
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]


def test_self_hosted_actions_do_not_use_sudo() -> None:
    for name in _SELF_HOSTED_ACTIONS:
        action = _ACTIONS / name / "action.yml"
        offenders = [line for line in _code_lines(action) if re.search(r"\bsudo\b", line)]
        assert not offenders, (
            f"{action.relative_to(_REPO_ROOT)} runs sudo, which exits 127 on the "
            f"isolated runners. Use scripts/reclaim-ownership.sh instead: {offenders}"
        )


def test_self_hosted_actions_reclaim_through_the_script() -> None:
    for name in _SELF_HOSTED_ACTIONS:
        text = (_ACTIONS / name / "action.yml").read_text(encoding="utf-8")
        assert "scripts/reclaim-ownership.sh" in text, (
            f"{name}: the ownership reset must call scripts/reclaim-ownership.sh, "
            "so the fixer's host-side ruff and git commit can write the files "
            "the container left behind."
        )


def test_reclaim_script_is_executable_and_chowns_in_a_container() -> None:
    assert _RECLAIM.exists(), "scripts/reclaim-ownership.sh is missing"
    assert _RECLAIM.stat().st_mode & 0o111, "scripts/reclaim-ownership.sh must be executable"
    code = "\n".join(
        line for line in _RECLAIM.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert not re.search(r"\bsudo\b", code)
    assert "--user 0:0" in code and "--entrypoint chown" in code
    assert "--network none" in code


def test_fixer_sets_up_python_before_installing_hook_tooling() -> None:
    text = (_WORKFLOWS / "review-fixer.yml").read_text(encoding="utf-8")
    setup_at = text.find("uses: actions/setup-python@")
    install_at = text.find("- name: Install pre-commit hook tooling")
    assert setup_at != -1, (
        "review-fixer.yml must run actions/setup-python before pip-installing "
        "the hook tooling: the isolated runners' python3 has no pip."
    )
    assert install_at != -1
    assert setup_at < install_at


def test_diagnose_never_interpolates_event_values_into_scripts() -> None:
    text = (_WORKFLOWS / "codex-diagnose-workflow-failure.yml").read_text(encoding="utf-8")
    in_run = False
    run_indent = 0
    offenders: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if in_run and stripped and indent <= run_indent:
            in_run = False
        if stripped.startswith("run:") or stripped.startswith("- run:"):
            in_run = True
            run_indent = indent
            continue
        if in_run and "${{" in line and "github.event" in line:
            offenders.append(line.strip())
    assert not offenders, (
        "Pass github.event values through env:, not ${{ }} inside run:. "
        f"Offending lines: {offenders}"
    )


def test_diagnose_prompt_keeps_head_branch_unexpanded() -> None:
    text = (_WORKFLOWS / "codex-diagnose-workflow-failure.yml").read_text(encoding="utf-8")
    calls = re.findall(r"envsubst[^<\n]*<\s*\.github/ci/prompts/diagnose-workflow-failure\.md", text)
    assert calls, "expected an envsubst call for the diagnose prompt"
    for call in calls:
        assert "HEAD_SHA" in call, (
            "envsubst must take an explicit variable list, so HEAD_BRANCH can be "
            f"left out: {call}"
        )
        assert "HEAD_BRANCH" not in call, (
            "HEAD_BRANCH must stay unexpanded in the diagnose prompt, so the agent "
            f"passes the env var rather than the fork-chosen text: {call}"
        )
