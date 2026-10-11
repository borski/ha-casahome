"""Read-only known onboarding visits, not the general visit calendar."""

from __future__ import annotations

from copy import copy
from datetime import UTC, datetime

from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityDescription,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import CasaConfigEntry
from .home_coordinator import ONBOARDING
from .home_data_entity import CasaHomeReadEntity

PARALLEL_UPDATES = 0
ONBOARDING_CALENDAR = CalendarEntityDescription(
    key="onboarding_visits", translation_key="onboarding_visits", icon="mdi:calendar-account"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one onboarding-only calendar per home."""
    coordinator = entry.runtime_data
    async_add_entities(
        CasaOnboardingCalendar(
            coordinator.home_coordinators[home.id], home, ONBOARDING_CALENDAR, ONBOARDING
        )
        for home in coordinator.data.homes
    )


class CasaOnboardingCalendar(CasaHomeReadEntity, CalendarEntity):
    """Filter a shared cached read locally. Queries never call booking APIs."""

    entity_description: CalendarEntityDescription

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.onboarding is not None

    @property
    def event(self) -> CalendarEvent | None:
        if not self.available:
            return None
        assert self.coordinator.data.onboarding is not None
        now = dt_util.utcnow()
        return next(
            (
                copy(event)
                for event in self.coordinator.data.onboarding
                if event.end_datetime_local > now
            ),
            None,
        )

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Events overlapping an aware half-open window, including ongoing visits."""
        if start_date.utcoffset() is None or end_date.utcoffset() is None:
            raise HomeAssistantError("Casa calendar needs an aware window with end after start")
        # Same-zone datetime comparison ignores DST folds. Compare instants.
        start_date = start_date.astimezone(UTC)
        end_date = end_date.astimezone(UTC)
        if end_date <= start_date:
            raise HomeAssistantError("Casa calendar needs an aware window with end after start")
        if not self.available:
            raise HomeAssistantError("Casa onboarding calendar is unavailable")
        assert self.coordinator.data.onboarding is not None
        return [
            copy(event)
            for event in self.coordinator.data.onboarding
            if event.start_datetime_local.astimezone(UTC) < end_date
            and event.end_datetime_local.astimezone(UTC) > start_date
        ]
