"""Synthetic inventory naming and placement through HA's real registries."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import pytest
from casahome.models.home import HomeDetailsResponse, HomeRoom
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casahome.const import DOMAIN
from custom_components.casahome.inventory_areas import InventoryAreaMarkers
from custom_components.casahome.inventory_devices import CasaInventoryStatus
from custom_components.casahome.inventory_registry import InventoryDeviceRegistry
from custom_components.casahome.read_models import InventoryRecord, ReadFamily, inventory_data

from . import expanded_fixtures as fixtures
from .conftest import HOME_ID
from .test_expanded_reads import coordinator, entity_id, setup


def record(
    record_id: str,
    *,
    name: str = "Backup Battery",
    room: str | None = "Garage",
    make: str | None = "Tesla",
    model: str | None = "Powerwall",
    known_rooms: tuple[str, ...] = (),
) -> InventoryRecord:
    return InventoryRecord(record_id, name, "ready", make, model, "TEST-SERIAL", room, known_rooms)


def response(*records: InventoryRecord) -> HomeDetailsResponse:
    result = fixtures.inventory(*(item.id for item in records))
    room_names = dict.fromkeys(
        room for item in records for room in item.known_rooms or ((item.room,) if item.room else ())
    )
    result.home.stories[0].rooms = [
        HomeRoom(id=f"room-{index}", name=name) for index, name in enumerate(room_names)
    ]
    for source, item in zip(result.entities, records, strict=True):
        source.type = item.name
        source.rooms = list(item.known_rooms or ((item.room,) if item.room else ()))
        assert source.make is not None and source.model is not None
        source.make.name = item.manufacturer or ""
        source.model.name = item.model or ""
        if item.manufacturer is None:
            source.make = None
        if item.model is None:
            source.model = None
    return result


def device(hass: HomeAssistant, entry: MockConfigEntry, record_id: str) -> dr.DeviceEntry:
    result = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{HOME_ID}_inventory_{record_id}"), entry.entry_id
    )
    assert result is not None
    return result


async def owner(
    hass: HomeAssistant, entry: MockConfigEntry, home_id: str = HOME_ID
) -> InventoryDeviceRegistry:
    markers = await InventoryAreaMarkers.async_load(hass, entry.entry_id)
    return InventoryDeviceRegistry(hass, entry.entry_id, home_id, markers)


async def refresh(
    hass: HomeAssistant, entry: MockConfigEntry, api: MagicMock, *records: InventoryRecord
) -> None:
    api.home_details.return_value = response(*records)
    await coordinator(entry, ReadFamily.INVENTORY).async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_four_identical_batteries_keep_numbers_across_membership_and_reload(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    items = tuple(record(key) for key in ("d", "b", "c", "a"))
    mock_api.home_details.return_value = response(*items)
    await setup(hass, config_entry)
    original = {item.id: device(hass, config_entry, item.id) for item in items}
    names = {key: entry.name for key, entry in original.items()}
    assert names == {
        "a": "Garage Backup Battery 1",
        "b": "Garage Backup Battery 2",
        "c": "Garage Backup Battery 3",
        "d": "Garage Backup Battery 4",
    }
    ids = {item.id: entity_id(hass, f"inventory_{item.id}_status") for item in items}
    for entry in original.values():
        assert entry.manufacturer == "Tesla" and entry.model == "Powerwall"
        assert not entry.connections and entry.entry_type is dr.DeviceEntryType.SERVICE
    await refresh(hass, config_entry, mock_api, *reversed(items))
    await refresh(hass, config_entry, mock_api, record("a"), record("c"), record("d"), record("0"))
    assert device(hass, config_entry, "0").name == "Garage Backup Battery 5"
    assert device(hass, config_entry, "b").name == names["b"]
    assert hass.states.get(ids["b"]).state == "unavailable"  # type: ignore[union-attr]
    await refresh(hass, config_entry, mock_api, record("0"), *items)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    for key, before in original.items():
        current = device(hass, config_entry, key)
        assert (current.id, current.name, current.identifiers) == (
            before.id,
            names[key],
            before.identifiers,
        )
        assert entity_id(hass, f"inventory_{key}_status") == ids[key]
        registered = er.async_get(hass).async_get(ids[key])
        assert (
            registered is not None and registered.unique_id == f"{HOME_ID}_inventory_{key}_status"
        )
    assert device(hass, config_entry, "0").name == "Garage Backup Battery 5"
    # A singleton doesn't lose its ordinal while its peers are temporarily absent.
    await refresh(hass, config_entry, mock_api, record("c"))
    assert device(hass, config_entry, "c").name == "Garage Backup Battery 3"
    await refresh(hass, config_entry, mock_api, *items, record("0"))
    assert {item.id: device(hass, config_entry, item.id).name for item in items} == names


async def test_initial_order_does_not_choose_ordinals(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    registry = await owner(hass, config_entry)
    items = (record("b"), record("a"), record("c"))
    registry.async_refresh(items)
    first = {item.id: device(hass, config_entry, item.id).name for item in items}
    for item in items:
        dr.async_get(hass).async_remove_device(device(hass, config_entry, item.id).id)
    registry.async_refresh(tuple(reversed(items)))
    assert {item.id: device(hass, config_entry, item.id).name for item in items} == first


async def test_new_peers_forcing_a_qualifier_keep_established_numbers(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    registry = await owner(hass, config_entry)
    a, b = record("a", model="M1"), record("b", model="M1")
    registry.async_refresh((a, b))
    assert device(hass, config_entry, "a").name == "Garage Backup Battery 1"
    assert device(hass, config_entry, "b").name == "Garage Backup Battery 2"
    # A lower-sorting M1 peer and a different model arrive together. The existing
    # devices keep their numbers inside the new qualified label.
    new = (record("0", model="M1"), a, b, record("z", model="M2"))
    registry.async_refresh(new)
    names = {item.id: device(hass, config_entry, item.id).name for item in new}
    assert names == {
        "0": "Garage Backup Battery (M1) 3",
        "a": "Garage Backup Battery (M1) 1",
        "b": "Garage Backup Battery (M1) 2",
        "z": "Garage Backup Battery (M2)",
    }
    registry.async_refresh(tuple(reversed(new)))
    assert {item.id: device(hass, config_entry, item.id).name for item in new} == names


async def test_distinct_models_and_makes_disambiguate_and_do_not_collapse_on_removal(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    items = (
        record("dish-a", name="Dishwasher", room="Kitchen", make="Bosch", model="Series 800"),
        record("dish-b", name="Dishwasher", room="Kitchen", make="Bosch", model="Series 500"),
        record("panel-a", name="Panel", model=None, make="Acme"),
        record("panel-b", name="Panel", model=None, make="Example"),
    )
    mock_api.home_details.return_value = response(*items)
    await setup(hass, config_entry)
    assert device(hass, config_entry, "dish-a").name == "Kitchen Dishwasher (Series 800)"
    assert device(hass, config_entry, "dish-b").name == "Kitchen Dishwasher (Series 500)"
    assert device(hass, config_entry, "panel-a").name == "Garage Panel (Acme)"
    assert device(hass, config_entry, "panel-b").name == "Garage Panel (Example)"
    await refresh(hass, config_entry, mock_api, items[0])
    assert device(hass, config_entry, "dish-a").name == "Kitchen Dishwasher (Series 800)"
    await refresh(hass, config_entry, mock_api, *reversed(items))
    assert device(hass, config_entry, "dish-a").name == "Kitchen Dishwasher (Series 800)"


async def test_model_and_make_qualifiers_survive_a_change_in_group_discriminator(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    items = (
        record("a", name="Panel", model="M1", make="Acme"),
        record("b", name="Panel", model="M1", make="Example"),
        record("c", name="Panel", model="M2", make="Acme"),
    )
    mock_api.home_details.return_value = response(*items)
    await setup(hass, config_entry)
    expected = {
        "a": "Garage Panel (Acme M1)",
        "b": "Garage Panel (Example M1)",
        "c": "Garage Panel (M2)",
    }
    assert {item.id: device(hass, config_entry, item.id).name for item in items} == expected
    # M2 disappearing leaves different makes but just one model. The compatible
    # combined qualifiers must stay, rather than switching to make-only labels.
    await refresh(hass, config_entry, mock_api, *items[:2])
    assert {item.id: device(hass, config_entry, item.id).name for item in items} == expected
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await refresh(hass, config_entry, mock_api, *reversed(items))
    assert {item.id: device(hass, config_entry, item.id).name for item in items} == expected
    # Actual source metadata changes still update the default and clear metadata
    # that the source no longer supplies, without affecting registry identity.
    before = device(hass, config_entry, "a")
    await refresh(hass, config_entry, mock_api, replace(items[0], model=None, manufacturer=None))
    updated = device(hass, config_entry, "a")
    assert updated.id == before.id and updated.name == "Garage Panel"
    assert updated.model is None and updated.manufacturer is None


@pytest.mark.parametrize(
    "item,expected",
    [
        (record("a", name="Air Purifier", room="Bedroom"), "Bedroom Air Purifier"),
        (record("a", name="Kitchen Hood", room="Kitchen"), "Kitchen Hood"),
        (record("a", name="KitchenHood", room="Kitchen"), "KitchenHood"),
        (record("a", name="Air Purifier", room=None, make="Acme"), "Acme Air Purifier"),
        (record("a", name="Air Purifier", room=None, make=None), "Air Purifier"),
        (record("a", name="", room=None, make=None), "Inventory item"),
        (
            record(
                "a",
                name="Air Purifier",
                room="Kitchen",
                known_rooms=("Kitchen", "Laundry"),
                make="Acme",
            ),
            "Acme Air Purifier",
        ),
    ],
)
async def test_base_names_and_legacy_room_projection_compatibility(
    hass: HomeAssistant, config_entry: MockConfigEntry, item: InventoryRecord, expected: str
) -> None:
    config_entry.add_to_hass(hass)
    (await owner(hass, config_entry)).async_refresh((item,))
    assert device(hass, config_entry, "a").name == expected


async def test_different_room_purifiers_and_unknown_source_room(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    items = (
        record("a", name="Air Purifier", room="Bedroom"),
        record("b", name="Air Purifier", room="Office"),
        record("c", name="Air Purifier", room=None, make="Acme"),
    )
    data = response(*items)
    data.entities[2].rooms = ["unknown-private-room-id"]
    mock_api.home_details.return_value = data
    await setup(hass, config_entry)
    assert [device(hass, config_entry, item.id).name for item in items] == [
        "Bedroom Air Purifier",
        "Office Air Purifier",
        "Acme Air Purifier",
    ]
    assert "unknown-private-room-id" not in repr(
        coordinator(config_entry, ReadFamily.INVENTORY).data
    )


async def test_metadata_updates_names_without_overwriting_entity_overrides(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    ar.async_get(hass).async_create("Garage")
    # A custom device name isn't an area choice, so a managed area still follows Casa.
    area = ar.async_get(hass).async_create("Office")
    items = (record("a"), record("b"))
    mock_api.home_details.return_value = response(*items)
    await setup(hass, config_entry)
    status_id = entity_id(hass, "inventory_a_status")
    entities = er.async_get(hass)
    entities.async_update_entity(
        status_id, new_entity_id="sensor.custom_battery", name="Custom status"
    )
    devices = dr.async_get(hass)
    before = device(hass, config_entry, "a")
    devices.async_update_device(before.id, name_by_user="My battery")
    await refresh(
        hass,
        config_entry,
        mock_api,
        replace(items[0], room="Office", model="Powerwall 3"),
        items[1],
    )
    updated = device(hass, config_entry, "a")
    assert updated.name == "Office Backup Battery"
    assert updated.model == "Powerwall 3"
    assert updated.name_by_user == "My battery" and updated.area_id == area.id
    registered = entities.async_get("sensor.custom_battery")
    assert registered is not None and registered.name == "Custom status"
    assert registered.unique_id == f"{HOME_ID}_inventory_a_status"
    assert updated.id == before.id and updated.identifiers == before.identifiers
    entities.async_update_entity("sensor.custom_battery", disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done(wait_background_tasks=True)
    await refresh(
        hass,
        config_entry,
        mock_api,
        replace(items[0], room="Office", manufacturer="New Make", model="New Model"),
        record("c", room="Office", model="Other Model"),
    )
    updated = device(hass, config_entry, "a")
    assert updated.name == "Office Backup Battery (New Model)"
    assert updated.manufacturer == "New Make" and updated.model == "New Model"
    assert updated.name_by_user == "My battery" and updated.area_id == area.id
    registered = entities.async_get("sensor.custom_battery")
    assert registered is not None and registered.disabled_by is er.RegistryEntryDisabler.USER
    # Status callbacks/registration consume the selected registry name, not a bare default.
    co = coordinator(config_entry, ReadFamily.INVENTORY)
    status = CasaInventoryStatus(
        co,
        config_entry.runtime_data.data.homes[0],
        "a",
        await owner(hass, config_entry),
    )
    assert status.device_info["name"] == updated.name
    assert "suggested_area" not in status.device_info


@pytest.mark.parametrize("match", ["name", "alias", "same_area_name_and_alias"])
async def test_exact_casefold_whitespace_existing_area_placement_once(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    match: str,
) -> None:
    areas = ar.async_get(hass)
    area = areas.async_create(
        "Test kitchen" if match != "alias" else "Cooking",
        aliases={"  TEST   kitchen  "} if match != "name" else set(),
    )
    item = record("oven", name="Oven", room="test   KITCHEN")
    mock_api.home_details.return_value = response(item)
    await setup(hass, config_entry)
    before = device(hass, config_entry, "oven")
    assert before.area_id == area.id
    assert len(tuple(areas.async_list_areas())) == 1
    dr.async_get(hass).async_update_device(before.id, area_id=None)
    await refresh(hass, config_entry, mock_api, item)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert device(hass, config_entry, "oven").area_id is None


@pytest.mark.parametrize(
    "case",
    [
        "absent",
        "fuzzy",
        "ambiguous_alias",
        "name_alias_collision",
        "multiple_rooms",
        "duplicate_room_names",
    ],
)
async def test_nonexact_or_ambiguous_placement_never_creates_areas(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    case: str,
) -> None:
    areas = ar.async_get(hass)
    item = record("a", room="Kitchen")
    if case == "fuzzy":
        areas.async_create("Main Kitchen")
    elif case == "ambiguous_alias":
        areas.async_create("Cooking", aliases={"Kitchen"})
        areas.async_create("Other", aliases={"kitchen"})
    elif case == "name_alias_collision":
        areas.async_create("Kitchen")
        areas.async_create("Cooking", aliases={"Kitchen"})
    elif case in {"multiple_rooms", "duplicate_room_names"}:
        areas.async_create("Kitchen")
        areas.async_create("Laundry")
        if case == "multiple_rooms":
            item = replace(item, known_rooms=("Kitchen", "Laundry"))
    data = response(item)
    if case == "duplicate_room_names":
        data.home.stories[0].rooms.append(HomeRoom(id="second-kitchen", name="Kitchen"))
    original = tuple(areas.async_list_areas())
    mock_api.home_details.return_value = data
    await setup(hass, config_entry)
    assert device(hass, config_entry, "a").area_id is None
    assert tuple(areas.async_list_areas()) == original
    state = hass.states.get(entity_id(hass, "inventory_a_status"))
    assert state is not None and state.attributes["suggested_room"] == "Kitchen"
    # The unplaced device stays managed. A later exact, unambiguous match places
    # it into the user's new area, but ambiguous rooms still never assign.
    for area in original:
        areas.async_delete(area.id)
    kitchen = areas.async_create("Kitchen")
    await coordinator(config_entry, ReadFamily.INVENTORY).async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    ambiguous = case in {"multiple_rooms", "duplicate_room_names"}
    assert device(hass, config_entry, "a").area_id == (None if ambiguous else kitchen.id)
    assert tuple(areas.async_list_areas()) == (kitchen,)


@pytest.mark.parametrize("override", ["none", "name", "area", "disabled", "nonlegacy"])
async def test_legacy_migration_respects_overrides_and_does_not_repeat_placement(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    override: str,
) -> None:
    config_entry.add_to_hass(hass)
    areas = ar.async_get(hass)
    garage = areas.async_create("Garage")
    custom = areas.async_create("Custom")
    devices = dr.async_get(hass)
    legacy = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"{HOME_ID}_inventory_a")},
        entry_type=dr.DeviceEntryType.SERVICE,
        name="Existing default" if override == "nonlegacy" else "Casa inventory Backup Battery",
    )
    if override == "name":
        devices.async_update_device(legacy.id, name_by_user="Custom battery")
    elif override == "area":
        devices.async_update_device(legacy.id, area_id=custom.id)
    elif override == "disabled":
        devices.async_update_device(legacy.id, disabled_by=dr.DeviceEntryDisabler.USER)
    registered = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{HOME_ID}_inventory_a_status",
        config_entry=config_entry,
        device_id=legacy.id,
        suggested_object_id="custom_inventory",
    )
    er.async_get(hass).async_update_entity(registered.entity_id, name="Custom entity")
    mock_api.home_details.return_value = response(record("a"), record("b"), record("c"))
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    migrated = device(hass, config_entry, "a")
    assert migrated.id == legacy.id and migrated.name == "Garage Backup Battery 1"
    assert migrated.area_id == (
        garage.id if override == "none" else custom.id if override == "area" else None
    )
    assert migrated.name_by_user == ("Custom battery" if override == "name" else None)
    assert migrated.disabled_by == (dr.DeviceEntryDisabler.USER if override == "disabled" else None)
    assert er.async_get(hass).async_get(registered.entity_id).name == "Custom entity"  # type: ignore[union-attr]
    assert entity_id(hass, "inventory_a_status") == registered.entity_id
    devices.async_update_device(migrated.id, area_id=None)
    await refresh(hass, config_entry, mock_api, record("0"), record("c"), record("a"), record("b"))
    assert device(hass, config_entry, "a").name == "Garage Backup Battery 1"
    assert device(hass, config_entry, "0").name == "Garage Backup Battery 4"
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert device(hass, config_entry, "a").area_id is None


async def test_entry_and_home_scoping_never_merges_or_updates_physical_devices(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    other = MockConfigEntry(domain="test", title="Other")
    other.add_to_hass(hass)
    devices = dr.async_get(hass)
    foreign = devices.async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={(DOMAIN, f"{HOME_ID}_inventory_a")},
        name="Physical battery",
        manufacturer="Tesla",
        model="Powerwall",
        serial_number="SYNTHETIC-0001",
    )
    config_entry.add_to_hass(hass)
    other_home = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "other_home_inventory_a")},
        entry_type=dr.DeviceEntryType.SERVICE,
        name="Garage Backup Battery 1",
    )
    mock_api.home_details.return_value = response(record("a"), record("b"))
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert device(hass, config_entry, "a").name == "Garage Backup Battery 1"
    assert devices.async_get(foreign.id, include_child_devices=False) == foreign
    assert devices.async_get(other_home.id, include_child_devices=False) == other_home
    assert device(hass, config_entry, "a").id != foreign.id


async def test_bounded_sanitized_names_keep_discriminators_and_ordinals(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    base = record(
        "a", name="Type " + "T" * 250, room="Room " + "R" * 250, model="Model " + "M" * 250
    )
    items = (base, replace(base, id="b"), replace(base, id="c", model="Other model"))
    data = response(*items)
    assert data.entities[0].type is not None
    data.entities[0].type += "\x00 https://example.invalid/private"
    mock_api.home_details.return_value = data
    await setup(hass, config_entry)
    names = [device(hass, config_entry, item.id).name for item in items]
    assert all(name is not None and len(name) <= 255 for name in names)
    assert len(set(names)) == 3
    assert names[0] is not None and names[0].endswith(") 1")
    assert names[1] is not None and names[1].endswith(") 2")
    assert names[2] is not None and names[2].endswith("(Other model)")
    assert "private" not in repr(names) and "\x00" not in repr(names)
    await refresh(hass, config_entry, mock_api, *reversed(items))
    assert [device(hass, config_entry, item.id).name for item in items] == names


def test_room_projection_collects_known_names_and_map_without_exporting_ids() -> None:
    data = response(record("a", room="Kitchen", known_rooms=("Kitchen", "Laundry")))
    data.entities[0].rooms = ["room-0", "Kitchen", "unknown-private-room"]
    data.room_entity_map = {"room-1": ["a"], "unknown-private-map": ["a"]}
    projected = inventory_data(data).inventory[0]
    assert projected.room == "Kitchen" and projected.known_rooms == ("Kitchen", "Laundry")
    assert "room-0" not in repr(projected) and "unknown-private" not in repr(projected)


def marker_store(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}.inventory_areas"


def identifier(record_id: str) -> str:
    return f"{HOME_ID}_inventory_{record_id}"


async def reload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_casa_room_change_moves_integration_placed_area(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    areas = ar.async_get(hass)
    patio = areas.async_create("Laundry Patio")
    garage = areas.async_create("Garage")
    mock_api.home_details.return_value = response(record("a", room="Laundry Patio"))
    await setup(hass, config_entry)
    before = device(hass, config_entry, "a")
    assert before.area_id == patio.id
    await refresh(hass, config_entry, mock_api, record("a", room="Garage"))
    moved = device(hass, config_entry, "a")
    assert (moved.id, moved.area_id, moved.name) == (before.id, garage.id, "Garage Backup Battery")
    assert len(tuple(areas.async_list_areas())) == 2


async def test_unmatched_room_clears_managed_area_and_a_later_match_places_again(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    areas = ar.async_get(hass)
    patio = areas.async_create("Laundry Patio")
    garage = areas.async_create("Garage")
    mock_api.home_details.return_value = response(record("a", room="Laundry Patio"))
    await setup(hass, config_entry)
    assert device(hass, config_entry, "a").area_id == patio.id
    await refresh(hass, config_entry, mock_api, record("a", room="Attic"))
    assert device(hass, config_entry, "a").area_id is None
    await refresh(hass, config_entry, mock_api, record("a", room="Garage"))
    assert device(hass, config_entry, "a").area_id == garage.id
    # Ambiguity is also "no target": an integration-placed area clears.
    await refresh(
        hass, config_entry, mock_api, record("a", known_rooms=("Garage", "Laundry Patio"))
    )
    assert device(hass, config_entry, "a").area_id is None
    assert {area.id for area in areas.async_list_areas()} == {patio.id, garage.id}


@pytest.mark.parametrize("change", ["move", "clear", "delete_area"])
async def test_any_user_area_change_ends_management_permanently(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
    change: str,
) -> None:
    areas = ar.async_get(hass)
    patio = areas.async_create("Laundry Patio")
    garage = areas.async_create("Garage")
    office = areas.async_create("Office")
    mock_api.home_details.return_value = response(record("a", room="Laundry Patio"))
    await setup(hass, config_entry)
    placed = device(hass, config_entry, "a")
    assert placed.area_id == patio.id
    if change == "move":
        dr.async_get(hass).async_update_device(placed.id, area_id=office.id)
    elif change == "clear":
        dr.async_get(hass).async_update_device(placed.id, area_id=None)
    else:
        areas.async_delete(patio.id)
    expected = office.id if change == "move" else None
    await refresh(hass, config_entry, mock_api, record("a", room="Garage"))
    assert device(hass, config_entry, "a").area_id == expected
    await reload(hass, config_entry)
    assert identifier("a") not in hass_storage[marker_store(config_entry)]["data"]["devices"]
    # Neither the old room, a recreated area, nor another move reclaims it.
    if change == "delete_area":
        areas.async_create("Laundry Patio")
    for room in ("Laundry Patio", "Garage", "Attic", "Garage"):
        await refresh(hass, config_entry, mock_api, record("a", room=room))
        assert device(hass, config_entry, "a").area_id == expected
    assert expected != garage.id


async def test_legacy_migration_becomes_managed_and_follows(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    config_entry.add_to_hass(hass)
    areas = ar.async_get(hass)
    garage = areas.async_create("Garage")
    patio = areas.async_create("Laundry Patio")
    legacy = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, identifier("a"))},
        entry_type=dr.DeviceEntryType.SERVICE,
        name="Casa inventory Backup Battery",
    )
    mock_api.home_details.return_value = response(record("a", room="Garage"))
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert device(hass, config_entry, "a").area_id == garage.id
    await refresh(hass, config_entry, mock_api, record("a", room="Laundry Patio"))
    migrated = device(hass, config_entry, "a")
    assert (migrated.id, migrated.area_id) == (legacy.id, patio.id)


async def test_devices_without_markers_are_unmanaged(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    """Devices placed before markers existed, or overridden at placement, stay put."""
    config_entry.add_to_hass(hass)
    areas = ar.async_get(hass)
    garage = areas.async_create("Garage")
    areas.async_create("Laundry Patio")
    devices = dr.async_get(hass)
    for key, area_id in (("a", garage.id), ("b", None)):
        created = devices.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={(DOMAIN, identifier(key))},
            entry_type=dr.DeviceEntryType.SERVICE,
            name=f"Garage Backup Battery ({key})",
        )
        devices.async_update_device(created.id, area_id=area_id)
    items = (record("a", model="a"), record("b", model="b"))
    mock_api.home_details.return_value = response(*items)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    for room in ("Laundry Patio", "Attic", "Garage"):
        await refresh(hass, config_entry, mock_api, *(replace(i, room=room) for i in items))
        assert device(hass, config_entry, "a").area_id == garage.id
        assert device(hass, config_entry, "b").area_id is None
    await reload(hass, config_entry)
    assert marker_store(config_entry) not in hass_storage


async def test_disabled_managed_device_pauses_without_losing_its_marker(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock, mock_auth: MagicMock
) -> None:
    areas = ar.async_get(hass)
    patio = areas.async_create("Laundry Patio")
    garage = areas.async_create("Garage")
    mock_api.home_details.return_value = response(record("a", room="Laundry Patio"))
    await setup(hass, config_entry)
    devices = dr.async_get(hass)
    devices.async_update_device(
        device(hass, config_entry, "a").id, disabled_by=dr.DeviceEntryDisabler.USER
    )
    await refresh(hass, config_entry, mock_api, record("a", room="Garage"))
    assert device(hass, config_entry, "a").area_id == patio.id
    devices.async_update_device(device(hass, config_entry, "a").id, disabled_by=None)
    await refresh(hass, config_entry, mock_api, record("a", room="Garage"))
    assert device(hass, config_entry, "a").area_id == garage.id


async def test_markers_survive_reload_and_are_removed_with_the_entry(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    areas = ar.async_get(hass)
    patio = areas.async_create("Laundry Patio")
    garage = areas.async_create("Garage")
    mock_api.home_details.return_value = response(
        record("a", room="Laundry Patio"), record("b", room="Attic")
    )
    await setup(hass, config_entry)
    # Reload immediately: the delayed save must be flushed, not lost.
    await reload(hass, config_entry)
    stored = hass_storage[marker_store(config_entry)]
    assert stored["version"] == 1
    assert stored["data"] == {"devices": {identifier("a"): patio.id, identifier("b"): None}}
    await refresh(
        hass, config_entry, mock_api, record("a", room="Garage"), record("b", room="Laundry Patio")
    )
    assert device(hass, config_entry, "a").area_id == garage.id
    assert device(hass, config_entry, "b").area_id == patio.id
    # Absent records keep markers. Deleted registry devices are pruned.
    dr.async_get(hass).async_remove_device(device(hass, config_entry, "b").id)
    await refresh(hass, config_entry, mock_api)
    await reload(hass, config_entry)
    assert hass_storage[marker_store(config_entry)]["data"] == {
        "devices": {identifier("a"): garage.id}
    }
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert marker_store(config_entry) not in hass_storage


async def test_follow_is_scoped_to_this_entry_and_home(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_auth: MagicMock,
) -> None:
    areas = ar.async_get(hass)
    patio = areas.async_create("Laundry Patio")
    garage = areas.async_create("Garage")
    other = MockConfigEntry(domain=DOMAIN, title="Other", unique_id="other-user")
    other.add_to_hass(hass)
    other_markers = {"devices": {identifier("a"): patio.id}}
    hass_storage[marker_store(other)] = {
        "version": 1,
        "minor_version": 1,
        "key": marker_store(other),
        "data": other_markers,
    }
    devices = dr.async_get(hass)
    foreign = devices.async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={(DOMAIN, identifier("a"))},
        entry_type=dr.DeviceEntryType.SERVICE,
        name="Laundry Patio Backup Battery",
    )
    devices.async_update_device(foreign.id, area_id=patio.id)
    config_entry.add_to_hass(hass)
    other_home = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "other_home_inventory_a")},
        entry_type=dr.DeviceEntryType.SERVICE,
        name="Laundry Patio Backup Battery",
    )
    devices.async_update_device(other_home.id, area_id=patio.id)
    mock_api.home_details.return_value = response(record("a", room="Laundry Patio"))
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await refresh(hass, config_entry, mock_api, record("a", room="Garage"))
    await reload(hass, config_entry)
    assert devices.async_get(foreign.id).area_id == patio.id  # type: ignore[union-attr]
    assert devices.async_get(other_home.id).area_id == patio.id  # type: ignore[union-attr]
    assert hass_storage[marker_store(other)]["data"] == other_markers
    own = device(hass, config_entry, "a")
    assert own.area_id == garage.id and own.id not in {foreign.id, other_home.id}
    assert len(tuple(areas.async_list_areas())) == 2
