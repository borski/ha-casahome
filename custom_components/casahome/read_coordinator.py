"""Independent aggregate read families, without changing bootstrap token ownership."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Any

from casahome import CasaApiError, CasaAuthError, CasaConnectionError, CasaResponseError
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, REQUEST_TIMEOUT_SECONDS, UPDATE_INTERVAL
from .read_models import (
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

if TYPE_CHECKING:
    from . import CasaConfigEntry
    from .coordinator import CasaCoordinator

_LOGGER = logging.getLogger(__name__)
CATALOG_INTERVAL = timedelta(hours=24)


async def _cancel_sdk_read(task: asyncio.Task[ReadData]) -> None:
    """Finish child cleanup without repeated cancellation or upstream error details."""
    if task.done():
        return
    if not task.cancelling():
        task.cancel()
    await asyncio.shield(asyncio.gather(task, return_exceptions=True))


class CasaReadCoordinator(DataUpdateCoordinator[ReadData]):
    """One GET per active family. Each family has its own timer and error state."""

    config_entry: CasaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CasaConfigEntry,
        main: CasaCoordinator,
        home_id: str,
        family: ReadFamily,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{family}",
            update_interval=(
                CATALOG_INTERVAL
                if family in {ReadFamily.INVENTORY, ReadFamily.MAINTENANCE}
                else UPDATE_INTERVAL
            ),
        )
        self.main_coordinator = main
        self.home_id = home_id
        self.family = family
        self.disabled = False
        self.data = ReadData()
        self._generation = 0
        self._home_available = self.home_available
        self._started = False
        self._unsub_main: Callable[[], None] | None = None
        self._fetch_task: asyncio.Task[ReadData] | None = None
        self.inventory_listener: Callable[[], None] | None = None
        entry.async_on_unload(self._stop_home_listener)

    @callback
    def _stop_home_listener(self) -> None:
        if self._unsub_main is not None:
            self._unsub_main()
            self._unsub_main = None

    @property
    def home_available(self) -> bool:
        return (
            self.main_coordinator.last_update_success
            and self.main_coordinator.data is not None
            and any(home.id == self.home_id for home in self.main_coordinator.data.homes)
        )

    @callback
    def async_update_home(self) -> None:
        was_available = self._home_available
        self._home_available = self.home_available
        if not self._home_available:
            if was_available:
                self._generation += 1
                self.async_set_updated_data(ReadData())
            self._unschedule_refresh()
        elif not was_available:
            self._request_fresh_data()

    @callback
    def _request_fresh_data(self) -> None:
        if self._started and self._read_eligible:
            self.config_entry.async_create_background_task(
                self.hass, self.async_request_refresh(), name=f"{self.name} fresh read"
            )

    @callback
    def async_add_listener(
        self, update_callback: Callable[[], None], context: Any = None
    ) -> Callable[[], None]:
        was_active = any(self.async_contexts())
        remove = super().async_add_listener(update_callback, context)
        if not was_active:
            self._generation += 1
            if any(self.async_contexts()) and self._unsub_main is None:
                self._unsub_main = self.main_coordinator.async_add_listener(self.async_update_home)
                self.async_update_home()
            self._request_fresh_data()

        @callback
        def remove_listener() -> None:
            remove()
            if not any(self.async_contexts()):
                self._generation += 1
                self.data = ReadData()
                self._stop_home_listener()

        return remove_listener

    @callback
    def _schedule_refresh(self) -> None:
        if not self._shutdown_requested and self.home_available and not self.disabled:
            super()._schedule_refresh()

    @callback
    def async_update_listeners(self) -> None:
        super().async_update_listeners()
        if self.inventory_listener is not None:
            self.inventory_listener()

    async def async_start(self) -> None:
        """Called as an entry-owned task after platforms have registered contexts."""
        self._started = True
        await self.async_refresh()

    @property
    def _read_eligible(self) -> bool:
        return (
            not self._shutdown_requested
            and self.home_available
            and not self.disabled
            and any(self.async_contexts())
        )

    def _is_current_read(self, generation: int) -> bool:
        return generation == self._generation and self._read_eligible

    async def async_shutdown(self) -> None:
        """Cancel the SDK child itself, including reads dispatched by HA timers."""
        await super().async_shutdown()
        self._started = False
        self._generation += 1
        self.data = ReadData()
        self._stop_home_listener()
        if (task := self._fetch_task) is not None:
            # Await only the child, never the dispatcher holding the debounce lock.
            # No upstream exceptions or cancellation details escape shutdown.
            await _cancel_sdk_read(task)

    async def _fetch_projection(self) -> ReadData:
        api = self.main_coordinator.api
        match self.family:
            case ReadFamily.INVENTORY:
                return inventory_data(await api.home_details(self.home_id))
            case ReadFamily.REQUESTS:
                return request_data(await api.requests_tab(self.home_id))
            case ReadFamily.MAINTENANCE:
                return maintenance_data(await api.autopilot_plans(self.home_id))
            case ReadFamily.HISTORY:
                return history_data(
                    await api.inventory_service_history(self.home_id, "home", self.home_id)
                )
            case ReadFamily.MAIL:
                return mail_data(await api.mail_items(self.home_id))
            case ReadFamily.PACKAGES:
                return package_data(await api.packages(self.home_id))
            case ReadFamily.CALENDAR:
                # Rolling 31 UTC days. Cache queries outside this coverage fail explicitly.
                start = dt_util.utcnow().astimezone(UTC).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) - timedelta(days=1)
                end = start + timedelta(days=31)
                return service_calendar_data(
                    await api.calendar(self.home_id, start, end), start, end
                )

    async def _async_update_data(self) -> ReadData:
        if not self._read_eligible:
            return ReadData()
        generation = self._generation
        data = ReadData()
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                # A debounce wrapper can finish before HA dispatches its timer.
                # Own the actual child through async_shutdown, not its dispatcher.
                # Shielding avoids repeated cancellation from entry task groups
                # interrupting the SDK's cancellation cleanup.
                task = self.hass.async_create_background_task(
                    self._fetch_projection(), name=f"{self.name} SDK read"
                )
                self._fetch_task = task
                try:
                    data = await asyncio.shield(task)
                finally:
                    try:
                        await _cancel_sdk_read(task)
                    finally:
                        if self._fetch_task is task:
                            self._fetch_task = None
        except CasaAuthError:
            if self._is_current_read(generation):
                # Clear cached values even when HA's coordinator keeps its previous data.
                self.data = ReadData()
                raise ConfigEntryAuthFailed("Casa rejected the refresh token") from None
        except CasaApiError as err:
            if self._is_current_read(generation) and err.status in {404, 405}:
                self.disabled = True
                self._unschedule_refresh()
                _LOGGER.warning(
                    "Casa read family %s returned HTTP %s. Disabled until reload",
                    self.family,
                    err.status,
                )
            elif self._is_current_read(generation):
                self.data = ReadData()
                raise UpdateFailed(f"Casa read family returned HTTP {err.status}") from None
        except (CasaConnectionError, CasaResponseError, TimeoutError):
            # Never log upstream exception details, including schema input values.
            if self._is_current_read(generation):
                self.data = ReadData()
                raise UpdateFailed("Casa read family is unavailable") from None
        if not self._is_current_read(generation):
            self._request_fresh_data()
            return ReadData()
        return data
