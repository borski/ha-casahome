"""Experimental (unverified endpoint) coordinator and sensors. All mocked, no live Casa."""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from casahome import CasaApiError, CasaAuthError, CasaConnectionError, CasaResponseError
from casahome.models import MemberCredits, WeatherResponse
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.casahome.const import CONF_ENABLE_EXPERIMENTAL, DOMAIN
from custom_components.casahome.experimental import CasaExperimentalCoordinator

from .conftest import HOME_ID

EXPERIMENTAL_KEYS = (
    "weather_temperature",
    "weather_condition",
    "credits_available",
    "handyman_hourly_rate",
)


def weather(units: str = "fahrenheit") -> WeatherResponse:
    return WeatherResponse(
        temperature_degrees=61.5, temperature_units=units, condition_type="cloudy"
    )


def credits() -> MemberCredits:
    return MemberCredits(home_id=HOME_ID, total_available=3, handyman_hourly_rate_cents=8500)


@pytest.fixture
def experimental_entry(config_entry: MockConfigEntry) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=config_entry.title,
        unique_id=config_entry.unique_id,
        data=dict(config_entry.data),
        options={CONF_ENABLE_EXPERIMENTAL: True},
    )


@pytest.fixture
def api_ok(mock_api: MagicMock, mock_auth: MagicMock) -> MagicMock:
    mock_api.weather.return_value = weather()
    mock_api.credits_summary.return_value = credits()
    return mock_api


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{HOME_ID}_{key}")
    assert entity_id is not None
    return entity_id


async def _enable_all(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    for key in EXPERIMENTAL_KEYS:
        registry.async_update_entity(_entity_id(hass, key), disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


def _experimental(entry: MockConfigEntry) -> CasaExperimentalCoordinator:
    coordinator: CasaExperimentalCoordinator = entry.runtime_data.experimental[HOME_ID]
    return coordinator


async def test_option_off_makes_zero_unverified_calls(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=46))
    await hass.async_block_till_done()

    assert api_ok.bootstrap.await_count >= 2  # verified polling did keep running
    api_ok.weather.assert_not_called()
    api_ok.credits_summary.assert_not_called()
    api_ok.badge_counts.assert_not_called()
    assert config_entry.runtime_data.experimental == {}
    registry = er.async_get(hass)
    for key in EXPERIMENTAL_KEYS:
        assert registry.async_get_entity_id("sensor", DOMAIN, f"{HOME_ID}_{key}") is None


async def test_option_on_creates_disabled_entities(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    await _setup(hass, experimental_entry)

    registry = er.async_get(hass)
    for key in EXPERIMENTAL_KEYS:
        entry = registry.async_get(_entity_id(hass, key))
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    api_ok.weather.assert_awaited_once_with(HOME_ID)
    api_ok.credits_summary.assert_awaited_once_with(HOME_ID)
    # Badges already come from bootstrap. The unverified badge endpoint is never called.
    api_ok.badge_counts.assert_not_called()


async def test_enabled_values(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    await _setup(hass, experimental_entry)
    await _enable_all(hass, experimental_entry)

    temperature = hass.states.get(_entity_id(hass, "weather_temperature"))
    assert temperature is not None
    # Test hass is metric. 61.5 °F reads as F upstream, HA displays it converted.
    assert float(temperature.state) == pytest.approx(16.39, abs=0.01)
    assert temperature.attributes["unit_of_measurement"] == "°C"
    condition = hass.states.get(_entity_id(hass, "weather_condition"))
    assert condition is not None and condition.state == "cloudy"
    available = hass.states.get(_entity_id(hass, "credits_available"))
    assert available is not None and available.state == "3"
    rate = hass.states.get(_entity_id(hass, "handyman_hourly_rate"))
    assert rate is not None
    assert float(rate.state) == pytest.approx(85.0)
    assert rate.attributes["unit_of_measurement"] == "USD"


async def test_unknown_temperature_units_is_unavailable(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    api_ok.weather.return_value = weather(units="kelvin-ish")
    await _setup(hass, experimental_entry)
    await _enable_all(hass, experimental_entry)

    state = hass.states.get(_entity_id(hass, "weather_temperature"))
    assert state is not None and state.state == STATE_UNAVAILABLE
    condition = hass.states.get(_entity_id(hass, "weather_condition"))
    assert condition is not None and condition.state == "cloudy"


@pytest.mark.parametrize("status", [404, 405])
async def test_missing_endpoint_disables_and_logs_once(
    hass: HomeAssistant,
    api_ok: MagicMock,
    experimental_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    api_ok.weather.side_effect = CasaApiError(status, "not_found")
    caplog.set_level(logging.WARNING)
    await _setup(hass, experimental_entry)
    await _enable_all(hass, experimental_entry)
    coordinator = _experimental(experimental_entry)
    calls_after_reload = api_ok.weather.await_count

    for _ in range(3):
        await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.disabled == {"weather"}
    assert api_ok.weather.await_count == calls_after_reload
    assert coordinator.last_update_success  # credits still worked
    warnings = [
        r
        for r in caplog.records
        if r.name == "custom_components.casahome.experimental" and r.levelno == logging.WARNING
    ]
    # One per entry lifetime. _enable_all reloads, which builds a fresh coordinator.
    assert len(warnings) == 2
    assert f"HTTP {status}" in warnings[-1].getMessage()

    temperature = hass.states.get(_entity_id(hass, "weather_temperature"))
    assert temperature is not None and temperature.state == STATE_UNAVAILABLE
    available = hass.states.get(_entity_id(hass, "credits_available"))
    assert available is not None and available.state == "3"


async def test_both_endpoints_missing_is_not_a_failure(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    api_ok.weather.side_effect = CasaApiError(404, "not_found")
    api_ok.credits_summary.side_effect = CasaApiError(404, "not_found")
    await _setup(hass, experimental_entry)

    coordinator = _experimental(experimental_entry)
    assert coordinator.disabled == {"weather", "credits_summary"}
    assert coordinator.last_update_success


@pytest.mark.parametrize(
    "error",
    [
        CasaConnectionError("down"),
        TimeoutError(),
        CasaResponseError("drift"),
        CasaApiError(500, "boom"),
    ],
)
async def test_transient_errors_keep_endpoint_enabled(
    hass: HomeAssistant,
    api_ok: MagicMock,
    experimental_entry: MockConfigEntry,
    error: Exception,
) -> None:
    api_ok.weather.side_effect = error
    api_ok.credits_summary.side_effect = error
    await _setup(hass, experimental_entry)

    assert experimental_entry.state is ConfigEntryState.LOADED  # never blocks setup
    coordinator = _experimental(experimental_entry)
    assert not coordinator.last_update_success
    assert coordinator.disabled == set()

    api_ok.weather.side_effect = None
    api_ok.credits_summary.side_effect = None
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data.weather is not None


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    await _setup(hass, experimental_entry)
    api_ok.weather.side_effect = CasaAuthError("TOKEN_EXPIRED")

    await _experimental(experimental_entry).async_refresh()
    await hass.async_block_till_done()

    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"].get("source") == SOURCE_REAUTH
    ]
    assert len(flows) == 1


async def test_errors_never_log_upstream_detail(
    hass: HomeAssistant,
    api_ok: MagicMock,
    experimental_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    api_ok.weather.side_effect = CasaApiError(404, "nf", display_message="secret-detail")
    api_ok.credits_summary.side_effect = CasaConnectionError("secret-connection-detail")
    await _setup(hass, experimental_entry)

    assert "secret-detail" not in caplog.text
    assert "secret-connection-detail" not in caplog.text
    assert "fake-refresh-token-not-real" not in caplog.text
