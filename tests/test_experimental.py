"""Experimental (unverified endpoint) coordinator and sensors. All mocked, no live Casa."""

from __future__ import annotations

import logging
import traceback
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from casahome import CasaApiError, CasaAuthError, CasaConnectionError, CasaResponseError
from casahome.models import MemberCredits, WeatherResponse
from homeassistant.config_entries import (
    RELOAD_AFTER_UPDATE_DELAY,
    SOURCE_REAUTH,
    ConfigEntryState,
)
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.casahome.const import CONF_ENABLE_EXPERIMENTAL, DOMAIN
from custom_components.casahome.experimental import CREDITS, WEATHER, CasaExperimentalCoordinator

from .conftest import HOME_ID, bootstrap_model

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
    await _enable_sensors(hass, entry, EXPERIMENTAL_KEYS)


async def _enable_sensors(
    hass: HomeAssistant, entry: MockConfigEntry, keys: tuple[str, ...]
) -> None:
    registry = er.async_get(hass)
    for key in keys:
        registry.async_update_entity(_entity_id(hass, key), disabled_by=None)
    # Let HA's registry handler perform its single delayed reload before polling tests.
    await hass.async_block_till_done()
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RELOAD_AFTER_UPDATE_DELAY + 1)
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


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

    await _enable_all(hass, experimental_entry)
    coordinator = _experimental(experimental_entry)
    api_ok.weather.side_effect = None
    api_ok.credits_summary.side_effect = None
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data.weather is not None


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    await _setup(hass, experimental_entry)
    await _enable_all(hass, experimental_entry)
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


@pytest.mark.parametrize("temperature", [float("nan"), float("inf"), float("-inf")])
async def test_nonfinite_temperature_is_unavailable(
    hass: HomeAssistant,
    api_ok: MagicMock,
    experimental_entry: MockConfigEntry,
    temperature: float,
) -> None:
    api_ok.weather.return_value = weather().model_copy(update={"temperature_degrees": temperature})
    await _setup(hass, experimental_entry)
    await _enable_all(hass, experimental_entry)

    state = hass.states.get(_entity_id(hass, "weather_temperature"))
    assert state is not None and state.state == STATE_UNAVAILABLE
    condition = hass.states.get(_entity_id(hass, "weather_condition"))
    assert condition is not None and condition.state == "cloudy"

    api_ok.weather.return_value = weather()
    await _experimental(experimental_entry).async_refresh()
    state = hass.states.get(_entity_id(hass, "weather_temperature"))
    assert state is not None and float(state.state) == pytest.approx(16.39, abs=0.01)


@pytest.mark.parametrize(
    ("keys", "endpoint"),
    [
        (("weather_temperature",), WEATHER),
        (("weather_condition",), WEATHER),
        (("credits_available",), CREDITS),
        (("handyman_hourly_rate",), CREDITS),
    ],
)
async def test_polling_fetches_only_active_endpoint_families(
    hass: HomeAssistant,
    api_ok: MagicMock,
    experimental_entry: MockConfigEntry,
    keys: tuple[str, ...],
    endpoint: str,
) -> None:
    await _setup(hass, experimental_entry)
    await _enable_sensors(hass, experimental_entry, keys)
    coordinator = _experimental(experimental_entry)
    assert set(coordinator.async_contexts()) == {endpoint}
    api_ok.weather.reset_mock()
    api_ok.credits_summary.reset_mock()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()

    assert api_ok.weather.await_count == (1 if endpoint == WEATHER else 0)
    assert api_ok.credits_summary.await_count == (1 if endpoint == CREDITS else 0)


async def test_disabled_entities_do_not_poll_after_initial_probe(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    await _setup(hass, experimental_entry)
    coordinator = _experimental(experimental_entry)
    assert not list(coordinator.async_contexts())
    api_ok.weather.reset_mock()
    api_ok.credits_summary.reset_mock()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=46))
    await hass.async_block_till_done()
    await coordinator.async_refresh()

    api_ok.weather.assert_not_called()
    api_ok.credits_summary.assert_not_called()
    assert coordinator._unsub_refresh is None


