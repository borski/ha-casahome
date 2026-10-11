"""The Casa integration."""

from __future__ import annotations

from casahome import CasaApi, CasaClient, FirebaseAuth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_EXPERIMENTAL,
    CONF_REFRESH_TOKEN,
    DEFAULT_ENABLE_EXPERIMENTAL,
)
from .coordinator import CasaCoordinator
from .experimental import CasaExperimentalCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

type CasaConfigEntry = ConfigEntry[CasaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: CasaConfigEntry) -> bool:
    """Set up Casa from a config entry."""
    loaded_options = dict(entry.options)
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
    coordinator = CasaCoordinator(hass, entry, api)

    @callback
    def _invalidate_token_persistence() -> None:
        coordinator.token_persistence_active = False

    entry.async_on_unload(_invalidate_token_persistence)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    if loaded_options.get(CONF_ENABLE_EXPERIMENTAL, DEFAULT_ENABLE_EXPERIMENTAL):
        for home in coordinator.data.homes:
            experimental = CasaExperimentalCoordinator(hass, entry, api, home.id, coordinator)
            # Plain refresh: an unverified endpoint failing must never block setup.
            await experimental.async_refresh()
            coordinator.experimental[home.id] = experimental

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Update listeners fire on every entry update, including token rotation.
    # Only an options change should reload.

    async def _async_reload_on_options_change(
        hass: HomeAssistant, updated: CasaConfigEntry
    ) -> None:
        if dict(updated.options) != loaded_options:
            await hass.config_entries.async_reload(updated.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options_change))
    if dict(entry.options) != loaded_options:
        hass.async_create_task(
            hass.config_entries.async_reload(entry.entry_id),
            name="Casa options changed during setup",
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CasaConfigEntry) -> bool:
    """Unload a config entry. The aiohttp session is HA's shared one, so it stays open."""
    coordinator = entry.runtime_data
    coordinator.token_persistence_active = False
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        coordinator.token_persistence_active = True
    return unloaded
