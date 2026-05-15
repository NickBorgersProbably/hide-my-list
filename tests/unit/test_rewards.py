"""Tests for app/tools/rewards.py — reward subsystem.

Coverage:
- PR-B5-T1: Sensitive-task suppression (muted emoji, no image)
- PR-B5-T2: Image gen failure falls back to emoji + real-life suggestion
- PR-B5-T3: Manifest written to Postgres; task_title never written to logs
- PR-B5-T4: CI grep — no 'task_title' literal in any committed Python source file
              (private column name, not var-name usage in production code)

Private data discipline: no real task titles in this test file.
All task_title values use placeholder strings (e.g., "Placeholder therapy task").
"""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# PR-B5-T1: Sensitive-task suppression
# ---------------------------------------------------------------------------

class TestSensitiveTaskSuppression:
    """Sensitive tasks must receive muted emoji and no image."""

    def test_is_sensitive_task_therapy(self) -> None:
        """Task title containing 'therapy' keyword classifies as sensitive."""
        from app.tools.rewards import is_sensitive_task
        assert is_sensitive_task("Placeholder therapy task") is True

    def test_is_sensitive_task_medical(self) -> None:
        """Task title containing 'doctor' classifies as sensitive."""
        from app.tools.rewards import is_sensitive_task
        assert is_sensitive_task("Placeholder doctor appointment") is True

    def test_is_sensitive_task_legal(self) -> None:
        """Task title containing 'lawyer' classifies as sensitive."""
        from app.tools.rewards import is_sensitive_task
        assert is_sensitive_task("Placeholder lawyer meeting") is True

    def test_is_sensitive_task_financial(self) -> None:
        """Task title containing 'taxes' classifies as sensitive."""
        from app.tools.rewards import is_sensitive_task
        assert is_sensitive_task("Placeholder taxes task") is True

    def test_is_sensitive_task_not_sensitive(self) -> None:
        """Neutral task title does not classify as sensitive."""
        from app.tools.rewards import is_sensitive_task
        assert is_sensitive_task("Placeholder grocery run") is False

    def test_sensitive_task_emoji_is_muted(self) -> None:
        """Sensitive task must receive muted celebration with no emoji fanfare."""
        from app.tools.rewards import get_celebration_emoji
        result = get_celebration_emoji("epic", sensitive_task=True)
        # Must be warm but not fanfare — no emoji characters
        assert result == "Done. That mattered."
        # Confirm no emoji present
        assert "🏆" not in result
        assert "🔥" not in result
        assert "💪" not in result

    def test_nonsensitive_epic_has_fanfare(self) -> None:
        """Non-sensitive epic task must have celebratory emoji.

        Checks across all epic templates — any one of them must contain
        at least one emoji from the full epic pool.
        """
        from app.tools.rewards import _EMOJI_TEMPLATES
        # Collect all emoji present in all epic templates
        all_epic_text = " ".join(_EMOJI_TEMPLATES["epic"])
        # Must contain at least one non-ASCII emoji character
        has_emoji = any(ord(c) > 127 for c in all_epic_text)
        assert has_emoji, "Epic intensity templates must contain celebratory emoji"

        # Also verify the muted path is NOT returned for non-sensitive
        from app.tools.rewards import get_celebration_emoji
        # Run multiple times to sample across pool (random.choice)
        results = {get_celebration_emoji("epic", sensitive_task=False) for _ in range(10)}
        # None of the results should be the sensitive-only muted message
        assert "Done. That mattered." not in results

    @pytest.mark.asyncio
    async def test_maybe_reward_sensitive_skips_image(self) -> None:
        """maybe_reward with a sensitive task title must not attempt image generation."""
        from app.tools import rewards as rewards_module

        # Patch generate_reward_image and write_reward_manifest
        with (
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock()) as mock_gen,
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-1>",
                task_title="Placeholder therapy appointment",  # sensitive keyword
                notion_page_id="<page-id-001>",
                streak=3,
                energy_required="Medium",
                time_estimate=30,
            )

        # Image generation must not have been called
        mock_gen.assert_not_called()
        # Result must not contain MEDIA: line
        assert "MEDIA:" not in result

    @pytest.mark.asyncio
    async def test_maybe_reward_sensitive_manifest_marks_sensitive(self) -> None:
        """maybe_reward must set sensitive_task=True on the manifest for sensitive tasks."""
        from app.tools import rewards as rewards_module

        recorded_calls: list[dict] = []

        async def fake_write_manifest(**kwargs: Any) -> uuid.UUID:
            recorded_calls.append(kwargs)
            return uuid.uuid4()

        with (
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock(return_value=None)),
            patch.object(rewards_module, "write_reward_manifest", new=fake_write_manifest),
        ):
            await rewards_module.maybe_reward(
                peer="<test-peer-2>",
                task_title="Placeholder doctor visit task",
                notion_page_id="<page-id-002>",
                streak=1,
                energy_required="High",
                time_estimate=60,
            )

        assert len(recorded_calls) == 1
        assert recorded_calls[0]["sensitive_task"] is True


