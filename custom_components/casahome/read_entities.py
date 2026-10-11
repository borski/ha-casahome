"""Scalar/count entities over isolated aggregate reads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from casahome.models import MemberSimpleHome
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CasaConfigEntry
from .entity import home_device_info
from .read_coordinator import CasaReadCoordinator
from .read_models import ReadFamily

READ_SENSORS: tuple[tuple[ReadFamily, str], ...] = (
    (ReadFamily.INVENTORY, "inventory_count"),
    (ReadFamily.REQUESTS, "requests_needs_review"),
    (ReadFamily.REQUESTS, "requests_unread_fetched"),
    (ReadFamily.REQUESTS, "requests_awaiting_reply"),
    (ReadFamily.REQUESTS, "requests_scheduling_approval"),
    (ReadFamily.MAINTENANCE, "maintenance_enabled"),
    (ReadFamily.MAINTENANCE, "maintenance_eligible"),
    (ReadFamily.HISTORY, "service_history_fetched"),
    (ReadFamily.MAIL, "mail_unread_fetched"),
    (ReadFamily.PACKAGES, "packages_nonarchived"),
)
READ_BINARY_SENSORS: tuple[tuple[ReadFamily, str, str], ...] = (
    (ReadFamily.REQUESTS, "requests_attention", "requests_need_attention"),
    (ReadFamily.MAIL, "mail_attention", "mail_unread_fetched"),
    (ReadFamily.PACKAGES, "packages_present", "packages_nonarchived"),
)


class CasaAggregateEntity(CoordinatorEntity[CasaReadCoordinator]):
    """Availability follows a successful current-generation projection."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CasaReadCoordinator, home: MemberSimpleHome, key: str) -> None:
        super().__init__(coordinator, context=coordinator.family)
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{home.id}_{key}"
        self._attr_device_info = home_device_info(home)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.home_available
            and self.coordinator.data.counts is not None
        )

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        return {"scope": "fetched_response"}


def add_read_sensors(entry: CasaConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    main = entry.runtime_data
    add(
        CasaCountSensor(main.read_coordinators[home.id][family], home, key)
        for home in main.data.homes
        for family, key in READ_SENSORS
    )


def add_read_binary_sensors(entry: CasaConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    main = entry.runtime_data
    add(
        CasaReadBinarySensor(main.read_coordinators[home.id][family], home, key, count_key)
        for home in main.data.homes
        for family, key, count_key in READ_BINARY_SENSORS
    )


class CasaCountSensor(CasaAggregateEntity, SensorEntity):
    """Fetched count, never a service-global total or a live appliance measurement."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:counter"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.count(self._key) if self.available else None


class CasaReadBinarySensor(CasaAggregateEntity, BinarySensorEntity):
    """Conservative explicit attention flags. Packages means present, not delivered."""

    _attr_icon = "mdi:bell-outline"

    def __init__(
        self,
        coordinator: CasaReadCoordinator,
        home: MemberSimpleHome,
        key: str,
        count_key: str,
    ) -> None:
        super().__init__(coordinator, home, key)
        self._count_key = count_key

    @property
    def is_on(self) -> bool | None:
        count = self.coordinator.data.count(self._count_key) if self.available else None
        return count > 0 if count is not None else None
