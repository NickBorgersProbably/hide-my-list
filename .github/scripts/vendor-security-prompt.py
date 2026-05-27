#!/usr/bin/env python3
"""Refresh the vendored Anthropic security-breadth prompt body.

Downloads `claudecode/prompts.py` from
`anthropics/claude-code-security-review` at the SHA recorded in
`.github/ci/vendored-prompts.env` (or at a SHA passed via $TARGET_SHA),
renders the `get_security_audit_prompt` template with sentinel
substitutions, and replaces the fenced VENDORED PROMPT block inside
`.github/scripts/review/prompts/security-breadth.md`.

This is invoked by the weekly `update-ai-clis` workflow when the
upstream commit-walk for the prompt file detects a SHA newer than
`CCSR_PINNED_SHA`. The workflow then opens a single chore PR bundling
this refresh with any CLI version bumps.

The script is intentionally fail-loud: if upstream renames the
function, moves the file, or changes the signature, the script raises
and the workflow exits non-zero without writing anything. The current
pinned SHA stays in place until a human resolves the breakage.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".github" / "ci" / "vendored-prompts.env"
PROMPT_FILE = REPO_ROOT / ".github" / "scripts" / "review" / "prompts" / "security-breadth.md"

BEGIN_MARKER = "<!-- BEGIN VENDORED PROMPT -->"
END_MARKER = "<!-- END VENDORED PROMPT -->"


# Sentinels that the rendered template will surface in place of the
# upstream f-string substitutions. Our prompt instructs the reviewer
# agent to decode $PR_TITLE_B64 / $PR_BODY_B64 at runtime and to use
# the diff via `git diff "${REVIEW_BASE_SHA}...HEAD"`, so the rendered
# template's per-PR fields are not load-bearing for the LLM — they
# read as placeholders the agent skips past.
SENTINEL_PR_DATA = {
    "number": "${PR_NUMBER}",
    "title": "<see decoded $PR_TITLE_B64 in surrounding prompt>",
    "user": "<author>",
    "head": {"repo": {"full_name": "${REPO}"}},
    "changed_files": "<see git diff in surrounding prompt>",
    "additions": "<N>",
    "deletions": "<N>",
}


def load_env(env_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def fetch_upstream(repo: str, sha: str, path: str) -> str:
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"
    print(f"vendor-security-prompt: fetching {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"upstream fetch failed: HTTP {resp.status}")
        return resp.read().decode("utf-8")


def render_prompt(prompts_py_src: str) -> str:
    """Import upstream prompts.py from source text and call its template fn."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "prompts.py"
        tmp.write_text(prompts_py_src)
        spec = importlib.util.spec_from_file_location("ccsr_prompts", tmp)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load spec for upstream prompts.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, "get_security_audit_prompt", None)
        if fn is None:
            raise RuntimeError(
                "upstream prompts.py no longer defines get_security_audit_prompt — "
                "the vendor script needs a manual update"
            )
        return fn(SENTINEL_PR_DATA, pr_diff=None, include_diff=False)


def replace_block(md_text: str, rendered: str) -> str:
    pattern = re.compile(
        re.escape(BEGIN_MARKER) + r"(.*?)" + re.escape(END_MARKER),
        flags=re.DOTALL,
    )
    if not pattern.search(md_text):
        raise RuntimeError(
            f"could not find {BEGIN_MARKER} / {END_MARKER} block in {PROMPT_FILE} — "
            "the prompt file's structure has drifted"
        )
    replacement = f"{BEGIN_MARKER}\n{rendered.strip()}\n{END_MARKER}"
    return pattern.sub(replacement, md_text, count=1)


def write_env_pin(env_path: Path, env: dict[str, str], new_sha: str) -> None:
    lines = env_path.read_text().splitlines()
    updated: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("CCSR_PINNED_SHA="):
            updated.append(f"CCSR_PINNED_SHA={new_sha}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(f"CCSR_PINNED_SHA={new_sha}")
    env_path.write_text("\n".join(updated) + "\n")


def main() -> int:
    env = load_env(ENV_FILE)
    target_sha = os.environ.get("TARGET_SHA") or env.get("CCSR_PINNED_SHA")
    if not target_sha:
        print(
            "vendor-security-prompt: no SHA available (set TARGET_SHA or pin "
            "CCSR_PINNED_SHA in .github/ci/vendored-prompts.env)",
            file=sys.stderr,
        )
        return 2

    src = fetch_upstream(env["CCSR_REPO"], target_sha, env["CCSR_PROMPT_PATH"])
    rendered = render_prompt(src)
    md = PROMPT_FILE.read_text()
    updated_md = replace_block(md, rendered)
    if updated_md != md:
        PROMPT_FILE.write_text(updated_md)
        print(f"vendor-security-prompt: rewrote vendored block in {PROMPT_FILE.name}", file=sys.stderr)
    else:
        print("vendor-security-prompt: vendored block unchanged", file=sys.stderr)

    if os.environ.get("TARGET_SHA") and target_sha != env.get("CCSR_PINNED_SHA"):
        write_env_pin(ENV_FILE, env, target_sha)
        print(f"vendor-security-prompt: updated CCSR_PINNED_SHA to {target_sha}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
