"""Token persistence rollback with real SDK auth and synthetic HTTP responses."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Generator, Iterable
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from casahome import CasaApi, CasaClient, FirebaseAuth
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casahome.const import CONF_DEVICE_ID, CONF_REFRESH_TOKEN

from .conftest import (
    DEVICE_ID,
    REFRESH_TOKEN,
    USER_ID,
    bootstrap_model,
    member_credits,
    weather_response,
)

ROTATED_TOKEN = "synthetic-rollback-rotated-token"
LATE_TOKEN = "synthetic-rollback-late-token"
REPLACEMENT_TOKEN = "synthetic-rollback-replacement-token"
type UnloadFailure = Literal["false", "raise", "cancel"]
FAILURES: tuple[UnloadFailure, ...] = ("false", "raise", "cancel")


def _respond_with_token(session: MagicMock, token: str) -> None:
    """Configure the next synthetic Firebase response, never an actual request."""
    response = session.post.return_value.__aenter__.return_value
    response.text.return_value = json.dumps(
        {
            "access_token": "synthetic-rollback-access-token",
            "expires_in": "3600",
            "token_type": "Bearer",
            "refresh_token": token,
            "id_token": "synthetic-rollback-id-token",
            "user_id": USER_ID,
            "project_id": "synthetic-rollback-project",
        }
    )


@pytest.fixture
def offline_sdk(caplog: pytest.LogCaptureFixture) -> Generator[MagicMock]:
    """Keep real CasaApi, CasaClient and FirebaseAuth, mocking only HTTP and the reads."""
    caplog.set_level(logging.DEBUG)
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.side_effect = AssertionError("Unexpected Casa HTTP request")
    response = MagicMock()
    response.status = 200
    response.text = AsyncMock()
    session.post.return_value.__aenter__.return_value = response
    session.post.return_value.__aexit__.return_value = False
    _respond_with_token(session, REFRESH_TOKEN)
    with (
        patch("custom_components.casahome.async_get_clientsession", return_value=session),
        patch.object(CasaApi, "bootstrap", new=AsyncMock(return_value=bootstrap_model())),
        # Setup probes these per home. Mocked like bootstrap, so no extra token refresh.
        patch.object(CasaApi, "weather", new=AsyncMock(return_value=weather_response())),
        patch.object(CasaApi, "credits_summary", new=AsyncMock(return_value=member_credits())),
    ):
        yield session
    session.request.assert_not_called()
    for token in (REFRESH_TOKEN, ROTATED_TOKEN, LATE_TOKEN, REPLACEMENT_TOKEN):
        assert token not in caplog.text


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> FirebaseAuth:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    api = entry.runtime_data.api
    assert isinstance(api, CasaApi)
    assert isinstance(api.client, CasaClient)
    assert isinstance(api.client.auth, FirebaseAuth)
    return api.client.auth


async def _unsuccessful_unload(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    failure: UnloadFailure,
    during_unload: Callable[[], Awaitable[None]],
) -> None:
    """Exercise HA's unload path, including genuine task cancellation, with bounded waits."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def unload_platforms(config_entry: MockConfigEntry, platforms: object) -> bool:
        assert config_entry is entry
        assert not entry.runtime_data.token_persistence_active
        await during_unload()
        entered.set()
        await release.wait()
        if failure == "raise":
            raise RuntimeError("Synthetic platform unload failure")
        return False

    with patch.object(hass.config_entries, "async_unload_platforms", side_effect=unload_platforms):
        task = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            if failure == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
            else:
                release.set()
                assert not await asyncio.wait_for(task, timeout=5)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert entry.state is (
        ConfigEntryState.UNLOAD_IN_PROGRESS
        if failure == "cancel"
        else ConfigEntryState.FAILED_UNLOAD
    )


@pytest.mark.parametrize("failure", FAILURES)
async def test_failed_unload_reconciles_live_rotated_token_once(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    offline_sdk: MagicMock,
    failure: UnloadFailure,
) -> None:
    auth = await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    _respond_with_token(offline_sdk, ROTATED_TOKEN)
    with (
        patch.object(auth, "_notify_rotated", wraps=auth._notify_rotated) as notified,
        patch.object(
            hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry
        ) as update,
        patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload,
    ):

        async def rotate() -> None:
            await auth.refresh()
            assert auth.refresh_token == ROTATED_TOKEN
            assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
            update.assert_not_called()

        await _unsuccessful_unload(hass, config_entry, failure, rotate)
        assert coordinator.token_persistence_active
        assert config_entry.data[CONF_REFRESH_TOKEN] == ROTATED_TOKEN
        update.assert_called_once_with(
            config_entry, data={CONF_REFRESH_TOKEN: ROTATED_TOKEN, CONF_DEVICE_ID: DEVICE_ID}
        )
        notified.assert_awaited_once_with(ROTATED_TOKEN)
        # The SDK doesn't fire another rotation callback for the same live token.
        await auth.refresh()
        assert config_entry.data[CONF_REFRESH_TOKEN] == ROTATED_TOKEN
        assert update.call_count == 1
        notified.assert_awaited_once_with(ROTATED_TOKEN)
        await hass.async_block_till_done()
        reload.assert_not_called()
    assert offline_sdk.post.call_count == 2


