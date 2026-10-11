"""Narrow projections over populated SDK reads. Never export upstream extras."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

from casahome import CasaResponseError
from casahome.models import OnboardingDetailsResponse, SuggestedTodosResponse
from homeassistant.components.calendar import CalendarEvent
from homeassistant.components.todo.const import TodoItemStatus
from homeassistant.components.todo.entity import TodoItem
from homeassistant.exceptions import HomeAssistantError

# Only this status has a known meaning. Others stay unmapped.
SUGGESTION_STATUSES = {"suggestion_concierge_approved": TodoItemStatus.NEEDS_ACTION}
_TITLE_URL = re.compile(
    r"\b(?:[a-z][a-z0-9+.-]*://|(?:mailto|data|tel|urn|file):|www\.)\S+", re.IGNORECASE
)
_TITLE_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


def _suggestion_title(title: str) -> str:
    """Plain, bounded titles only. No URL targets or invisible control characters."""
    title = _TITLE_LINK.sub(r"\1", title)
    title = _TITLE_URL.sub("", title)
    title = "".join(" " if unicodedata.category(char).startswith("C") else char for char in title)
    return " ".join(title.split())[:255] or "Casa suggestion"


def suggested_todo_items(response: SuggestedTodosResponse) -> tuple[TodoItem, ...]:
    """Preserve source order and stable UIDs. First duplicate wins."""
    seen: set[str] = set()
    items: list[TodoItem] = []
    for todo in response.suggested_todos:
        if todo.id in seen:
            continue
        seen.add(todo.id)
        items.append(
            TodoItem(
                uid=todo.id,
                summary=_suggestion_title(todo.title),
                status=SUGGESTION_STATUSES.get(todo.status),
            )
        )
    return tuple(items)


def onboarding_events(response: OnboardingDetailsResponse) -> tuple[CalendarEvent, ...]:
    """Convert epoch seconds to UTC visits. Invalid times fail the whole read."""
    seen: set[str] = set()
    events: list[CalendarEvent] = []
    for event in response.calendar_events:
        # SDK fields are ints, not ISO strings, milliseconds, or forecast dates.
        try:
            start = datetime.fromtimestamp(event.start_at, UTC)
            end = datetime.fromtimestamp(event.end_at, UTC)
        except (ValueError, OverflowError, OSError):
            raise CasaResponseError("Invalid Casa onboarding timestamp") from None
        if end <= start:
            raise CasaResponseError("Invalid Casa onboarding duration") from None
        if event.id in seen:
            continue
        seen.add(event.id)
        try:
            events.append(
                CalendarEvent(uid=event.id, start=start, end=end, summary="Casa onboarding visit")
            )
        except (HomeAssistantError, ValueError, OverflowError):
            raise CasaResponseError("Invalid Casa onboarding event") from None
    # Order instants, not local wall times across a repeated DST hour.
    # These starts were all constructed above as aware UTC datetimes.
    return tuple(sorted(events, key=lambda event: event.start))
