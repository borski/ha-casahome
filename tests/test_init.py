"""Setup, unload and config entry migration."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casahome.const import CONF_DEVICE_ID, CONF_REFRESH_TOKEN, DOMAIN

from .conftest import DEVICE_ID, HOME_ID, REFRESH_TOKEN, USER_ID


async def test_load_unload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    mock_api.bootstrap.assert_awaited_once()

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_reuses_stored_device_id(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_api.init_cls.call_args.kwargs["device_id"] == DEVICE_ID
    assert mock_auth.captured["args"][1] == REFRESH_TOKEN


PROMOTED_KEYS = (
    "weather_temperature",
    "weather_condition",
    "credits_available",
    "handyman_hourly_rate",
)


def _legacy_entry(options: dict[str, Any]) -> MockConfigEntry:
    """An entry as 0.1.0 stored it: minor version 1 with the experimental option."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Michael",
        unique_id=USER_ID,
        data={CONF_REFRESH_TOKEN: REFRESH_TOKEN, CONF_DEVICE_ID: DEVICE_ID},
        options=options,
        version=1,
        minor_version=1,
    )


async def test_migrate_option_on_drops_option_and_enables_integration_disabled_sensors(
    hass: HomeAssistant, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    entry = _legacy_entry({"enable_experimental": True})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    disabled_before = {
        ("sensor", "weather_temperature"): er.RegistryEntryDisabler.INTEGRATION,
        ("sensor", "credits_available"): er.RegistryEntryDisabler.INTEGRATION,
        ("sensor", "handyman_hourly_rate"): er.RegistryEntryDisabler.INTEGRATION,
        # A user's choice survives migration.
        ("sensor", "weather_condition"): er.RegistryEntryDisabler.USER,
        # Collective is promoted independently by the 1.3 migration.
        ("binary_sensor", "casa_collective"): er.RegistryEntryDisabler.INTEGRATION,
    }
    for (domain, key), disabler in disabled_before.items():
        registry.async_get_or_create(
            domain, DOMAIN, f"{HOME_ID}_{key}", config_entry=entry, disabled_by=disabler
        )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert (entry.version, entry.minor_version) == (1, 3)
    assert entry.options == {}

    def disabled_by(domain: str, key: str) -> er.RegistryEntryDisabler | None:
        entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{HOME_ID}_{key}")
        assert entity_id is not None
        registered = registry.async_get(entity_id)
        assert registered is not None
        return registered.disabled_by

    assert disabled_by("sensor", "weather_temperature") is None
    assert disabled_by("sensor", "credits_available") is None
    assert disabled_by("sensor", "handyman_hourly_rate") is None
    assert disabled_by("sensor", "weather_condition") is er.RegistryEntryDisabler.USER
    assert disabled_by("binary_sensor", "casa_collective") is None
    credits_id = registry.async_get_entity_id("sensor", DOMAIN, f"{HOME_ID}_credits_available")
    assert credits_id is not None
    state = hass.states.get(credits_id)
    assert state is not None and state.state == "3"


@pytest.mark.parametrize("options", [{"enable_experimental": False}, {}])
async def test_migrate_option_off_or_missing(
    hass: HomeAssistant, mock_api: MagicMock, mock_auth: MagicMock, options: dict[str, Any]
) -> None:
    entry = _legacy_entry(options)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert (entry.version, entry.minor_version) == (1, 3)
    assert entry.options == {}
    registry = er.async_get(hass)
    for key in PROMOTED_KEYS:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{HOME_ID}_{key}")
        assert entity_id is not None
        registered = registry.async_get(entity_id)
        assert registered is not None and registered.disabled_by is None


async def test_migrate_refuses_future_version(
    hass: HomeAssistant, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    entry = _legacy_entry({})
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, version=2, minor_version=1)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    mock_api.bootstrap.assert_not_called()
