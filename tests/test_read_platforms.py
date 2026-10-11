"""Read-only native HA todo/calendar behavior over spec-bound offline SDK mocks."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from casahome import CasaApiError, CasaAuthError, CasaConnectionError, CasaResponseError
from casahome.models import OnboardingDetailsResponse, SuggestedTodosResponse, WeatherResponse
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.calendar.const import DATA_COMPONENT as CALENDAR_COMPONENT
from homeassistant.components.todo.const import DATA_COMPONENT as TODO_COMPONENT
from homeassistant.components.todo.const import TodoItemStatus
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.casahome.calendar import CasaOnboardingCalendar
from custom_components.casahome.const import DOMAIN
from custom_components.casahome.home_coordinator import (
    ONBOARDING,
    SUGGESTED_TODOS,
    CasaHomeCoordinator,
    HomeData,
)
from custom_components.casahome.projections import onboarding_events, suggested_todo_items
from custom_components.casahome.todo import CasaSuggestedTodoList

from .conftest import HOME_ID, bootstrap_model, load_fixture_json
from .read_fixtures import EVENT_START, onboarding, suggestions

KEYS = {"todo": "suggested_todos", "calendar": "onboarding_visits"}


@pytest.fixture
def read_api(mock_api: MagicMock, mock_auth: MagicMock) -> MagicMock:
    mock_api.suggested_todos.return_value = suggestions()
    mock_api.onboarding_details_typed.return_value = onboarding()
    return mock_api


def entity_id(hass: HomeAssistant, domain: str, home_id: str = HOME_ID) -> str:
    result = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{home_id}_{KEYS[domain]}")
    assert result is not None
    return result


def todo_entity(hass: HomeAssistant) -> CasaSuggestedTodoList:
    entity = hass.data[TODO_COMPONENT].get_entity(entity_id(hass, "todo"))
    assert isinstance(entity, CasaSuggestedTodoList)
    return entity


def calendar_entity(hass: HomeAssistant) -> CasaOnboardingCalendar:
    entity = hass.data[CALENDAR_COMPONENT].get_entity(entity_id(hass, "calendar"))
    assert isinstance(entity, CasaOnboardingCalendar)
    return entity


def coordinator(entry: MockConfigEntry) -> CasaHomeCoordinator:
    result: CasaHomeCoordinator = entry.runtime_data.home_coordinators[HOME_ID]
    return result


async def setup(
    hass: HomeAssistant, entry: MockConfigEntry, *, disabled: tuple[str, ...] = ()
) -> None:
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for domain in disabled:
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{HOME_ID}_{KEYS[domain]}",
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def disable(hass: HomeAssistant, *domains: str) -> None:
    registry = er.async_get(hass)
    for domain in domains:
        registry.async_update_entity(
            entity_id(hass, domain), disabled_by=er.RegistryEntryDisabler.USER
        )
    await hass.async_block_till_done()


def assert_unavailable(hass: HomeAssistant) -> None:
    for domain in KEYS:
        state = hass.states.get(entity_id(hass, domain))
        assert state is not None and state.state == STATE_UNAVAILABLE
    assert todo_entity(hass).todo_items is None
    assert calendar_entity(hass).event is None


async def test_setup_counts_device_labels_and_call_budget(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock
) -> None:
    await setup(hass, config_entry)
    entries = er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
    assert len(entries) == 33
    assert sum(entry.disabled_by is None for entry in entries) == 33
    for domain in KEYS:
        entry = er.async_get(hass).async_get(entity_id(hass, domain))
        assert entry is not None and entry.disabled_by is None
        assert entry.translation_key == KEYS[domain]
        state = hass.states.get(entry.entity_id)
        assert state is not None and state.attributes["supported_features"] == 0
    assert todo_entity(hass).name == "Suggested tasks"
    assert calendar_entity(hass).name == "Onboarding visits"
    assert todo_entity(hass).supported_features == calendar_entity(hass).supported_features == 0
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)) == 1
    for method in (
        "bootstrap",
        "weather",
        "credits_summary",
        "suggested_todos",
        "onboarding_details_typed",
    ):
        assert getattr(read_api, method).await_count == 1
    assert {call[0] for call in read_api.method_calls} == {
        "bootstrap",
        "weather",
        "credits_summary",
        "suggested_todos",
        "onboarding_details_typed",
        "home_details",
        "requests_tab",
        "autopilot_plans",
        "inventory_service_history",
        "mail_items",
        "packages",
        "calendar",
    }
    read_api.todo_details.assert_not_called()
    read_api.calendar.assert_awaited_once()
    read_api.create_todo.assert_not_called()


async def test_native_todo_get_items_and_minimal_projection(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock
) -> None:
    await setup(hass, config_entry)
    response = await hass.services.async_call(
        "todo",
        "get_items",
        {"entity_id": entity_id(hass, "todo")},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    todo_response = response[entity_id(hass, "todo")]
    assert isinstance(todo_response, dict)
    items = todo_response["items"]
    assert items == [
        {
            "uid": f"synthetic-todo-{index}",
            "summary": f"Inspect sample item {index}",
            "status": "needs_action",
        }
        for index in range(10)
    ]
    state = hass.states.get(entity_id(hass, "todo"))
    assert state is not None and state.state == "10"
    assert state.attributes["supported_features"] == 0
    assert all(
        item.due is None and item.completed is None and item.description is None
        for item in todo_entity(hass).todo_items or ()
    )
    rendered = json.dumps(response, default=str)
    assert "synthetic-private" not in rendered and "example.invalid" not in rendered
    assert "synthetic-private" not in repr(coordinator(config_entry).data)
    # Returned mutable HA dataclasses cannot modify the coordinator's cache.
    copied = todo_entity(hass).todo_items
    assert copied is not None
    copied[0].summary = "Changed by a caller"
    assert (todo_entity(hass).todo_items or [])[0].summary == "Inspect sample item 0"


@pytest.mark.parametrize(
    "status", ["materialized", "suggestion_member_rejected", "future_status", "unknown"]
)
def test_unobserved_status_does_not_imply_completion_or_acceptance(status: str) -> None:
    items = suggested_todo_items(suggestions(1, status=status))
    assert items[0].status is None
    assert items[0].completed is None and items[0].due is None


def test_source_order_stable_uids_and_first_duplicate_wins() -> None:
    response = suggestions(3)
    first, second, third = response.suggested_todos
    response.suggested_todos = [
        third,
        second,
        third.model_copy(update={"title": "Duplicate"}),
        first,
    ]
    items = suggested_todo_items(response)
    assert [item.uid for item in items] == [third.id, second.id, first.id]
    assert items[0].summary == third.title
    assert items[0].status is TodoItemStatus.NEEDS_ACTION


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Inspect [vent](https://example.invalid/private)\n\x00now", "Inspect vent now"),
        ("Inspect vent https://example.invalid/private www.example.invalid/image", "Inspect vent"),
        ("mailto:private@example.invalid", "Casa suggestion"),
        ("data:text/plain,synthetic-image", "Casa suggestion"),
        ("file:/synthetic-private/path", "Casa suggestion"),
        ("x" * 300, "x" * 255),
    ],
)
def test_titles_are_bounded_plain_text_without_urls(title: str, expected: str) -> None:
    response = suggestions(1)
    response.suggested_todos[0].title = title
    assert suggested_todo_items(response)[0].summary == expected


@pytest.mark.parametrize(
    ("service", "payload"),
    [
        ("add_item", {"item": "New item"}),
        ("update_item", {"item": "synthetic-todo-0", "status": "completed"}),
        ("update_item", {"item": "synthetic-todo-0", "rename": "Changed"}),
        ("remove_item", {"item": "synthetic-todo-0"}),
        ("remove_completed_items", {}),
    ],
)
async def test_native_todo_mutations_rejected(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    read_api: MagicMock,
    service: str,
    payload: dict[str, str],
) -> None:
    await setup(hass, config_entry)
    calls = list(read_api.method_calls)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "todo", service, {"entity_id": entity_id(hass, "todo"), **payload}, blocking=True
        )
    assert read_api.method_calls == calls
    read_api.create_todo.assert_not_called()


async def test_todo_subscribers_receive_item_changes_and_home_loss(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock
) -> None:
    await setup(hass, config_entry)
    entity = todo_entity(hass)
    subscriber = MagicMock()
    unsub = entity.async_subscribe_updates(subscriber)
    try:
        read_api.suggested_todos.return_value = suggestions(2)
        await coordinator(config_entry).async_refresh()
        assert len(subscriber.call_args.args[0]) == 2
        subscriber.reset_mock()
        await coordinator(config_entry).async_refresh()
        subscriber.assert_not_called()
        config_entry.runtime_data.async_set_updated_data(bootstrap_model(homes=[]))
        subscriber.assert_called_once_with(None)
    finally:
        unsub()


async def test_todo_websocket_move_rejected_without_sdk_calls(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    read_api: MagicMock,
    hass_ws_client: Any,
) -> None:
    await setup(hass, config_entry)
    calls = list(read_api.method_calls)
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "todo/item/move",
            "entity_id": entity_id(hass, "todo"),
            "uid": "synthetic-todo-0",
            "previous_uid": "synthetic-todo-1",
        }
    )
    result = await client.receive_json()
    assert result["success"] is False and result["error"]["code"] == "not_supported"
    assert read_api.method_calls == calls


@pytest.mark.parametrize("domain", ["todo", "calendar"])
async def test_empty_is_available_not_a_failed_read(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock, domain: str
) -> None:
    read_api.suggested_todos.return_value = SuggestedTodosResponse(suggested_todos=[])
    read_api.onboarding_details_typed.return_value = OnboardingDetailsResponse(calendar_events=[])
    await setup(hass, config_entry)
    state = hass.states.get(entity_id(hass, domain))
    assert state is not None and state.state == ("0" if domain == "todo" else "off")
    assert todo_entity(hass).todo_items == []
    start = datetime.fromtimestamp(EVENT_START, UTC)
    assert (
        await calendar_entity(hass).async_get_events(hass, start, start + timedelta(days=1)) == []
    )


async def test_calendar_epoch_seconds_generic_metadata_and_queries_are_cached(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock
) -> None:
    await setup(hass, config_entry)
    entity = calendar_entity(hass)
    start = datetime.fromtimestamp(EVENT_START, UTC)
    calls = list(read_api.method_calls)
    events = await entity.async_get_events(
        hass, start - timedelta(hours=1), start + timedelta(hours=4)
    )
    assert [event.uid for event in events] == ["synthetic-visit-first", "synthetic-visit-later"]
    assert events[0].start == start
    assert events[0].end == start + timedelta(hours=1)
    assert isinstance(events[0].start, datetime)
    assert events[0].start.tzinfo is UTC
    assert events[0].start.timestamp() == EVENT_START
    assert all(event.summary == "Casa onboarding visit" for event in events)
    assert all(
        event.description is None and event.location is None and event.rrule is None
        for event in events
    )
    assert "synthetic-private" not in json.dumps([event.as_dict() for event in events])
    events[0].summary = "Changed by caller"
    assert (await entity.async_get_events(hass, start, start + timedelta(hours=1)))[
        0
    ].summary == "Casa onboarding visit"
    assert read_api.method_calls == calls


async def test_native_calendar_service_reads_cache_and_rejects_create(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock
) -> None:
    await setup(hass, config_entry)
    calls = list(read_api.method_calls)
    start = datetime.fromtimestamp(EVENT_START, UTC)
    window = {
        "entity_id": entity_id(hass, "calendar"),
        "start_date_time": start.isoformat(),
        "end_date_time": (start + timedelta(hours=4)).isoformat(),
    }
    response = await hass.services.async_call(
        "calendar", "get_events", window, blocking=True, return_response=True
    )
    assert response is not None
    calendar_response = response[entity_id(hass, "calendar")]
    assert isinstance(calendar_response, dict)
    events = calendar_response["events"]
    assert isinstance(events, list) and len(events) == 2
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "calendar", "create_event", {**window, "summary": "No write"}, blocking=True
        )
    assert read_api.method_calls == calls


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (-3600, 0, []),
        (3600, 7200, []),
        (0, 1, ["synthetic-visit-first"]),
        (3599, 7201, ["synthetic-visit-first", "synthetic-visit-later"]),
        (-1, 10801, ["synthetic-visit-first", "synthetic-visit-later"]),
    ],
)
async def test_calendar_half_open_overlap_with_non_utc_bounds(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    read_api: MagicMock,
    left: int,
    right: int,
    expected: list[str],
) -> None:
    await setup(hass, config_entry)
    tz = ZoneInfo("America/Los_Angeles")
    start = datetime.fromtimestamp(EVENT_START + left, tz)
    end = datetime.fromtimestamp(EVENT_START + right, ZoneInfo("Europe/Oslo"))
    events = await calendar_entity(hass).async_get_events(hass, start, end)
    assert [event.uid for event in events] == expected


@pytest.mark.parametrize("invalid", ["naive", "empty", "reversed"])
async def test_calendar_rejects_invalid_windows(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock, invalid: str
) -> None:
    await setup(hass, config_entry)
    start = datetime.fromtimestamp(EVENT_START, UTC)
    end = start + timedelta(hours=1)
    if invalid == "naive":
        start = start.replace(tzinfo=None)
    elif invalid == "empty":
        end = start
    else:
        start, end = end, start
    with pytest.raises(HomeAssistantError, match="aware window"):
        await calendar_entity(hass).async_get_events(hass, start, end)


async def test_calendar_orders_and_filters_utc_instants_across_dst_fold(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock
) -> None:
    await hass.config.async_set_time_zone("America/Los_Angeles")
    tz = ZoneInfo("America/Los_Angeles")
    # Fixed synthetic inputs, independent of today's date or captured visits.
    first = datetime(2030, 11, 3, 1, 45, tzinfo=tz, fold=0)
    second = datetime(2030, 11, 3, 1, 15, tzinfo=tz, fold=1)
    assert first.astimezone(UTC) < second.astimezone(UTC)
    read_api.onboarding_details_typed.return_value = OnboardingDetailsResponse.model_validate(
        {
            "calendar_events": [
                {
                    "id": uid,
                    "start_at": int(start.timestamp()),
                    "end_at": int(start.timestamp()) + 300,
                }
                for uid, start in (
                    ("synthetic-fold-later", second),
                    ("synthetic-fold-first", first),
                )
            ]
        }
    )
    await setup(hass, config_entry)
    cached = coordinator(config_entry).data.onboarding
    assert cached is not None
    assert [event.uid for event in cached] == ["synthetic-fold-first", "synthetic-fold-later"]
    left = datetime(2030, 11, 3, 1, 30, tzinfo=tz, fold=0)
    right = left.replace(fold=1)
    events = await calendar_entity(hass).async_get_events(hass, left, right)
    assert [event.uid for event in events] == ["synthetic-fold-first", "synthetic-fold-later"]


@pytest.mark.parametrize(
    ("start", "end"), [(123, 123), (124, 123), (10**18, 10**18 + 1), (-(10**18), 0)]
)
def test_invalid_epoch_or_duration_fails_projection(start: int, end: int) -> None:
    response = OnboardingDetailsResponse.model_validate(
        {"calendar_events": [{"id": "synthetic-bad-visit", "start_at": start, "end_at": end}]}
    )
    with pytest.raises(CasaResponseError):
        onboarding_events(response)


async def test_native_calendar_state_transitions_and_next_event(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    read_api: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    start = datetime.fromtimestamp(EVENT_START, UTC)
    freezer.move_to(start - timedelta(seconds=1))
    await setup(hass, config_entry)
    entity = calendar_entity(hass)
    assert entity.event is not None and entity.event.uid == "synthetic-visit-first"
    assert entity.state == "off"
    for offset, expected in ((0, "on"), (3600, "off"), (7200, "on"), (10800, "off")):
        now = start + timedelta(seconds=offset)
        freezer.move_to(now)
        async_fire_time_changed(hass, now)
        await hass.async_block_till_done()
        state = hass.states.get(entity_id(hass, "calendar"))
        assert state is not None and state.state == expected
    assert entity.event is None


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
async def test_calendar_websocket_mutations_rejected(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    read_api: MagicMock,
    hass_ws_client: Any,
    operation: str,
) -> None:
    await setup(hass, config_entry)
    client = await hass_ws_client(hass)
    data: dict[str, Any] = {
        "id": 1,
        "type": f"calendar/event/{operation}",
        "entity_id": entity_id(hass, "calendar"),
    }
    if operation != "create":
        data["uid"] = "synthetic-visit-first"
    if operation != "delete":
        start = datetime.fromtimestamp(EVENT_START, UTC)
        data["event"] = {
            "summary": "No write",
            "dtstart": start.isoformat(),
            "dtend": (start + timedelta(hours=1)).isoformat(),
        }
    calls = list(read_api.method_calls)
    await client.send_json(data)
    result = await client.receive_json()
    assert result["success"] is False and result["error"]["code"] == "not_supported"
    assert read_api.method_calls == calls


@pytest.mark.parametrize("disabled", [("todo",), ("calendar",), ("todo", "calendar")])
async def test_disabled_reads_never_probe_at_setup_or_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    read_api: MagicMock,
    disabled: tuple[str, ...],
) -> None:
    await setup(hass, config_entry, disabled=disabled)
    for domain, method in (("todo", "suggested_todos"), ("calendar", "onboarding_details_typed")):
        assert getattr(read_api, method).await_count == (0 if domain in disabled else 1)
        getattr(read_api, method).reset_mock()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=14))
    await hass.async_block_till_done()
    read_api.suggested_todos.assert_not_called()
    read_api.onboarding_details_typed.assert_not_called()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()
    for domain, method in (("todo", "suggested_todos"), ("calendar", "onboarding_details_typed")):
        assert getattr(read_api, method).await_count == (0 if domain in disabled else 1)


async def test_two_homes_isolated_and_entry_schema_unchanged(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock
) -> None:
    raw = load_fixture_json("bootstrap.json")
    second_id = "00000000-0000-4000-8000-000000000009"
    second = {**raw["homes"][0], "id": second_id}
    read_api.bootstrap.return_value = bootstrap_model(homes=[raw["homes"][0], second])
    read_api.suggested_todos.side_effect = [suggestions(1), suggestions(2)]
    await setup(hass, config_entry)
    assert (config_entry.version, config_entry.minor_version) == (1, 3)
    assert config_entry.options == {}
    for domain in KEYS:
        assert entity_id(hass, domain, HOME_ID) != entity_id(hass, domain, second_id)
    first_state = hass.states.get(entity_id(hass, "todo"))
    second_state = hass.states.get(entity_id(hass, "todo", second_id))
    assert first_state is not None and first_state.state == "1"
    assert second_state is not None and second_state.state == "2"
    assert read_api.suggested_todos.await_count == 2
    assert read_api.onboarding_details_typed.await_count == 2


@pytest.mark.parametrize("endpoint", [SUGGESTED_TODOS, ONBOARDING])
@pytest.mark.parametrize(
    "error",
    [
        CasaApiError(404, "synthetic-private-error"),
        CasaApiError(405, "synthetic-private-error"),
        CasaApiError(500, "synthetic-private-error"),
        CasaConnectionError("synthetic-private-error"),
        CasaResponseError("synthetic-private-error"),
        TimeoutError(),
    ],
)
async def test_read_errors_isolated_and_sanitized(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    read_api: MagicMock,
    endpoint: str,
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    getattr(read_api, endpoint).side_effect = error
    await setup(hass, config_entry)
    failed_domain = "todo" if endpoint == SUGGESTED_TODOS else "calendar"
    working_domain = "calendar" if failed_domain == "todo" else "todo"
    state = hass.states.get(entity_id(hass, failed_domain))
    assert state is not None and state.state == STATE_UNAVAILABLE
    state = hass.states.get(entity_id(hass, working_domain))
    assert state is not None and state.state != STATE_UNAVAILABLE
    co = coordinator(config_entry)
    await co.async_refresh()
    is_missing = isinstance(error, CasaApiError) and error.status in {404, 405}
    assert getattr(read_api, endpoint).await_count == (1 if is_missing else 2)
    assert (endpoint in co.disabled) is is_missing
    assert "synthetic-private-error" not in caplog.text
    getattr(read_api, endpoint).side_effect = None
    if not is_missing:
        await co.async_refresh()
        state = hass.states.get(entity_id(hass, failed_domain))
        assert state is not None and state.state != STATE_UNAVAILABLE


@pytest.mark.parametrize("endpoint", [SUGGESTED_TODOS, ONBOARDING])
async def test_new_reads_trigger_sanitized_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    read_api: MagicMock,
    endpoint: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup(hass, config_entry)
    getattr(read_api, endpoint).side_effect = CasaAuthError("synthetic-private-auth")
    await coordinator(config_entry).async_refresh()
    await hass.async_block_till_done()
    assert_unavailable(hass)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1 and flows[0]["context"]["source"] == "reauth"
    assert "synthetic-private-auth" not in caplog.text


async def test_bodyless_304_preserves_only_known_current_generation_calendar(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock
) -> None:
    read_api.onboarding_details_typed.return_value = None
    await setup(hass, config_entry)
    entity = calendar_entity(hass)
    assert not entity.available  # No cache to reuse. Never fabricate an empty calendar.
    read_api.onboarding_details_typed.return_value = onboarding()
    await coordinator(config_entry).async_refresh()
    previous = coordinator(config_entry).data.onboarding
    read_api.onboarding_details_typed.return_value = None
    await coordinator(config_entry).async_refresh()
    assert entity.available and coordinator(config_entry).data.onboarding == previous
    main = config_entry.runtime_data
    main.async_set_updated_data(bootstrap_model(homes=[]))
    assert coordinator(config_entry).data == HomeData()
    main.async_set_updated_data(bootstrap_model())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not entity.available and coordinator(config_entry).data.onboarding is None
    read_api.onboarding_details_typed.return_value = OnboardingDetailsResponse(calendar_events=[])
    await coordinator(config_entry).async_refresh()
    assert entity.available and entity.event is None


async def test_home_loss_stops_reads_and_recovery_needs_fresh_data(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock
) -> None:
    await setup(hass, config_entry)
    main = config_entry.runtime_data
    main.async_set_update_error(Exception("synthetic-bootstrap-failure"))
    assert_unavailable(hass)
    read_api.suggested_todos.reset_mock()
    read_api.onboarding_details_typed.reset_mock()
    await coordinator(config_entry).async_refresh()
    read_api.suggested_todos.assert_not_called()
    read_api.onboarding_details_typed.assert_not_called()
    assert coordinator(config_entry)._unsub_refresh is None
    read_api.suggested_todos.side_effect = CasaConnectionError("synthetic-recovery-failure")
    read_api.onboarding_details_typed.side_effect = CasaConnectionError(
        "synthetic-recovery-failure"
    )
    main.async_set_updated_data(bootstrap_model())
    assert_unavailable(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert_unavailable(hass)
    start = datetime.fromtimestamp(EVENT_START, UTC)
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await calendar_entity(hass).async_get_events(hass, start, start + timedelta(hours=1))
    read_api.suggested_todos.side_effect = None
    read_api.onboarding_details_typed.side_effect = None
    await coordinator(config_entry).async_refresh()
    assert todo_entity(hass).available and calendar_entity(hass).available


@pytest.mark.parametrize("endpoint", [SUGGESTED_TODOS, ONBOARDING])
@pytest.mark.parametrize("stale_missing", [False, True])
@pytest.mark.parametrize("debounce_expires", [False, True])
async def test_inflight_read_discarded_across_home_loss_even_after_return(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    read_api: MagicMock,
    endpoint: str,
    stale_missing: bool,
    debounce_expires: bool,
) -> None:
    await setup(hass, config_entry)
    co = coordinator(config_entry)
    main = config_entry.runtime_data
    entered = asyncio.Event()
    release = asyncio.Event()
    fresh_entered = asyncio.Event()
    fresh_release = asyncio.Event()
    calls = 0

    async def response(home_id: str) -> SuggestedTodosResponse | OnboardingDetailsResponse:
        nonlocal calls
        assert home_id == HOME_ID
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            if stale_missing:
                raise CasaApiError(404, "synthetic-stale-missing")
            return suggestions(10) if endpoint == SUGGESTED_TODOS else onboarding()
        fresh_entered.set()
        await fresh_release.wait()
        return (
            suggestions(1) if endpoint == SUGGESTED_TODOS else onboarding(start=EVENT_START + 86400)
        )

    getattr(read_api, endpoint).side_effect = response
    refresh = asyncio.create_task(co.async_refresh())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        main.async_set_updated_data(bootstrap_model(homes=[]))
        main.async_set_updated_data(bootstrap_model())
        assert_unavailable(hass)
        await asyncio.sleep(0)
        if debounce_expires:
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
            await hass.async_block_till_done(wait_background_tasks=True)
        release.set()
        await asyncio.wait_for(refresh, timeout=5)
        assert co.data == HomeData() and not co.disabled
        assert_unavailable(hass)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=22))
        await asyncio.wait_for(fresh_entered.wait(), timeout=5)
        assert_unavailable(hass)
    finally:
        release.set()
        fresh_release.set()
        await asyncio.wait_for(refresh, timeout=5)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == 2
    assert todo_entity(hass).available and calendar_entity(hass).available
    if endpoint == SUGGESTED_TODOS:
        assert len(todo_entity(hass).todo_items or ()) == 1
    else:
        assert co.data.onboarding is not None
        assert co.data.onboarding[0].start == datetime.fromtimestamp(EVENT_START + 86400, UTC)


async def test_disabling_calendar_during_todo_read_skips_the_following_request(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock
) -> None:
    await setup(hass, config_entry)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(home_id: str) -> SuggestedTodosResponse:
        entered.set()
        await release.wait()
        return suggestions(1)

    read_api.suggested_todos.side_effect = blocked
    read_api.onboarding_details_typed.reset_mock()
    refresh = asyncio.create_task(coordinator(config_entry).async_refresh())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await disable(hass, "calendar", "todo")
        assert not refresh.done()  # Context removal doesn't cancel an in-flight read.
    finally:
        release.set()
        await asyncio.wait_for(refresh, timeout=5)
    read_api.onboarding_details_typed.assert_not_called()
    assert coordinator(config_entry).data.suggested_todos is None
    assert coordinator(config_entry).data.onboarding is None


async def test_setup_refresh_mode_cannot_change_an_inflight_normal_refresh(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock
) -> None:
    await setup(hass, config_entry)
    co = coordinator(config_entry)
    entered = asyncio.Event()
    release = asyncio.Event()
    original: WeatherResponse = read_api.weather.return_value

    async def blocked_weather(home_id: str) -> WeatherResponse:
        entered.set()
        await release.wait()
        return original

    read_api.weather.side_effect = blocked_weather
    read_api.weather.reset_mock()
    read_api.credits_summary.reset_mock()
    refresh = asyncio.create_task(co.async_refresh())
    prime = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        prime = asyncio.create_task(co.async_refresh_new_platforms())
        await asyncio.sleep(0)
        assert not co._refresh_new_platforms
        assert not prime.done()
    finally:
        release.set()
        await asyncio.wait_for(refresh, timeout=5)
        if prime is not None:
            await asyncio.wait_for(prime, timeout=5)
    read_api.weather.assert_awaited_once_with(HOME_ID)
    read_api.credits_summary.assert_awaited_once_with(HOME_ID)
    assert not co._refresh_new_platforms


@pytest.mark.parametrize("endpoint", [SUGGESTED_TODOS, ONBOARDING])
async def test_unload_cancels_new_reads_recovery_listeners_and_alarms(
    hass: HomeAssistant, config_entry: MockConfigEntry, read_api: MagicMock, endpoint: str
) -> None:
    await setup(hass, config_entry)
    main = config_entry.runtime_data
    co = coordinator(config_entry)
    calendar = calendar_entity(hass)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(home_id: str) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    getattr(read_api, endpoint).side_effect = blocked
    refresh = config_entry.async_create_background_task(
        hass, co.async_refresh(), name="Synthetic read"
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    main.async_set_updated_data(bootstrap_model(homes=[]))
    main.async_set_updated_data(bootstrap_model())
    await asyncio.sleep(0)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert refresh.cancelled() and cancelled.is_set()
    assert not main._listeners and not co._listeners
    assert co._unsub_refresh is None and co._debounced_refresh._timer_task is None
    assert not config_entry._background_tasks
    assert calendar._alarm_unsubs is None
    read_api.suggested_todos.reset_mock()
    read_api.onboarding_details_typed.reset_mock()
    main.async_set_updated_data(bootstrap_model())
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=46))
    await hass.async_block_till_done(wait_background_tasks=True)
    read_api.suggested_todos.assert_not_called()
    read_api.onboarding_details_typed.assert_not_called()
