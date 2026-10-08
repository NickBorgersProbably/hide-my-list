"""The devcontainer receives Claude's access token, never its refresh token.

Claude's OAuth refresh token rotates on use. A container holding a copy of the
host's `~/.claude/.credentials.json` can refresh it and invalidate the host's
login, along with every other session on the machine. So the host-side init
script writes only the short-lived access token, and the container exports it
as `CLAUDE_CODE_OAUTH_TOKEN`.

These tests run the real scripts against fake credentials in a throwaway HOME.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

_DEVCONTAINER_DIR = Path(__file__).parent.parent.parent / ".devcontainer"
_FAKE_ACCESS = "fake-access-token-AAAA"
_FAKE_REFRESH = "fake-refresh-token-ZZZZ"


def _write_creds(home: Path, *, expires_in_s: int) -> None:
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / ".credentials.json").write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": _FAKE_ACCESS,
                "refreshToken": _FAKE_REFRESH,
                "expiresAt": int((time.time() + expires_in_s) * 1000),
            }
        }),
        encoding="utf-8",
    )


def _run_init(tmp_path: Path, home: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    # Copy the scripts so the run never writes into the real .devcontainer/.
    work = tmp_path / "devcontainer"
    shutil.copytree(_DEVCONTAINER_DIR, work, ignore=shutil.ignore_patterns(".*"))
    result = subprocess.run(
        ["bash", str(work / "init-host-credentials.sh")],
        env={"PATH": os.environ["PATH"], "HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
    )
    return result, work


def test_init_writes_only_the_access_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_creds(home, expires_in_s=3600)
    _, work = _run_init(tmp_path, home)

    token_file = work / ".claude-oauth-token"
    assert token_file.read_text(encoding="utf-8").strip() == _FAKE_ACCESS
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    for path in work.iterdir():
        if path.is_file():
            assert _FAKE_REFRESH not in path.read_text(encoding="utf-8", errors="ignore")
    assert not (work / ".claude-credentials").exists()


def test_init_removes_a_credentials_copy_left_by_older_revisions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_creds(home, expires_in_s=3600)
    work = tmp_path / "devcontainer"
    shutil.copytree(_DEVCONTAINER_DIR, work, ignore=shutil.ignore_patterns(".*"))
    (work / ".claude-credentials").write_text(_FAKE_REFRESH, encoding="utf-8")
    subprocess.run(
        ["bash", str(work / "init-host-credentials.sh")],
        env={"PATH": os.environ["PATH"], "HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert not (work / ".claude-credentials").exists()


def test_init_warns_and_writes_nothing_when_expired(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_creds(home, expires_in_s=-60)
    result, work = _run_init(tmp_path, home)
    assert "expired" in result.stderr
    assert (work / ".claude-oauth-token").read_text(encoding="utf-8") == ""
    assert _FAKE_REFRESH not in result.stdout + result.stderr


def test_init_continues_without_a_host_login(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result, work = _run_init(tmp_path, home)
    assert "No Claude login" in result.stderr
    assert (work / ".claude-oauth-token").read_text(encoding="utf-8") == ""


def test_wiring_is_idempotent_and_exports_the_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text(_FAKE_ACCESS + "\n", encoding="utf-8")
    for _ in range(2):
        subprocess.run(
            ["bash", str(_DEVCONTAINER_DIR / "wire-claude-token.sh"), str(token_file)],
            env={"PATH": os.environ["PATH"], "HOME": str(home)},
            capture_output=True,
            text=True,
            check=True,
        )
    for rc in (".bashrc", ".profile", ".zshrc"):
        assert (home / rc).read_text(encoding="utf-8").count("claude-oauth-token-export") == 1

    # Login (.profile) and interactive (.bashrc) shells both see the token.
    for rc in (".profile", ".bashrc"):
        out = subprocess.run(
            ["bash", "-c", f'. "{home / rc}"; printf %s "$CLAUDE_CODE_OAUTH_TOKEN"'],
            env={"PATH": os.environ["PATH"], "HOME": str(home)},
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout == _FAKE_ACCESS


def test_wiring_flags_a_credentials_file_with_a_refresh_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_creds(home, expires_in_s=3600)
    result = subprocess.run(
        ["bash", str(_DEVCONTAINER_DIR / "wire-claude-token.sh"), str(tmp_path / "t")],
        env={"PATH": os.environ["PATH"], "HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "holds a refresh token" in result.stderr
    assert _FAKE_REFRESH not in result.stdout + result.stderr


def test_post_create_never_installs_a_credentials_file() -> None:
    text = (_DEVCONTAINER_DIR / "post-create.sh").read_text(encoding="utf-8")
    assert "wire-claude-token.sh" in text
    assert 'cp "$CLAUDE_CRED_FILE"' not in text
    assert ".claude/.credentials.json" not in text.replace("~/.claude/.credentials.json", "")


def test_sync_script_never_links_the_credentials_file() -> None:
    text = (_DEVCONTAINER_DIR / "sync-claude-config.sh").read_text(encoding="utf-8")
    items = [line for line in text.splitlines() if line.startswith(("LINK_ITEMS=", "COPY_ITEMS="))]
    assert items and not any("credentials" in line for line in items)
