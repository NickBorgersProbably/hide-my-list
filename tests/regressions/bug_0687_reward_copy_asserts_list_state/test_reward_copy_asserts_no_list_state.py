"""Regression: an epic reward said "INBOX ZERO!" with tasks still open (bug #687).

The reward layer does not know whether the list is empty or a project is
finished, so no template may claim either.
"""
from __future__ import annotations

import re

from app.tools.rewards import _EMOJI_TEMPLATES, get_celebration_emoji

_STATE_CLAIM = re.compile(
    r"(?i)inbox zero|project complete|all (tasks )?(done|cleared)|list is empty"
)


def test_no_template_states_a_list_or_project_fact() -> None:
    offenders = [
        (intensity, text)
        for intensity, templates in _EMOJI_TEMPLATES.items()
        for text in templates
        if _STATE_CLAIM.search(text)
    ]
    assert offenders == []


def test_the_epic_draw_never_returns_a_state_claim() -> None:
    # Every epic template, reached through the public draw.
    drawn = {get_celebration_emoji("epic") for _ in range(200)}
    assert drawn <= set(_EMOJI_TEMPLATES["epic"])
    assert not [text for text in drawn if _STATE_CLAIM.search(text)]
