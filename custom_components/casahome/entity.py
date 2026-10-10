"""Shared base for Casa entities. One device per Casa home."""

from __future__ import annotations

from casahome.models import MemberSimpleHome
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONFIGURATION_URL, DOMAIN, MANUFACTURER, MODEL
from .coordinator import CasaCoordinator


def _device_name(home: MemberSimpleHome) -> str:
    # display_address joins the parts with commas, so blank parts leave " , ,  ".
    name = home.display_address.strip(" ,")
    return name if any(c.isalnum() for c in name) else "Casa Home"


def home_device_info(home: MemberSimpleHome) -> DeviceInfo:
    """Device for one Casa home. Name is the street address, the member's own data."""
    return DeviceInfo(
        identifiers={(DOMAIN, home.id)},
        manufacturer=MANUFACTURER,
        model=MODEL,
        name=_device_name(home),
        configuration_url=CONFIGURATION_URL,
        entry_type=DeviceEntryType.SERVICE,
    )


class CasaHomeEntity(CoordinatorEntity[CasaCoordinator]):
    """An entity bound to one home in the bootstrap response."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CasaCoordinator,
        home: MemberSimpleHome,
        description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._home_id = home.id
        self._attr_unique_id = f"{home.id}_{description.key}"
        self._attr_device_info = home_device_info(home)

    @property
    def home(self) -> MemberSimpleHome | None:
        """The current home, or None if Casa stopped returning it."""
        for home in self.coordinator.data.homes:
            if home.id == self._home_id:
                return home
        return None

    @property
    def available(self) -> bool:
        return super().available and self.home is not None
