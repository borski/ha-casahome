"""Read-only regular service calendar with explicit cached window coverage."""

from __future__ import annotations

from collections.abc import Mapping
from copy import copy
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import CasaConfigEntry
from .read_entities import CasaAggregateEntity
from .read_models import ReadFamily


def add_service_calendars(entry: CasaConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    main = entry.runtime_data
    add(
        CasaServiceCalendar(
            main.read_coordinators[home.id][ReadFamily.CALENDAR], home, "service_visits"
        )
        for home in main.data.homes
    )


class CasaServiceCalendar(CasaAggregateEntity, CalendarEntity):
    """A rolling cache, not an unbounded historical calendar API."""

    _attr_supported_features = 0
    _attr_icon = "mdi:calendar-wrench"

    @property
    def available(self) -> bool:
        end = self.coordinator.data.coverage_end
        return super().available and end is not None and end > dt_util.utcnow()

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        data = self.coordinator.data
        return {
            "item_schema": "provisional",
            "timestamp_codec": "epoch_seconds_assumed",
            "coverage_start": data.coverage_start.isoformat()
            if self.available and data.coverage_start
            else None,
            "coverage_end": data.coverage_end.isoformat()
            if self.available and data.coverage_end
            else None,
        }

    @property
    def event(self) -> CalendarEvent | None:
        if not self.available:
            return None
        now = dt_util.utcnow()
        return next(
            (
                copy(event)
                for event in self.coordinator.data.events
                if event.end_datetime_local > now
            ),
            None,
        )

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        if start_date.utcoffset() is None or end_date.utcoffset() is None:
            raise HomeAssistantError("Casa calendar needs an aware window")
        start = start_date.astimezone(UTC)
        end = end_date.astimezone(UTC)
        if end <= start:
            raise HomeAssistantError("Casa calendar needs end after start")
        if not self.available:
            raise HomeAssistantError("Casa service calendar is unavailable")
        data = self.coordinator.data
        if (
            data.coverage_start is None
            or data.coverage_end is None
            or start < data.coverage_start
            or end > data.coverage_end
        ):
            raise HomeAssistantError("Casa service calendar window is outside fetched coverage")
        return [
            copy(event)
            for event in data.events
            if event.start_datetime_local.astimezone(UTC) < end
            and event.end_datetime_local.astimezone(UTC) > start
        ]