@pytest.mark.parametrize("failure", FAILURES)
async def test_failed_unload_preserves_replacement_token_ownership(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    offline_sdk: MagicMock,
    failure: UnloadFailure,
) -> None:
    auth = await _setup(hass, config_entry)
    _respond_with_token(offline_sdk, ROTATED_TOKEN)
    replacement_data = {**config_entry.data, CONF_REFRESH_TOKEN: REPLACEMENT_TOKEN}
    with (
        patch.object(
            hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry
        ) as update,
        patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload,
    ):

        async def replace_owner() -> None:
            await auth.refresh()
            assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
            # This is the data update performed when reauth accepts a replacement credential.
            hass.config_entries.async_update_entry(config_entry, data=replacement_data)

        await _unsuccessful_unload(hass, config_entry, failure, replace_owner)
        assert config_entry.runtime_data.token_persistence_active
        assert auth.refresh_token == ROTATED_TOKEN
        assert config_entry.data[CONF_REFRESH_TOKEN] == REPLACEMENT_TOKEN
        update.assert_called_once_with(config_entry, data=replacement_data)
        await auth.refresh()
        _respond_with_token(offline_sdk, LATE_TOKEN)
        await auth.refresh()
        assert auth.refresh_token == LATE_TOKEN
        assert config_entry.data[CONF_REFRESH_TOKEN] == REPLACEMENT_TOKEN
        assert update.call_count == 1
        await hass.async_block_till_done()
        reload.assert_not_called()


@pytest.mark.parametrize("failure", FAILURES)
async def test_failed_unload_without_rotation_does_not_update_or_notify(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    offline_sdk: MagicMock,
    failure: UnloadFailure,
) -> None:
    auth = await _setup(hass, config_entry)
    listener = AsyncMock()
    remove_listener = config_entry.add_update_listener(listener)
    try:
        with (
            patch.object(auth, "_notify_rotated", wraps=auth._notify_rotated) as notified,
            patch.object(
                hass.config_entries,
                "async_update_entry",
                wraps=hass.config_entries.async_update_entry,
            ) as update,
            patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload,
        ):

            async def refresh_without_rotation() -> None:
                await auth.refresh()

            await _unsuccessful_unload(hass, config_entry, failure, refresh_without_rotation)
            assert config_entry.runtime_data.token_persistence_active
            assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
            await auth.refresh()
            await hass.async_block_till_done()
            notified.assert_not_called()
            update.assert_not_called()
            listener.assert_not_called()
            reload.assert_not_called()
    finally:
        remove_listener()
    assert offline_sdk.post.call_count == 2


async def test_successful_unload_does_not_resume_or_allow_late_rotation(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    offline_sdk: MagicMock,
) -> None:
    auth = await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    original_unload = hass.config_entries.async_unload_platforms
    _respond_with_token(offline_sdk, ROTATED_TOKEN)

    async def unload_platforms(entry: MockConfigEntry, platforms: Iterable[str]) -> bool:
        assert not coordinator.token_persistence_active
        await auth.refresh()
        assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
        return await original_unload(entry, platforms)

    with (
        patch.object(hass.config_entries, "async_unload_platforms", side_effect=unload_platforms),
        patch.object(
            hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry
        ) as update,
        patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload,
    ):
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        assert config_entry.state is ConfigEntryState.NOT_LOADED
        assert not coordinator.token_persistence_active
        _respond_with_token(offline_sdk, LATE_TOKEN)
        await auth.refresh()
        assert auth.refresh_token == LATE_TOKEN
        assert config_entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN
        await hass.async_block_till_done()
        update.assert_not_called()
        reload.assert_not_called()


async def test_active_rotation_persists_once_without_reloading(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    offline_sdk: MagicMock,
) -> None:
    auth = await _setup(hass, config_entry)
    _respond_with_token(offline_sdk, ROTATED_TOKEN)
    with (
        patch.object(auth, "_notify_rotated", wraps=auth._notify_rotated) as notified,
        patch.object(
            hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry
        ) as update,
        patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload,
    ):
        await auth.refresh()
        await auth.refresh()
        assert auth.refresh_token == ROTATED_TOKEN
        assert config_entry.data[CONF_REFRESH_TOKEN] == ROTATED_TOKEN
        update.assert_called_once_with(
            config_entry, data={CONF_REFRESH_TOKEN: ROTATED_TOKEN, CONF_DEVICE_ID: DEVICE_ID}
        )
        notified.assert_awaited_once_with(ROTATED_TOKEN)
        await hass.async_block_till_done()
        reload.assert_not_called()
