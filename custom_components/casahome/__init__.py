"""The Casa integration."""

from __future__ import annotations

import asyncio

from casahome import CasaApi, CasaClient, FirebaseAuth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_DEVICE_ID, CONF_REFRESH_TOKEN, LEGACY_CONF_ENABLE_EXPERIMENTAL
from .coordinator import CasaCoordinator
from .home_coordinator import CasaHomeCoordinator
from .inventory_areas import InventoryAreaMarkers
from .read_coordinator import CasaReadCoordinator
from .read_models import ReadFamily

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.TODO,
    Platform.CALENDAR,
]

# Sensors that sat behind the experimental option before minor version 2.
# Frozen history for migration, not the live sensor list.
_PROMOTED_SENSOR_KEYS = frozenset(
    {"weather_temperature", "weather_condition", "credits_available", "handyman_hourly_rate"}
)

type CasaConfigEntry = ConfigEntry[CasaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: CasaConfigEntry) -> bool:
    """Set up Casa from a config entry."""
    persisted_token = entry.data[CONF_REFRESH_TOKEN]
    coordinator: CasaCoordinator | None = None

    @callback
    def _persist_rotated_token(new_token: str) -> None:
        # Runs inside the SDK's refresh lock on the event loop. Sync and cheap only.
        nonlocal persisted_token
        if (
            coordinator is None
            or not coordinator.token_persistence_active
            or entry.data[CONF_REFRESH_TOKEN] != persisted_token
            or new_token == persisted_token
        ):
            return
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: new_token}
        )
        persisted_token = new_token

    session = async_get_clientsession(hass)
    auth = FirebaseAuth(
        session,
        persisted_token,
        on_refresh_token_rotated=_persist_rotated_token,
    )
    api = CasaApi(
        CasaClient(session, auth),
        device_id=entry.data[CONF_DEVICE_ID],
        timezone=hass.config.time_zone,
    )

    @callback
    def _resume_token_persistence() -> None:
        # A refresh may have rotated while writes were paused for unload. The SDK
        # won't repeat its callback for the same token, so reconcile explicitly.
        assert coordinator is not None
        coordinator.token_persistence_active = True
        _persist_rotated_token(auth.refresh_token)

    coordinator = CasaCoordinator(
        hass, entry, api, resume_token_persistence=_resume_token_persistence
    )

    @callback
    def _invalidate_token_persistence() -> None:
        coordinator.token_persistence_active = False

    entry.async_on_unload(_invalidate_token_persistence)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    for home in coordinator.data.homes:
        home_coordinator = CasaHomeCoordinator(hass, entry, api, home.id, coordinator)
        # Existing sensor unit setup needs the weather/credits probe before add.
        await home_coordinator.async_refresh()
        coordinator.home_coordinators[home.id] = home_coordinator
        coordinator.read_coordinators[home.id] = {
            family: CasaReadCoordinator(hass, entry, coordinator, home.id, family)
            for family in ReadFamily
        }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Entities have registered their contexts now. New read families never probe
    # disabled entities. Keep the historical weather/credits setup probe.
    for home_coordinator in coordinator.home_coordinators.values():
        # Plain refresh: per-home failures must never block bootstrap setup.
        await home_coordinator.async_refresh_new_platforms()

    async def start_reads() -> None:
        # Independent bounded reads run concurrently after bootstrap setup. HA owns
        # and cancels this task on unload. The shared SDK session stays open.
        async with asyncio.TaskGroup() as tasks:
            for families in coordinator.read_coordinators.values():
                for read_coordinator in families.values():
                    tasks.create_task(read_coordinator.async_start())

    entry.async_create_background_task(hass, start_reads(), name="Casa aggregate reads")
    return True


def _is_promoted_sensor(unique_id: str) -> bool:
    """Unique ids are ``{home_id}_{key}``."""
    return any(unique_id.endswith(f"_{key}") for key in _PROMOTED_SENSOR_KEYS)


async def async_migrate_entry(hass: HomeAssistant, entry: CasaConfigEntry) -> bool:
    """Retain 1.2 promotion and enable the Collective beta diagnostic in 1.3."""
    if entry.version > 1:
        # Downgrade from a future version. Refuse rather than guess.
        return False
    if entry.minor_version < 2:
        registry = er.async_get(hass)
        for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
            # Only undo the integration's old default. Respect anything a user disabled.
            if (
                registered.domain == Platform.SENSOR
                and registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
                and _is_promoted_sensor(registered.unique_id)
            ):
                registry.async_update_entity(registered.entity_id, disabled_by=None)
        options = {
            key: value
            for key, value in entry.options.items()
            if key != LEGACY_CONF_ENABLE_EXPERIMENTAL
        }
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    if entry.minor_version < 3:
        registry = er.async_get(hass)
        for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                registered.domain == Platform.BINARY_SENSOR
                and registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
                and registered.unique_id.endswith("_casa_collective")
            ):
                registry.async_update_entity(registered.entity_id, disabled_by=None)
        hass.config_entries.async_update_entry(entry, minor_version=3)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CasaConfigEntry) -> bool:
    """Unload a config entry. The aiohttp session is HA's shared one, so it stays open."""
    coordinator = entry.runtime_data
    coordinator.token_persistence_active = False
    unloaded = False
    try:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        return unloaded
    finally:
        if not unloaded:
            coordinator.async_resume_token_persistence()


async def async_remove_entry(hass: HomeAssistant, entry: CasaConfigEntry) -> None:
    """Deleting the integration leaves no inventory area markers behind."""
    await InventoryAreaMarkers.async_remove(hass, entry.entry_id)
