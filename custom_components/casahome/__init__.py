"""The Casa integration."""

from __future__ import annotations

from casahome import CasaApi, CasaClient, FirebaseAuth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_DEVICE_ID, CONF_REFRESH_TOKEN
from .coordinator import CasaCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

type CasaConfigEntry = ConfigEntry[CasaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: CasaConfigEntry) -> bool:
    """Set up Casa from a config entry."""

    @callback
    def _persist_rotated_token(new_token: str) -> None:
        # Runs inside the SDK's refresh lock on the event loop. Sync and cheap only.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: new_token}
        )

    session = async_get_clientsession(hass)
    auth = FirebaseAuth(
        session,
        entry.data[CONF_REFRESH_TOKEN],
        on_refresh_token_rotated=_persist_rotated_token,
    )
    api = CasaApi(
        CasaClient(session, auth),
        device_id=entry.data[CONF_DEVICE_ID],
        timezone=hass.config.time_zone,
    )
    coordinator = CasaCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Update listeners fire on every entry update, including token rotation.
    # Only an options change should reload.
    loaded_options = dict(entry.options)

    async def _async_reload_on_options_change(
        hass: HomeAssistant, updated: CasaConfigEntry
    ) -> None:
        if dict(updated.options) != loaded_options:
            await hass.config_entries.async_reload(updated.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options_change))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CasaConfigEntry) -> bool:
    """Unload a config entry. The aiohttp session is HA's shared one, so it stays open."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
