"""All expansion tests use synthetic SDK models and the actual HA harness."""

from __future__ import annotations

import asyncio
import json
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from casahome import CasaApiError, CasaAuthError, CasaConnectionError, CasaResponseError
from casahome.models.calendar import VisitsCalendarResponse
from casahome.models.mailbox import MailboxMailItemsResponse, PackagesResponse
from casahome.models.maintenance import InventoryServiceHistoryResponse
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.casahome.const import DOMAIN
from custom_components.casahome.read_coordinator import CasaReadCoordinator
from custom_components.casahome.read_entities import READ_BINARY_SENSORS, READ_SENSORS
from custom_components.casahome.read_models import (
    ReadData,
    ReadFamily,
    history_data,
    inventory_data,
    mail_data,
    maintenance_data,
    package_data,
    request_data,
    service_calendar_data,
)
from custom_components.casahome.service_calendar import CasaServiceCalendar

from . import expanded_fixtures as fixtures
from .conftest import HOME_ID, bootstrap_model

METHODS = {
    ReadFamily.INVENTORY: "home_details",
    ReadFamily.REQUESTS: "requests_tab",
    ReadFamily.MAINTENANCE: "autopilot_plans",
    ReadFamily.HISTORY: "inventory_service_history",
    ReadFamily.MAIL: "mail_items",
    ReadFamily.PACKAGES: "packages",
    ReadFamily.CALENDAR: "calendar",
}


def coordinator(entry: MockConfigEntry, family: ReadFamily) -> CasaReadCoordinator:
    result: CasaReadCoordinator = entry.runtime_data.read_coordinators[HOME_ID][family]
    return result


def entity_id(hass: HomeAssistant, key: str, domain: str = "sensor") -> str:
    result = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{HOME_ID}_{key}")
    assert result is not None
    return result


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def family_keys(family: ReadFamily) -> list[tuple[str, str]]:
    return (
        [("sensor", key) for f, key in READ_SENSORS if f == family]
        + [("binary_sensor", key) for f, key, _ in READ_BINARY_SENSORS if f == family]
        + ([("calendar", "service_visits")] if family == ReadFamily.CALENDAR else [])
    )


def test_allowlisted_projections_and_explicit_counts() -> None:
    inv = fixtures.inventory("oven")
    bulb = inv.entities[0].model_copy(update={"id": "bulb", "category": "Bulbs"})
    paint = bulb.model_copy(update={"id": "paint", "category": "Paint"})
    inv.entities.extend([bulb, paint, inv.entities[0]])
    result = inventory_data(inv)
    assert result.count("inventory_count") == 3
    assert len(result.inventory) == 1
    record = result.inventory[0]
    assert (record.manufacturer, record.model, record.serial_number, record.room) == (
        "Test Make",
        "Test Model",
        "SYNTHETIC-0001",
        "Test kitchen",
    )
    assert record.status == "future_status"  # Don't guess new record states.
    assert "private" not in repr(result)
    requests = fixtures.requests(
        fixtures.request_card(
            "r",
            has_unread_messages=True,
            concierge_awaiting_reply=True,
            pending_schedule_prompt_count=3,
        ),
        fixtures.request_card("r"),
    )
    assert request_data(requests).counts == (
        ("requests_needs_review", 1),
        ("requests_unread_fetched", 1),
        ("requests_awaiting_reply", 1),
        ("requests_scheduling_approval", 1),
        ("requests_need_attention", 1),
    )
    assert "private" not in repr(request_data(requests))
    assert maintenance_data(fixtures.maintenance()).counts == (
        ("maintenance_enabled", 1),
        ("maintenance_eligible", 1),
    )
    history = InventoryServiceHistoryResponse.model_validate(
        {
            "autopilot_plans": [{"title": "private"}],
            "history_items": [{"task_id": "t", "task_title": "private", "serviced_at": 1}],
            "future": "private",
        }
    )
    assert history_data(history).counts == (("service_history_fetched", 1),)
    assert "private" not in repr(history_data(history))


