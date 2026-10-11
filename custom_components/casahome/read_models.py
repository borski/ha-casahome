"""Allowlisted projections for aggregate reads. No SDK objects reach attributes."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from casahome import CasaResponseError
from casahome.models.calendar import VisitsCalendarResponse
from casahome.models.home import HomeDetailsResponse
from casahome.models.mailbox import MailboxMailItemsResponse, PackagesResponse
from casahome.models.maintenance import AutopilotCategory, InventoryServiceHistoryResponse
from casahome.models.requests import RequestsResponse
from homeassistant.components.calendar import CalendarEvent


class ReadFamily(StrEnum):
    """One aggregate GET and independent availability per family."""

    INVENTORY = "inventory"
    REQUESTS = "requests"
    MAINTENANCE = "maintenance"
    HISTORY = "history"
    MAIL = "mail"
    PACKAGES = "packages"
    CALENDAR = "service_calendar"


@dataclass(frozen=True)
class InventoryRecord:
    """Virtual inventory identity, not a live appliance connection."""

    id: str
    name: str
    status: str
    manufacturer: str | None
    model: str | None
    serial_number: str | None
    room: str | None


@dataclass(frozen=True)
class ReadData:
    """None is unavailable. Empty collections are successful empty responses."""

    counts: tuple[tuple[str, int], ...] | None = None
    inventory: tuple[InventoryRecord, ...] = ()
    events: tuple[CalendarEvent, ...] = ()
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None

    def count(self, key: str) -> int | None:
        return next((value for name, value in self.counts or () if name == key), None)


_URL = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.|mailto:|data:|tel:)\S+", re.I)
_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
# Unknown categories remain in the total, without creating speculative devices.
DEVICE_CATEGORIES = frozenset({"appliances", "electronics", "utility", "utilities", "safety"})


def bounded_text(value: str | None) -> str | None:
    """Only named text fields, bounded and stripped of URLs and controls."""
    if value is None:
        return None
    value = _URL.sub("", value)
    value = "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)
    return " ".join(value.split())[:255] or None


def inventory_data(response: HomeDetailsResponse) -> ReadData:
    rooms = {
        room.id: bounded_text(room.name) for story in response.home.stories for room in story.rooms
    }
    records: list[InventoryRecord] = []
    seen: set[str] = set()
    for entity in response.entities:
        if entity.id in seen:
            continue
        seen.add(entity.id)
        if entity.category.casefold().strip() not in DEVICE_CATEGORIES or not _ID.fullmatch(
            entity.id
        ):
            continue
        serial = entity.property_value("serial_number")
        room_ids = entity.rooms or [
            room_id for room_id, ids in response.room_entity_map.items() if entity.id in ids
        ]
        records.append(
            InventoryRecord(
                id=entity.id,
                name=bounded_text(entity.type) or "Casa inventory item",
                status=bounded_text(entity.status) or "unknown",
                manufacturer=bounded_text(entity.make.name) if entity.make else None,
                model=bounded_text(entity.model.name) if entity.model else None,
                serial_number=bounded_text(serial) if isinstance(serial, str) else None,
                room=next((rooms[r] for r in room_ids if rooms.get(r)), None),
            )
        )
    return ReadData(counts=(("inventory_count", len(seen)),), inventory=tuple(records))


def request_data(response: RequestsResponse) -> ReadData:
    """Count fetched cards only. Duplicate flags combine by explicit OR."""
    lane = response.requests
    cards = [*lane.needs_review, *(card for section in lane.sections for card in section.items)]
    review = {card.id for card in lane.needs_review}
    unread = {card.id for card in cards if card.has_unread_messages}
    reply = {card.id for card in cards if card.concierge_awaiting_reply}
    scheduling = {card.id for card in cards if card.pending_schedule_prompt_count > 0}
    return ReadData(
        counts=(
            ("requests_needs_review", len(review)),
            ("requests_unread_fetched", len(unread)),
            ("requests_awaiting_reply", len(reply)),
            ("requests_scheduling_approval", len(scheduling)),
            ("requests_need_attention", len(review | unread | reply | scheduling)),
        )
    )


def maintenance_data(categories: list[AutopilotCategory]) -> ReadData:
    plans = {plan.plan_id: plan for category in categories for plan in category.plans}
    return ReadData(
        counts=(
            ("maintenance_enabled", sum(plan.enabled for plan in plans.values())),
            ("maintenance_eligible", len(plans)),
        )
    )


def history_data(response: InventoryServiceHistoryResponse) -> ReadData:
    """Only typed history_items. No invented dates or opaque plan counts."""
    return ReadData(counts=(("service_history_fetched", len(response.history_items)),))


def mail_data(response: MailboxMailItemsResponse) -> ReadData:
    unread = {
        item.id for item in response.mail_items if item.archived_at is None and item.read_at is None
    }
    return ReadData(counts=(("mail_unread_fetched", len(unread)),))


def package_data(response: PackagesResponse) -> ReadData:
    # The SDK uses an explicit archived bool, not mail's archived_at timestamp.
    packages = {item.id for item in response.packages if not item.archived}
    return ReadData(counts=(("packages_nonarchived", len(packages)),))


def service_calendar_data(
    response: VisitsCalendarResponse, start: datetime, end: datetime
) -> ReadData:
    """Provisional seconds codec. Reject milliseconds, reversed and implausible bounds."""
    events: list[CalendarEvent] = []
    seen: set[str] = set()
    for item in response.events:
        if (
            not _ID.fullmatch(item.id)
            or isinstance(item.start_time, bool)
            or isinstance(item.end_time, bool)
            or not 946684800 <= item.start_time < item.end_time <= 4102444800
        ):
            raise CasaResponseError("Invalid Casa service calendar event") from None
        event_start = datetime.fromtimestamp(item.start_time, UTC)
        event_end = datetime.fromtimestamp(item.end_time, UTC)
        if item.id in seen or event_start >= end or event_end <= start:
            continue
        seen.add(item.id)
        events.append(
            CalendarEvent(
                uid=item.id, start=event_start, end=event_end, summary="Casa service visit"
            )
        )
    return ReadData(
        counts=(),
        events=tuple(sorted(events, key=lambda event: event.start)),
        coverage_start=start,
        coverage_end=end,
    )
