"""Virtual Casa records linked to the home. Never merge real appliance devices."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from casahome.models import MemberSimpleHome
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaConfigEntry
from .const import DOMAIN
from .inventory_areas import InventoryAreaMarkers
from .inventory_registry import InventoryDeviceRegistry
from .read_coordinator import CasaReadCoordinator
from .read_entities import CasaAggregateEntity
from .read_models import InventoryRecord, ReadFamily


async def async_add_inventory_devices(
    entry: CasaConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    """Discovery callback isn't a polling context. All disabled means no inventory I/O.

    Area markers load before any refresh can run and flush on unload, so a reload
    never reads a store with a delayed save still pending.
    """
    main = entry.runtime_data
    markers = await InventoryAreaMarkers.async_load(main.hass, entry.entry_id)
    entry.async_on_unload(markers.async_flush)
    for home in main.data.homes:
        coordinator = main.read_coordinators[home.id][ReadFamily.INVENTORY]
        devices = InventoryDeviceRegistry(main.hass, entry.entry_id, home.id, markers)
        seen: set[str] = set()
        # Enabled registry records must own real entity contexts before the first
        # read, even when the home count is disabled. Placeholders carry no cached
        # inventory values and never create a separate discovery/polling context.
        registered_ids = re.compile(
            rf"{re.escape(home.id)}_inventory_([a-zA-Z0-9_-]{{1,128}})_status\Z"
        )
        for registered in er.async_entries_for_config_entry(
            er.async_get(main.hass), entry.entry_id
        ):
            if (
                registered.domain == "sensor"
                and registered.platform == DOMAIN
                and registered.disabled_by is None
                and (match := registered_ids.fullmatch(registered.unique_id)) is not None
            ):
                record_id = match.group(1)
                seen.add(record_id)
                add([CasaInventoryStatus(coordinator, home, record_id, devices)])

        @callback
        def discover(
            coordinator: CasaReadCoordinator = coordinator,
            home: MemberSimpleHome = home,
            seen: set[str] = seen,
            devices: InventoryDeviceRegistry = devices,
        ) -> None:
            if not coordinator.home_available or coordinator.data.counts is None:
                return
            devices.async_refresh(coordinator.data.inventory)
            records = [record for record in coordinator.data.inventory if record.id not in seen]
            seen.update(record.id for record in records)
            add(CasaInventoryStatus(coordinator, home, record.id, devices) for record in records)

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
        self,
        coordinator: CasaReadCoordinator,
        home: MemberSimpleHome,
        record_id: str,
        devices: InventoryDeviceRegistry,
    ) -> None:
        super().__init__(coordinator, home, "inventory_record_status")
        self.entity_description = SensorEntityDescription(
            key="inventory_record_status", translation_key="inventory_record_status"
        )
        self._record_id = record_id
        self._devices = devices
        self._attr_unique_id = f"{home.id}_inventory_{record_id}_status"

    @property
    def device_info(self) -> DeviceInfo:
        return self._devices.device_info(self._record_id, self.record)

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
        # Retain the legacy suggestion even when placement is ambiguous or skipped.
        return {"suggested_room": record.room if record else None}
