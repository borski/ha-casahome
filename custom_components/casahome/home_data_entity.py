"""Shared entity lifecycle for the context-driven todo and calendar platforms."""

from __future__ import annotations

from casahome.models import MemberSimpleHome
from homeassistant.core import callback
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .entity import home_device_info
from .home_coordinator import CasaHomeCoordinator, HomeEndpoint


class CasaHomeReadEntity(CoordinatorEntity[CasaHomeCoordinator]):
    """Listen to home authorization as well as the endpoint's safe projection."""

    _attr_has_entity_name = True
    _attr_supported_features = 0

    def __init__(
        self,
        coordinator: CasaHomeCoordinator,
        home: MemberSimpleHome,
        description: EntityDescription,
        endpoint: HomeEndpoint,
    ) -> None:
        super().__init__(coordinator, context=endpoint)
        self.entity_description = description
        self._attr_unique_id = f"{home.id}_{description.key}"
        self._attr_device_info = home_device_info(home)

    async def async_added_to_hass(self) -> None:
        """Main updates invalidate cached data before it can be shown again."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.main_coordinator.async_add_listener(
                self._handle_main_coordinator_update
            )
        )
        self.coordinator.async_update_home()

    @callback
    def _handle_main_coordinator_update(self) -> None:
        self.coordinator.async_update_home()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.home_available
            and self.coordinator.data is not None
        )
