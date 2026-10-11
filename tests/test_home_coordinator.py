"""Per-home weather and credits coordinator and sensors. All mocked, no live Casa."""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from casahome import CasaApiError, CasaAuthError, CasaConnectionError, CasaResponseError
from casahome.models import MemberCredits, WeatherResponse
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.casahome.const import DOMAIN
from custom_components.casahome.home_coordinator import (
    CREDITS,
    WEATHER,
    CasaHomeCoordinator,
    HomeData,
)

from .conftest import HOME_ID, bootstrap_model

HOME_DATA_KEYS = (
    "weather_temperature",
    "weather_condition",
    "credits_available",
    "handyman_hourly_rate",
)


def weather(units: str = "F") -> WeatherResponse:
    # "F" and "cloudy" mirror the live shape. Condition is free text upstream too.
    return WeatherResponse(
        temperature_degrees=61.5, temperature_units=units, condition_type="cloudy"
    )


def credits() -> MemberCredits:
    return MemberCredits(home_id=HOME_ID, total_available=3, handyman_hourly_rate_cents=8500)


@pytest.fixture
def api_ok(mock_api: MagicMock, mock_auth: MagicMock) -> MagicMock:
    mock_api.weather.return_value = weather()
    mock_api.credits_summary.return_value = credits()
    return mock_api


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    # These tests isolate weather/credits. Expanded read families have their own
    # platform and lifecycle tests, and must not mask failures here.
    registry = er.async_get(hass)
    for domain, key in (("todo", "suggested_todos"), ("calendar", "onboarding_visits")):
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{HOME_ID}_{key}",
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{HOME_ID}_{key}")
    assert entity_id is not None
    return entity_id


async def _disable_sensors(hass: HomeAssistant, keys: tuple[str, ...]) -> None:
    """User-disable sensors. Disabling removes the entity without reloading the entry."""
    registry = er.async_get(hass)
    for key in keys:
        registry.async_update_entity(
            _entity_id(hass, key), disabled_by=er.RegistryEntryDisabler.USER
        )
    await hass.async_block_till_done()


def _home_coordinator(entry: MockConfigEntry) -> CasaHomeCoordinator:
    coordinator: CasaHomeCoordinator = entry.runtime_data.home_coordinators[HOME_ID]
    return coordinator


def _assert_home_data_unavailable(hass: HomeAssistant) -> None:
    for key in HOME_DATA_KEYS:
        state = hass.states.get(_entity_id(hass, key))
        assert state is not None and state.state == STATE_UNAVAILABLE


async def test_setup_creates_enabled_sensors_and_probes_once(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    registry = er.async_get(hass)
    for key in HOME_DATA_KEYS:
        entry = registry.async_get(_entity_id(hass, key))
        assert entry is not None
        assert entry.disabled_by is None
    rate = registry.async_get(_entity_id(hass, "handyman_hourly_rate"))
    assert rate is not None and rate.entity_category is EntityCategory.DIAGNOSTIC
    api_ok.weather.assert_awaited_once_with(HOME_ID)
    api_ok.credits_summary.assert_awaited_once_with(HOME_ID)
    # Badges already come from bootstrap. The separate badge endpoint is never called.
    api_ok.badge_counts.assert_not_called()
    assert set(_home_coordinator(config_entry).async_contexts()) == {WEATHER, CREDITS}


async def test_polls_every_fifteen_minutes(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    api_ok.weather.reset_mock()
    api_ok.credits_summary.reset_mock()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=14))
    await hass.async_block_till_done()
    api_ok.weather.assert_not_called()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()
    api_ok.weather.assert_awaited_once_with(HOME_ID)
    api_ok.credits_summary.assert_awaited_once_with(HOME_ID)


