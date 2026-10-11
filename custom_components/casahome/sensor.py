"""Casa sensors. Every value here comes from the verified bootstrap response."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any

from casahome.models import Bootstrap, MemberSimpleHome
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CasaConfigEntry
from .entity import CasaHomeEntity, home_device_info
from .experimental import (
    CREDITS,
    WEATHER,
    CasaExperimentalCoordinator,
    ExperimentalData,
    ExperimentalEndpoint,
)

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


def _temperature_unit(data: ExperimentalData) -> str | None:
    # temperature_units is a free string upstream. Its values were never observed.
    units = (data.weather.temperature_units if data.weather else "").strip().lower()
    if units in ("f", "fahrenheit", "°f", "imperial"):
        return UnitOfTemperature.FAHRENHEIT
    if units in ("c", "celsius", "°c", "metric"):
        return UnitOfTemperature.CELSIUS
    return None


def _condition(data: ExperimentalData) -> str | None:
    if data.weather is None:
        return None
    return str(getattr(data.weather.condition_type, "value", data.weather.condition_type))


def _temperature(data: ExperimentalData) -> float | None:
    if data.weather is None or not isfinite(data.weather.temperature_degrees):
        return None
    return data.weather.temperature_degrees


@dataclass(frozen=True, kw_only=True)
class CasaExperimentalSensorEntityDescription(SensorEntityDescription):
    """Sensor over an unverified endpoint. Off by default, behind the option gate too."""

    value_fn: Callable[[ExperimentalData], Any]
    endpoint: ExperimentalEndpoint
    unit_fn: Callable[[ExperimentalData], str | None] | None = None
    entity_registry_enabled_default: bool = False


EXPERIMENTAL_SENSORS: tuple[CasaExperimentalSensorEntityDescription, ...] = (
    CasaExperimentalSensorEntityDescription(
        key="weather_temperature",
        endpoint=WEATHER,
        translation_key="weather_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_temperature,
        unit_fn=_temperature_unit,
    ),
    CasaExperimentalSensorEntityDescription(
        key="weather_condition",
        endpoint=WEATHER,
        translation_key="weather_condition",
        icon="mdi:weather-partly-cloudy",
        value_fn=_condition,
    ),
    CasaExperimentalSensorEntityDescription(
        key="credits_available",
        endpoint=CREDITS,
        translation_key="credits_available",
        icon="mdi:ticket-percent",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.credits.total_available if d.credits else None,
    ),
    CasaExperimentalSensorEntityDescription(
        key="handyman_hourly_rate",
        endpoint=CREDITS,
        translation_key="handyman_hourly_rate",
        device_class=SensorDeviceClass.MONETARY,
        # Field is named *_cents. Casa is US only, so USD is an assumption, not observed.
        native_unit_of_measurement="USD",
        suggested_display_precision=2,
        value_fn=lambda d: d.credits.handyman_hourly_rate_cents / 100 if d.credits else None,
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
    # Empty unless the experimental option is on.
    async_add_entities(
        CasaExperimentalSensor(experimental, home, description)
        for home in coordinator.data.homes
        if (experimental := coordinator.experimental.get(home.id)) is not None
        for description in EXPERIMENTAL_SENSORS
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


class CasaExperimentalSensor(CoordinatorEntity[CasaExperimentalCoordinator], SensorEntity):
    """A sensor fed by an unverified Casa endpoint."""

    _attr_has_entity_name = True
    entity_description: CasaExperimentalSensorEntityDescription

    def __init__(
        self,
        coordinator: CasaExperimentalCoordinator,
        home: MemberSimpleHome,
        description: CasaExperimentalSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, context=description.endpoint)
        self.entity_description = description
        self._attr_unique_id = f"{home.id}_{description.key}"
        self._attr_device_info = home_device_info(home)

    async def async_added_to_hass(self) -> None:
        """Listen to bootstrap as well as experimental endpoint updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.main_coordinator.async_add_listener(
                self._handle_main_coordinator_update
            )
        )
        self.coordinator.async_update_home()

    @callback
    def _handle_main_coordinator_update(self) -> None:
        """Immediately reflect home removal or bootstrap failure."""
        self.coordinator.async_update_home()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if (
            not super().available
            or not self.coordinator.home_available
            or self.coordinator.data is None
        ):
            return False
        if self.entity_description.unit_fn and self.native_unit_of_measurement is None:
            return False
        return self.entity_description.value_fn(self.coordinator.data) is not None

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def native_unit_of_measurement(self) -> str | None:
        fn = self.entity_description.unit_fn
        if fn is None:
            return self.entity_description.native_unit_of_measurement
        return fn(self.coordinator.data) if self.coordinator.data else None
