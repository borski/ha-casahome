"""Opt-in polling of Casa endpoints that have never been seen in live traffic.

Paths and methods come from static analysis of the Casa app. They may 404,
405, or return a different shape. Nothing here runs unless the
``enable_experimental`` option is on.
"""

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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, REQUEST_TIMEOUT_SECONDS, UPDATE_INTERVAL

if TYPE_CHECKING:
    from . import CasaConfigEntry
    from .coordinator import CasaCoordinator

_LOGGER = logging.getLogger(__name__)

# HTTP statuses that mean "this endpoint doesn't exist as guessed". Stop calling it.
DISABLING_STATUSES = frozenset({404, 405})

WEATHER: Final = "weather"
CREDITS: Final = "credits_summary"
type ExperimentalEndpoint = Literal["weather", "credits_summary"]


@dataclass(frozen=True)
class ExperimentalData:
    """Latest experimental values for one home. None means unavailable this cycle."""

    weather: WeatherResponse | None = None
    credits: MemberCredits | None = None


class CasaExperimentalCoordinator(DataUpdateCoordinator[ExperimentalData]):
    """Poll the unverified weather and credits endpoints for one home."""

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
            name=f"{DOMAIN}_experimental",
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
        # Lives for the life of the entry. A reload (or HA restart) retries.
        self.disabled: set[ExperimentalEndpoint] = set()

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
        """Pause polling on home loss and refresh active sensors on recovery."""
        was_available = self._home_available
        self._home_available = self.home_available
        if not self._home_available:
            if was_available:
                self._home_generation += 1
                self.async_set_updated_data(ExperimentalData())
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

    async def _fetch[T](
        self, name: ExperimentalEndpoint, call: Callable[[str], Awaitable[T]]
    ) -> T | None:
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
                    "Casa experimental endpoint %s returned HTTP %s. Disabled until reload",
                    name,
                    err.status,
                )
            else:
                _LOGGER.debug("Casa experimental endpoint %s returned HTTP %s", name, err.status)
            return None
        except (CasaConnectionError, CasaResponseError, TimeoutError) as err:
            _LOGGER.debug("Casa experimental endpoint %s failed: %s", name, type(err).__name__)
            return None

    async def _async_update_data(self) -> ExperimentalData:
        # Only the explicit option-on setup probe may fetch without entity listeners.
        initial_probe = self._initial_probe
        self._initial_probe = False
        if not self.home_available:
            return ExperimentalData()
        generation = self._home_generation
        attempted: set[ExperimentalEndpoint] = set()
        weather = None
        credits = None
        try:
            if initial_probe or WEATHER in self.async_contexts():
                attempted.add(WEATHER)
                weather = await self._fetch(WEATHER, self.api.weather)
            if (
                generation == self._home_generation
                and self.home_available
                and (initial_probe or CREDITS in self.async_contexts())
            ):
                attempted.add(CREDITS)
                credits = await self._fetch(CREDITS, self.api.credits_summary)
        except CasaAuthError:
            raise ConfigEntryAuthFailed("Casa rejected the refresh token") from None
        if generation != self._home_generation or not self.home_available:
            if self.home_available and any(self.async_contexts()):
                # Queue a fresh cycle if the earlier recovery debounce expired in flight.
                await self.async_request_refresh()
            return ExperimentalData()
        endpoints = attempted if initial_probe else attempted.intersection(self.async_contexts())
        weather = weather if WEATHER in endpoints else None
        credits = credits if CREDITS in endpoints else None
        if weather is None and credits is None and endpoints - self.disabled:
            raise UpdateFailed("Casa experimental endpoints returned no data")
        return ExperimentalData(weather=weather, credits=credits)
