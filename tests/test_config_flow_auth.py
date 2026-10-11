"""Offline auth ordering, rotation recovery, and pending credential lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from functools import partial
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from casahome import CasaApi, CasaAuthError, CasaClient, FirebaseAuth
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casahome.config_flow import CasaConfigFlow
from custom_components.casahome.const import CONF_DEVICE_ID, CONF_REFRESH_TOKEN, DOMAIN

from .conftest import DEVICE_ID, REFRESH_TOKEN, USER_ID, bootstrap_model, load_fixture_json
from .test_config_flow import NEW_TOKEN, _loaded, _start

CHANGED_TOKEN = "fake-separate-submitted-credential"
LATEST_TOKEN = "fake-second-rotation"


@pytest.mark.parametrize("submitted_b", [REFRESH_TOKEN, CHANGED_TOKEN])
@pytest.mark.parametrize("reauth", [False, True])
async def test_overlapping_flow_submissions_preserve_later_rotation(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    submitted_b: str,
    reauth: bool,
) -> None:
    """Actual HA FlowManager accepts A and B concurrently, not just direct step calls."""
    if reauth:
        await _loaded(hass, config_entry)
        initial = await config_entry.start_reauth_flow(hass)
        flow_id = initial["flow_id"]
    else:
        flow_id = await _start(hass)
    mock_api.bootstrap.reset_mock()
    flow = cast(CasaConfigFlow, hass.config_entries.flow._progress[flow_id])
    a_started = asyncio.Event()
    a_release = asyncio.Event()
    b_submitted = asyncio.Event()
    b_rotated = asyncio.Event()
    b_release = asyncio.Event()
    tokens: list[str] = []
    callbacks: list[Callable[[str], None]] = []

    def _auth_factory(
        session: object, token: str, *, on_refresh_token_rotated: Callable[[str], None]
    ) -> MagicMock:
        tokens.append(token)
        callbacks.append(on_refresh_token_rotated)
        attempt = len(tokens)
        auth = MagicMock(spec=FirebaseAuth)
        auth.user_id = None
        auth.refresh_token = token

        async def _authenticate() -> str:
            if attempt == 1:
                a_started.set()
                await a_release.wait()
                raise CasaAuthError("fake-A-invalid")
            auth.user_id = USER_ID
            if attempt == 2:
                auth.refresh_token = NEW_TOKEN
                on_refresh_token_rotated(NEW_TOKEN)
            return "fake-id-token"

        auth.get_id_token = AsyncMock(side_effect=_authenticate)
        return auth

    async def _bootstrap() -> object:
        if mock_api.bootstrap.await_count == 1:
            b_rotated.set()
            await b_release.wait()
            raise TimeoutError()
        return bootstrap_model()

    async def _submit_b() -> Any:
        b_submitted.set()
        return await hass.config_entries.flow.async_configure(
            flow_id, {CONF_REFRESH_TOKEN: submitted_b}
        )

    mock_api.bootstrap.side_effect = _bootstrap
    with patch("custom_components.casahome.config_flow.FirebaseAuth", side_effect=_auth_factory):
        task_a = asyncio.create_task(
            hass.config_entries.flow.async_configure(flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN})
        )
        await a_started.wait()
        task_b = asyncio.create_task(_submit_b())
        await b_submitted.wait()
        # Give B a turn. Without serialization it rotates before A's failure cleanup.
        await asyncio.sleep(0)
        a_release.set()
        result_a = await task_a
        assert result_a["errors"] == {"base": "invalid_auth"}
        await b_rotated.wait()
        b_release.set()
        result_b = await task_b
        assert result_b["errors"] == {"base": "cannot_connect"}
        assert flow._pending_token == NEW_TOKEN
        assert flow._submitted_token == submitted_b
        assert len(hass.config_entries.async_entries(DOMAIN)) == int(reauth)
        assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
        # A completed callback must not overwrite B, even when both submit the same token.
        callbacks[0](LATEST_TOKEN)
        assert flow._pending_token == NEW_TOKEN
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_REFRESH_TOKEN: submitted_b}
        )
    assert tokens == [REFRESH_TOKEN, submitted_b, NEW_TOKEN]
    if reauth:
        assert result["reason"] == "reauth_successful"
        assert config_entry.data[CONF_REFRESH_TOKEN] == NEW_TOKEN
    else:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_REFRESH_TOKEN] == NEW_TOKEN
    assert flow._pending_token is None
    await hass.async_block_till_done()


@pytest.mark.parametrize("reauth", [False, True])
@pytest.mark.parametrize("remove_flow", [False, True])
async def test_queued_submission_cancellation_and_flow_removal(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    reauth: bool,
    remove_flow: bool,
) -> None:
    if reauth:
        await _loaded(hass, config_entry)
        initial = await config_entry.start_reauth_flow(hass)
        flow_id = initial["flow_id"]
    else:
        flow_id = await _start(hass)
    mock_auth.flow_cls.reset_mock()
    flow = cast(CasaConfigFlow, hass.config_entries.flow._progress[flow_id])
    a_started = asyncio.Event()
    a_release = asyncio.Event()
    b_submitted = asyncio.Event()

    async def _bootstrap() -> object:
        a_started.set()
        await a_release.wait()
        if remove_flow:
            return bootstrap_model()  # Removed reauth must not persist a late success.
        raise TimeoutError()

    async def _submit_b() -> Any:
        b_submitted.set()
        return await hass.config_entries.flow.async_configure(
            flow_id, {CONF_REFRESH_TOKEN: CHANGED_TOKEN}
        )

    _rotate_on_auth(mock_auth)
    mock_api.bootstrap.side_effect = _bootstrap
    task_a = asyncio.create_task(
        hass.config_entries.flow.async_configure(flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN})
    )
    await a_started.wait()
    callback_a = mock_auth.captured["kwargs"]["on_refresh_token_rotated"]
    task_b = asyncio.create_task(_submit_b())
    await b_submitted.wait()
    await asyncio.sleep(0)
    assert mock_auth.flow_cls.call_count == 1  # B is queued, not authenticating.
    if remove_flow:
        hass.config_entries.flow.async_abort(flow_id)
        callback_a(LATEST_TOKEN)
        assert flow._pending_token is None
        a_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task_a
        with pytest.raises(asyncio.CancelledError):
            await task_b
        callback_a(LATEST_TOKEN)
        assert flow._submitted_token is None
        assert flow._pending_token is None
    else:
        task_b.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task_b
        # Cancelling a waiter must not clear the active attempt's rotated token.
        assert flow._pending_token == NEW_TOKEN
        a_release.set()
        result_a = await task_a
        assert result_a["errors"] == {"base": "cannot_connect"}
        assert flow._pending_token == NEW_TOKEN
        callback_a(LATEST_TOKEN)
        assert flow._pending_token == NEW_TOKEN
        mock_api.bootstrap.side_effect = None
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
        )
        assert mock_auth.flow_cls.call_args.args[1] == NEW_TOKEN
        if reauth:
            assert result["reason"] == "reauth_successful"
        else:
            assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    if remove_flow:
        assert mock_auth.flow_cls.call_count == 1
        assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
        assert len(hass.config_entries.async_entries(DOMAIN)) == int(reauth)


def _rotate_on_auth(auth: MagicMock, token: str = NEW_TOKEN) -> None:
    """Mirror the real SDK: identity and rotation are established by get_id_token."""

    async def _authenticate() -> str:
        auth.user_id = USER_ID
        auth.refresh_token = token
        auth.captured["kwargs"]["on_refresh_token_rotated"](token)
        return "fake-id-token"

    auth.get_id_token.side_effect = _authenticate


@pytest.mark.parametrize("reauth", [False, True])
async def test_rotated_token_survives_bootstrap_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    reauth: bool,
) -> None:
    if reauth:
        await _loaded(hass, config_entry)
        initial = await config_entry.start_reauth_flow(hass)
        flow_id = initial["flow_id"]
    else:
        flow_id = await _start(hass)
    flow = cast(CasaConfigFlow, hass.config_entries.flow._progress[flow_id])
    _rotate_on_auth(mock_auth)
    mock_api.bootstrap.side_effect = TimeoutError()
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    assert flow._pending_token == NEW_TOKEN
    assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
    assert len(hass.config_entries.async_entries(DOMAIN)) == int(reauth)
    device_id = mock_api.flow_cls.call_args.kwargs["device_id"]
    if reauth:
        assert device_id == DEVICE_ID

    async def _bootstrap_rotates_again() -> object:
        mock_auth.refresh_token = LATEST_TOKEN
        mock_auth.captured["kwargs"]["on_refresh_token_rotated"](LATEST_TOKEN)
        return mock_api.bootstrap.return_value

    _rotate_on_auth(mock_auth, NEW_TOKEN)
    mock_api.bootstrap.side_effect = _bootstrap_rotates_again
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    assert mock_auth.flow_cls.call_args.args[1] == NEW_TOKEN
    assert mock_api.flow_cls.call_args.kwargs["device_id"] == device_id
    assert flow._pending_token is None
    assert flow._submitted_token is None
    if reauth:
        assert result["reason"] == "reauth_successful"
        assert config_entry.data[CONF_REFRESH_TOKEN] == LATEST_TOKEN
    else:
        assert result["data"][CONF_REFRESH_TOKEN] == LATEST_TOKEN
    await hass.async_block_till_done()


@pytest.mark.parametrize("next_submitted", [CHANGED_TOKEN, ""])
async def test_changed_submission_discards_pending_token(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    next_submitted: str,
) -> None:
    flow_id = await _start(hass)
    _rotate_on_auth(mock_auth)
    mock_api.bootstrap.side_effect = TimeoutError()
    await hass.config_entries.flow.async_configure(flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN})
    # A failed new credential must never fall back to the previous cached token.
    mock_auth.get_id_token.side_effect = CasaAuthError("invalid submitted credential")
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: next_submitted}
    )
    assert mock_auth.captured["args"][1] == next_submitted
    assert result["errors"] == {"base": "invalid_auth"}
    flow = cast(CasaConfigFlow, hass.config_entries.flow._progress[flow_id])
    assert flow._submitted_token is None
    assert flow._pending_token is None


@pytest.mark.parametrize("failure_stage", ["authenticate", "bootstrap"])
async def test_bad_auth_clears_rotated_retry_cache(
    hass: HomeAssistant, mock_api: MagicMock, mock_auth: MagicMock, failure_stage: str
) -> None:
    flow_id = await _start(hass)
    _rotate_on_auth(mock_auth)
    mock_api.bootstrap.side_effect = TimeoutError()
    await hass.config_entries.flow.async_configure(flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN})
    if failure_stage == "authenticate":
        mock_auth.get_id_token.side_effect = CasaAuthError("revoked")
    else:
        mock_api.bootstrap.side_effect = CasaAuthError("rejected")
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    assert mock_auth.captured["args"][1] == NEW_TOKEN
    assert result["errors"] == {"base": "invalid_auth"}
    flow = cast(CasaConfigFlow, hass.config_entries.flow._progress[flow_id])
    assert flow._pending_token is None
    assert flow._submitted_token is None
    # Once rejected, a subsequent retry starts with the supplied credential again.
    await hass.config_entries.flow.async_configure(flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN})
    assert mock_auth.captured["args"][1] == REFRESH_TOKEN


async def test_flow_removal_clears_cache_and_fences_late_rotation(
    hass: HomeAssistant, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    flow_id = await _start(hass)
    _rotate_on_auth(mock_auth)
    mock_api.bootstrap.side_effect = TimeoutError()
    await hass.config_entries.flow.async_configure(flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN})
    flow = cast(CasaConfigFlow, hass.config_entries.flow._progress[flow_id])
    late_callback = mock_auth.captured["kwargs"]["on_refresh_token_rotated"]
    hass.config_entries.flow.async_abort(flow_id)
    late_callback(LATEST_TOKEN)
    assert flow._pending_token is None
    assert flow._submitted_token is None
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize("stage", ["authenticate", "bootstrap"])
async def test_validation_task_cancellation_clears_cache(
    hass: HomeAssistant, mock_api: MagicMock, mock_auth: MagicMock, stage: str
) -> None:
    flow_id = await _start(hass)
    flow = cast(CasaConfigFlow, hass.config_entries.flow._progress[flow_id])
    started = asyncio.Event()

    async def _wait() -> None:
        started.set()
        await asyncio.Event().wait()

    _rotate_on_auth(mock_auth)
    if stage == "authenticate":
        mock_auth.get_id_token.side_effect = _wait
    else:
        mock_api.bootstrap.side_effect = _wait
    task = asyncio.create_task(
        hass.config_entries.flow.async_configure(flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN})
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert flow._pending_token is None
    assert flow._submitted_token is None
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(flow_id)


@pytest.mark.parametrize("error_type", [CasaAuthError, TimeoutError, RuntimeError])
async def test_failed_form_and_full_caplog_never_include_secrets(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
) -> None:
    caplog.set_level(logging.DEBUG)
    _rotate_on_auth(mock_auth)
    canary = "fake-response-body-phone-code-id-token-secret"
    mock_api.bootstrap.side_effect = error_type(f"{canary} {REFRESH_TOKEN} {NEW_TOKEN}")
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
    )
    assert result["type"] is FlowResultType.FORM
    # Includes schema marker descriptions/defaults, context, and description placeholders.
    schema = result["data_schema"]
    assert schema is not None
    exposed = repr(result) + repr(schema.schema) + caplog.text
    for secret in (REFRESH_TOKEN, NEW_TOKEN, canary, "fake-id-token"):
        assert secret not in exposed
    assert not hass.config_entries.async_entries(DOMAIN)


def _http_response(status: int, body: dict[str, Any]) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=json.dumps(body))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def _token_response(user_id: str = USER_ID, refresh_token: str = NEW_TOKEN) -> dict[str, str]:
    return {
        "access_token": "fake-access-token",
        "expires_in": "3600",
        "token_type": "Bearer",
        "refresh_token": refresh_token,
        "id_token": "fake-id-token",
        "user_id": user_id,
        "project_id": "fake-project",
    }


async def test_real_sdk_http_rotation_then_bootstrap_failure_and_retry(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Real SDK auth/client/API, all HTTP mocked. Never contacts a provider."""
    caplog.set_level(logging.DEBUG)
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post.side_effect = [
        _http_response(200, _token_response()),
        _http_response(200, _token_response(refresh_token=LATEST_TOKEN)),
    ]
    canary = "fake-sensitive-bootstrap-error"
    session.request.side_effect = [
        _http_response(503, {"error": {"code": "unavailable", "displayMessage": canary}}),
        _http_response(200, load_fixture_json("bootstrap.json")),
    ]
    flow_id = await _start(hass)
    with (
        patch(
            "custom_components.casahome.config_flow.async_get_clientsession", return_value=session
        ),
        patch(
            "custom_components.casahome.config_flow.FirebaseAuth",
            partial(FirebaseAuth, api_key="fake-key", token_url="https://firebase.invalid/token"),
        ),
        patch(
            "custom_components.casahome.config_flow.CasaClient",
            partial(CasaClient, base_url="https://casa.invalid"),
        ),
        patch("custom_components.casahome.config_flow.CasaApi", CasaApi),
    ):
        failed = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
        )
        assert failed["errors"] == {"base": "unknown"}  # Existing CasaApiError mapping.
        assert not hass.config_entries.async_entries(DOMAIN)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_REFRESH_TOKEN: REFRESH_TOKEN}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REFRESH_TOKEN] == LATEST_TOKEN
    assert [call.kwargs["data"]["refresh_token"] for call in session.post.call_args_list] == [
        REFRESH_TOKEN,
        NEW_TOKEN,
    ]
    for call in session.request.call_args_list:
        assert call.args == ("POST", "https://casa.invalid/protected/member/bootstrap")
        assert call.kwargs["headers"]["Authorization"] == "Bearer fake-id-token"
        assert (
            json.loads(call.kwargs["data"])["user_device"]["device_id"]
            == result["data"][CONF_DEVICE_ID]
        )
    for secret in (REFRESH_TOKEN, NEW_TOKEN, LATEST_TOKEN, "fake-id-token", canary):
        assert secret not in caplog.text
        assert secret not in repr(failed)
    await hass.async_block_till_done()


