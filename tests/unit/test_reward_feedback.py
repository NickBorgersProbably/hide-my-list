"""Tests for Signal-reaction reward feedback collection.

Coverage:
- _extract_reaction parsing: happy path, isRemove, no-reaction envelope
- Emoji-to-score mapping table
- record_reward_feedback: match within window, no match, outside window,
  already-rated (idempotent), unknown emoji

Private data discipline: no real peer numbers, no real task titles.
All peer values use placeholder strings.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reaction_envelope(
    source: str,
    emoji: str,
    target_sent_timestamp: int,
    is_remove: bool = False,
) -> dict[str, Any]:
    """Build a minimal signal-cli reaction envelope."""
    return {
        "envelope": {
            "source": source,
            "dataMessage": {
                "message": None,
                "reaction": {
                    "emoji": emoji,
                    "targetAuthor": "+15559876543",
                    "targetSentTimestamp": target_sent_timestamp,
                    "isRemove": is_remove,
                },
            },
        }
    }


def _text_envelope(source: str, message: str) -> dict[str, Any]:
    """Build a minimal signal-cli text message envelope."""
    return {
        "envelope": {
            "source": source,
            "dataMessage": {"message": message},
        }
    }


# ---------------------------------------------------------------------------
# _extract_reaction tests
# ---------------------------------------------------------------------------

class TestExtractReaction:
    """_extract_reaction correctly parses reaction envelopes."""

    def test_valid_reaction_returns_tuple(self) -> None:
        """Happy path: reaction envelope returns (peer, emoji, timestamp)."""
        from app.ingress.signal_listener import _extract_reaction

        env = _reaction_envelope("<peer>", "👍", 1716800000000)
        result = _extract_reaction(env)

        assert result is not None
        peer, emoji, ts = result
        assert peer == "<peer>"
        assert emoji == "👍"
        assert ts == 1716800000000

    def test_is_remove_returns_none(self) -> None:
        """isRemove: true reactions are not recorded — return None."""
        from app.ingress.signal_listener import _extract_reaction

        env = _reaction_envelope("<peer>", "👍", 1716800000000, is_remove=True)
        result = _extract_reaction(env)

        assert result is None

    def test_text_message_envelope_returns_none(self) -> None:
        """Envelope with message but no reaction returns None."""
        from app.ingress.signal_listener import _extract_reaction

        env = _text_envelope("<peer>", "hello")
        result = _extract_reaction(env)

        assert result is None

    def test_missing_source_returns_none(self) -> None:
        """Envelope without source returns None."""
        from app.ingress.signal_listener import _extract_reaction

        env = {
            "envelope": {
                "dataMessage": {
                    "reaction": {
                        "emoji": "👍",
                        "targetSentTimestamp": 1716800000000,
                        "isRemove": False,
                    }
                }
            }
        }
        result = _extract_reaction(env)
        assert result is None

    def test_empty_envelope_returns_none(self) -> None:
        """Completely empty envelope returns None."""
        from app.ingress.signal_listener import _extract_reaction

        result = _extract_reaction({})
        assert result is None

    def test_no_data_message_returns_none(self) -> None:
        """Envelope with source but no dataMessage returns None."""
        from app.ingress.signal_listener import _extract_reaction

        env = {"envelope": {"source": "<peer>"}}
        result = _extract_reaction(env)
        assert result is None

    def test_timestamp_cast_to_int(self) -> None:
        """target_sent_timestamp is cast to int in the returned tuple."""
        from app.ingress.signal_listener import _extract_reaction

        # Supply as float to verify the cast
        env = {
            "envelope": {
                "source": "<peer>",
                "dataMessage": {
                    "reaction": {
                        "emoji": "❤️",
                        "targetSentTimestamp": 1716800000000,
                        "isRemove": False,
                    }
                },
            }
        }
        result = _extract_reaction(env)
        assert result is not None
        _, _, ts = result
        assert isinstance(ts, int)

    def test_nonnumeric_timestamp_returns_none(self) -> None:
        """Non-numeric targetSentTimestamp returns None (no ValueError raised)."""
        from app.ingress.signal_listener import _extract_reaction

        env = {
            "envelope": {
                "source": "<peer>",
                "dataMessage": {
                    "reaction": {
                        "emoji": "👍",
                        "targetSentTimestamp": "not-a-number",
                        "isRemove": False,
                    }
                },
            }
        }
        result = _extract_reaction(env)
        assert result is None


# ---------------------------------------------------------------------------
# Emoji-to-score mapping tests
# ---------------------------------------------------------------------------

class TestEmojiScoreMapping:
    """_FEEDBACK_EMOJI_SCORES maps known emojis correctly; unknown → 0."""

    def test_thumbs_up_positive(self) -> None:
        from app.tools.rewards import _FEEDBACK_EMOJI_SCORES
        assert _FEEDBACK_EMOJI_SCORES["👍"] == +1

    def test_thumbs_down_negative(self) -> None:
        from app.tools.rewards import _FEEDBACK_EMOJI_SCORES
        assert _FEEDBACK_EMOJI_SCORES["👎"] == -1

    def test_unknown_emoji_not_in_table(self) -> None:
        """Unknown emoji is not in the mapping table — callers use .get(emoji, 0)."""
        from app.tools.rewards import _FEEDBACK_EMOJI_SCORES
        # Shrug is not a defined signal
        assert "🤷" not in _FEEDBACK_EMOJI_SCORES

    def test_get_unknown_emoji_yields_zero(self) -> None:
        """record_reward_feedback uses .get(emoji, 0) for unknown emojis."""
        from app.tools.rewards import _FEEDBACK_EMOJI_SCORES
        assert _FEEDBACK_EMOJI_SCORES.get("🤷", 0) == 0

    def test_all_positive_entries_are_plus_one(self) -> None:
        """All positive entries map to +1."""
        from app.tools.rewards import _FEEDBACK_EMOJI_SCORES
        positives = ["👍", "❤️", "🎉", "🔥", "😍", "💯"]
        for emoji in positives:
            assert _FEEDBACK_EMOJI_SCORES[emoji] == +1, f"{emoji} should be +1"

    def test_all_negative_entries_are_minus_one(self) -> None:
        """All negative entries map to -1."""
        from app.tools.rewards import _FEEDBACK_EMOJI_SCORES
        negatives = ["👎", "😞", "😕", "💔"]
        for emoji in negatives:
            assert _FEEDBACK_EMOJI_SCORES[emoji] == -1, f"{emoji} should be -1"


# ---------------------------------------------------------------------------
# record_reward_feedback DB logic tests
# ---------------------------------------------------------------------------

def _make_mock_conn(fetchone_return: dict | None) -> MagicMock:
    """Build a mock psycopg connection whose execute().fetchone() returns the given row."""
    mock_cursor = MagicMock()
    mock_cursor.fetchone = AsyncMock(return_value=fetchone_return)

    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock(return_value=mock_cursor)
    return mock_conn


@asynccontextmanager
async def _fake_db(conn: MagicMock) -> AsyncGenerator[MagicMock, None]:
    yield conn


class TestRecordRewardFeedback:
    """record_reward_feedback DB logic: match/no-match/window/idempotency/unknown-emoji.

    The function uses a tight ±30-second window around the reaction's
    targetSentTimestamp (the exact timestamp of the reacted-to message) to
    identify the specific reward, preventing false attribution to other messages.
    """

    # A fixed "now" in ms for deterministic tests
    _TS_MS: int = 1716800000000
    _TARGET_DT: datetime = datetime.fromtimestamp(_TS_MS / 1000.0, tz=UTC)

    @pytest.mark.asyncio
    async def test_match_within_window_returns_true(self) -> None:
        """Row found matching the target timestamp → feedback columns updated, returns True."""
        from app.tools import rewards as rewards_module

        matching_id = str(uuid.uuid4())
        mock_conn = _make_mock_conn({"id": matching_id})

        with patch("app.tools.db.get_db_conn", return_value=_fake_db(mock_conn)):
            result = await rewards_module.record_reward_feedback(
                peer="<test-peer>",
                emoji="👍",
                target_sent_timestamp=self._TS_MS,
            )

        assert result is True
        # execute called twice: SELECT then UPDATE
        assert mock_conn.execute.await_count == 2
        update_call = mock_conn.execute.await_args_list[1]
        update_sql = update_call.args[0]
        assert "UPDATE reward_manifests" in update_sql
        update_params = update_call.args[1]
        # score=+1, emoji stored verbatim, id matches
        assert update_params[0] == 1   # score
        assert update_params[1] == "👍"
        assert update_params[2] == matching_id

    @pytest.mark.asyncio
    async def test_no_match_returns_false(self) -> None:
        """No rewards for peer → returns False, UPDATE never called."""
        from app.tools import rewards as rewards_module

        mock_conn = _make_mock_conn(None)

        with patch("app.tools.db.get_db_conn", return_value=_fake_db(mock_conn)):
            result = await rewards_module.record_reward_feedback(
                peer="<test-peer-no-rewards>",
                emoji="👍",
                target_sent_timestamp=self._TS_MS,
            )

        assert result is False
        # Only SELECT was called
        assert mock_conn.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_outside_window_returns_false(self) -> None:
        """Row exists but outside the ±30-second window → no match, returns False.

        Simulated by fetchone returning None (the DB query filters by tight window).
        """
        from app.tools import rewards as rewards_module

        # fetchone=None simulates the query returning no row (outside window)
        mock_conn = _make_mock_conn(None)

        # Use a timestamp far from any reward (e.g., 3 hours off)
        old_ts = int((self._TARGET_DT - timedelta(hours=3)).timestamp() * 1000)

        with patch("app.tools.db.get_db_conn", return_value=_fake_db(mock_conn)):
            result = await rewards_module.record_reward_feedback(
                peer="<test-peer>",
                emoji="👍",
                target_sent_timestamp=old_ts,
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_already_rated_reward_returns_false(self) -> None:
        """feedback_at IS NOT NULL filter prevents double-write.

        Simulated by fetchone returning None (the WHERE clause excludes rated rows).
        """
        from app.tools import rewards as rewards_module

        mock_conn = _make_mock_conn(None)

        with patch("app.tools.db.get_db_conn", return_value=_fake_db(mock_conn)):
            result = await rewards_module.record_reward_feedback(
                peer="<test-peer>",
                emoji="🎉",
                target_sent_timestamp=self._TS_MS,
            )

        assert result is False
        assert mock_conn.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_unknown_emoji_records_zero_score(self) -> None:
        """Unknown emoji → score=0 stored; feedback_emoji stored verbatim."""
        from app.tools import rewards as rewards_module

        matching_id = str(uuid.uuid4())
        mock_conn = _make_mock_conn({"id": matching_id})

        with patch("app.tools.db.get_db_conn", return_value=_fake_db(mock_conn)):
            result = await rewards_module.record_reward_feedback(
                peer="<test-peer>",
                emoji="🤷",
                target_sent_timestamp=self._TS_MS,
            )

        assert result is True
        update_call = mock_conn.execute.await_args_list[1]
        update_params = update_call.args[1]
        assert update_params[0] == 0     # score = 0 for unknown emoji
        assert update_params[1] == "🤷"  # emoji stored verbatim

    @pytest.mark.asyncio
    async def test_db_exception_returns_false_no_propagation(self) -> None:
        """DB failure must not propagate — returns False and logs exception."""
        from app.tools import rewards as rewards_module

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(side_effect=RuntimeError("DB is down"))

        with patch("app.tools.db.get_db_conn", return_value=_fake_db(mock_conn)):
            result = await rewards_module.record_reward_feedback(
                peer="<test-peer>",
                emoji="👍",
                target_sent_timestamp=self._TS_MS,
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_negative_emoji_records_minus_one(self) -> None:
        """Negative emoji (👎) → score=-1 stored."""
        from app.tools import rewards as rewards_module

        matching_id = str(uuid.uuid4())
        mock_conn = _make_mock_conn({"id": matching_id})

        with patch("app.tools.db.get_db_conn", return_value=_fake_db(mock_conn)):
            result = await rewards_module.record_reward_feedback(
                peer="<test-peer>",
                emoji="👎",
                target_sent_timestamp=self._TS_MS,
            )

        assert result is True
        update_call = mock_conn.execute.await_args_list[1]
        update_params = update_call.args[1]
        assert update_params[0] == -1


# ---------------------------------------------------------------------------
# Authorization: reaction from unauthorized peer must be dropped
# ---------------------------------------------------------------------------

async def _async_gen(envelopes: list[dict[str, Any]]):
    for env in envelopes:
        yield env


class TestReactionAuthorization:
    """Reactions from unauthorized peers are dropped before any DB write."""

    @pytest.mark.asyncio
    async def test_unauthorized_reaction_dropped_no_db_write(self) -> None:
        """Reaction from a peer NOT in authorized_peers must be dropped silently."""
        from app.ingress.signal_listener import SignalListener

        graph = AsyncMock()
        listener = SignalListener(
            graph=graph,
            authorized_peers=frozenset({"+15551234567"}),
        )

        env = _reaction_envelope("+19990001111", "👍", 1716800000000)
        with (
            patch(
                "app.ingress.signal_listener.receive_messages",
                return_value=_async_gen([env]),
            ),
            patch(
                "app.tools.rewards.record_reward_feedback",
                new=AsyncMock(),
            ) as mock_feedback,
        ):
            await listener.run()

        # Graph never invoked
        graph.ainvoke.assert_not_awaited()
        # Feedback function never called
        mock_feedback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authorized_reaction_calls_feedback(self) -> None:
        """Reaction from authorized peer routes to record_reward_feedback."""
        from app.ingress.signal_listener import SignalListener

        graph = AsyncMock()
        listener = SignalListener(
            graph=graph,
            authorized_peers=frozenset({"+15551234567"}),
        )

        ts = 1716800000000
        env = _reaction_envelope("+15551234567", "👍", ts)
        with (
            patch(
                "app.ingress.signal_listener.receive_messages",
                return_value=_async_gen([env]),
            ),
            patch(
                "app.ingress.signal_listener.SignalListener._handle_reaction",
                new=AsyncMock(),
            ) as mock_handle,
        ):
            await listener.run()

        # _handle_reaction called with correct args; graph NOT invoked
        mock_handle.assert_awaited_once_with("+15551234567", "👍", ts)
        graph.ainvoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authorized_reaction_invokes_record_reward_feedback(self) -> None:
        """_handle_reaction calls record_reward_feedback with correct kwargs."""
        from app.ingress.signal_listener import SignalListener

        graph = AsyncMock()
        listener = SignalListener(
            graph=graph,
            authorized_peers=frozenset({"+15551234567"}),
        )

        ts = 1716800000000
        mock_record = AsyncMock(return_value=True)

        with patch("app.tools.rewards.record_reward_feedback", mock_record):
            await listener._handle_reaction("+15551234567", "👍", ts)

        mock_record.assert_awaited_once_with(
            peer="+15551234567",
            emoji="👍",
            target_sent_timestamp=ts,
        )


# ---------------------------------------------------------------------------
# load_feedback_history tests
# ---------------------------------------------------------------------------

class TestLoadFeedbackHistory:
    """load_feedback_history returns rated rows; fails gracefully on DB error."""

    @pytest.mark.asyncio
    async def test_returns_rows_from_db(self) -> None:
        """Rated rows from DB are returned as structured dicts."""
        from app.tools import rewards as rewards_module

        now = datetime.now(UTC)
        mock_rows = [
            {"feedback_score": 1, "feedback_at": now},
            {"feedback_score": -1, "feedback_at": now - timedelta(days=1)},
        ]
        mock_cursor = MagicMock()
        mock_cursor.fetchall = AsyncMock(return_value=mock_rows)
        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(return_value=mock_cursor)

        with patch("app.tools.db.get_db_conn", return_value=_fake_db(mock_conn)):
            result = await rewards_module.load_feedback_history("<test-peer>")

        assert len(result) == 2
        assert result[0]["score"] == 1
        assert result[1]["score"] == -1
        # theme_family/style/palette are empty (not stored in manifest)
        assert result[0]["theme_family"] == ""
        assert "timestamp" in result[0]

    @pytest.mark.asyncio
    async def test_db_failure_returns_empty_list(self) -> None:
        """DB failure returns [] without propagation — feedback degrades gracefully."""
        from app.tools import rewards as rewards_module

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(side_effect=RuntimeError("DB down"))

        with patch("app.tools.db.get_db_conn", return_value=_fake_db(mock_conn)):
            result = await rewards_module.load_feedback_history("<test-peer>")

        assert result == []

    @pytest.mark.asyncio
    async def test_empty_result_returns_empty_list(self) -> None:
        """No rated rows → empty list."""
        from app.tools import rewards as rewards_module

        mock_cursor = MagicMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(return_value=mock_cursor)

        with patch("app.tools.db.get_db_conn", return_value=_fake_db(mock_conn)):
            result = await rewards_module.load_feedback_history("<test-peer>")

        assert result == []


# ---------------------------------------------------------------------------
# maybe_reward feedback wiring tests
# ---------------------------------------------------------------------------

class TestMaybeRewardFeedbackWiring:
    """maybe_reward loads feedback history and passes it to generate_reward_image."""

    @pytest.mark.asyncio
    async def test_maybe_reward_passes_feedback_history_to_image_gen(self) -> None:
        """maybe_reward passes loaded feedback_history to generate_reward_image."""
        import uuid as uuid_mod

        from app.tools import rewards as rewards_module

        fake_feedback = [{"score": 1, "timestamp": datetime.now(UTC).isoformat(),
                          "theme_family": "", "style": "", "palette": ""}]
        recorded_gen_calls: list[dict] = []

        async def fake_gen(**kwargs: Any) -> None:
            recorded_gen_calls.append(kwargs)
            return None

        with (
            patch.object(rewards_module, "load_feedback_history",
                         new=AsyncMock(return_value=fake_feedback)),
            patch.object(rewards_module, "generate_reward_image", new=fake_gen),
            patch.object(rewards_module, "write_reward_manifest",
                         new=AsyncMock(return_value=uuid_mod.uuid4())),
            patch.object(rewards_module, "compute_intensity",
                         return_value=("medium", 40)),
        ):
            await rewards_module.maybe_reward(
                peer="<test-peer>",
                task_title="Placeholder task",
                notion_page_id="<page-id>",
                streak=2,
            )

        assert len(recorded_gen_calls) == 1
        assert recorded_gen_calls[0]["feedback_history"] == fake_feedback

    @pytest.mark.asyncio
    async def test_maybe_reward_lightest_skips_feedback_load(self) -> None:
        """maybe_reward does not load feedback for lightest intensity (no image)."""
        from app.tools import rewards as rewards_module

        mock_load = AsyncMock(return_value=[])

        with (
            patch.object(rewards_module, "load_feedback_history", new=mock_load),
            patch.object(rewards_module, "generate_reward_image",
                         new=AsyncMock(return_value=None)),
            patch.object(rewards_module, "write_reward_manifest",
                         new=AsyncMock(return_value=None)),
            patch.object(rewards_module, "compute_intensity",
                         return_value=("lightest", 5)),
        ):
            await rewards_module.maybe_reward(
                peer="<test-peer>",
                task_title="Placeholder task",
                notion_page_id="<page-id>",
                streak=1,
            )

        mock_load.assert_not_awaited()
