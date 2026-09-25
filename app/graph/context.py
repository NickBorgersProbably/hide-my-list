"""Shared conversation context: the recent-task ledger and prompt renderers.

Every turn used to start cold. Nothing recorded the task intake had just
created or the reminder the worker had just delivered, so a bare "Done!" or a
"what task?" a minute later had no anchor, and each node windowed
`state["messages"]` its own way. This module is the one place that:

- writes the recent-task ledger (`record_task_event`) with consistent dedupe,
  prune, and cap rules;
- renders the ledger and the message history for prompts
  (`render_recent_tasks`, `render_history`);
- hydrates the ledger from Postgres at the start of every turn
  (`hydrate_context`, the graph's entry node).

Privacy: titles and message text are the user's own words. They go into
prompts and the checkpoint, never into logs — log ids, counts, booleans, and
error types only. The ledger never stores a sent message body: reminder
deliveries enter it with an empty title unless the ledger already knows the
page's title.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog

from app.graph.state import (
    RecentTaskEntry,
    RecentTaskEvent,
    RecentTaskKind,
    State,
)

log = structlog.get_logger(__name__)

# Entries kept in the ledger. Enough to cover a few adds, a suggestion, and the
# day's reminders; small enough that the rendered block stays a glance.
LEDGER_CAP = 8

# Entries older than this are pruned. Recent outbound rows are read over the
# same window.
LEDGER_MAX_AGE = timedelta(days=7)

# Messages of history rendered into a prompt, one line each.
HISTORY_TURNS = 8

# Characters kept from each rendered message.
HISTORY_CHARS = 400

_NO_HISTORY = "No prior context."
_NO_RECENT_TASKS = "None yet."
_UNDATED = "1970-01-01T00:00:00+00:00"

_KINDS: frozenset[str] = frozenset({"task", "reminder"})
_EVENTS: frozenset[str] = frozenset(
    {"added", "suggested", "completed", "reminded", "nudged", "rejected"}
)
_ROLE_LABELS = {"human": "user", "ai": "assistant"}


def _parse_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _valid_entry(raw: object) -> RecentTaskEntry | None:
    """Return a clean copy of a stored entry, or None if it is unusable.

    Checkpoints are durable and outlive code changes, so a stored entry is
    validated rather than trusted. Extra keys are dropped here, which is also
    what keeps anything other than the ledger's own fields out of a prompt.
    """
    if not isinstance(raw, Mapping):
        return None
    page_id = raw.get("page_id")
    kind = raw.get("kind")
    event = raw.get("event")
    at = raw.get("at")
    title = raw.get("title", "")
    if not isinstance(page_id, str) or not page_id:
        return None
    if kind not in _KINDS or event not in _EVENTS:
        return None
    if _parse_at(at) is None:
        return None
    return {
        "page_id": page_id,
        "title": title.strip() if isinstance(title, str) else "",
        "kind": cast(RecentTaskKind, kind),
        "event": cast(RecentTaskEvent, event),
        "at": cast(str, at),
    }


def prune_recent_tasks(
    entries: Iterable[object] | None, *, now: datetime
) -> list[RecentTaskEntry]:
    """Drop malformed and stale entries, dedupe by page, sort newest first, cap.

    Among entries for the same page the newest wins. Ties keep the earlier
    position, so an entry a writer just placed at the front stays there.
    """
    cutoff = now - LEDGER_MAX_AGE
    kept: list[tuple[datetime, int, RecentTaskEntry]] = []
    seen: set[str] = set()
    cleaned = [entry for entry in (_valid_entry(raw) for raw in (entries or [])) if entry]
    ordered = sorted(
        enumerate(cleaned),
        key=lambda pair: (-_at_or_min(pair[1]).timestamp(), pair[0]),
    )
    for index, entry in ordered:
        at = _at_or_min(entry)
        if at < cutoff or entry["page_id"] in seen:
            continue
        seen.add(entry["page_id"])
        kept.append((at, index, entry))
    return [entry for _, _, entry in kept[:LEDGER_CAP]]


def _at_or_min(entry: Mapping[str, Any]) -> datetime:
    return _parse_at(entry.get("at")) or datetime.min.replace(tzinfo=UTC)


def ledger_entry(
    entries: Iterable[object] | None, page_id: str
) -> RecentTaskEntry | None:
    """Return the ledger's entry for `page_id`, or None if it has none."""
    for raw in entries or []:
        entry = _valid_entry(raw)
        if entry and entry["page_id"] == page_id:
            return entry
    return None


