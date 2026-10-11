"""Security audit regressions. SDK calls are mocked, HA flows and lifecycle are real."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from casahome import CasaApiError, CasaAuthError, CasaConnectionError, CasaResponseError
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casahome.const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_EXPERIMENTAL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.casahome.diagnostics import async_get_config_entry_diagnostics

from .conftest import DEVICE_ID, HOME_ID, REFRESH_TOKEN, bootstrap_model

SENSITIVE_MARKER = "synthetic-audit-private-marker-not-a-real-credential"
ROTATED_TOKEN = "synthetic-audit-rotated-token"
REAUTH_TOKEN = "synthetic-audit-reauth-token"
STALE_TOKEN = "synthetic-audit-stale-token"
ERROR_KINDS = ("auth", "connection", "timeout", "schema", "api")
EXPERIMENTAL_KEYS = (
    "weather_temperature",
    "weather_condition",
    "credits_available",
    "handyman_hourly_rate",
)


def _error(kind: str) -> Exception:
    if kind == "auth":
        return CasaAuthError(f"auth response: {SENSITIVE_MARKER}")
    if kind == "connection":
        return CasaConnectionError(f"connection response: {SENSITIVE_MARKER}")
    if kind == "timeout":
        return TimeoutError(f"timeout response: {SENSITIVE_MARKER}")
    if kind == "schema":
        return CasaResponseError(f"schema response: {SENSITIVE_MARKER}")
    if kind == "api":
        return CasaApiError(503, SENSITIVE_MARKER, display_message=SENSITIVE_MARKER)
    return RuntimeError(f"unexpected response: {SENSITIVE_MARKER}")


def _sensitive_failure(kind: str) -> Callable[[], Coroutine[Any, Any, Any]]:
    """Raise a genuine nested exception chain, not just a message on a mock."""

    async def fail() -> Any:
        try:
            try:
                raise ValueError(f"private response body: {SENSITIVE_MARKER}")
            except ValueError as err:
                raise RuntimeError(f"private SDK detail: {SENSITIVE_MARKER}") from err
        except RuntimeError as err:
            raise _error(kind) from err

    return fail


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def _assert_private_logs(caplog: pytest.LogCaptureFixture) -> None:
    # Inspect all loggers, including HA's DEBUG "Full error" traceback records.
    assert SENSITIVE_MARKER not in caplog.text
    assert REFRESH_TOKEN not in caplog.text
    for record in caplog.records:
        if record.exc_info:
            assert SENSITIVE_MARKER not in "".join(traceback.format_exception(*record.exc_info))


@pytest.mark.parametrize("kind", ERROR_KINDS)
async def test_bootstrap_error_has_private_debug_traceback(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    await _setup(hass, config_entry)
    mock_api.bootstrap.side_effect = _sensitive_failure(kind)

    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    coordinator = config_entry.runtime_data
    assert not coordinator.last_update_success
    expected = ConfigEntryAuthFailed if kind == "auth" else UpdateFailed
    assert isinstance(coordinator.last_exception, expected)
    assert coordinator.last_exception.__cause__ is None
    assert coordinator.last_exception.__suppress_context__
    assert SENSITIVE_MARKER not in "".join(traceback.format_exception(coordinator.last_exception))
    if kind != "auth":
        assert any(record.exc_info for record in caplog.records)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert sum(flow["context"].get("source") == SOURCE_REAUTH for flow in flows) == (
        1 if kind == "auth" else 0
    )
    _assert_private_logs(caplog)


@pytest.mark.parametrize("kind", ERROR_KINDS)
async def test_first_bootstrap_failure_keeps_full_debug_logs_private(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    mock_api.bootstrap.side_effect = _sensitive_failure(kind)
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is (
        ConfigEntryState.SETUP_ERROR if kind == "auth" else ConfigEntryState.SETUP_RETRY
    )
    _assert_private_logs(caplog)


@pytest.mark.parametrize("kind", (*ERROR_KINDS, "unexpected"))
@pytest.mark.parametrize("source", (SOURCE_USER, SOURCE_REAUTH))
async def test_token_validation_flows_keep_nested_errors_private(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    source: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    if source == SOURCE_REAUTH:
        await _setup(hass, config_entry)
        result = await config_entry.start_reauth_flow(hass)
    else:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": source})
    assert result["type"] is FlowResultType.FORM
    mock_api.bootstrap.side_effect = _sensitive_failure(kind)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    key = (
        "invalid_auth"
        if kind == "auth"
        else ("cannot_connect" if kind in ("connection", "timeout") else "unknown")
    )
    assert result["errors"] == {"base": key}
    assert SENSITIVE_MARKER not in json.dumps(result["errors"])
    if source == SOURCE_REAUTH:
        assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
        assert config_entry.state is ConfigEntryState.LOADED
    _assert_private_logs(caplog)


async def test_diagnostics_redacts_entire_arbitrary_calendar_options(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    # No conventional secret/email keys. List[Any] can contain any future upstream shape.
    calendar_options = [
        SENSITIVE_MARKER,
        {
            "label": SENSITIVE_MARKER,
            "future": [
                {"arbitrary": {"deep": [SENSITIVE_MARKER]}},
                [SENSITIVE_MARKER, {SENSITIVE_MARKER: "private calendar metadata"}],
            ],
        },
    ]
    mock_api.bootstrap.return_value = bootstrap_model(calendar_sync_options=calendar_options)
    await _setup(hass, config_entry)
    before = config_entry.runtime_data.data.model_dump(mode="json")

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["bootstrap"]["calendar_sync_options"] == "**REDACTED**"
    serialized = json.dumps(diagnostics)
    assert SENSITIVE_MARKER not in serialized
    assert REFRESH_TOKEN not in serialized
    assert DEVICE_ID not in serialized
    assert diagnostics["last_update_success"] is True
    assert diagnostics["bootstrap"]["homes"]
    assert config_entry.runtime_data.data.model_dump(mode="json") == before


async def test_old_rotation_callback_cannot_clobber_actual_reauth_before_reload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    await _setup(hass, config_entry)
    old_callback = mock_auth.captured["kwargs"]["on_refresh_token_rotated"]
    # Exercise last_persisted_token tracking across successive legitimate rotations.
    assert old_callback(ROTATED_TOKEN) is None
    assert old_callback(f"{ROTATED_TOKEN}-again") is None
    await hass.async_block_till_done()
    assert config_entry.data[CONF_REFRESH_TOKEN] == f"{ROTATED_TOKEN}-again"
    assert mock_api.init_cls.call_count == 1

    reload_entered = asyncio.Event()
    release_reload = asyncio.Event()
    original_reload = hass.config_entries.async_reload

    async def delayed_reload(entry_id: str, *args: Any, **kwargs: Any) -> bool:
        reload_entered.set()
        await release_reload.wait()
        return await original_reload(entry_id, *args, **kwargs)

    with patch.object(hass.config_entries, "async_reload", side_effect=delayed_reload):
        try:
            result = await config_entry.start_reauth_flow(hass)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_REFRESH_TOKEN: REAUTH_TOKEN}
            )
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "reauth_successful"
            await asyncio.wait_for(reload_entered.wait(), timeout=5)
            assert config_entry.state is ConfigEntryState.LOADED
            assert config_entry.data[CONF_REFRESH_TOKEN] == REAUTH_TOKEN
            assert old_callback(STALE_TOKEN) is None
            assert config_entry.data[CONF_REFRESH_TOKEN] == REAUTH_TOKEN
        finally:
            release_reload.set()
            await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert mock_api.init_cls.call_count == 2
    new_callback = mock_auth.captured["kwargs"]["on_refresh_token_rotated"]
    assert new_callback is not old_callback
    assert new_callback(ROTATED_TOKEN) is None
    assert old_callback(STALE_TOKEN) is None
    await hass.async_block_till_done()
    assert config_entry.data[CONF_REFRESH_TOKEN] == ROTATED_TOKEN
    assert config_entry.data[CONF_DEVICE_ID] == DEVICE_ID
    assert mock_api.init_cls.call_count == 2


async def test_old_rotation_callback_is_inactive_after_unload_and_same_token_reload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    await _setup(hass, config_entry)
    old_callback = mock_auth.captured["kwargs"]["on_refresh_token_rotated"]
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED

    assert old_callback(STALE_TOKEN) is None
    assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    new_callback = mock_auth.captured["kwargs"]["on_refresh_token_rotated"]
    assert new_callback is not old_callback
    # Same token as the old callback's snapshot. Equality alone isn't sufficient.
    assert old_callback(STALE_TOKEN) is None
    assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
    assert new_callback(ROTATED_TOKEN) is None
    assert config_entry.data[CONF_REFRESH_TOKEN] == ROTATED_TOKEN
    await hass.async_block_till_done()
    assert config_entry.data[CONF_DEVICE_ID] == DEVICE_ID
    assert mock_api.init_cls.call_count == 2


@pytest.mark.parametrize("unloaded", [True, False])
async def test_rotation_guard_covers_platform_unload_in_progress(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    unloaded: bool,
) -> None:
    """Invalidate before awaiting platform unload, restoring ownership only on failure."""
    from custom_components.casahome import async_unload_entry

    await _setup(hass, config_entry)
    callback = mock_auth.captured["kwargs"]["on_refresh_token_rotated"]

    async def unload_platforms(entry: Any, platforms: Any) -> bool:
        # This is the window before HA cancels in-flight entry tasks.
        callback(STALE_TOKEN)
        assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
        return unloaded

    with patch.object(hass.config_entries, "async_unload_platforms", side_effect=unload_platforms):
        assert await async_unload_entry(hass, config_entry) is unloaded

    callback(ROTATED_TOKEN)
    assert config_entry.data[CONF_REFRESH_TOKEN] == (REFRESH_TOKEN if unloaded else ROTATED_TOKEN)
    await hass.async_block_till_done()


async def _set_options(hass: HomeAssistant, entry: MockConfigEntry, enabled: bool) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ENABLE_EXPERIMENTAL: enabled}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ENABLE_EXPERIMENTAL] is enabled


@pytest.mark.parametrize(
    ("stage", "initial_enabled"),
    [
        ("bootstrap", False),
        ("bootstrap", True),
        ("experimental_weather", True),
        ("experimental_credits", True),
        ("forwarding", False),
        ("forwarding", True),
    ],
)
async def test_options_changed_during_setup_reconcile_active_configuration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    stage: str,
    initial_enabled: bool,
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_ENABLE_EXPERIMENTAL: initial_enabled}
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    blocked = False
    forwarded_options: list[bool] = []

    async def pause_once() -> None:
        nonlocal blocked
        if not blocked:
            blocked = True
            entered.set()
            await release.wait()

    async def bootstrap() -> Any:
        if stage == "bootstrap":
            await pause_once()
        return bootstrap_model()

    weather_result = mock_api.weather.return_value

    async def weather(home_id: str) -> Any:
        assert home_id == HOME_ID
        if stage == "experimental_weather":
            await pause_once()
        return weather_result

    credits_result = mock_api.credits_summary.return_value

    async def credits(home_id: str) -> Any:
        assert home_id == HOME_ID
        if stage == "experimental_credits":
            await pause_once()
        return credits_result

    mock_api.bootstrap.side_effect = bootstrap
    mock_api.weather.side_effect = weather
    mock_api.credits_summary.side_effect = credits
    original_forward = hass.config_entries.async_forward_entry_setups

    async def forward(entry: Any, platforms: Any) -> None:
        forwarded_options.append(bool(entry.runtime_data.experimental))
        if stage == "forwarding":
            await pause_once()
        await original_forward(entry, platforms)

    final_enabled = not initial_enabled
    with patch.object(hass.config_entries, "async_forward_entry_setups", side_effect=forward):
        setup_task = hass.async_create_task(hass.config_entries.async_setup(config_entry.entry_id))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert config_entry.state is ConfigEntryState.SETUP_IN_PROGRESS
            # Use the real HA options flow while setup is paused before listener registration.
            await _set_options(hass, config_entry, final_enabled)
        finally:
            release.set()
            assert await asyncio.wait_for(setup_task, timeout=10)
            await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.options[CONF_ENABLE_EXPERIMENTAL] is final_enabled
    assert bool(config_entry.runtime_data.experimental) is final_enabled
    assert forwarded_options == [initial_enabled, final_enabled]
    assert mock_api.init_cls.call_count == 2
    if final_enabled:
        registry = er.async_get(hass)
        for key in EXPERIMENTAL_KEYS:
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{HOME_ID}_{key}")
            assert entity_id is not None
            registered = registry.async_get(entity_id)
            assert registered is not None
            assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        mock_api.weather.assert_awaited_with(HOME_ID)
        mock_api.credits_summary.assert_awaited_with(HOME_ID)
    mock_api.badge_counts.assert_not_called()

    # The reconciled setup must still install a working listener for later options changes.
    await _set_options(hass, config_entry, initial_enabled)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert bool(config_entry.runtime_data.experimental) is initial_enabled
    assert mock_api.init_cls.call_count == 3
