#!/usr/bin/env python3
"""One-shot state migration: OpenClaw state.json → Postgres.

Reads OpenClaw's state.json and writes to the Postgres database used by
the new Python/LangGraph stack.

What is migrated:
  - state.json.user_preferences → user_prefs table (UPSERT per peer)
  - state.json.recent_outbound (unexpired entries) → recent_outbound table
    (ON CONFLICT DO NOTHING — idempotent)

What is NOT migrated (explicitly discarded):
  - streak: reset to zero. OpenClaw has been functionally unreliable;
    streak continuity would be illusory. User confirmed this decision.
  - active_task: user starts fresh. No carry-over.
  - tasks_completed_today: reset to zero.
  - conversation_state: no carry-over; new stack initialises to 'idle'.
  - MEMORY.md / memory/ daily files: skipped. User can keep journaling
    separately. See docs/python-rewrite/rollback.md for details.

Usage:
  python scripts/migrate_state_json.py [--state-json PATH] [--peer PEER] [--dry-run]

Arguments:
  --state-json PATH   Path to state.json (default: state.json in repo root)
  --peer PEER         E.164 Signal number of the user (required for DB write)
                      Reads SIGNAL_ACCOUNT env var as fallback.
  --dry-run           Print what would be written without touching the DB.

Environment:
  DATABASE_URL        Postgres DSN (required unless --dry-run)
  SIGNAL_ACCOUNT      Fallback for --peer

Exit codes:
  0  success (or dry-run completed)
  1  error (bad input, DB failure, etc.)

Private data discipline: this script logs field names and counts, NOT values.
Do not add log lines that print task titles, phone numbers, reminder content,
or other user-private data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate OpenClaw state.json to Postgres (one-shot cutover tool)."
    )
    parser.add_argument(
        "--state-json",
        default=str(Path(__file__).parent.parent / "state.json"),
        help="Path to OpenClaw state.json (default: <repo-root>/state.json)",
    )
    parser.add_argument(
        "--peer",
        default=os.environ.get("SIGNAL_ACCOUNT", ""),
        help="E.164 Signal number for the user (or set SIGNAL_ACCOUNT env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without touching the database.",
    )
    return parser.parse_args()


def _load_state_json(path: str) -> dict[str, Any]:
    """Load and parse state.json. Raises SystemExit on error."""
    p = Path(path)
    if not p.exists():
        print(f"ERROR: state.json not found at {path}", file=sys.stderr)
        print("       If OpenClaw was never run, there may be no state to migrate.", file=sys.stderr)
        sys.exit(1)

    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: state.json is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print("ERROR: state.json root must be a JSON object.", file=sys.stderr)
        sys.exit(1)

    return data


def _extract_user_prefs(state: dict[str, Any]) -> dict[str, Any]:
    """Extract user_preferences blob from state. Returns {} if absent."""
    prefs = state.get("user_preferences", {})
    if not isinstance(prefs, dict):
        print(
            f"WARNING: state.json.user_preferences is not a dict (got {type(prefs).__name__}); "
            "treating as empty.",
            file=sys.stderr,
        )
        return {}
    return prefs


def _extract_recent_outbound(state: dict[str, Any], peer: str) -> list[dict[str, Any]]:
    """Extract and normalise recent_outbound entries.

    Returns only unexpired entries with the required fields.
    Expired entries (expires_at < now) are silently dropped.
    Malformed entries (missing required fields) are warned and dropped.
    """
    raw_entries = state.get("recent_outbound", [])
    if not isinstance(raw_entries, list):
        print(
            "WARNING: state.json.recent_outbound is not a list; skipping.",
            file=sys.stderr,
        )
        return []

    now = _now()
    valid: list[dict[str, Any]] = []

    for i, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            print(f"WARNING: recent_outbound[{i}] is not a dict; skipping.", file=sys.stderr)
            continue

        # Required field: page_id maps to notion_page_id.
        page_id = entry.get("page_id") or entry.get("notion_page_id", "")
        if not page_id:
            print(f"WARNING: recent_outbound[{i}] missing page_id; skipping.", file=sys.stderr)
            continue

        # Determine signal_timestamp.
        # OpenClaw state.json may not have this; generate a synthetic one.
        signal_ts = entry.get("signal_timestamp")
        if signal_ts is None:
            # Use a synthetic timestamp: index-based offset from epoch milliseconds.
            # Guaranteed unique within this migration run.
            signal_ts = int(now.timestamp() * 1000) + i

        # Parse expires_at.
        expires_at_raw = entry.get("expires_at")
        if expires_at_raw:
            try:
                expires_at = datetime.fromisoformat(
                    expires_at_raw.replace("Z", "+00:00") if isinstance(expires_at_raw, str) else str(expires_at_raw)
                )
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
            except (ValueError, AttributeError):
                expires_at = now + timedelta(hours=24)
        else:
            expires_at = now + timedelta(hours=24)

        # Skip expired entries.
        if expires_at <= now:
            continue

        # Parse sent_at.
        sent_at_raw = entry.get("sent_at")
        if sent_at_raw:
            try:
                sent_at = datetime.fromisoformat(
                    sent_at_raw.replace("Z", "+00:00") if isinstance(sent_at_raw, str) else str(sent_at_raw)
                )
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=UTC)
            except (ValueError, AttributeError):
                sent_at = now
        else:
            sent_at = now

        valid.append({
            "peer": peer,
            "signal_timestamp": signal_ts,
            "notion_page_id": page_id,
            "reminder_type": entry.get("type", "reminder"),
            "title": "",  # Do NOT migrate title — private data. Use empty string.
            "prompt_kind": entry.get("status", entry.get("prompt_kind", "sent")),
            "sent_at": sent_at,
            "awaiting_reply": bool(entry.get("awaiting_response", entry.get("awaiting_reply", True))),
            "expires_at": expires_at,
        })

    return valid


def _extract_reward_prefs(prefs: dict[str, Any]) -> dict[str, Any]:
    """Extract reward-related preferences from user_preferences."""
    reward_intensity = prefs.get("reward_intensity", "medium")
    if reward_intensity not in {"lightest", "low", "medium", "high", "epic"}:
        reward_intensity = "medium"

    reward_kinds = prefs.get("reward_kinds_enabled", ["emoji", "image"])
    if not isinstance(reward_kinds, list):
        reward_kinds = ["emoji", "image"]

    sensitive_task_mode = bool(prefs.get("sensitive_task_mode", False))

    return {
        "reward_intensity": reward_intensity,
        "reward_kinds_enabled": json.dumps(reward_kinds),
        "sensitive_task_mode": sensitive_task_mode,
    }


def _print_plan(
    peer: str,
    prefs: dict[str, Any],
    reward_prefs: dict[str, Any],
    outbound_rows: list[dict[str, Any]],
) -> None:
    """Print a human-readable dry-run plan. No private values."""
    print("=== DRY RUN — no DB writes ===")
    print()
    print(f"Peer (Signal number): {peer}")
    print()
    print("user_prefs UPSERT:")
    print(f"  prefs_json keys: {sorted(prefs.keys())!r} ({len(prefs)} keys)")
    print(f"  reward_intensity: {reward_prefs['reward_intensity']!r}")
    print(f"  reward_kinds_enabled: {reward_prefs['reward_kinds_enabled']!r}")
    print(f"  sensitive_task_mode: {reward_prefs['sensitive_task_mode']!r}")
    print()
    print(f"recent_outbound INSERT (ON CONFLICT DO NOTHING): {len(outbound_rows)} unexpired entries")
    print()
    print("NOT migrated (explicitly discarded):")
    print("  streak → reset to 0")
    print("  active_task → user starts fresh")
    print("  tasks_completed_today → reset to 0")
    print("  conversation_state → initialised to 'idle' by new stack")
    print("  MEMORY.md / memory/ daily files → see docs/python-rewrite/rollback.md")
    print()
    print("=== END DRY RUN ===")


def _write_to_db(
    peer: str,
    prefs: dict[str, Any],
    reward_prefs: dict[str, Any],
    outbound_rows: list[dict[str, Any]],
) -> None:
    """Write migration data to Postgres. Idempotent."""
    import psycopg

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL env var not set.", file=sys.stderr)
        sys.exit(1)

    now = _now()

    with psycopg.connect(db_url) as conn:
        # UPSERT user_prefs.
        conn.execute(
            """
            INSERT INTO user_prefs
              (peer, prefs_json, reward_intensity, reward_kinds_enabled,
               sensitive_task_mode, created_at, updated_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
            ON CONFLICT (peer)
            DO UPDATE SET
              prefs_json = EXCLUDED.prefs_json,
              reward_intensity = EXCLUDED.reward_intensity,
              reward_kinds_enabled = EXCLUDED.reward_kinds_enabled,
              sensitive_task_mode = EXCLUDED.sensitive_task_mode,
              updated_at = EXCLUDED.updated_at
            """,
            (
                peer,
                json.dumps(prefs),
                reward_prefs["reward_intensity"],
                reward_prefs["reward_kinds_enabled"],
                reward_prefs["sensitive_task_mode"],
                now,
                now,
            ),
        )
        print(f"user_prefs: UPSERTED for peer (field count: {len(prefs)})")

        # Insert recent_outbound rows (idempotent via ON CONFLICT DO NOTHING).
        inserted = 0
        skipped = 0
        for row in outbound_rows:
            result = conn.execute(
                """
                INSERT INTO recent_outbound
                  (peer, signal_timestamp, notion_page_id, reminder_type, title,
                   prompt_kind, sent_at, awaiting_reply, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (peer, signal_timestamp) DO NOTHING
                """,
                (
                    row["peer"],
                    row["signal_timestamp"],
                    row["notion_page_id"],
                    row["reminder_type"],
                    row["title"],
                    row["prompt_kind"],
                    row["sent_at"],
                    row["awaiting_reply"],
                    row["expires_at"],
                ),
            )
            if result.rowcount == 1:
                inserted += 1
            else:
                skipped += 1

        print(f"recent_outbound: inserted={inserted} skipped(already exists)={skipped}")
        conn.commit()

    print()
    print("Migration complete.")
    print()
    print("NOT migrated (explicitly discarded):")
    print("  streak → reset to 0")
    print("  active_task → user starts fresh")
    print("  tasks_completed_today → reset to 0")
    print("  conversation_state → initialised to 'idle' by new stack")
    print("  MEMORY.md / memory/ daily files → see docs/python-rewrite/rollback.md")


def main() -> None:
    args = _parse_args()

    state = _load_state_json(args.state_json)

    peer = args.peer.strip()
    if not peer:
        print(
            "ERROR: --peer is required (or set SIGNAL_ACCOUNT env var).",
            file=sys.stderr,
        )
        sys.exit(1)

    if not peer.startswith("+"):
        print(
            f"WARNING: peer '{peer}' does not look like an E.164 number (expected '+...').",
            file=sys.stderr,
        )

    prefs = _extract_user_prefs(state)
    reward_prefs = _extract_reward_prefs(prefs)
    outbound_rows = _extract_recent_outbound(state, peer)

    if args.dry_run:
        _print_plan(peer, prefs, reward_prefs, outbound_rows)
        return

    _write_to_db(peer, prefs, reward_prefs, outbound_rows)


if __name__ == "__main__":
    main()