def record_task_event(
    existing: Iterable[object] | None,
    *,
    page_id: str,
    title: str,
    kind: RecentTaskKind,
    event: RecentTaskEvent,
    now: datetime,
) -> list[RecentTaskEntry]:
    """Return a new ledger with this event recorded.

    One entry per page. A newer event replaces an older one for the same page;
    an older event (a delivery re-merged on a later turn, say) never replaces a
    newer one. Either way a known title is kept when the other side has none,
    because an empty title means "not known here", not "renamed to nothing".
    The input is not mutated.
    """
    current = [entry for entry in (_valid_entry(raw) for raw in (existing or [])) if entry]
    if not page_id:
        return prune_recent_tasks(current, now=now)

    at = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    new: RecentTaskEntry = {
        "page_id": page_id,
        "title": (title or "").strip(),
        "kind": kind,
        "event": event,
        "at": at.isoformat(),
    }

    previous = next((entry for entry in current if entry["page_id"] == page_id), None)
    others = [entry for entry in current if entry["page_id"] != page_id]
    if previous is not None:
        previous_at = _at_or_min(previous)
        if previous_at > at:
            if not previous["title"] and new["title"]:
                previous = {**previous, "title": new["title"]}
            return prune_recent_tasks([previous, *others], now=at)
        if not new["title"]:
            new["title"] = previous["title"]

    return prune_recent_tasks([new, *others], now=at)


