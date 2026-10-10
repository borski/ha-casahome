"""Coordinator error mapping, reauth trigger, recovery, token rotation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from casahome import CasaApiError, CasaAuthError, CasaConnectionError, CasaResponseError
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casahome.const import CONF_DEVICE_ID, CONF_REFRESH_TOKEN, DOMAIN

from .conftest import DEVICE_ID, REFRESH_TOKEN, bootstrap_model

NEW_TOKEN = "fake-refresh-token-rotated"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    entry.add_to_hass(hass)
    ok = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return ok


def _reauth_flows(hass: HomeAssistant) -> list[object]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"].get("source") == SOURCE_REAUTH
    ]


@pytest.mark.parametrize(
    "error",
    [
        CasaConnectionError("down"),
        TimeoutError(),
        CasaApiError(503, "unavailable"),
        CasaResponseError("drift"),
    ],
)
async def test_first_refresh_transient_is_setup_retry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    error: Exception,
) -> None:
    mock_api.bootstrap.side_effect = error
    assert not await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert _reauth_flows(hass) == []


async def test_first_refresh_auth_is_setup_error_and_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    mock_api.bootstrap.side_effect = CasaAuthError("revoked")
    assert not await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass)) == 1


async def test_auth_failure_mid_life_starts_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    assert await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data

    mock_api.bootstrap.side_effect = CasaAuthError("revoked")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert len(_reauth_flows(hass)) == 1


async def test_connection_error_then_recovery(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    assert await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert coordinator.last_update_success

    mock_api.bootstrap.side_effect = CasaConnectionError("down")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert _reauth_flows(hass) == []

    mock_api.bootstrap.side_effect = None
    mock_api.bootstrap.return_value = bootstrap_model()
    await coordinator.async_refresh()
    assert coordinator.last_update_success


async def test_http_error_message_has_status_only(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    assert await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_api.bootstrap.side_effect = CasaApiError(
        500, "server_error", display_message="secret-upstream-detail"
    )
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert "500" in str(coordinator.last_exception)
    assert "secret-upstream-detail" not in str(coordinator.last_exception)


async def test_schema_mismatch_warns_once_per_streak(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data

    def ours() -> int:
        return sum(
            1
            for r in caplog.records
            if r.name == "custom_components.casahome.coordinator"
            and r.levelname == "WARNING"
            and r.getMessage() == "Casa bootstrap response didn't match the expected schema"
        )

    mock_api.bootstrap.side_effect = CasaResponseError("drift")
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert ours() == 1

    mock_api.bootstrap.side_effect = None
    mock_api.bootstrap.return_value = bootstrap_model()
    await coordinator.async_refresh()

    caplog.clear()
    mock_api.bootstrap.side_effect = CasaResponseError("drift")
    await coordinator.async_refresh()
    assert ours() == 1


async def test_rotation_persists_token_and_keeps_device(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    assert await _setup(hass, config_entry)
    callback = mock_auth.captured["kwargs"]["on_refresh_token_rotated"]

    result = callback(NEW_TOKEN)
    await hass.async_block_till_done()

    assert result is None  # sync callback, runs under the SDK refresh lock
    assert config_entry.data[CONF_REFRESH_TOKEN] == NEW_TOKEN
    assert config_entry.data[CONF_DEVICE_ID] == DEVICE_ID
    assert config_entry.state is ConfigEntryState.LOADED


async def test_tokens_never_logged(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    assert await _setup(hass, config_entry)
    mock_auth.captured["kwargs"]["on_refresh_token_rotated"](NEW_TOKEN)
    mock_api.bootstrap.side_effect = CasaAuthError("revoked")
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert REFRESH_TOKEN not in caplog.text
    assert NEW_TOKEN not in caplog.text