def test_mail_and_packages_use_distinct_explicit_archive_fields() -> None:
    mail = MailboxMailItemsResponse.model_validate(
        {
            "mail_items": [
                {
                    "id": "unread",
                    "scan_status": "future",
                    "received_at": "opaque",
                    "sender": "private",
                    "summary": "private",
                },
                {
                    "id": "read",
                    "scan_status": "future",
                    "received_at": "opaque",
                    "read_at": "opaque",
                },
                {
                    "id": "archived",
                    "scan_status": "future",
                    "received_at": "opaque",
                    "archived_at": "opaque",
                },
            ]
        }
    )
    assert mail_data(mail).count("mail_unread_fetched") == 1
    package = {
        "id": "p",
        "carrier_id": "carrier",
        "created_at": 1,
        "updated_at": 1,
        "status": "future",
        "status_detail": "private",
        "direction": "future",
        "tracking_number": "private",
        "purchased_item": {"private": "private"},
    }
    packages = fixtures.packages().model_copy(
        update={
            "packages": PackagesResponse.model_validate(
                {
                    "next_pickup_lockdown": 0,
                    "next_pickup_start": 0,
                    "next_pickup_end": 0,
                    "packages": [package, {**package, "id": "archived", "archived": True}],
                }
            ).packages
        }
    )
    assert package_data(packages).count("packages_nonarchived") == 1
    assert "private" not in repr(mail_data(mail)) + repr(package_data(packages))


async def test_all_entities_and_exact_get_budget(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    mock_api.home_details.return_value = fixtures.inventory("oven")
    mock_api.requests_tab.return_value = fixtures.requests(
        fixtures.request_card(
            "r",
            has_unread_messages=True,
            concierge_awaiting_reply=True,
            pending_schedule_prompt_count=2,
        )
    )
    mock_api.autopilot_plans.return_value = fixtures.maintenance()
    mock_api.inventory_service_history.return_value = (
        InventoryServiceHistoryResponse.model_validate(
            {
                "autopilot_plans": [],
                "history_items": [
                    {"task_id": "history", "task_title": "private", "serviced_at": 1}
                ],
            }
        )
    )
    mock_api.mail_items.return_value = MailboxMailItemsResponse.model_validate(
        {
            "mail_items": [
                {
                    "id": "mail",
                    "scan_status": "future",
                    "received_at": "opaque",
                    "sender": "private",
                    "summary": "private",
                }
            ]
        }
    )
    mock_api.packages.return_value = PackagesResponse.model_validate(
        {
            "next_pickup_lockdown": 0,
            "next_pickup_start": 0,
            "next_pickup_end": 0,
            "packages": [
                {
                    "id": "package",
                    "carrier_id": "carrier",
                    "created_at": 1,
                    "updated_at": 1,
                    "status": "future",
                    "status_detail": "private",
                    "direction": "future",
                    "tracking_number": "private",
                    "pickup_qr_code": "private",
                    "purchased_item": {"private": "private"},
                }
            ],
        }
    )
    now = dt_util.utcnow()
    mock_api.calendar.return_value = VisitsCalendarResponse.model_validate(
        {
            "events": [
                {
                    "id": "visit",
                    "start_time": int((now + timedelta(hours=1)).timestamp()),
                    "end_time": int((now + timedelta(hours=2)).timestamp()),
                    "kind": {"private": "private"},
                }
            ]
        }
    )
    await setup(hass, config_entry)
    entries = er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
    assert len(entries) == 34  # 33 home entities and one inventory status.
    expected = {
        "inventory_count": "1",
        "requests_needs_review": "1",
        "maintenance_enabled": "1",
        "maintenance_eligible": "1",
        "mail_unread_fetched": "1",
        "packages_nonarchived": "1",
        "service_history_fetched": "1",
        "requests_unread_fetched": "1",
        "requests_awaiting_reply": "1",
        "requests_scheduling_approval": "1",
    }
    for key, value in expected.items():
        state = hass.states.get(entity_id(hass, key))
        assert state is not None and state.state == value
        assert state.attributes["scope"] == "fetched_response"
    for key in ("requests_attention", "mail_attention", "packages_present"):
        state = hass.states.get(entity_id(hass, key, "binary_sensor"))
        assert state is not None and state.state == "on"
    for family, method in METHODS.items():
        call = getattr(mock_api, method)
        assert call.await_count == 1
        if family == ReadFamily.HISTORY:
            call.assert_awaited_once_with(HOME_ID, "home", HOME_ID)
        elif family == ReadFamily.CALENDAR:
            home, start, end = call.call_args.args
            assert home == HOME_ID and end - start == timedelta(days=31)
            assert start.tzinfo == end.tzinfo == UTC
        else:
            call.assert_awaited_once_with(HOME_ID)
    mock_api.bootstrap.assert_awaited_once()
    serialized = json.dumps([dict(state.attributes) for state in hass.states.async_all()])
    assert "private" not in serialized
    assert not list(ar.async_get(hass).async_list_areas())


@pytest.mark.parametrize("family", list(ReadFamily))
async def test_disabled_family_no_setup_io_or_periodic_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    family: ReadFamily,
) -> None:
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for domain, key in family_keys(family):
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{HOME_ID}_{key}",
            config_entry=config_entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    co = coordinator(config_entry, family)
    assert not list(co.async_contexts())
    await co.async_refresh()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=2))
    await hass.async_block_till_done(wait_background_tasks=True)
    getattr(mock_api, METHODS[family]).assert_not_called()
    assert co._unsub_refresh is None