@pytest.mark.parametrize("user_id", ["fake-different-account", ""])
async def test_real_sdk_reauth_identity_rejected_before_any_casa_http(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    caplog: pytest.LogCaptureFixture,
    user_id: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    await _loaded(hass, config_entry)
    original_data = dict(config_entry.data)
    runtime_auth_calls = mock_auth.init_cls.call_count
    initial = await config_entry.start_reauth_flow(hass)
    flow_id = initial["flow_id"]
    flow = cast(CasaConfigFlow, hass.config_entries.flow._progress[flow_id])
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post.return_value = _http_response(200, _token_response(user_id=user_id))
    with (
        patch(
            "custom_components.casahome.config_flow.async_get_clientsession", return_value=session
        ),
        patch(
            "custom_components.casahome.config_flow.FirebaseAuth",
            partial(FirebaseAuth, api_key="fake-key", token_url="https://firebase.invalid/token"),
        ),
        patch("custom_components.casahome.config_flow.CasaClient", CasaClient),
        patch("custom_components.casahome.config_flow.CasaApi", CasaApi),
        patch.object(hass.config_entries, "async_update_entry") as update_entry,
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_REFRESH_TOKEN: CHANGED_TOKEN}
        )
        update_entry.assert_not_called()
    if user_id:
        assert result["reason"] == "wrong_account"
    else:
        assert result["errors"] == {"base": "invalid_auth"}
    session.post.assert_called_once()
    session.request.assert_not_called()
    assert dict(config_entry.data) == original_data
    assert hass.config_entries.async_entries(DOMAIN) == [config_entry]
    assert flow._submitted_token is None
    assert flow._pending_token is None
    await hass.async_block_till_done()
    assert mock_auth.init_cls.call_count == runtime_auth_calls
    for secret in (CHANGED_TOKEN, NEW_TOKEN, "fake-id-token"):
        assert secret not in caplog.text
        assert secret not in repr(result)
