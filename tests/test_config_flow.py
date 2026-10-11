"""Config and reauth flows."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from casahome import CasaApiError, CasaAuthError, CasaConnectionError, CasaResponseError
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casahome.const import CONF_DEVICE_ID, CONF_REFRESH_TOKEN, DOMAIN

from .conftest import DEVICE_ID, REFRESH_TOKEN, USER_ID, bootstrap_model

NEW_TOKEN = "fake-refresh-token-rotated"


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    flow_id: str = result["flow_id"]
    return flow_id


async def test_user_happy_path(
    hass: HomeAssistant, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test"  # synthetic fixture first_name
    assert result["result"].unique_id == USER_ID
    assert result["data"][CONF_REFRESH_TOKEN] == REFRESH_TOKEN
    device_id = result["data"][CONF_DEVICE_ID]
    assert str(uuid.UUID(device_id)).upper() == device_id
    assert result["options"] == {}
    assert result["result"].minor_version == 3  # new entries never need migration
    # The flow's bootstrap and setup's bootstrap must share one device id.
    assert mock_api.flow_cls.call_args.kwargs["device_id"] == device_id
    assert mock_api.init_cls.call_args.kwargs["device_id"] == device_id


async def test_user_stores_rotated_token(
    hass: HomeAssistant, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    async def _rotate() -> str:
        mock_auth.user_id = USER_ID
        mock_auth.refresh_token = NEW_TOKEN
        mock_auth.captured["kwargs"]["on_refresh_token_rotated"](NEW_TOKEN)
        return "fake-id-token"

    mock_auth.get_id_token.side_effect = _rotate
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    assert result["data"][CONF_REFRESH_TOKEN] == NEW_TOKEN


async def test_title_falls_back_to_casa(
    hass: HomeAssistant, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    mock_api.bootstrap.return_value = bootstrap_model(first_name="")
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    assert result["title"] == "Casa"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (CasaAuthError("bad"), "invalid_auth"),
        (CasaConnectionError("down"), "cannot_connect"),
        (TimeoutError(), "cannot_connect"),
        (CasaApiError(500, "server_error"), "unknown"),
        (CasaResponseError("drift"), "unknown"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_errors_then_recover(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    error: Exception,
    key: str,
) -> None:
    mock_api.bootstrap.side_effect = error
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}
    device_id = mock_api.flow_cls.call_args.kwargs["device_id"]

    mock_api.bootstrap.side_effect = None
    mock_api.bootstrap.return_value = bootstrap_model()
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert result["data"][CONF_DEVICE_ID] == device_id
    assert mock_api.flow_cls.call_args.kwargs["device_id"] == device_id
    assert mock_api.init_cls.call_args.kwargs["device_id"] == device_id


async def test_unknown_error_logs_class_only(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_api.bootstrap.side_effect = RuntimeError("secret-detail-not-for-logs")
    flow_id = await _start(hass)
    await hass.config_entries.flow.async_configure(flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN})
    assert "RuntimeError" in caplog.text
    assert "secret-detail-not-for-logs" not in caplog.text
    assert REFRESH_TOKEN not in caplog.text


async def test_missing_user_id_is_invalid_auth(
    hass: HomeAssistant, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    mock_auth.get_id_token.side_effect = None
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    mock_api.bootstrap.assert_not_awaited()


async def test_single_instance(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_duplicate_account_aborts(
    hass: HomeAssistant, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    """Unique id guard, checked without single_config_entry short-circuiting it."""
    flow_id = await _start(hass)
    # Add the same account after the form opened, before submit.
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        data={CONF_REFRESH_TOKEN: REFRESH_TOKEN, CONF_DEVICE_ID: DEVICE_ID},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def _loaded(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_reauth_saves_token_and_reuses_device(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    await _loaded(hass, config_entry)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: NEW_TOKEN}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_REFRESH_TOKEN] == NEW_TOKEN
    assert config_entry.data[CONF_DEVICE_ID] == DEVICE_ID
    assert mock_api.flow_cls.call_args.kwargs["device_id"] == DEVICE_ID
    # Reloaded: setup ran a second time with the new token.
    assert mock_auth.captured["args"][1] == NEW_TOKEN
    assert config_entry.state is ConfigEntryState.LOADED


async def test_reauth_wrong_account(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    await _loaded(hass, config_entry)
    result = await config_entry.start_reauth_flow(hass)

    async def _authenticate_wrong_account() -> str:
        mock_auth.user_id = "someone-else"
        mock_auth.refresh_token = "fake-wrong-account-rotated"
        mock_auth.captured["kwargs"]["on_refresh_token_rotated"](mock_auth.refresh_token)
        return "fake-id-token"

    mock_auth.get_id_token.side_effect = _authenticate_wrong_account
    mock_api.bootstrap.reset_mock()
    mock_api.flow_cls.reset_mock()
    original_data = dict(config_entry.data)
    runtime_auth_calls = mock_auth.init_cls.call_count
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: NEW_TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    await hass.async_block_till_done()
    assert dict(config_entry.data) == original_data
    mock_api.bootstrap.assert_not_awaited()
    mock_api.flow_cls.assert_not_called()
    assert mock_auth.init_cls.call_count == runtime_auth_calls
    assert hass.config_entries.async_entries(DOMAIN) == [config_entry]


async def test_reauth_error_shows_form(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    await _loaded(hass, config_entry)
    result = await config_entry.start_reauth_flow(hass)
    mock_api.bootstrap.side_effect = CasaAuthError("bad")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: NEW_TOKEN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_no_options_flow(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    await _loaded(hass, config_entry)
    assert not config_entry.supports_options


async def test_token_rotation_does_not_reload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    await _loaded(hass, config_entry)
    calls_before = mock_api.init_cls.call_count
    mock_auth.captured["kwargs"]["on_refresh_token_rotated"](NEW_TOKEN)
    await hass.async_block_till_done()
    assert config_entry.data[CONF_REFRESH_TOKEN] == NEW_TOKEN
    assert mock_api.init_cls.call_count == calls_before