async def test_missing_active_family_does_not_fetch_disabled_entities_family(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    api_ok.weather.side_effect = CasaApiError(404, "not_found")
    await _setup(hass, experimental_entry)
    await _enable_sensors(hass, experimental_entry, ("weather_condition",))
    api_ok.weather.reset_mock()
    api_ok.credits_summary.reset_mock()
    coordinator = _experimental(experimental_entry)

    await coordinator.async_refresh()
    assert coordinator.last_update_success
    api_ok.weather.assert_not_called()
    api_ok.credits_summary.assert_not_called()


async def test_disabling_family_removes_its_context_and_main_listener(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    await _setup(hass, experimental_entry)
    await _enable_all(hass, experimental_entry)
    coordinator = _experimental(experimental_entry)
    main = experimental_entry.runtime_data
    listener_count = len(main._listeners)
    registry = er.async_get(hass)
    for key in ("weather_temperature", "weather_condition"):
        registry.async_update_entity(
            _entity_id(hass, key), disabled_by=er.RegistryEntryDisabler.USER
        )
    await hass.async_block_till_done()
    assert set(coordinator.async_contexts()) == {CREDITS}
    assert len(main._listeners) == listener_count - 2
    api_ok.weather.reset_mock()
    api_ok.credits_summary.reset_mock()

    await coordinator.async_refresh()
    api_ok.weather.assert_not_called()
    api_ok.credits_summary.assert_awaited_once_with(HOME_ID)

    for key in ("credits_available", "handyman_hourly_rate"):
        registry.async_update_entity(
            _entity_id(hass, key), disabled_by=er.RegistryEntryDisabler.USER
        )
    await hass.async_block_till_done()
    assert not list(coordinator.async_contexts())
    assert len(main._listeners) == listener_count - 4
    assert coordinator._unsub_refresh is None


async def test_home_removal_stops_polling_and_reappearance_recovers(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    await _setup(hass, experimental_entry)
    await _enable_all(hass, experimental_entry)
    main = experimental_entry.runtime_data
    coordinator = _experimental(experimental_entry)
    api_ok.weather.reset_mock()
    api_ok.credits_summary.reset_mock()

    api_ok.bootstrap.return_value = bootstrap_model(homes=[])
    main.async_set_updated_data(api_ok.bootstrap.return_value)
    for key in EXPERIMENTAL_KEYS:
        state = hass.states.get(_entity_id(hass, key))
        assert state is not None and state.state == STATE_UNAVAILABLE
    assert coordinator._unsub_refresh is None

    for minutes in (16, 32, 48):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
        await hass.async_block_till_done()
    await coordinator.async_refresh()
    api_ok.weather.assert_not_called()
    api_ok.credits_summary.assert_not_called()
    assert coordinator._unsub_refresh is None

    api_ok.bootstrap.return_value = bootstrap_model()
    main.async_set_updated_data(api_ok.bootstrap.return_value)
    await hass.async_block_till_done(wait_background_tasks=True)
    api_ok.weather.assert_awaited_once_with(HOME_ID)
    api_ok.credits_summary.assert_awaited_once_with(HOME_ID)
    assert coordinator._unsub_refresh is not None
    for key in EXPERIMENTAL_KEYS:
        state = hass.states.get(_entity_id(hass, key))
        assert state is not None and state.state != STATE_UNAVAILABLE

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=64))
    await hass.async_block_till_done()
    assert api_ok.weather.await_count == 2
    assert api_ok.credits_summary.await_count == 2


async def test_bootstrap_failure_immediately_marks_experimental_entities_unavailable(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    await _setup(hass, experimental_entry)
    await _enable_all(hass, experimental_entry)
    main = experimental_entry.runtime_data
    main.async_set_update_error(Exception("synthetic bootstrap failure"))

    for key in EXPERIMENTAL_KEYS:
        state = hass.states.get(_entity_id(hass, key))
        assert state is not None and state.state == STATE_UNAVAILABLE
    assert _experimental(experimental_entry)._unsub_refresh is None

    main.async_set_updated_data(bootstrap_model())
    await hass.async_block_till_done(wait_background_tasks=True)
    for key in EXPERIMENTAL_KEYS:
        state = hass.states.get(_entity_id(hass, key))
        assert state is not None and state.state != STATE_UNAVAILABLE


async def test_unload_removes_main_and_experimental_listeners(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    await _setup(hass, experimental_entry)
    await _enable_all(hass, experimental_entry)
    main = experimental_entry.runtime_data
    coordinator = _experimental(experimental_entry)
    assert len(list(coordinator.async_contexts())) == len(EXPERIMENTAL_KEYS)

    assert await hass.config_entries.async_unload(experimental_entry.entry_id)
    await hass.async_block_till_done()
    assert not main._listeners
    assert not coordinator._listeners
    assert coordinator._unsub_refresh is None

    api_ok.weather.reset_mock()
    api_ok.credits_summary.reset_mock()
    main.async_set_updated_data(bootstrap_model())
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=46))
    await hass.async_block_till_done()
    api_ok.weather.assert_not_called()
    api_ok.credits_summary.assert_not_called()


async def test_auth_error_suppresses_sensitive_exception_chain(
    hass: HomeAssistant, api_ok: MagicMock, experimental_entry: MockConfigEntry
) -> None:
    await _setup(hass, experimental_entry)
    await _enable_all(hass, experimental_entry)
    api_ok.weather.side_effect = CasaAuthError("synthetic-secret-token")

    with pytest.raises(ConfigEntryAuthFailed) as raised:
        await _experimental(experimental_entry)._async_update_data()

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert "synthetic-secret-token" not in "".join(traceback.format_exception(raised.value))
