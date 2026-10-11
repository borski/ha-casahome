"""Read-only Casa suggestions. No acceptance, completion, or task mutation."""

from __future__ import annotations

from copy import copy

from homeassistant.components.todo.entity import TodoItem, TodoListEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CasaConfigEntry
from .home_coordinator import SUGGESTED_TODOS
from .home_data_entity import CasaHomeReadEntity

PARALLEL_UPDATES = 0
SUGGESTIONS = EntityDescription(
    key="suggested_todos", translation_key="suggested_todos", icon="mdi:clipboard-list-outline"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CasaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one suggestion list per home from the initial bootstrap."""
    coordinator = entry.runtime_data
    async_add_entities(
        CasaSuggestedTodoList(
            coordinator.home_coordinators[home.id], home, SUGGESTIONS, SUGGESTED_TODOS
        )
        for home in coordinator.data.homes
    )


class CasaSuggestedTodoList(CasaHomeReadEntity, TodoListEntity):
    """Suggestion titles and UIDs only. Unsupported statuses stay unset."""

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.suggested_todos is not None

    @property
    def todo_items(self) -> list[TodoItem] | None:
        if not self.available:
            return None
        assert self.coordinator.data.suggested_todos is not None
        return [copy(item) for item in self.coordinator.data.suggested_todos]
