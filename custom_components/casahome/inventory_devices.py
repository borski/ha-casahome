"""Virtual Casa records linked to the home. Never merge real appliance devices."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from casahome.models import MemberSimpleHome
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaConfigEntry
from .const import DOMAIN
from .read_coordinator import CasaReadCoordinator
from .read_entities import CasaAggregateEntity
from .read_models import InventoryRecord, ReadFamily


def inventory_device_info(
    home_id: str, record: InventoryRecord, parent_id: str | None
) -> DeviceInfo:
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{home_id}_inventory_{record.id}")},
        entry_type=DeviceEntryType.SERVICE,
        name=f"Casa inventory {record.name}",
        manufacturer=record.manufacturer,
        model=record.model,
        serial_number=record.serial_number,
    )
    if parent_id is not None:
        info["via_device_id"] = parent_id
    return info


def add_inventory_devices(entry: CasaConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    """Discovery callback isn't a polling context. All disabled means no inventory I/O."""
    main = entry.runtime_data
    for home in main.data.homes:
        coordinator = main.read_coordinators[home.id][ReadFamily.INVENTORY]
        seen: set[str] = set()

        @callback
        def discover(
            coordinator: CasaReadCoordinator = coordinator,
            home: MemberSimpleHome = home,
            seen: set[str] = seen,
        ) -> None:
            if not coordinator.home_available or coordinator.data.counts is None:
                return
            registry = dr.async_get(coordinator.hass)
            for record in coordinator.data.inventory:
                if device := registry.async_get_device_by_identifier(
                    (DOMAIN, f"{home.id}_inventory_{record.id}"), entry.entry_id
                ):
                    # Refresh metadata even when the record's diagnostic is disabled.
                    # Never touch name_by_user or area_id.
                    registry.async_update_device(
                        device.id,
                        name=f"Casa inventory {record.name}",
                        manufacturer=record.manufacturer,
                        model=record.model,
                        serial_number=record.serial_number,
                    )
            records = [record for record in coordinator.data.inventory if record.id not in seen]
            seen.update(record.id for record in records)
            add(CasaInventoryStatus(coordinator, home, record) for record in records)

        coordinator.inventory_listener = discover

        @callback
        def stop_discovery(coordinator: CasaReadCoordinator = coordinator) -> None:
            coordinator.inventory_listener = None

        entry.async_on_unload(stop_discovery)


class CasaInventoryStatus(CasaAggregateEntity, SensorEntity):
    """One diagnostic record status, not 165 static property sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clipboard-check-outline"

    def __init__(
        self, coordinator: CasaReadCoordinator, home: MemberSimpleHome, record: InventoryRecord
    ) -> None:
        super().__init__(coordinator, home, "inventory_record_status")
        self.entity_description = SensorEntityDescription(
            key="inventory_record_status", translation_key="inventory_record_status"
        )
        self._record_id = record.id
        self._attr_unique_id = f"{home.id}_inventory_{record.id}_status"
        parent = dr.async_get(coordinator.hass).async_get_device_by_identifier(
            (DOMAIN, home.id), coordinator.config_entry.entry_id
        )
        self._parent_id = parent.id if parent else None
        self._attr_device_info = inventory_device_info(home.id, record, self._parent_id)

    @property
    def record(self) -> InventoryRecord | None:
        if not super().available:
            return None
        return next((r for r in self.coordinator.data.inventory if r.id == self._record_id), None)

    @property
    def available(self) -> bool:
        return self.record is not None

    @property
    def native_value(self) -> str | None:
        record = self.record
        return record.status if record else None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        record = self.record
        # HA's suggested_area creates areas. Keep this a suggestion, not an assignment.
        return {"suggested_room": record.room if record else None}

    @callback
    def _handle_coordinator_update(self) -> None:
        if record := self.record:
            self._attr_device_info = inventory_device_info(
                self.coordinator.home_id, record, self._parent_id
            )
        super()._handle_coordinator_update()
