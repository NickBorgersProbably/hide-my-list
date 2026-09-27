"""Which reminder a time-only follow-up ("make it 6pm") moves.

A reminder the user just set, or one that was just delivered, is the natural
referent of "actually make it 6pm", "move it to tomorrow 9am", or "push that
to 8". Without an anchor intake saves that follow-up as a second reminder and
leaves the original firing at the old time.

The recent-task ledger is the anchor. `hydrate_context` merges every
delivery into it, so it already covers a reminder the worker just sent as
well as one intake just created. This module:

- picks the candidate reminders from the ledger (`recent_reminder_entries`),
- confirms each against its Notion page and reads its current time
  (`load_reschedule_candidates`),
- renders them for the intake prompt under short labels (`R1`, `R2`, ...)
  so page ids never reach a prompt (`render_reschedule_candidates`),
- maps the model's `reschedule_of` label back to a candidate, rejecting any
  label it did not show (`resolve_reschedule_target`).

The model decides whether the message is a time change; the code only
validates the label. Everything here fails open: a lookup error drops the
candidate, and with no candidate intake creates a reminder as usual.

Privacy: titles are the user's words. They go into the prompt only, never
into logs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

log = structlog.get_logger(__name__)

# Ledger events that make a reminder the referent of a time change: intake
# just set it, or the worker just delivered it.
_RESCHEDULABLE_EVENTS = frozenset({"added", "reminded"})

# "make it 6pm" is about a reminder from this conversation's recent past, not
# one from last week.
RESCHEDULE_FRESHNESS = timedelta(hours=24)

# Candidates shown to the model. The newest reminder is almost always the
# referent; a second or third covers "no, the other one".
MAX_RESCHEDULE_CANDIDATES = 3

# Per-page Notion lookup bound. A slow Notion drops the candidate rather than
# stalling the turn.
CANDIDATE_LOOKUP_TIMEOUT_SECONDS = 5.0

_NO_CANDIDATES = "None."


@dataclass(frozen=True)
class RescheduleCandidate:
    """A reminder the user may be moving, as shown to the intake model."""

    label: str
    page_id: str
    title: str
    remind_at: datetime | None
    status: str


def _parse_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def recent_reminder_entries(
    entries: Iterable[object] | None,
    *,
    now: datetime,
    limit: int = MAX_RESCHEDULE_CANDIDATES,
) -> list[tuple[str, str]]:
    """Return `(page_id, title)` of the newest fresh reminder ledger entries.

    Keeps entries of kind `reminder` whose latest event is `added` or
    `reminded`, that carry a title, and that are at most
    `RESCHEDULE_FRESHNESS` old. The ledger is stored newest first with one
    entry per page, so stored order is kept. An entry with no timestamp is
    kept: `hydrate_context` stamps every entry it writes, and only a static
    eval fixture omits one. An entry with an unparseable timestamp is
    dropped.
    """
    picked: list[tuple[str, str]] = []
    for raw in entries or []:
        if len(picked) >= limit:
            break
        if not isinstance(raw, Mapping):
            continue
        if raw.get("kind") != "reminder" or raw.get("event") not in _RESCHEDULABLE_EVENTS:
            continue
        page_id = raw.get("page_id")
        title = raw.get("title")
        if not isinstance(page_id, str) or not page_id:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        raw_at = raw.get("at")
        if raw_at not in (None, ""):
            at = _parse_at(raw_at)
            if at is None or now - at > RESCHEDULE_FRESHNESS:
                continue
        if any(existing == page_id for existing, _ in picked):
            continue
        picked.append((page_id, title.strip()))
    return picked


def _date_start(props: Mapping[str, Any], key: str) -> str:
    prop = props.get(key)
    if not isinstance(prop, Mapping):
        return ""
    date = prop.get("date")
    if not isinstance(date, Mapping):
        return ""
    start = date.get("start")
    return start if isinstance(start, str) else ""


async def _load_one(page_id: str) -> Mapping[str, Any]:
    from app.tools import notion

    page = await asyncio.wait_for(notion.get_page(page_id), CANDIDATE_LOOKUP_TIMEOUT_SECONDS)
    props = page.get("properties") if isinstance(page, dict) else None
    return props if isinstance(props, Mapping) else {}


async def load_reschedule_candidates(
    entries: Iterable[object] | None, *, now: datetime
) -> list[RescheduleCandidate]:
    """Confirm the ledger's recent reminders against Notion and label them.

    Each page from `recent_reminder_entries` is read once, concurrently. A
    page that is not a reminder page, or whose read fails or times out, is
    dropped. Labels are assigned after filtering, so the model always sees
    `R1` first. With no ledger reminder there is no Notion call at all.
    """
    from app.graph.nodes._task_match import extract_checkbox, extract_select

    picked = recent_reminder_entries(entries, now=now)
    if not picked:
        return []
    results = await asyncio.gather(
        *(_load_one(page_id) for page_id, _ in picked), return_exceptions=True
    )
    candidates: list[RescheduleCandidate] = []
    failed: list[str] = []
    for (page_id, title), result in zip(picked, results, strict=True):
        if isinstance(result, BaseException):
            failed.append(type(result).__name__)
            continue
        props = dict(result)
        if not extract_checkbox(props, "Is Reminder"):
            continue
        candidates.append(
            RescheduleCandidate(
                label=f"R{len(candidates) + 1}",
                page_id=page_id,
                title=title,
                remind_at=_parse_at(_date_start(props, "Remind At")),
                status=extract_select(props, "Status"),
            )
        )
    if failed:
        log.warning(
            "intake_node.reschedule_candidate_lookup_failed",
            error_type=sorted(set(failed)),
            failed_count=len(failed),
            lookup_count=len(picked),
        )
    return candidates


def _zone(user_timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(user_timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def render_reschedule_candidates(
    candidates: Iterable[RescheduleCandidate], *, user_timezone: str
) -> str:
    """One line per candidate: label, quoted title, current reminder time.

    The time is shown in the user's timezone with its UTC offset, so the
    model can keep the reminder's date when the user names only a new clock
    time. Titles are flattened to one line so a stored line break cannot
    read as a second candidate.
    """
    zone = _zone(user_timezone)
    lines: list[str] = []
    for candidate in candidates:
        flat = " ".join(candidate.title.split())
        if candidate.remind_at is not None:
            local = candidate.remind_at.astimezone(zone)
            when = f"set for {local.strftime('%a %Y-%m-%d %H:%M')} ({local.isoformat()})"
        else:
            when = "time unknown"
        lines.append(f'- {candidate.label}: "{flat}" — {when}')
    return "\n".join(lines) if lines else _NO_CANDIDATES


def resolve_reschedule_target(
    raw: object, candidates: Iterable[RescheduleCandidate]
) -> RescheduleCandidate | None:
    """Return the candidate the model named in `reschedule_of`, or None.

    Only a label the prompt showed resolves. Case and surrounding whitespace
    are ignored; anything else (null, a page id, a title, an unknown label)
    resolves to None and intake creates a reminder as usual.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    wanted = raw.strip().upper()
    for candidate in candidates:
        if candidate.label == wanted:
            return candidate
    return None
