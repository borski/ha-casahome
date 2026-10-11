"""Persistent record of which inventory device areas the integration still owns."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypedDict

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_STORE_VERSION = 1
_SAVE_DELAY = 1


class _StoredMarkers(TypedDict):
    devices: dict[str, str | None]


def _store(hass: HomeAssistant, entry_id: str) -> Store[_StoredMarkers]:
    return Store(hass, _STORE_VERSION, f"{DOMAIN}.{entry_id}.inventory_areas")


class InventoryAreaMarkers:
    """Device identifier -> area ID the integration last assigned (None if it cleared one).

    A present key means the integration still manages that device's area. The key
    is the stable inventory identifier, so it survives registry ID churn. Any
    area the user sets, clears, or deletes away from the marker releases it for good.
    """

    def __init__(self, store: Store[_StoredMarkers], markers: dict[str, str | None]) -> None:
        self._store = store
        self._markers = markers
        self._dirty = False

    @classmethod
    async def async_load(cls, hass: HomeAssistant, entry_id: str) -> InventoryAreaMarkers:
        store = _store(hass, entry_id)
        stored = await store.async_load()
        devices = stored.get("devices", {}) if isinstance(stored, dict) else {}
        markers = {
            key: value
            for key, value in devices.items()
            if isinstance(key, str) and (value is None or isinstance(value, str))
        }
        return cls(store, markers)

    @staticmethod
    async def async_remove(hass: HomeAssistant, entry_id: str) -> None:
        await _store(hass, entry_id).async_remove()

    def __contains__(self, identifier: str) -> bool:
        return identifier in self._markers

    def area_id(self, identifier: str) -> str | None:
        return self._markers.get(identifier)

    @callback
    def assign(self, identifier: str, area_id: str | None) -> None:
        if identifier in self._markers and self._markers[identifier] == area_id:
            return
        self._markers[identifier] = area_id
        self._schedule_save()

    @callback
    def release(self, identifier: str) -> None:
        if identifier in self._markers:
            del self._markers[identifier]
            self._schedule_save()

    @callback
    def prune(self, prefix: str, existing: Iterable[str]) -> None:
        """Forget only this home's markers whose registry device no longer exists."""
        keep = set(existing)
        for identifier in [key for key in self._markers if key.startswith(prefix)]:
            if identifier not in keep:
                self.release(identifier)

    async def async_flush(self) -> None:
        """A reload loads a fresh store, so never leave a delayed save pending."""
        if self._dirty:
            await self._store.async_save(self._data())

    def _data(self) -> _StoredMarkers:
        self._dirty = False
        return {"devices": dict(self._markers)}

    @callback
    def _schedule_save(self) -> None:
        self._dirty = True
        self._store.async_delay_save(self._data, _SAVE_DELAY)