# ---------------------------------------------------------------------------
# PR-B5-T2: Image gen failure falls back to emoji + real-life suggestion
# ---------------------------------------------------------------------------

class TestImageGenFallback:
    """When image generation fails, rewards must fall back gracefully."""

    @pytest.mark.asyncio
    async def test_image_gen_failure_triggers_fallback(self) -> None:
        """Image gen returning None on medium intensity must add fallback suggestion."""
        from app.tools import rewards as rewards_module

        # Ensure OPENAI_API_KEY is set so the function tries to call image gen
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock(return_value=None)),
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            patch.object(rewards_module, "compute_intensity", return_value=("medium", 40)),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-3>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-003>",
                streak=2,
                energy_required="Medium",
                time_estimate=30,
            )

        # Must contain a fallback suggestion (plain text, no MEDIA:)
        assert "MEDIA:" not in result
        lines = result.strip().split("\n")
        assert len(lines) >= 2, "Expected celebration + fallback on separate lines"

    @pytest.mark.asyncio
    async def test_image_gen_success_no_fallback(self) -> None:
        """When image gen succeeds, no fallback suggestion is appended."""
        from app.tools import rewards as rewards_module

        fake_path = "/tmp/reward_artifacts/test-image.png"
        with (
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock(return_value=fake_path)),
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            patch.object(rewards_module, "compute_intensity", return_value=("high", 70)),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-4>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-004>",
                streak=5,
                energy_required="High",
                time_estimate=60,
            )

        assert f"MEDIA:{fake_path}" in result

    @pytest.mark.asyncio
    async def test_lightest_never_attempts_image(self) -> None:
        """Lightest intensity must never attempt image generation."""
        from app.tools import rewards as rewards_module

        with (
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock()) as mock_gen,
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            patch.object(rewards_module, "compute_intensity", return_value=("lightest", 5)),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-5>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-005>",
                streak=1,
                energy_required="Low",
                time_estimate=5,
            )

        mock_gen.assert_not_called()
        assert "MEDIA:" not in result

    def test_generate_reward_image_returns_none_without_api_key(self) -> None:
        """generate_reward_image must return None immediately when OPENAI_API_KEY unset."""
        import asyncio
        from app.tools.rewards import generate_reward_image

        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            result = asyncio.get_event_loop().run_until_complete(
                generate_reward_image(
                    intensity="medium",
                    streak_count=2,
                    task_descriptions=["Placeholder description"],
                )
            )
        assert result is None

    def test_fallback_reward_pool_has_min_size(self) -> None:
        """Fallback reward pool must have at least 12 entries."""
        from app.tools.rewards import _FALLBACK_REWARDS
        assert len(_FALLBACK_REWARDS) >= 12


# ---------------------------------------------------------------------------
# PR-B5-T3: Manifest written to Postgres, task_title never to logs
# ---------------------------------------------------------------------------

class TestManifestWriting:
    """Manifest must be written to Postgres; task_title must never appear in log output."""

    @pytest.mark.asyncio
    async def test_manifest_inserts_to_postgres(self) -> None:
        """write_reward_manifest must execute an INSERT with all required columns."""
        from app.tools import rewards as rewards_module

        executed_queries: list[str] = []
        executed_params: list[tuple] = []

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(side_effect=lambda q, p=None: executed_queries.append(q) or executed_params.append(p))

        @asynccontextmanager
        async def fake_get_db_conn() -> AsyncGenerator[MagicMock, None]:
            yield mock_conn

        with patch("app.tools.db.get_db_conn", fake_get_db_conn):
            result = await rewards_module.write_reward_manifest(
                peer="<test-peer-6>",
                notion_page_id="<page-id-006>",
                task_title="Placeholder private task",
                reward_kind="emoji+image",
                intensity="high",
                streak_count=3,
                delivered_at=datetime.now(UTC),
                sensitive_task=False,
            )

        assert result is not None
        assert len(executed_queries) == 1
        query = executed_queries[0]
        assert "reward_manifests" in query
        assert "INSERT" in query.upper()

        # Verify task_title is passed as a parameter (not embedded in query string)
        params = executed_params[0]
        assert "Placeholder private task" in params

    @pytest.mark.asyncio
    async def test_task_title_never_in_log_output(self, caplog: pytest.LogCaptureFixture) -> None:
        """write_reward_manifest must not emit task_title to any log record."""
        from app.tools import rewards as rewards_module

        private_title = "Placeholder-private-do-not-log-5x9z"

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock()

        @asynccontextmanager
        async def fake_get_db_conn() -> AsyncGenerator[MagicMock, None]:
            yield mock_conn

        with caplog.at_level(logging.DEBUG):
            with patch("app.tools.db.get_db_conn", fake_get_db_conn):
                await rewards_module.write_reward_manifest(
                    peer="<test-peer-7>",
                    notion_page_id="<page-id-007>",
                    task_title=private_title,
                    reward_kind="emoji",
                    intensity="low",
                    streak_count=1,
                    delivered_at=datetime.now(UTC),
                    sensitive_task=False,
                )

        # task_title must not appear in any log record
        all_log_text = " ".join(r.getMessage() for r in caplog.records)
        assert private_title not in all_log_text, (
            "task_title leaked into log output — private data violation"
        )

    @pytest.mark.asyncio
    async def test_maybe_reward_task_title_never_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """maybe_reward must not emit task_title in any log record at any level."""
        from app.tools import rewards as rewards_module

        private_title = "Placeholder-private-maybe-reward-7a3b"

        with (
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock(return_value=None)),
            patch.object(rewards_module, "write_reward_manifest", new=AsyncMock(return_value=uuid.uuid4())),
            caplog.at_level(logging.DEBUG),
        ):
            await rewards_module.maybe_reward(
                peer="<test-peer-8>",
                task_title=private_title,
                notion_page_id="<page-id-008>",
                streak=1,
                energy_required="Low",
                time_estimate=10,
            )

        all_log_text = " ".join(r.getMessage() for r in caplog.records)
        assert private_title not in all_log_text, (
            "task_title leaked from maybe_reward into log output — private data violation"
        )

    @pytest.mark.asyncio
    async def test_manifest_failure_does_not_crash_maybe_reward(self) -> None:
        """A Postgres failure in write_reward_manifest must not prevent reward delivery."""
        from app.tools import rewards as rewards_module

        async def crashing_manifest(**_kwargs: Any) -> None:
            raise RuntimeError("DB connection refused")

        with (
            patch.object(rewards_module, "generate_reward_image", new=AsyncMock(return_value=None)),
            patch.object(rewards_module, "write_reward_manifest", new=crashing_manifest),
        ):
            result = await rewards_module.maybe_reward(
                peer="<test-peer-9>",
                task_title="Placeholder task title",
                notion_page_id="<page-id-009>",
                streak=1,
                energy_required="Low",
                time_estimate=10,
            )

        # Must still return a celebration string
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# PR-B5-T4: CI grep — 'task_title' as private column name must not leak
#            as a literal string in structlog field keys in committed source
# ---------------------------------------------------------------------------