async def test_catalog_daily_dynamic_fifteen_minute_rates(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    await setup(hass, config_entry)
    for method in METHODS.values():
        getattr(mock_api, method).reset_mock()
    now = dt_util.utcnow()
    async_fire_time_changed(hass, now + timedelta(minutes=16))
    await hass.async_block_till_done(wait_background_tasks=True)
    for family, method in METHODS.items():
        assert getattr(mock_api, method).await_count == (
            0 if family in {ReadFamily.INVENTORY, ReadFamily.MAINTENANCE} else 1
        )
    async_fire_time_changed(hass, now + timedelta(hours=24, minutes=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api.home_details.await_count == mock_api.autopilot_plans.await_count == 1


async def test_disabling_all_entities_does_not_keep_bootstrap_alive(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    await setup(hass, config_entry)
    main = config_entry.runtime_data
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, config_entry.entry_id):
        registry.async_update_entity(
            registered.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not main._listeners and main._unsub_refresh is None
    for co in main.read_coordinators[HOME_ID].values():
        assert not co._listeners and co._unsub_main is None
    mock_api.bootstrap.reset_mock()
    for method in METHODS.values():
        getattr(mock_api, method).reset_mock()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=2))
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_api.bootstrap.assert_not_called()
    for method in METHODS.values():
        getattr(mock_api, method).assert_not_called()


@pytest.mark.parametrize("family", list(ReadFamily))
@pytest.mark.parametrize(
    "error",
    [
        CasaConnectionError("private-error"),
        CasaResponseError("private-schema"),
        CasaApiError(500, "private-http"),
        TimeoutError(),
    ],
)
async def test_failure_isolated_clears_cache_recovers_and_hides_errors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    family: ReadFamily,
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup(hass, config_entry)
    co = coordinator(config_entry, family)
    getattr(mock_api, METHODS[family]).side_effect = error
    with pytest.raises(UpdateFailed) as raised:
        await co._async_update_data()
    assert raised.value.__suppress_context__
    assert "private" not in "".join(traceback.format_exception(raised.value))
    await co.async_refresh()
    assert co.data == ReadData() and not co.last_update_success
    assert all(
        coordinator(config_entry, other).data.counts is not None
        for other in ReadFamily
        if other != family
    )
    assert "private" not in caplog.text
    getattr(mock_api, METHODS[family]).side_effect = None
    await co.async_refresh()
    assert co.last_update_success and co.data.counts is not None


@pytest.mark.parametrize("family", list(ReadFamily))
@pytest.mark.parametrize("status", [404, 405])
async def test_disable_once_until_reload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    family: ReadFamily,
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup(hass, config_entry)
    call = getattr(mock_api, METHODS[family])
    call.reset_mock()
    call.side_effect = CasaApiError(status, "private", display_message="private")
    co = coordinator(config_entry, family)
    for _ in range(3):
        await co.async_refresh()
    assert call.await_count == 1 and co.disabled
    assert co.data == ReadData() and co._unsub_refresh is None
    assert sum("Disabled until reload" in record.message for record in caplog.records) == 1
    assert "private" not in caplog.text
    call.side_effect = None
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not coordinator(config_entry, family).disabled and call.await_count == 2


@pytest.mark.parametrize("family", list(ReadFamily))
@pytest.mark.parametrize("stale_missing", [False, True])
async def test_home_loss_inflight_drops_stale_success_and_stale_404(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    family: ReadFamily,
    stale_missing: bool,
) -> None:
    await setup(hass, config_entry)
    co = coordinator(config_entry, family)
    main = config_entry.runtime_data
    entered, release = asyncio.Event(), asyncio.Event()
    call = getattr(mock_api, METHODS[family])

    async def blocked(*args: Any) -> Any:
        entered.set()
        await release.wait()
        if stale_missing:
            raise CasaApiError(404, "private-stale-error")
        return call.return_value

    call.side_effect = blocked
    refresh = asyncio.create_task(co.async_refresh())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        main.async_set_updated_data(bootstrap_model(homes=[]))
        assert co.data == ReadData() and co._unsub_refresh is None
        await co._async_update_data()
        main.async_set_updated_data(bootstrap_model())
        release.set()
        await asyncio.wait_for(refresh, 5)
        assert not co.disabled and co.data == ReadData()
    finally:
        release.set()
        await refresh
        call.side_effect = None
        await co.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert co.data.counts is not None


@pytest.mark.parametrize("family", list(ReadFamily))
@pytest.mark.parametrize("bootstrap_fails", [False, True])
async def test_home_loss_stops_io_and_failed_recovery_never_restores_cache(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    family: ReadFamily,
    bootstrap_fails: bool,
) -> None:
    await setup(hass, config_entry)
    co = coordinator(config_entry, family)
    main = config_entry.runtime_data
    call = getattr(mock_api, METHODS[family])
    call.reset_mock()
    if bootstrap_fails:
        main.async_set_update_error(UpdateFailed("Synthetic bootstrap failure"))
    else:
        main.async_set_updated_data(bootstrap_model(homes=[]))
    assert co.data == ReadData() and co._unsub_refresh is None
    for domain, key in family_keys(family):
        state = hass.states.get(entity_id(hass, key, domain))
        assert state is not None and state.state == "unavailable"
    await co.async_refresh()
    call.assert_not_called()
    call.side_effect = CasaConnectionError("private-recovery")
    main.async_set_updated_data(bootstrap_model())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert co.data == ReadData() and not co.last_update_success
    call.side_effect = None
    await co.async_refresh()
    assert co.data.counts is not None


async def test_slow_new_family_does_not_block_setup_or_other_families(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def block(home_id: str) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    mock_api.requests_tab.side_effect = block
    config_entry.add_to_hass(hass)
    assert await asyncio.wait_for(hass.config_entries.async_setup(config_entry.entry_id), 5)
    await asyncio.wait_for(entered.wait(), 5)
    await hass.async_block_till_done()
    for family, method in METHODS.items():
        assert getattr(mock_api, method).await_count == 1
        if family != ReadFamily.REQUESTS:
            assert coordinator(config_entry, family).data.counts is not None
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert cancelled.is_set()


@pytest.mark.parametrize("family", list(ReadFamily))
async def test_disable_reenable_inflight_cannot_publish_old_generation(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    family: ReadFamily,
) -> None:
    await setup(hass, config_entry)
    co = CasaReadCoordinator(hass, config_entry, config_entry.runtime_data, HOME_ID, family)
    remove_context = co.async_add_listener(lambda: None, context=family)
    await co.async_start()
    entered, release = asyncio.Event(), asyncio.Event()
    original = co._fetch_projection

    async def blocked() -> ReadData:
        entered.set()
        await release.wait()
        return await original()

    # Exercise public contexts without HA's delayed enabling reload.
    from unittest.mock import patch

    with patch.object(co, "_fetch_projection", side_effect=blocked):
        refresh = asyncio.create_task(co.async_refresh())
        await asyncio.wait_for(entered.wait(), 5)
        remove_context()
        assert co.data == ReadData()
        remove_restored = co.async_add_listener(lambda: None, context=family)
        release.set()
        await asyncio.wait_for(refresh, 5)
        assert co.data == ReadData()
    await co.async_refresh()
    assert co.data.counts is not None
    remove_restored()


async def test_inventory_dynamic_devices_removal_metadata_and_user_customizations(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    mock_api.home_details.return_value = fixtures.inventory("oven")
    await setup(hass, config_entry)
    co = coordinator(config_entry, ReadFamily.INVENTORY)
    registry = dr.async_get(hass)
    parent = registry.async_get_device_by_identifier((DOMAIN, HOME_ID), config_entry.entry_id)
    device = registry.async_get_device_by_identifier(
        (DOMAIN, f"{HOME_ID}_inventory_oven"), config_entry.entry_id
    )
    assert parent is not None and device is not None
    assert device.via_device_id == parent.id
    assert (device.manufacturer, device.model, device.serial_number) == (
        "Test Make",
        "Test Model",
        "SYNTHETIC-0001",
    )
    area = ar.async_get(hass).async_create("User room")
    registry.async_update_device(device.id, name_by_user="My appliance", area_id=area.id)
    status_id = entity_id(hass, "inventory_oven_status")
    er.async_get(hass).async_update_entity(status_id, name="My status")
    mock_api.home_details.return_value = fixtures.inventory("second")
    await co.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(status_id)
    assert state is not None and state.state == "unavailable"
    assert registry.async_get(device.id) is not None
    assert (
        registry.async_get_device_by_identifier(
            (DOMAIN, f"{HOME_ID}_inventory_second"), config_entry.entry_id
        )
        is not None
    )
    replacement = fixtures.inventory("oven", "second")
    assert replacement.entities[0].model is not None
    replacement.entities[0].model.name = "New Test Model"
    replacement.entities[0].status = "new_status"
    mock_api.home_details.return_value = replacement
    await co.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    updated = registry.async_get(device.id, include_child_devices=False)
    assert updated is not None
    assert updated.model == "New Test Model"
    assert updated.name_by_user == "My appliance" and updated.area_id == area.id
    registered = er.async_get(hass).async_get(status_id)
    assert registered is not None and registered.name == "My status"
    state = hass.states.get(status_id)
    assert state is not None and state.state == "new_status"
    assert state.attributes["suggested_room"] == "Test kitchen"
    er.async_get(hass).async_update_entity(status_id, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    replacement.entities[0].model.name = "Model while diagnostic disabled"
    await co.async_refresh()
    updated = registry.async_get(device.id, include_child_devices=False)
    assert updated is not None and updated.model == "Model while diagnostic disabled"
    for key in ("inventory_count", "inventory_second_status"):
        er.async_get(hass).async_update_entity(
            entity_id(hass, key), disabled_by=er.RegistryEntryDisabler.USER
        )
    await hass.async_block_till_done()
    mock_api.home_details.reset_mock()
    await co.async_refresh()
    mock_api.home_details.assert_not_called()
    assert not list(co.async_contexts()) and co.inventory_listener is not None


def test_calendar_projection_dst_overlap_dedup_and_no_kind_exports() -> None:
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=0)
    end = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
    event = {
        "id": "visit",
        "start_time": int(start.timestamp()),
        "end_time": int(end.timestamp()),
        "kind": {"private": "private-vendor"},
        "location": "private-location",
    }
    response = VisitsCalendarResponse.model_validate(
        {
            "events": [
                event,
                event,
                {
                    **event,
                    "id": "outside",
                    "start_time": int(end.timestamp()) + 7200,
                    "end_time": int(end.timestamp()) + 10800,
                },
            ]
        }
    )
    data = service_calendar_data(response, start.astimezone(UTC), end.astimezone(UTC))
    assert len(data.events) == 1
    visit = data.events[0]
    assert (
        visit.summary == "Casa service visit"
        and visit.description is None
        and visit.location is None
    )
    assert visit.end - visit.start == timedelta(hours=1)  # type: ignore[operator]
    assert "private" not in repr(data)


@pytest.mark.parametrize(
    "start,end",
    [
        (0, 1),
        (1_900_000_000, 1_900_000_000),
        (1_900_000_001, 1_900_000_000),
        (1_900_000_000_000, 1_900_000_001_000),
    ],
)
def test_calendar_rejects_invalid_epoch_bounds(start: int, end: int) -> None:
    response = VisitsCalendarResponse.model_validate(
        {"events": [{"id": "v", "start_time": start, "end_time": end, "kind": "private"}]}
    )
    with pytest.raises(CasaResponseError, match="Invalid Casa service"):
        service_calendar_data(
            response, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
        )


async def test_calendar_cache_coverage_guards_and_no_query_io(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    await setup(hass, config_entry)
    co = coordinator(config_entry, ReadFamily.CALENDAR)
    home = config_entry.runtime_data.data.homes[0]
    calendar = CasaServiceCalendar(co, home, "service_visits")
    start, end = co.data.coverage_start, co.data.coverage_end
    assert start is not None and end is not None
    assert await calendar.async_get_events(hass, start, end) == []
    for s, e in [
        (start - timedelta(seconds=1), end),
        (start, end + timedelta(seconds=1)),
        (start.replace(tzinfo=None), end),
        (end, start),
    ]:
        with pytest.raises(HomeAssistantError):
            await calendar.async_get_events(hass, s, e)
    assert calendar.supported_features == 0
    assert calendar.extra_state_attributes["item_schema"] == "provisional"
    mock_api.calendar.assert_awaited_once()
    mock_api.calendar.side_effect = CasaResponseError("private")
    await co.async_refresh()
    assert calendar.event is None and co.data == ReadData()
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await calendar.async_get_events(hass, start, end)


@pytest.mark.parametrize("family", list(ReadFamily))
async def test_auth_privacy_and_unload_cancellation(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    family: ReadFamily,
) -> None:
    await setup(hass, config_entry)
    co = coordinator(config_entry, family)
    main = config_entry.runtime_data
    call = getattr(mock_api, METHODS[family])
    call.side_effect = CasaAuthError("private-token")
    with pytest.raises(ConfigEntryAuthFailed) as raised:
        await co._async_update_data()
    assert "private-token" not in "".join(traceback.format_exception(raised.value))
    assert co.data == ReadData()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def block(*args: Any) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    call.side_effect = block
    refresh = config_entry.async_create_background_task(
        hass, co.async_refresh(), name="Synthetic read"
    )
    await asyncio.wait_for(entered.wait(), 5)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert cancelled.is_set() and refresh.cancelled()
    assert not main._listeners
    for family_co in main.read_coordinators[HOME_ID].values():
        assert not family_co._listeners and family_co._unsub_refresh is None
        assert family_co.inventory_listener is None
        assert family_co._debounced_refresh._timer_task is None


@pytest.mark.parametrize(
    "disabler", [er.RegistryEntryDisabler.USER, er.RegistryEntryDisabler.INTEGRATION, None]
)
async def test_collective_migration_preserves_user_choice(
    hass: HomeAssistant,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    config_entry: MockConfigEntry,
    disabler: er.RegistryEntryDisabler | None,
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, minor_version=2)
    registered = er.async_get(hass).async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{HOME_ID}_casa_collective",
        config_entry=config_entry,
        disabled_by=disabler,
    )
    before = dict(config_entry.data)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    updated = er.async_get(hass).async_get(registered.entity_id)
    assert updated is not None and updated.entity_id == registered.entity_id
    assert updated.disabled_by == (disabler if disabler == er.RegistryEntryDisabler.USER else None)
    assert config_entry.minor_version == 3 and dict(config_entry.data) == before
