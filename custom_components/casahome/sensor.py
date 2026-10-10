"""Casa sensors. Every value here comes from the verified bootstrap response."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from casahome.models import Bootstrap, MemberSimpleHome
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaConfigEntry
from .entity import CasaHomeEntity

PARALLEL_UPDATES = 0

HOME_STATUSES = ["onboarding", "active", "inactive"]


def _badge(feature: str) -> Callable[[MemberSimpleHome, Bootstrap], int | None]:
    def _value(home: MemberSimpleHome, _: Bootstrap) -> int | None:
        if home.badge_counts is None:
            return None
        return home.badge_counts.features.get(feature, 0)

    return _value


def _status(home: MemberSimpleHome, _: Bootstrap) -> str | None:
    value = str(getattr(home.status, "value", home.status))
    # Unknown upstream values read as unknown rather than breaking the enum.
    return value if value in HOME_STATUSES else None


@dataclass(frozen=True, kw_only=True)
class CasaSensorEntityDescription(SensorEntityDescription):
    """Sensor description with a value function over the bootstrap data."""

    value_fn: Callable[[MemberSimpleHome, Bootstrap], Any]
    available_fn: Callable[[MemberSimpleHome, Bootstrap], bool] = lambda _h, _b: True
    attrs_fn: Callable[[MemberSimpleHome, Bootstrap], Mapping[str, Any]] | None = None


def _has_badges(home: MemberSimpleHome, _: Bootstrap) -> bool:
    return home.badge_counts is not None


SENSORS: tuple[CasaSensorEntityDescription, ...] = (
    CasaSensorEntityDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=HOME_STATUSES,
        value_fn=_status,
    ),
    CasaSensorEntityDescription(
        key="plan_state",
        translation_key="plan_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda home, _: home.plan_state,
    ),
    CasaSensorEntityDescription(
        key="requests_badge",
        translation_key="requests_badge",
        icon="mdi:clipboard-list",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_badge("requests"),
        available_fn=_has_badges,
    ),
    CasaSensorEntityDescription(
        key="recurring_vendors_badge",
        translation_key="recurring_vendors_badge",
        icon="mdi:account-wrench",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_badge("recurring_vendors"),
        available_fn=_has_badges,
    ),
    CasaSensorEntityDescription(
        key="mailbox_badge",
        translation_key="mailbox_badge",
        icon="mdi:mailbox",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_badge("mailbox"),
        available_fn=_has_badges,
    ),
    CasaSensorEntityDescription(
        key="badge_total",
        translation_key="badge_total",
        icon="mdi:bell-badge",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda home, _: home.badge_counts.total if home.badge_counts else None,
        available_fn=_has_badges,
    ),
    CasaSensorEntityDescription(
        key="pending_agreements",
        translation_key="pending_agreements",
        icon="mdi:file-sign",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda home, _: len(home.pending_agreements),
        # Names only. Agreement content never leaves the SDK.
        attrs_fn=lambda home, _: {"names": [a.name for a in home.pending_agreements]},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Casa sensors, one set per home."""
    coordinator = entry.runtime_data
    async_add_entities(
        CasaSensor(coordinator, home, description)
        for home in coordinator.data.homes
        for description in SENSORS
    )


class CasaSensor(CasaHomeEntity, SensorEntity):
    """A Casa sensor."""

    entity_description: CasaSensorEntityDescription

    @property
    def available(self) -> bool:
        home = self.home
        return (
            super().available
            and home is not None
            and self.entity_description.available_fn(home, self.coordinator.data)
        )

    @property
    def native_value(self) -> Any:
        home = self.home
        if home is None:
            return None
        return self.entity_description.value_fn(home, self.coordinator.data)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        home = self.home
        if home is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(home, self.coordinator.data)
