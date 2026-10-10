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
from typing import TYPE_CHECKING

from casahome import (
    CasaApi,
    CasaApiError,
    CasaAuthError,
    CasaConnectionError,
    CasaResponseError,
)
from casahome.models import MemberCredits, WeatherResponse
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, REQUEST_TIMEOUT_SECONDS, UPDATE_INTERVAL

if TYPE_CHECKING:
    from . import CasaConfigEntry

_LOGGER = logging.getLogger(__name__)

# HTTP statuses that mean "this endpoint doesn't exist as guessed". Stop calling it.
DISABLING_STATUSES = frozenset({404, 405})

WEATHER = "weather"
CREDITS = "credits_summary"


@dataclass(frozen=True)
class ExperimentalData:
    """Latest experimental values for one home. None means unavailable this cycle."""

    weather: WeatherResponse | None = None
    credits: MemberCredits | None = None


class CasaExperimentalCoordinator(DataUpdateCoordinator[ExperimentalData]):
    """Poll the unverified weather and credits endpoints for one home."""

    config_entry: CasaConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: CasaConfigEntry, api: CasaApi, home_id: str
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
        # Lives for the life of the entry. A reload (or HA restart) retries.
        self.disabled: set[str] = set()

    async def _fetch[T](self, name: str, call: Callable[[str], Awaitable[T]]) -> T | None:
        if name in self.disabled:
            return None
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                return await call(self.home_id)
        except CasaAuthError:
            raise
        except CasaApiError as err:
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
        try:
            weather = await self._fetch(WEATHER, self.api.weather)
            credits = await self._fetch(CREDITS, self.api.credits_summary)
        except CasaAuthError as err:
            raise ConfigEntryAuthFailed("Casa rejected the refresh token") from err
        if weather is None and credits is None and self.disabled != {WEATHER, CREDITS}:
            raise UpdateFailed("Casa experimental endpoints returned no data")
        return ExperimentalData(weather=weather, credits=credits)