def _relative_age(at: datetime, now: datetime) -> str:
    seconds = max(0, int((now - at).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    return f"{hours // 24} d ago"


def _renderable(raw: object) -> tuple[RecentTaskEntry, datetime | None] | None:
    """Validate an entry for rendering; one with no timestamp renders without an age.

    Eval fixtures are static and cannot carry a fresh timestamp, so they omit
    `at`. The writers always stamp one.
    """
    if not isinstance(raw, Mapping):
        return None
    if not raw.get("at"):
        undated = _valid_entry({**raw, "at": _UNDATED})
        return (undated, None) if undated else None
    entry = _valid_entry(raw)
    return (entry, _parse_at(entry["at"])) if entry else None


RECENT_TASK_TITLE_CHARS = 120


def render_recent_tasks(entries: Iterable[object] | None, *, now: datetime) -> str:
    """Render the ledger as one line per entry, in stored (newest-first) order.

    Only the ledger's own fields are rendered — never a page id, never any
    extra key a stored entry might carry. An entry whose title is unknown says
    so rather than borrowing text from anywhere else.
    """
    lines: list[str] = []
    for raw in entries or []:
        renderable = _renderable(raw)
        if renderable is None:
            continue
        entry, at = renderable
        # One entry is always exactly one rendered line: a stored title with a
        # line break would otherwise read as several ledger entries.
        flat = " ".join(entry["title"].split())
        if len(flat) > RECENT_TASK_TITLE_CHARS:
            flat = flat[: RECENT_TASK_TITLE_CHARS - 1].rstrip() + "…"
        title = f'"{flat}"' if flat else "(untitled)"
        marker = " [reminder]" if entry["kind"] == "reminder" else ""
        age = f" {_relative_age(at, now)}" if at is not None else ""
        lines.append(f"- {title}{marker} — {entry['event']}{age}")
    return "\n".join(lines) if lines else _NO_RECENT_TASKS


def render_history(
    messages: Sequence[Any] | None,
    *,
    turns: int = HISTORY_TURNS,
    chars: int = HISTORY_CHARS,
) -> str:
    """Render the last `turns` messages as `user: …` / `assistant: …` lines.

    Each message is flattened to one line and cut to `chars` characters.
    """
    lines: list[str] = []
    for message in list(messages or [])[-turns:] if turns > 0 else []:
        role = str(getattr(message, "type", "message"))
        label = _ROLE_LABELS.get(role, role)
        content = " ".join(str(getattr(message, "content", "")).split())
        lines.append(f"{label}: {content[:chars]}")
    return "\n".join(lines) if lines else _NO_HISTORY


def _as_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return _parse_at(value)


# Whole-message replies that accept the task just suggested. Matched after
# lowercasing and stripping punctuation, against the entire message: anything
# longer or different ("sure, but later", "ok what else?") goes to the model,
# so a hedge or a question is never read as a commitment. Entries are stored
# in normalized form: "let's do it" is "lets do it", "I'll take it" is
# "ill take it".
_ACCEPTANCE_PHRASES: frozenset[str] = frozenset({
    "sure",
    "ok",
    "okay",
    "yes",
    "yep",
    "yeah",
    "fine",
    "sounds good",
    "lets do it",
    "ok lets do it",
    "do it",
    "that one",
    "ok that one",
    "sure that one",
    "ill do that",
    "ill take it",
})

# A suggestion older than this is not what a bare "sure" is answering.
_SUGGESTION_MAX_AGE = timedelta(hours=24)


def _normalize_reply(text: str) -> str:
    """Lowercase, drop punctuation (apostrophes included), collapse spaces."""
    stripped = re.sub(r"[^\w\s]", "", text.lower())
    return " ".join(stripped.split())


def accepted_suggestion(state: State, *, now: datetime) -> RecentTaskEntry | None:
    """Return the suggestion this message accepts, or None.

    Only when nothing is active, the newest ledger entry is a titled
    `suggested` event from the last 24 hours, and the whole message is a
    short affirmative. A rejection alternative stays Pending with no active
    task, so this is how its acceptance is recognised: `classify_intent`
    routes such a message to chat without consulting the model, and
    `chat_node` performs the transition.
    """
    if state.get("active_task"):
        return None
    if _normalize_reply(state.get("incoming") or "") not in _ACCEPTANCE_PHRASES:
        return None
    ledger = prune_recent_tasks(state.get("recent_tasks"), now=now)
    if not ledger:
        return None
    newest = ledger[0]
    if newest["event"] != "suggested" or not newest["title"]:
        return None
    at = _parse_at(newest["at"])
    if at is None or now - at > _SUGGESTION_MAX_AGE:
        return None
    return newest


async def hydrate_context(state: State) -> dict[str, Any]:
    """Graph entry node: merge the peer's recent deliveries into the ledger.

    Reminder deliveries happen outside the graph, so the checkpoint never sees
    them. Each turn this node reads the peer's `recent_outbound` rows from the
    last `LEDGER_MAX_AGE` and records each as `reminded` (or `nudged` for a
    deadline row), keeping any title the ledger already has.

    Fail-soft: any error keeps the existing ledger (pruned) and logs one
    warning with the error type. It never raises — a failure here must not
    reach classify_intent's error fallback and cost the user their reply.
    """
    now = datetime.now(UTC)
    existing = state.get("recent_tasks") or []
    try:
        ledger = prune_recent_tasks(existing, now=now)
    except Exception:
        ledger = []

    peer = state.get("peer", "")
    if not peer or not os.environ.get("DATABASE_URL"):
        return {"recent_tasks": ledger}

    try:
        from app.tools.reminders import fetch_recent_outbound

        rows = await fetch_recent_outbound(peer, LEDGER_MAX_AGE.total_seconds())
        merged = ledger
        for row in rows:
            page_id = str(row.get("notion_page_id") or "")
            sent_at = _as_utc(row.get("sent_at"))
            if not page_id or sent_at is None:
                continue
            event: RecentTaskEvent = (
                "nudged" if row.get("reminder_type") == "deadline" else "reminded"
            )
            known = ledger_entry(merged, page_id)
            kind: RecentTaskKind = (
                known["kind"] if known else ("task" if event == "nudged" else "reminder")
            )
            merged = record_task_event(
                merged,
                page_id=page_id,
                title="",
                kind=kind,
                event=event,
                now=sent_at,
            )
        merged = prune_recent_tasks(merged, now=now)
        log.info(
            "hydrate_context.merged",
            row_count=len(rows),
            ledger_count=len(merged),
        )
        return {"recent_tasks": merged}
    except Exception as exc:
        # Error type and counts only: the rows carry page ids tied to a peer,
        # and a driver error string can echo connection details.
        log.warning(
            "hydrate_context.recent_outbound_failed",
            error_type=type(exc).__name__,
            existing_count=len(existing) if isinstance(existing, list) else 0,
        )
        return {"recent_tasks": ledger}