class TestPrivateDataDiscipline:
    """CI-style checks for private data leakage in source files."""

    def test_no_task_title_in_structlog_field_keys(self) -> None:
        """No Python source file may pass 'task_title' as a structlog field key.

        task_title is a private Postgres column. It may appear as a variable name
        or parameter name in application code, but must NEVER appear as a string
        literal key in a log.info/log.warning/log.error/log.debug call.

        The pattern checked is: log.<level>(..., task_title=... OR "task_title"=...)
        """
        import re
        from pathlib import Path

        # Pattern: any structlog call with task_title as a keyword argument
        # or as a string literal key
        log_call_with_task_title = re.compile(
            r'\blog\.(info|warning|error|debug|exception)\s*\([^)]*\btask_title\s*='
        )

        repo_root = Path(__file__).parent.parent.parent
        source_dirs = [repo_root / "app", repo_root / "tests"]

        violations: list[str] = []
        for source_dir in source_dirs:
            for py_file in source_dir.rglob("*.py"):
                content = py_file.read_text(encoding="utf-8")
                if log_call_with_task_title.search(content):
                    violations.append(str(py_file.relative_to(repo_root)))

        assert not violations, (
            f"task_title found as a structlog field key in: {violations}. "
            "task_title is private — never log it."
        )

    def test_reward_manifests_sql_marks_task_title_private(self) -> None:
        """The reward_manifests migration must mark task_title as a private column."""
        from pathlib import Path

        migration_path = (
            Path(__file__).parent.parent.parent / "migrations" / "0002_reward_manifests.sql"
        )
        assert migration_path.is_file(), f"Migration not found: {migration_path}"

        content = migration_path.read_text(encoding="utf-8")
        # Must contain the private marker comment for task_title
        assert "PRIVATE" in content, "Migration must mark task_title as PRIVATE"
        assert "task_title" in content

    def test_no_real_task_title_in_fixture_files(self) -> None:
        """Test fixtures must not contain real task_title values.

        All fixture task titles must use placeholder strings, not real content.
        """
        import json
        from pathlib import Path

        fixtures_dir = Path(__file__).parent.parent / "fixtures"
        if not fixtures_dir.is_dir():
            pytest.skip("No fixtures directory found")

        # Known-safe placeholder prefix
        safe_prefixes = ("Placeholder", "<", "Test", "test", "sample", "Sample")

        for fixture_file in fixtures_dir.glob("*.json"):
            content = json.loads(fixture_file.read_text(encoding="utf-8"))
            # Look for any task_title fields in the fixture
            def check_values(obj: Any, path: str = "") -> list[str]:
                violations = []
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k == "task_title" and isinstance(v, str):
                            if not any(v.startswith(p) for p in safe_prefixes) and v != "":
                                violations.append(f"{fixture_file.name}:{path}.{k}={v!r}")
                        violations.extend(check_values(v, f"{path}.{k}"))
                elif isinstance(obj, list):
                    for i, item in enumerate(obj):
                        violations.extend(check_values(item, f"{path}[{i}]"))
                return violations

            found = check_values(content)
            assert not found, (
                f"Possible real task_title in fixture: {found}. Use placeholder values."
            )
