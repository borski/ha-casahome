"""Context-driven per-home reads. Bootstrap authorizes which homes can be polled."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from casahome import (
    CasaApi,
    CasaApiError,
    CasaAuthError,
    CasaConnectionError,
    CasaResponseError,
)
from casahome.models import MemberCredits, WeatherResponse
from homeassistant.components.calendar import CalendarEvent
from homeassistant.components.todo.entity import TodoItem
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, REQUEST_TIMEOUT_SECONDS, UPDATE_INTERVAL
from .projections import onboarding_events, suggested_todo_items

if TYPE_CHECKING:
    from . import CasaConfigEntry
    from .coordinator import CasaCoordinator

_LOGGER = logging.getLogger(__name__)

# HTTP statuses that mean the endpoint is gone for this account. Stop calling it.
DISABLING_STATUSES = frozenset({404, 405})

WEATHER: Final = "weather"
CREDITS: Final = "credits_summary"
SUGGESTED_TODOS: Final = "suggested_todos"
ONBOARDING: Final = "onboarding_details_typed"
type HomeEndpoint = Literal[
    "weather", "credits_summary", "suggested_todos", "onboarding_details_typed"
]


@dataclass(frozen=True)
class HomeData:
    """Latest safe projections for one home. None means unavailable this cycle."""

    weather: WeatherResponse | None = None
    credits: MemberCredits | None = None
    suggested_todos: tuple[TodoItem, ...] | None = None
    onboarding: tuple[CalendarEvent, ...] | None = None


class CasaHomeCoordinator(DataUpdateCoordinator[HomeData]):
    """Share polling, auth, and generation guards across a home's read families."""

    config_entry: CasaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CasaConfigEntry,
        api: CasaApi,
        home_id: str,
        main_coordinator: CasaCoordinator | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_home",
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self.home_id = home_id
        self.main_coordinator = (
            main_coordinator if main_coordinator is not None else entry.runtime_data
        )
        self._home_available = self.home_available
        self._home_generation = 0
        self._initial_probe = True
        self._refresh_new_platforms = False
        # Lives for the life of the entry. A reload (or HA restart) retries.
        self.disabled: set[HomeEndpoint] = set()

    @property
    def home_available(self) -> bool:
        """Return whether bootstrap still authorizes access to this home."""
        return (
            self.main_coordinator.last_update_success
            and self.main_coordinator.data is not None
            and any(home.id == self.home_id for home in self.main_coordinator.data.homes)
        )

    @callback
    def async_update_home(self) -> None:
        """Pause polling on home loss and refresh active entities on recovery."""
        was_available = self._home_available
        self._home_available = self.home_available
        if not self._home_available:
            if was_available:
                self._home_generation += 1
                self.async_set_updated_data(HomeData())
            self._unschedule_refresh()
        elif not was_available and any(self.async_contexts()):
            self.config_entry.async_create_background_task(
                self.hass,
                self.async_request_refresh(),
                name=f"{self.name} home recovered",
            )

    @callback
    def _schedule_refresh(self) -> None:
        """Only schedule recurring updates while the home is accessible."""
        if self.home_available:
            super()._schedule_refresh()

    async def _fetch[T](self, name: HomeEndpoint, call: Callable[[str], Awaitable[T]]) -> T | None:
        if name in self.disabled or not self.home_available:
            return None
        generation = self._home_generation
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                return await call(self.home_id)
        except CasaAuthError:
            raise
        except CasaApiError as err:
            if generation != self._home_generation or not self.home_available:
                return None
            if err.status in DISABLING_STATUSES:
                self.disabled.add(name)
                _LOGGER.warning(
                    "Casa endpoint %s returned HTTP %s. Disabled until reload",
                    name,
                    err.status,
                )
            else:
                _LOGGER.debug("Casa endpoint %s returned HTTP %s", name, err.status)
            return None
        except (CasaConnectionError, CasaResponseError, TimeoutError) as err:
            _LOGGER.debug("Casa endpoint %s failed: %s", name, type(err).__name__)
            return None

    async def _async_update_data(self) -> HomeData:
        # Weather/credits retain their setup probe. New families are always lazy.
        initial_probe = self._initial_probe
        self._initial_probe = False
        if not self.home_available:
            return HomeData()
        generation = self._home_generation
        attempted: set[HomeEndpoint] = set()
        weather = self.data.weather if self._refresh_new_platforms and self.data else None
        credits = self.data.credits if self._refresh_new_platforms and self.data else None
        todos = None
        onboarding = None
        try:
            if initial_probe or WEATHER in self.async_contexts():
                attempted.add(WEATHER)
                if not self._refresh_new_platforms:
                    weather = await self._fetch(WEATHER, self.api.weather)
            if (
                generation == self._home_generation
                and self.home_available
                and (initial_probe or CREDITS in self.async_contexts())
            ):
                attempted.add(CREDITS)
                if not self._refresh_new_platforms:
                    credits = await self._fetch(CREDITS, self.api.credits_summary)
            if (
                generation == self._home_generation
                and self.home_available
                and SUGGESTED_TODOS in self.async_contexts()
            ):
                attempted.add(SUGGESTED_TODOS)
                todos = await self._fetch(SUGGESTED_TODOS, self._fetch_suggested_todos)
            if (
                generation == self._home_generation
                and self.home_available
                and ONBOARDING in self.async_contexts()
            ):
                attempted.add(ONBOARDING)
                onboarding = await self._fetch(ONBOARDING, self._fetch_onboarding)
        except CasaAuthError:
            raise ConfigEntryAuthFailed("Casa rejected the refresh token") from None
        if generation != self._home_generation or not self.home_available:
            if self.home_available and any(self.async_contexts()):
                # Queue a fresh cycle if the earlier recovery debounce expired in flight.
                await self.async_request_refresh()
            return HomeData()
        endpoints = attempted.intersection(self.async_contexts())
        if initial_probe:
            endpoints.update(attempted.intersection({WEATHER, CREDITS}))
        weather = weather if WEATHER in endpoints else None
        credits = credits if CREDITS in endpoints else None
        todos = todos if SUGGESTED_TODOS in endpoints else None
        onboarding = onboarding if ONBOARDING in endpoints else None
        if all(value is None for value in (weather, credits, todos, onboarding)) and (
            endpoints - self.disabled
        ):
            raise UpdateFailed("Casa home endpoints returned no data")
        return HomeData(
            weather=weather, credits=credits, suggested_todos=todos, onboarding=onboarding
        )

    async def async_refresh_new_platforms(self) -> None:
        """Finish setup after contexts exist without repeating the sensor probe."""
        # Match HA's async_refresh lock and handler. Set the mode only after
        # acquiring that lock, so it can't change an in-flight normal refresh.
        async with self._debounced_refresh.async_lock():
            self._refresh_new_platforms = True
            try:
                await self._async_refresh(log_failures=True)
            finally:
                self._refresh_new_platforms = False

    async def _fetch_suggested_todos(self, home_id: str) -> tuple[TodoItem, ...]:
        """One aggregate GET, never a detail request per suggestion."""
        return suggested_todo_items(await self.api.suggested_todos(home_id))

    async def _fetch_onboarding(self, home_id: str) -> tuple[CalendarEvent, ...] | None:
        """A bodyless 304 can reuse only this home's current generation's cache."""
        response = await self.api.onboarding_details_typed(home_id)
        if response is None:
            return self.data.onboarding if self.data is not None else None
        return onboarding_events(response)
