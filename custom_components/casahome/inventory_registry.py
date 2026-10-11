"""Room-aware defaults and one-time placement for virtual inventory devices."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from custom_components.casahome.const import DOMAIN
from custom_components.casahome.read_models import InventoryRecord

_LEGACY_PREFIX = "Casa inventory "
_ORDINAL = re.compile(r" ([1-9][0-9]*)\Z")
# Leave space for ordinals and HA's diagnostic entity name/object ID.
_MAX_LABEL = 200


def _normalized(value: str) -> str:
    return " ".join(value.split()).casefold()


def _single_room(record: InventoryRecord) -> str | None:
    """Old projections may only supply room. New ones retain ambiguity."""
    rooms = record.known_rooms or ((record.room,) if record.room else ())
    return rooms[0] if len(rooms) == 1 else None


def _prefixed(prefix: str | None, name: str) -> str:
    if prefix is None or _normalized(name).startswith(f"{_normalized(prefix)} "):
        return name[:_MAX_LABEL]
    if _normalized(name) == _normalized(prefix):
        return name[:_MAX_LABEL]
    if (
        name.casefold().startswith(prefix.casefold())
        and name[len(prefix) : len(prefix) + 1].isupper()
    ):
        return name[:_MAX_LABEL]
    return f"{prefix[:100]} {name[:100]}"[:_MAX_LABEL].rstrip()


def _base_name(record: InventoryRecord) -> str:
    return _prefixed(_single_room(record) or record.manufacturer, record.name or "Inventory item")


def _qualified_name(base: str, detail: str | None) -> str:
    if not detail:
        return base
    # Reserve space for the discriminator, even with a very long room/type.
    suffix = f" ({detail[:100]})"
    return f"{base[: _MAX_LABEL - len(suffix)].rstrip()}{suffix}"


def _numbered_name(label: str, ordinal: int) -> str:
    suffix = f" {ordinal}"
    return f"{label[: 255 - len(suffix)].rstrip()}{suffix}"


def _existing_ordinal(label: str, name: str | None) -> int | None:
    if name == label:
        return 0
    if name and (match := _ORDINAL.search(name)):
        ordinal = int(match.group(1))
        if _numbered_name(label, ordinal) == name:
            return ordinal
    return None


def _record_labels(record: InventoryRecord) -> tuple[str, ...]:
    base = _base_name(record)
    details = (
        record.model,
        record.manufacturer,
        _prefixed(record.manufacturer, record.model or ""),
    )
    return (base, *(_qualified_name(base, detail) for detail in details if detail))


def resolve_inventory_names(
    records: tuple[InventoryRecord, ...], devices: dict[str, dr.DeviceEntry]
) -> dict[str, str]:
    """Reserve generated names in retained registry entries, including absent records.

    IDs sort only the initial allocation, never an existing numbered allocation.
    Qualifiers and numbers survive shrinking groups and reload without extra storage.
    """
    groups: dict[str, list[InventoryRecord]] = defaultdict(list)
    for record in records:
        groups[_normalized(_base_name(record))].append(record)

    labels: dict[str, str] = {}
    for group in groups.values():
        models = {_normalized(record.model or "") for record in group}
        makes = {_normalized(record.manufacturer or "") for record in group}
        for record in group:
            detail = None
            if len(models) > 1:
                detail = record.model or record.manufacturer
                peers = [
                    peer
                    for peer in group
                    if _normalized(peer.model or "") == _normalized(record.model or "")
                ]
                if len({_normalized(peer.manufacturer or "") for peer in peers}) > 1:
                    detail = _prefixed(record.manufacturer, record.model or "")
            elif len(makes) > 1:
                detail = record.manufacturer
            label = _qualified_name(_base_name(record), detail)
            device = devices.get(record.id)
            if device:
                # Keep compatible qualifiers even when a shrinking group's
                # preferred discriminator changes from model to make.
                label = next(
                    (
                        candidate
                        for candidate in _record_labels(record)
                        if (detail is None or candidate != _base_name(record))
                        if _existing_ordinal(candidate, device.name) is not None
                    ),
                    label,
                )
            labels[record.id] = label

    reserved = {_normalized(device.name) for device in devices.values() if device.name}
    result: dict[str, str] = {}
    by_label: dict[str, list[InventoryRecord]] = defaultdict(list)
    for record in records:
        by_label[_normalized(labels[record.id])].append(record)
    # Preserve established numbered names before allocating any new ones.
    for record in records:
        device = devices.get(record.id)
        if (
            device
            and (ordinal := _existing_ordinal(labels[record.id], device.name)) is not None
            and (ordinal or len(by_label[_normalized(labels[record.id])]) == 1)
        ):
            assert device.name is not None
            result[record.id] = device.name
    for record in sorted(records, key=lambda record: record.id):
        if record.id in result:
            continue
        label = labels[record.id]
        if len(by_label[_normalized(label)]) == 1 and _normalized(label) not in reserved:
            name = label
        else:
            ordinal = 1
            while _normalized(name := _numbered_name(label, ordinal)) in reserved:
                ordinal += 1
        result[record.id] = name
        reserved.add(_normalized(name))
    return result


def _matching_area(areas: Iterable[ar.AreaEntry], room: str | None) -> str | None:
    if room is None:
        return None
    key = _normalized(room)
    matches = {
        area.id
        for area in areas
        if key in {_normalized(area.name), *(_normalized(alias) for alias in area.aliases)}
    }
    return next(iter(matches)) if len(matches) == 1 else None


class InventoryDeviceRegistry:
    """One registry owner per home. Entities always read its selected default."""

    def __init__(self, hass: HomeAssistant, entry_id: str, home_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._home_id = home_id
        self._registry = dr.async_get(hass)
        self._identifier_prefix = f"{home_id}_inventory_"

    def _device(self, record_id: str) -> dr.DeviceEntry | None:
        return self._registry.async_get_device_by_identifier(
            (DOMAIN, f"{self._identifier_prefix}{record_id}"), self._entry_id
        )

    def device_info(self, record_id: str, record: InventoryRecord | None = None) -> DeviceInfo:
        """Placeholders omit metadata. Fresh entities never recompute a bare name."""
        info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._identifier_prefix}{record_id}")},
            entry_type=DeviceEntryType.SERVICE,
        )
        if record is not None:
            device = self._device(record_id)
            if device is not None:
                info["name"] = device.name
            info.update(
                {
                    "manufacturer": record.manufacturer,
                    "model": record.model,
                    "serial_number": record.serial_number,
                }
            )
        if parent := self._registry.async_get_device_by_identifier(
            (DOMAIN, self._home_id), self._entry_id
        ):
            info["via_device_id"] = parent.id
        return info

    @callback
    def async_refresh(self, records: tuple[InventoryRecord, ...]) -> None:
        devices = {
            identifier.removeprefix(self._identifier_prefix): device
            for device in dr.async_entries_for_config_entry(self._registry, self._entry_id)
            if device.entry_type is DeviceEntryType.SERVICE
            for domain, identifier in device.identifiers
            if domain == DOMAIN and identifier.startswith(self._identifier_prefix)
        }
        names = resolve_inventory_names(records, devices)
        areas = tuple(ar.async_get(self._hass).async_list_areas())
        for record in records:
            device = self._device(record.id)
            place = device is None or bool(device.name and device.name.startswith(_LEGACY_PREFIX))
            if device is None:
                info = self.device_info(record.id, record)
                info["name"] = names[record.id]
                device = self._registry.async_get_or_create(config_entry_id=self._entry_id, **info)
            else:
                device = self._registry.async_update_device(
                    device.id,
                    name=names[record.id],
                    manufacturer=record.manufacturer,
                    model=record.model,
                    serial_number=record.serial_number,
                )
            # The migrated name itself is the persistent one-shot marker, even
            # if no area matches. Never recreate areas or reclaim a cleared one.
            if (
                place
                and device
                and device.area_id is None
                and device.name_by_user is None
                and device.disabled_by is None
                and (area_id := _matching_area(areas, _single_room(record)))
            ):
                self._registry.async_update_device(device.id, area_id=area_id)