async def test_enabled_values(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

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


@pytest.mark.parametrize(
    ("units", "celsius"),
    [("F", 16.39), ("fahrenheit", 16.39), (" °F ", 16.39), ("C", 61.5), ("metric", 61.5)],
)
async def test_temperature_unit_spellings(
    hass: HomeAssistant,
    api_ok: MagicMock,
    config_entry: MockConfigEntry,
    units: str,
    celsius: float,
) -> None:
    api_ok.weather.return_value = weather(units=units)
    await _setup(hass, config_entry)

    state = hass.states.get(_entity_id(hass, "weather_temperature"))
    assert state is not None and float(state.state) == pytest.approx(celsius, abs=0.01)


async def test_unknown_temperature_units_is_unavailable(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    api_ok.weather.return_value = weather(units="kelvin-ish")
    await _setup(hass, config_entry)

    state = hass.states.get(_entity_id(hass, "weather_temperature"))
    assert state is not None and state.state == STATE_UNAVAILABLE
    condition = hass.states.get(_entity_id(hass, "weather_condition"))
    assert condition is not None and condition.state == "cloudy"


@pytest.mark.parametrize("status", [404, 405])
async def test_missing_endpoint_disables_and_logs_once(
    hass: HomeAssistant,
    api_ok: MagicMock,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    api_ok.weather.side_effect = CasaApiError(status, "not_found")
    caplog.set_level(logging.WARNING)
    await _setup(hass, config_entry)
    coordinator = _home_coordinator(config_entry)
    calls_after_setup = api_ok.weather.await_count

    for _ in range(3):
        await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.disabled == {"weather"}
    assert api_ok.weather.await_count == calls_after_setup == 1
    assert coordinator.last_update_success  # credits still worked
    warnings = [
        r
        for r in caplog.records
        if r.name == "custom_components.casahome.home_coordinator" and r.levelno == logging.WARNING
    ]
    # One per entry lifetime.
    assert len(warnings) == 1
    assert f"HTTP {status}" in warnings[-1].getMessage()

    temperature = hass.states.get(_entity_id(hass, "weather_temperature"))
    assert temperature is not None and temperature.state == STATE_UNAVAILABLE
    available = hass.states.get(_entity_id(hass, "credits_available"))
    assert available is not None and available.state == "3"


async def test_both_endpoints_missing_is_not_a_failure(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    api_ok.weather.side_effect = CasaApiError(404, "not_found")
    api_ok.credits_summary.side_effect = CasaApiError(404, "not_found")
    await _setup(hass, config_entry)

    coordinator = _home_coordinator(config_entry)
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
    config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    api_ok.weather.side_effect = error
    api_ok.credits_summary.side_effect = error
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED  # never blocks setup
    coordinator = _home_coordinator(config_entry)
    assert not coordinator.last_update_success
    assert coordinator.disabled == set()

    coordinator = _home_coordinator(config_entry)
    api_ok.weather.side_effect = None
    api_ok.credits_summary.side_effect = None
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data.weather is not None


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    api_ok.weather.side_effect = CasaAuthError("TOKEN_EXPIRED")

    await _home_coordinator(config_entry).async_refresh()
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
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    api_ok.weather.side_effect = CasaApiError(404, "nf", display_message="secret-detail")
    api_ok.credits_summary.side_effect = CasaConnectionError("secret-connection-detail")
    await _setup(hass, config_entry)

    assert "secret-detail" not in caplog.text
    assert "secret-connection-detail" not in caplog.text
    assert "fake-refresh-token-not-real" not in caplog.text


@pytest.mark.parametrize("temperature", [float("nan"), float("inf"), float("-inf")])
async def test_nonfinite_temperature_is_unavailable(
    hass: HomeAssistant,
    api_ok: MagicMock,
    config_entry: MockConfigEntry,
    temperature: float,
) -> None:
    api_ok.weather.return_value = weather().model_copy(update={"temperature_degrees": temperature})
    await _setup(hass, config_entry)

    state = hass.states.get(_entity_id(hass, "weather_temperature"))
    assert state is not None and state.state == STATE_UNAVAILABLE
    condition = hass.states.get(_entity_id(hass, "weather_condition"))
    assert condition is not None and condition.state == "cloudy"

    api_ok.weather.return_value = weather()
    await _home_coordinator(config_entry).async_refresh()
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
    config_entry: MockConfigEntry,
    keys: tuple[str, ...],
    endpoint: str,
) -> None:
    await _setup(hass, config_entry)
    await _disable_sensors(hass, tuple(key for key in HOME_DATA_KEYS if key not in keys))
    coordinator = _home_coordinator(config_entry)
    assert set(coordinator.async_contexts()) == {endpoint}
    api_ok.weather.reset_mock()
    api_ok.credits_summary.reset_mock()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()

    assert api_ok.weather.await_count == (1 if endpoint == WEATHER else 0)
    assert api_ok.credits_summary.await_count == (1 if endpoint == CREDITS else 0)


async def test_user_disabled_entities_do_not_poll_after_initial_probe(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    await _disable_sensors(hass, HOME_DATA_KEYS)
    coordinator = _home_coordinator(config_entry)
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
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    api_ok.weather.side_effect = CasaApiError(404, "not_found")
    await _setup(hass, config_entry)
    await _disable_sensors(hass, tuple(key for key in HOME_DATA_KEYS if key != "weather_condition"))
    api_ok.weather.reset_mock()
    api_ok.credits_summary.reset_mock()
    coordinator = _home_coordinator(config_entry)

    await coordinator.async_refresh()
    assert coordinator.last_update_success
    api_ok.weather.assert_not_called()
    api_ok.credits_summary.assert_not_called()


async def test_disabling_family_removes_its_context_and_main_listener(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    coordinator = _home_coordinator(config_entry)
    main = config_entry.runtime_data
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
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    main = config_entry.runtime_data
    coordinator = _home_coordinator(config_entry)
    api_ok.weather.reset_mock()
    api_ok.credits_summary.reset_mock()

    api_ok.bootstrap.return_value = bootstrap_model(homes=[])
    main.async_set_updated_data(api_ok.bootstrap.return_value)
    for key in HOME_DATA_KEYS:
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
    for key in HOME_DATA_KEYS:
        state = hass.states.get(_entity_id(hass, key))
        assert state is not None and state.state != STATE_UNAVAILABLE

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=64))
    await hass.async_block_till_done()
    assert api_ok.weather.await_count == 2
    assert api_ok.credits_summary.await_count == 2


async def test_bootstrap_failure_immediately_marks_home_data_sensors_unavailable(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    main = config_entry.runtime_data
    main.async_set_update_error(Exception("synthetic bootstrap failure"))

    for key in HOME_DATA_KEYS:
        state = hass.states.get(_entity_id(hass, key))
        assert state is not None and state.state == STATE_UNAVAILABLE
    assert _home_coordinator(config_entry)._unsub_refresh is None

    main.async_set_updated_data(bootstrap_model())
    await hass.async_block_till_done(wait_background_tasks=True)
    for key in HOME_DATA_KEYS:
        state = hass.states.get(_entity_id(hass, key))
        assert state is not None and state.state != STATE_UNAVAILABLE


async def test_unload_removes_main_and_home_listeners(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    main = config_entry.runtime_data
    coordinator = _home_coordinator(config_entry)
    assert len(list(coordinator.async_contexts())) == len(HOME_DATA_KEYS)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
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
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    api_ok.weather.side_effect = CasaAuthError("synthetic-secret-token")

    with pytest.raises(ConfigEntryAuthFailed) as raised:
        await _home_coordinator(config_entry)._async_update_data()

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert "synthetic-secret-token" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("disable_all", [False, True])
@pytest.mark.parametrize("weather_fails", [False, True])
async def test_disabled_family_is_not_started_after_inflight_weather(
    hass: HomeAssistant,
    api_ok: MagicMock,
    config_entry: MockConfigEntry,
    disable_all: bool,
    weather_fails: bool,
) -> None:
    await _setup(hass, config_entry)
    coordinator = _home_coordinator(config_entry)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_weather(home_id: str) -> WeatherResponse:
        assert home_id == HOME_ID
        entered.set()
        await release.wait()
        if weather_fails:
            raise CasaConnectionError("synthetic weather failure")
        return weather()

    api_ok.weather.side_effect = blocked_weather
    api_ok.credits_summary.reset_mock()
    refresh = asyncio.create_task(coordinator.async_refresh())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        keys = HOME_DATA_KEYS if disable_all else ("credits_available", "handyman_hourly_rate")
        registry = er.async_get(hass)
        for key in keys:
            registry.async_update_entity(
                _entity_id(hass, key), disabled_by=er.RegistryEntryDisabler.USER
            )
        await hass.async_block_till_done()
        assert set(coordinator.async_contexts()) == (set() if disable_all else {WEATHER})
        assert not refresh.done()  # Removing listeners mustn't cancel the active SDK call.
    finally:
        release.set()
        await asyncio.wait_for(refresh, timeout=5)

    api_ok.credits_summary.assert_not_called()
    assert coordinator.last_update_success is (disable_all or not weather_fails)
    if disable_all:
        assert coordinator.data == HomeData()
        assert coordinator._unsub_refresh is None
    elif weather_fails:
        assert isinstance(coordinator.last_exception, UpdateFailed)


async def test_inactive_weather_success_does_not_mask_active_credits_failure(
    hass: HomeAssistant, api_ok: MagicMock, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    coordinator = _home_coordinator(config_entry)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_weather(home_id: str) -> WeatherResponse:
        entered.set()
        await release.wait()
        return weather()

    api_ok.weather.side_effect = blocked_weather
    api_ok.credits_summary.side_effect = CasaConnectionError("synthetic credits failure")
    api_ok.credits_summary.reset_mock()
    refresh = asyncio.create_task(coordinator.async_refresh())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        registry = er.async_get(hass)
        for key in ("weather_temperature", "weather_condition"):
            registry.async_update_entity(
                _entity_id(hass, key), disabled_by=er.RegistryEntryDisabler.USER
            )
        await hass.async_block_till_done()
        assert set(coordinator.async_contexts()) == {CREDITS}
    finally:
        release.set()
        await asyncio.wait_for(refresh, timeout=5)

    api_ok.credits_summary.assert_awaited_once_with(HOME_ID)
    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception, UpdateFailed)
    for key in ("credits_available", "handyman_hourly_rate"):
        state = hass.states.get(_entity_id(hass, key))
        assert state is not None and state.state == STATE_UNAVAILABLE


@pytest.mark.parametrize("bootstrap_fails", [False, True])
@pytest.mark.parametrize("recovery_fails", [False, True])
async def test_recovery_requires_fresh_home_data(
    hass: HomeAssistant,
    api_ok: MagicMock,
    config_entry: MockConfigEntry,
    bootstrap_fails: bool,
    recovery_fails: bool,
) -> None:
    await _setup(hass, config_entry)
    main = config_entry.runtime_data
    coordinator = _home_coordinator(config_entry)
    if bootstrap_fails:
        main.async_set_update_error(Exception("synthetic bootstrap failure"))
    else:
        main.async_set_updated_data(bootstrap_model(homes=[]))
    assert coordinator.data == HomeData()
    _assert_home_data_unavailable(hass)
    assert coordinator._unsub_refresh is None

    entered = asyncio.Event()
    release = asyncio.Event()
    fresh_weather = weather().model_copy(
        update={"temperature_degrees": 72.0, "condition_type": "sunny"}
    )
    fresh_credits = credits().model_copy(update={"total_available": 7})

    async def blocked_weather(home_id: str) -> WeatherResponse:
        entered.set()
        await release.wait()
        if recovery_fails:
            raise CasaConnectionError("synthetic recovery failure")
        return fresh_weather

    api_ok.weather.side_effect = blocked_weather
    api_ok.credits_summary.side_effect = (
        CasaConnectionError("synthetic recovery failure") if recovery_fails else None
    )
    api_ok.credits_summary.return_value = fresh_credits
    entity_ids = {_entity_id(hass, key) for key in HOME_DATA_KEYS}
    observed_states: list[str] = []

    @callback
    def record_state(event: Event[EventStateChangedData]) -> None:
        if event.data["entity_id"] in entity_ids and (state := event.data["new_state"]):
            observed_states.append(state.state)

    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, record_state)
    try:
        main.async_set_updated_data(bootstrap_model())
        await asyncio.wait_for(entered.wait(), timeout=5)
        _assert_home_data_unavailable(hass)
        assert coordinator.data == HomeData()
        assert all(state == STATE_UNAVAILABLE for state in observed_states)
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        unsubscribe()

    if recovery_fails:
        assert not coordinator.last_update_success
        assert coordinator.data == HomeData()
        _assert_home_data_unavailable(hass)
        assert all(state == STATE_UNAVAILABLE for state in observed_states)
    else:
        assert coordinator.last_update_success
        assert coordinator.data == HomeData(weather=fresh_weather, credits=fresh_credits)
        condition = hass.states.get(_entity_id(hass, "weather_condition"))
        assert condition is not None and condition.state == "sunny"
        available = hass.states.get(_entity_id(hass, "credits_available"))
        assert available is not None and available.state == "7"


@pytest.mark.parametrize("blocked_endpoint", [WEATHER, CREDITS])
@pytest.mark.parametrize("stale_endpoint_missing", [False, True])
@pytest.mark.parametrize("recovery_debounce_expires", [False, True])
async def test_inflight_response_cannot_survive_home_loss_and_reappearance(
    hass: HomeAssistant,
    api_ok: MagicMock,
    config_entry: MockConfigEntry,
    blocked_endpoint: str,
    stale_endpoint_missing: bool,
    recovery_debounce_expires: bool,
) -> None:
    await _setup(hass, config_entry)
    main = config_entry.runtime_data
    coordinator = _home_coordinator(config_entry)
    stale_entered = asyncio.Event()
    stale_release = asyncio.Event()
    fresh_entered = asyncio.Event()
    fresh_release = asyncio.Event()
    fresh_weather = weather().model_copy(
        update={"temperature_degrees": 72.0, "condition_type": "sunny"}
    )
    fresh_credits = credits().model_copy(update={"total_available": 7})
    weather_calls = 0
    credits_calls = 0

    async def wait_for_stale_response() -> None:
        stale_entered.set()
        await stale_release.wait()
        if stale_endpoint_missing:
            raise CasaApiError(404, "synthetic missing home endpoint")

    async def weather_response(home_id: str) -> WeatherResponse:
        nonlocal weather_calls
        weather_calls += 1
        if weather_calls == 1:
            if blocked_endpoint == WEATHER:
                await wait_for_stale_response()
            return weather()
        fresh_entered.set()
        await fresh_release.wait()
        return fresh_weather

    async def credits_response(home_id: str) -> MemberCredits:
        nonlocal credits_calls
        credits_calls += 1
        if blocked_endpoint == CREDITS and credits_calls == 1:
            await wait_for_stale_response()
            return credits()
        return fresh_credits

    api_ok.weather.side_effect = weather_response
    api_ok.credits_summary.side_effect = credits_response
    refresh = asyncio.create_task(coordinator.async_refresh())
    try:
        await asyncio.wait_for(stale_entered.wait(), timeout=5)
        main.async_set_updated_data(bootstrap_model(homes=[]))
        assert coordinator.data == HomeData()
        _assert_home_data_unavailable(hass)
        main.async_set_updated_data(bootstrap_model())
        _assert_home_data_unavailable(hass)
        await asyncio.sleep(0)  # Let the real recovery request enter HA's debouncer.
        if recovery_debounce_expires:
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
            await hass.async_block_till_done(wait_background_tasks=True)
            assert weather_calls == 1

        stale_release.set()
        await asyncio.wait_for(refresh, timeout=5)
        assert coordinator.data == HomeData()
        assert coordinator.disabled == set()  # A stale 404 mustn't disable the recovered endpoint.
        _assert_home_data_unavailable(hass)
        if blocked_endpoint == WEATHER:
            assert credits_calls == 0  # Don't continue an invalidated polling cycle.

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=22))
        await asyncio.wait_for(fresh_entered.wait(), timeout=5)
        assert coordinator.data == HomeData()
        _assert_home_data_unavailable(hass)
    finally:
        stale_release.set()
        fresh_release.set()
        await asyncio.wait_for(refresh, timeout=5)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert coordinator.last_update_success
    assert coordinator.data == HomeData(weather=fresh_weather, credits=fresh_credits)
    assert weather_calls == 2
    assert credits_calls == (2 if blocked_endpoint == CREDITS else 1)
    condition = hass.states.get(_entity_id(hass, "weather_condition"))
    assert condition is not None and condition.state == "sunny"
    available = hass.states.get(_entity_id(hass, "credits_available"))
    assert available is not None and available.state == "7"


@pytest.mark.parametrize("blocked_endpoint", [WEATHER, CREDITS])
async def test_unload_cancels_inflight_request_and_pending_home_recovery(
    hass: HomeAssistant,
    api_ok: MagicMock,
    config_entry: MockConfigEntry,
    blocked_endpoint: str,
) -> None:
    await _setup(hass, config_entry)
    main = config_entry.runtime_data
    coordinator = _home_coordinator(config_entry)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_response(home_id: str) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    api_ok.weather.reset_mock()
    api_ok.credits_summary.reset_mock()
    if blocked_endpoint == WEATHER:
        api_ok.weather.side_effect = blocked_response
    else:
        api_ok.credits_summary.side_effect = blocked_response
    refresh = config_entry.async_create_background_task(
        hass, coordinator.async_refresh(), name="Synthetic home refresh"
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    main.async_set_updated_data(bootstrap_model(homes=[]))
    main.async_set_updated_data(bootstrap_model())
    await asyncio.sleep(0)
    _assert_home_data_unavailable(hass)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert refresh.cancelled()
    assert cancelled.is_set()
    assert not coordinator._listeners
    assert not main._listeners
    assert coordinator._unsub_refresh is None
    assert coordinator._debounced_refresh._timer_task is None
    assert not config_entry._background_tasks

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=46))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert api_ok.weather.await_count == 1
    assert api_ok.credits_summary.await_count == (1 if blocked_endpoint == CREDITS else 0)
