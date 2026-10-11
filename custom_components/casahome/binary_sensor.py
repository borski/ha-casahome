"""Casa bootstrap diagnostics and aggregate attention binary sensors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from casahome.models import Bootstrap, MemberSimpleHome
from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaConfigEntry
from .entity import CasaHomeEntity
from .read_entities import add_read_binary_sensors

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class CasaBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Binary sensor description. A None value means unavailable."""

    value_fn: Callable[[MemberSimpleHome, Bootstrap], bool | None]


BINARY_SENSORS: tuple[CasaBinarySensorEntityDescription, ...] = (
    CasaBinarySensorEntityDescription(
        key="has_pending_agreements",
        translation_key="has_pending_agreements",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda home, _: len(home.pending_agreements) > 0,
    ),
    CasaBinarySensorEntityDescription(
        key="membership_active",
        translation_key="membership_active",
        icon="mdi:home-heart",
        value_fn=lambda home, _: home.membership_active,
    ),
    CasaBinarySensorEntityDescription(
        key="has_payment_method",
        translation_key="has_payment_method",
        icon="mdi:credit-card-check",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda home, _: home.has_payment_method,
    ),
    CasaBinarySensorEntityDescription(
        key="has_pending_post_onboarding",
        translation_key="has_pending_post_onboarding",
        icon="mdi:clipboard-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda home, _: home.has_pending_post_onboarding,
    ),
    CasaBinarySensorEntityDescription(
        key="calendar_sync_active",
        translation_key="calendar_sync_active",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _, bootstrap: bootstrap.calendar_sync_active,
    ),
    CasaBinarySensorEntityDescription(
        key="casa_collective",
        translation_key="casa_collective",
        icon="mdi:account-group",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _, bootstrap: bootstrap.is_casa_collective,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Casa binary sensors, one set per home."""
    coordinator = entry.runtime_data
    add_read_binary_sensors(entry, async_add_entities)
    async_add_entities(
        CasaBinarySensor(coordinator, home, description)
        for home in coordinator.data.homes
        for description in BINARY_SENSORS
    )


class CasaBinarySensor(CasaHomeEntity, BinarySensorEntity):
    """A Casa binary sensor."""

    entity_description: CasaBinarySensorEntityDescription

    def _value(self) -> bool | None:
        home = self.home
        if home is None:
            return None
        return self.entity_description.value_fn(home, self.coordinator.data)

    @property
    def available(self) -> bool:
        return super().available and self._value() is not None

    @property
    def is_on(self) -> bool | None:
        return self._value()

    @property
    def extra_state_attributes(self) -> Mapping[str, str] | None:
        return {"program": "beta"} if self.entity_description.key == "casa_collective" else None
