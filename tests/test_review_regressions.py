"""Review regressions. Synthetic reads only, including real HA timer dispatch."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from casahome import CasaAuthError
from casahome.models.home import HomeDetailsResponse, HomeRoom
from casahome.models.mailbox import MailboxMailItemsResponse
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.casahome.const import DOMAIN
from custom_components.casahome.read_models import ReadData, ReadFamily, inventory_data

from . import expanded_fixtures as fixtures
from .conftest import HOME_ID, bootstrap_model
from .test_expanded_reads import coordinator, entity_id, setup


@pytest.mark.parametrize("record_missing", [False, True])
async def test_count_disabled_cold_reload_restores_status_context_without_cached_values(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    record_missing: bool,
) -> None:
    mock_api.home_details.return_value = fixtures.inventory("oven")
    await setup(hass, config_entry)
    registry = er.async_get(hass)
    status_id = entity_id(hass, "inventory_oven_status")
    devices = dr.async_get(hass)
    device = devices.async_get_device_by_identifier(
        (DOMAIN, f"{HOME_ID}_inventory_oven"), config_entry.entry_id
    )
    assert device is not None
    area = ar.async_get(hass).async_create("User area")
    devices.async_update_device(device.id, name_by_user="User appliance", area_id=area.id)
    registry.async_update_entity(status_id, name="User status")
    registry.async_update_entity(
        entity_id(hass, "inventory_count"), disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    old = coordinator(config_entry, ReadFamily.INVENTORY)
    mock_api.home_details.reset_mock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def fresh_inventory(home_id: str) -> HomeDetailsResponse:
        assert home_id == HOME_ID
        entered.set()
        await release.wait()
        return fixtures.inventory() if record_missing else fixtures.inventory("oven")

    mock_api.home_details.side_effect = fresh_inventory
    try:
        assert await hass.config_entries.async_reload(config_entry.entry_id)
        await asyncio.wait_for(entered.wait(), 5)
        current = coordinator(config_entry, ReadFamily.INVENTORY)
        assert current is not old and current.data == ReadData()
        assert list(current.async_contexts()) == [ReadFamily.INVENTORY]
        state = hass.states.get(status_id)
        assert state is not None and state.state == "unavailable"
        assert state.attributes.get("suggested_room") is None
        registered = registry.async_get(status_id)
        assert registered is not None and registered.name == "User status"
        restored_device = devices.async_get(device.id, include_child_devices=False)
        assert restored_device is not None
        assert restored_device.manufacturer == "Test Make"
        assert restored_device.model == "Test Model"
        assert restored_device.serial_number == "SYNTHETIC-0001"
        assert (
            restored_device.name_by_user == "User appliance" and restored_device.area_id == area.id
        )
        assert not old._listeners and old._unsub_main is None
        assert old._unsub_refresh is None and old.inventory_listener is None
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    mock_api.home_details.assert_awaited_once_with(HOME_ID)
    state = hass.states.get(status_id)
    assert state is not None
    assert state.state == ("unavailable" if record_missing else "future_status")
    if record_missing:
        mock_api.home_details.side_effect = None
        mock_api.home_details.return_value = fixtures.inventory("oven")
        await current.async_refresh()
        state = hass.states.get(status_id)
        assert state is not None and state.state == "future_status"
    # A missing registered record owns a normal entity context, not a permanent seed.
    registry.async_update_entity(status_id, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_api.home_details.reset_mock()
    await current.async_refresh()
    assert not list(current.async_contexts()) and current._unsub_main is None
    assert current._unsub_refresh is None
    mock_api.home_details.assert_not_called()


async def test_all_inventory_entities_disabled_before_reload_never_seed_io(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    mock_api.home_details.return_value = fixtures.inventory("oven")
    await setup(hass, config_entry)
    registry = er.async_get(hass)
    for key in ("inventory_count", "inventory_oven_status"):
        registry.async_update_entity(
            entity_id(hass, key), disabled_by=er.RegistryEntryDisabler.USER
        )
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_api.home_details.reset_mock()
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    co = coordinator(config_entry, ReadFamily.INVENTORY)
    assert not list(co.async_contexts()) and co._unsub_main is None
    assert co._unsub_refresh is None
    await co.async_refresh()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=2))
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_api.home_details.assert_not_called()
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert co.inventory_listener is None and not co._listeners
    assert co._debounced_refresh._timer_task is None


@pytest.mark.parametrize(
    "transition", ["home_removed", "home_reappeared", "contexts_removed", "shutdown"]
)
async def test_stale_auth_does_not_start_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    transition: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup(hass, config_entry)
    co = coordinator(config_entry, ReadFamily.MAIL)
    main = config_entry.runtime_data
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def old_auth_failure(home_id: str) -> MailboxMailItemsResponse:
        nonlocal calls
        calls += 1
        if calls > 1:
            return fixtures.mail()
        entered.set()
        # Even an SDK that surfaces auth while cleaning up mustn't start reauth.
        with suppress(asyncio.CancelledError):
            await release.wait()
        raise CasaAuthError("synthetic-private-old-auth")

    mock_api.mail_items.side_effect = old_auth_failure
    refresh = asyncio.create_task(co.async_refresh())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if transition.startswith("home_"):
            main.async_set_updated_data(bootstrap_model(homes=[]))
            if transition == "home_reappeared":
                main.async_set_updated_data(bootstrap_model())
        elif transition == "contexts_removed":
            registry = er.async_get(hass)
            for domain, key in (
                ("sensor", "mail_unread_fetched"),
                ("binary_sensor", "mail_attention"),
            ):
                registry.async_update_entity(
                    entity_id(hass, key, domain), disabled_by=er.RegistryEntryDisabler.USER
                )
            await hass.async_block_till_done()
            assert not list(co.async_contexts())
        else:
            await co.async_shutdown()
        release.set()
        await asyncio.wait_for(refresh, 5)
        assert co.data == ReadData()
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert "synthetic-private-old-auth" not in caplog.text
        if transition == "home_reappeared":
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
            await hass.async_block_till_done(wait_background_tasks=True)
            assert calls == 2 and co.data.counts is not None
        else:
            await co._async_update_data()
            assert calls == 1  # No fresh read for a vanished/disabled/shutdown family.
    finally:
        release.set()
        await refresh


async def test_current_auth_still_starts_private_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup(hass, config_entry)
    co = coordinator(config_entry, ReadFamily.MAIL)
    mock_api.mail_items.side_effect = CasaAuthError("synthetic-private-current-auth")
    await co.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1 and flows[0]["context"]["source"] == SOURCE_REAUTH
    assert co.data == ReadData() and not co.last_update_success
    assert co._fetch_task is None
    assert "synthetic-private-current-auth" not in caplog.text


@pytest.mark.parametrize("dispatcher", ["initial", "periodic", "debounce"])
async def test_sdk_fetch_cancelled_before_unload_returns_for_every_dispatcher(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    dispatcher: str,
) -> None:
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_mail(home_id: str) -> MailboxMailItemsResponse:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Confirm unload awaits cancellation cleanup rather than merely requesting it.
            await asyncio.sleep(0)
            cancelled.set()
            raise
        return fixtures.mail()

    if dispatcher == "initial":
        mock_api.mail_items.side_effect = blocked_mail
        config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        co = coordinator(config_entry, ReadFamily.MAIL)
    else:
        await setup(hass, config_entry)
        co = coordinator(config_entry, ReadFamily.MAIL)
        if dispatcher == "debounce":
            await co.async_request_refresh()  # Successful immediate call establishes cooldown.
            assert co._debounced_refresh._timer_task is not None
        mock_api.mail_items.side_effect = blocked_mail
        if dispatcher == "periodic":
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
        else:
            wrapper = config_entry.async_create_background_task(
                hass, co.async_request_refresh(), name="Synthetic queued refresh"
            )
            await wrapper
            assert wrapper.done() and co._fetch_task is None
            assert co._debounced_refresh._execute_at_end_of_timer
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    main = config_entry.runtime_data
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task = co._fetch_task
        assert task is not None and not task.done()
        assert await asyncio.wait_for(hass.config_entries.async_unload(config_entry.entry_id), 5)
        assert cancelled.is_set() and task.cancelled()  # Before another loop drain.
        assert co._fetch_task is None and co._unsub_refresh is None
        assert co._debounced_refresh._timer_task is None
        assert co._unsub_main is None and not main._listeners
        assert not co._listeners and not config_entry._background_tasks
        await hass.async_block_till_done(wait_background_tasks=True)
        assert not co._debounced_refresh._execute_lock.locked()
    finally:
        release.set()


@pytest.mark.parametrize("cancel_by", ["dispatcher", "deadline"])
async def test_sdk_child_is_cancelled_on_dispatcher_cancellation_or_timeout(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    cancel_by: str,
) -> None:
    await setup(hass, config_entry)
    co = coordinator(config_entry, ReadFamily.MAIL)
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_mail(home_id: str) -> MailboxMailItemsResponse:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            cancelled.set()
            raise
        return fixtures.mail()

    mock_api.mail_items.side_effect = blocked_mail
    timeout = 0.02 if cancel_by == "deadline" else 30
    with patch("custom_components.casahome.read_coordinator.REQUEST_TIMEOUT_SECONDS", timeout):
        refresh = asyncio.create_task(co.async_refresh())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            child = co._fetch_task
            assert child is not None
            if cancel_by == "dispatcher":
                refresh.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await refresh
            else:
                await asyncio.wait_for(refresh, 5)
                assert isinstance(co.last_exception, UpdateFailed)
            assert cancelled.is_set() and child.cancelled() and co._fetch_task is None
        finally:
            release.set()


@pytest.mark.parametrize(
    "refs,mapped,expected",
    [
        (["Kitchen"], False, "Kitchen"),
        (["Kitchen", "Laundry"], False, "Kitchen"),
        (["Laundry", "Kitchen"], False, "Laundry"),
        (["00000000-0000-4000-8000-000000000003"], False, "Kitchen"),
        ([], True, "Kitchen"),
        (["unknown-private-room"], True, "Kitchen"),
        (["unknown-private-room"], False, None),
        (["kitchen"], False, None),
    ],
)
def test_room_suggestions_accept_only_this_homes_exact_names_ids_and_map(
    refs: list[str], mapped: bool, expected: str | None
) -> None:
    response = fixtures.inventory("oven")
    room_id = "00000000-0000-4000-8000-000000000003"
    response.home.stories[0].rooms = [
        HomeRoom(id=room_id, name="Kitchen"),
        HomeRoom(id="laundry", name="Laundry"),
    ]
    response.entities[0].rooms = refs
    response.room_entity_map = {room_id: ["oven"]} if mapped else {}
    data = inventory_data(response)
    assert data.inventory[0].room == expected
    assert "unknown-private-room" not in repr(data)


async def test_room_name_suggestion_does_not_assign_or_create_areas(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    response = fixtures.inventory("oven")
    response.home.stories[0].rooms[0].name = "Kitchen"
    response.entities[0].rooms = ["Kitchen"]
    mock_api.home_details.return_value = response
    await setup(hass, config_entry)
    state = hass.states.get(entity_id(hass, "inventory_oven_status"))
    assert state is not None and state.attributes["suggested_room"] == "Kitchen"
    assert not list(ar.async_get(hass).async_list_areas())
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{HOME_ID}_inventory_oven"), config_entry.entry_id
    )
    assert device is not None and device.area_id is None
