#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

python3 - "$REPO_ROOT" <<'PY'
import sys
import re
import shlex
from pathlib import Path


repo_root = Path(sys.argv[1])
targets = sorted(
    {
        * (repo_root / ".github" / "workflows").glob("*.yml"),
        * (repo_root / ".github" / "workflows").glob("*.yaml"),
        * (repo_root / ".github" / "actions").rglob("*.yml"),
        * (repo_root / ".github" / "actions").rglob("*.yaml"),
    }
)

errors = []
run_pattern = re.compile(r"^(?P<prefix>\s*(?:-\s*)?)run:\s*(?P<value>.*)$")
assignment_pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
command_breaks = {
    ";",
    "&",
    "|",
    "(",
    ")",
    "{",
    "}",
    "if",
    "then",
    "elif",
    "else",
    "fi",
    "do",
    "done",
    "while",
    "until",
    "for",
    "case",
    "in",
    "esac",
}


def iter_run_blocks(lines):
    idx = 0
    line_count = len(lines)

    while idx < line_count:
        match = run_pattern.match(lines[idx])
        if not match:
            idx += 1
            continue

        value = match.group("value").strip()
        indent = len(match.group("prefix"))
        start_line = idx + 1

        if value and not value.startswith("|") and not value.startswith(">"):
            yield start_line, [value]
            idx += 1
            continue

        idx += 1
        block_lines = []
        while idx < line_count:
            raw_line = lines[idx]
            if raw_line.strip():
                current_indent = len(raw_line) - len(raw_line.lstrip(" "))
                if current_indent <= indent:
                    break
            block_lines.append(raw_line)
            idx += 1

        non_empty = [line for line in block_lines if line.strip()]
        if non_empty:
            base_indent = min(len(line) - len(line.lstrip(" ")) for line in non_empty)
            block_lines = [line[base_indent:] if line.strip() else "" for line in block_lines]

        yield start_line + 1, block_lines


def normalize_command(command):
    return " ".join(command.replace("\n", " ").split())


def shell_tokens(command):
    normalized = command.replace("\\\n", " ")
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return list(lexer)


def collect_logical_command(run_lines, start_idx):
    idx = start_idx
    logical_lines = [run_lines[idx]]

    while True:
        command = "\n".join(logical_lines)
        needs_more = logical_lines[-1].rstrip().endswith("\\")
        try:
            shell_tokens(command)
        except ValueError as exc:
            if (
                ("No closing quotation" in str(exc) or "No escaped character" in str(exc))
                and idx + 1 < len(run_lines)
            ):
                idx += 1
                logical_lines.append(run_lines[idx])
                continue
            return command, idx

        if needs_more and idx + 1 < len(run_lines):
            idx += 1
            logical_lines.append(run_lines[idx])
            continue

        return command, idx


def heredoc_specs(command):
    try:
        tokens = shell_tokens(command)
    except ValueError:
        return []

    specs = []
    idx = 0

    while idx < len(tokens):
        token = tokens[idx]
        if token in {"<<", "<<-"}:
            if idx + 1 < len(tokens):
                specs.append((tokens[idx + 1], token == "<<-"))
                idx += 2
                continue
        elif token.startswith("<<-") and token != "<<-":
            specs.append((token[3:], True))
        elif token.startswith("<<") and token != "<<":
            specs.append((token[2:], False))
        idx += 1

    return specs


def invalid_gh_api_usage(command):
    try:
        tokens = shell_tokens(command)
    except ValueError:
        return False

    idx = 0

    while idx < len(tokens):
        if tokens[idx] in command_breaks:
            idx += 1
            continue

        start = idx
        while start < len(tokens) and assignment_pattern.fullmatch(tokens[start]):
            start += 1

        if start + 1 < len(tokens) and tokens[start] == "gh" and tokens[start + 1] == "api":
            end = start + 2
            flags = set()
            while end < len(tokens) and tokens[end] not in command_breaks:
                token = tokens[end]
                if token == "--slurp" or token.startswith("--slurp="):
                    flags.add("--slurp")
                elif token == "--jq" or token.startswith("--jq="):
                    flags.add("--jq")
                elif token == "--template" or token.startswith("--template="):
                    flags.add("--template")
                end += 1

            if "--slurp" in flags and ("--jq" in flags or "--template" in flags):
                return True

            idx = end
            continue

        idx = start + 1 if start == idx else start

    return False

for path in targets:
    lines = path.read_text(encoding="utf-8").splitlines()
    for run_start, run_lines in iter_run_blocks(lines):
        idx = 0
        active_heredocs = []
        while idx < len(run_lines):
            raw_line = run_lines[idx]
            if active_heredocs:
                delimiter, allow_tabs = active_heredocs[0]
                candidate = raw_line.lstrip("\t") if allow_tabs else raw_line
                if candidate == delimiter:
                    active_heredocs.pop(0)
                idx += 1
                continue

            stripped = raw_line.lstrip()

            if not stripped or stripped.startswith("#"):
                idx += 1
                continue

            start_line = run_start + idx
            command, idx = collect_logical_command(run_lines, idx)
            if invalid_gh_api_usage(command):
                rendered = normalize_command(command)
                errors.append(
                    f"ERROR: {path.relative_to(repo_root)}:{start_line}: "
                    f"`gh api` cannot combine `--slurp` with `--jq` or `--template`: {rendered}"
                )

            active_heredocs.extend(heredoc_specs(command))
            idx += 1

print("=== Checking gh api flag compatibility ===")

if errors:
    for error in errors:
        print(error)
    print(f"FAILED: {len(errors)} invalid gh api command(s) found")
    sys.exit(1)

print("All gh api invocations use supported flag combinations")
PY
