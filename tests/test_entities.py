"""Entities, devices, diagnostics, and the never-expose list. All from the synthetic fixture."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE, Platform
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.casahome.const import DOMAIN

from .conftest import DEVICE_ID, HOME_ID, REFRESH_TOKEN, bootstrap_model, load_fixture_json

pytestmark = pytest.mark.usefixtures("mock_api", "mock_auth")

SENSOR = "sensor.123_example_st_springfield_ca_00000"
BINARY = "binary_sensor.123_example_st_springfield_ca_00000"


def _state(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def _never_expose_values() -> list[str]:
    """Raw fixture values that must never appear anywhere HA renders or exports."""
    data = load_fixture_json("bootstrap.json")
    home = data["homes"][0]
    values = [
        data["phone_number"],
        data["email"],
        home["matterport_model_id"],
        home["payment_description"],
        home["home_image_url"],
        home["pending_agreements"][0]["content"],
        str(home["latitude"]),
        str(home["longitude"]),
        REFRESH_TOKEN,
        DEVICE_ID,
    ]
    return values


@pytest.mark.parametrize("platform", [Platform.SENSOR, Platform.BINARY_SENSOR])
async def test_entity_snapshots(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    config_entry: MockConfigEntry,
    platform: Platform,
) -> None:
    await _setup(hass, config_entry)
    entities = [
        e
        for e in er.async_entries_for_config_entry(entity_registry, config_entry.entry_id)
        if e.domain == platform
    ]
    assert entities
    for entry in sorted(entities, key=lambda e: e.entity_id):
        assert entry == snapshot(name=f"{entry.entity_id}-entry")
        state = hass.states.get(entry.entity_id)
        if entry.disabled_by is None:
            assert state == snapshot(name=f"{entry.entity_id}-state")
        else:
            assert state is None


async def test_entity_counts_and_states(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    # 11 original and 10 aggregate count sensors, all enabled by default.
    assert len([e for e in entries if e.domain == "sensor"]) == 21
    assert len([e for e in entries if e.domain == "binary_sensor"]) == 9
    assert {e.unique_id for e in entries} >= {f"{HOME_ID}_status", f"{HOME_ID}_mailbox_badge"}

    assert _state(hass, f"{SENSOR}_status").state == "active"
    assert _state(hass, f"{SENSOR}_plan_state").state == "active"
    assert _state(hass, f"{SENSOR}_unread_mail").state == "0"
    pending = _state(hass, f"{SENSOR}_pending_agreements")
    assert pending.state == "1"
    assert pending.attributes["names"] == ["Member Terms of Use"]
    assert _state(hass, f"{SENSOR}_weather_condition").state == "clear"
    assert _state(hass, f"{SENSOR}_credits_available").state == "3"
    assert float(_state(hass, f"{SENSOR}_handyman_hourly_rate").state) == 85.0
    assert _state(hass, f"{BINARY}_agreement_needs_signature").state == "on"
    assert _state(hass, f"{BINARY}_membership_active").state == "off"
    assert _state(hass, f"{BINARY}_calendar_sync").state == "off"
    collective = registry.async_get(f"{BINARY}_casa_collective_member")
    assert collective is not None
    assert collective.disabled_by is None


async def test_one_device_per_home(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    devices = dr.async_entries_for_config_entry(device_registry, config_entry.entry_id)
    assert len(devices) == 1
    device = devices[0]
    assert device.identifiers == {(DOMAIN, HOME_ID)}
    assert device.manufacturer == "Casa"
    assert device.model == "Casa membership"
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    assert device.name == "123 Example St, Springfield, CA 00000"


async def test_two_homes_two_devices(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
) -> None:
    data = load_fixture_json("bootstrap.json")
    second = {**data["homes"][0], "id": "00000000-0000-4000-8000-000000000009"}
    mock_api.bootstrap.return_value = bootstrap_model(homes=[data["homes"][0], second])
    await _setup(hass, config_entry)
    devices = dr.async_entries_for_config_entry(device_registry, config_entry.entry_id)
    assert {next(iter(d.identifiers))[1] for d in devices} == {
        HOME_ID,
        "00000000-0000-4000-8000-000000000009",
    }


async def test_device_name_without_address(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    mock_api: MagicMock,
) -> None:
    data = load_fixture_json("bootstrap.json")
    blank = {
        **data["homes"][0],
        "address_street_number": "",
        "address_street": "",
        "address_city": "",
        "address_state": "",
        "address_postal_code": "",
    }
    mock_api.bootstrap.return_value = bootstrap_model(homes=[blank])
    await _setup(hass, config_entry)
    (device,) = dr.async_entries_for_config_entry(device_registry, config_entry.entry_id)
    assert device.name == "Casa Home"


async def test_missing_badge_counts_makes_badges_unavailable(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock
) -> None:
    data = load_fixture_json("bootstrap.json")
    mock_api.bootstrap.return_value = bootstrap_model(
        homes=[{**data["homes"][0], "badge_counts": None}]
    )
    await _setup(hass, config_entry)
    for key in ("unread_requests", "unread_recurring_vendors", "unread_mail", "unread_total"):
        assert _state(hass, f"{SENSOR}_{key}").state == STATE_UNAVAILABLE
    assert _state(hass, f"{SENSOR}_status").state == "active"


async def test_calendar_sync_none_is_unavailable(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock
) -> None:
    mock_api.bootstrap.return_value = bootstrap_model(calendar_sync_active=None)
    await _setup(hass, config_entry)
    assert _state(hass, f"{BINARY}_calendar_sync").state == STATE_UNAVAILABLE


async def test_unknown_status_reads_unknown(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock
) -> None:
    data = load_fixture_json("bootstrap.json")
    mock_api.bootstrap.return_value = bootstrap_model(
        homes=[{**data["homes"][0], "status": "paused_for_winter"}]
    )
    await _setup(hass, config_entry)
    assert _state(hass, f"{SENSOR}_status").state == "unknown"


async def test_home_vanishing_makes_entities_unavailable(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: MagicMock
) -> None:
    await _setup(hass, config_entry)
    mock_api.bootstrap.return_value = bootstrap_model(homes=[])
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()
    assert _state(hass, f"{SENSOR}_status").state == STATE_UNAVAILABLE
    assert _state(hass, f"{BINARY}_membership_active").state == STATE_UNAVAILABLE


async def test_never_expose_absent_from_states(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    rendered = json.dumps(
        [
            {"state": s.state, "attributes": dict(s.attributes)}
            for s in hass.states.async_all()
            if s.entity_id.startswith(("sensor.", "binary_sensor."))
        ],
        default=str,
    )
    for value in _never_expose_values():
        assert value not in rendered, value


async def test_diagnostics_redacted(
    hass: HomeAssistant, hass_client: Any, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)
    dumped = json.dumps(diagnostics)
    for value in _never_expose_values():
        assert value not in dumped, value
    for value in ("123", "Example St", "Springfield", "00000", "visa", "0000"):
        assert f'"{value}"' not in dumped, value
    home = diagnostics["bootstrap"]["homes"][0]
    assert home["status"] == "active"
    assert home["pending_agreements"][0]["name"] == "Member Terms of Use"
    assert home["pending_agreements"][0]["content"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["refresh_token"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["device_id"] == "**REDACTED**"
    for key in ("last_name", "email", "phone_number"):
        assert diagnostics["bootstrap"][key] == "**REDACTED**"


async def test_never_expose_keys_absent_from_attributes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    banned = {"last_name", "email", "phone_number", "latitude", "longitude", "content"}
    for state in hass.states.async_all():
        if state.entity_id.startswith(("sensor.", "binary_sensor.")):
            assert banned.isdisjoint(state.attributes), state.entity_id


async def test_diagnostics_drops_undeclared_upstream_fields(
    hass: HomeAssistant, hass_client: Any, config_entry: MockConfigEntry, mock_api: MagicMock
) -> None:
    data = load_fixture_json("bootstrap.json")
    home = {**data["homes"][0], "gate_code": "1234-new-field"}
    mock_api.bootstrap.return_value = bootstrap_model(homes=[home], ssn_like="999-00-1234")
    await _setup(hass, config_entry)
    dumped = json.dumps(await get_diagnostics_for_config_entry(hass, hass_client, config_entry))
    assert "1234-new-field" not in dumped
    assert "999-00-1234" not in dumped
