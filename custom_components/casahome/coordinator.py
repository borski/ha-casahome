"""Coordinator that polls Casa's bootstrap endpoint, the only VERIFIED read."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from casahome import (
    CasaApi,
    CasaApiError,
    CasaAuthError,
    CasaConnectionError,
    CasaResponseError,
)
from casahome.models import Bootstrap
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, REQUEST_TIMEOUT_SECONDS, UPDATE_INTERVAL

if TYPE_CHECKING:
    from . import CasaConfigEntry

_LOGGER = logging.getLogger(__name__)


class CasaCoordinator(DataUpdateCoordinator[Bootstrap]):
    """Fetch the member bootstrap every 15 minutes."""

    config_entry: CasaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CasaConfigEntry, api: CasaApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self._schema_warned = False

    async def _async_update_data(self) -> Bootstrap:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                data = await self.api.bootstrap()
        except CasaAuthError as err:
            raise ConfigEntryAuthFailed("Casa rejected the refresh token") from err
        except (CasaConnectionError, TimeoutError) as err:
            raise UpdateFailed("Casa unreachable") from err
        except CasaResponseError as err:
            if not self._schema_warned:
                _LOGGER.warning("Casa bootstrap response didn't match the expected schema")
                self._schema_warned = True
            raise UpdateFailed("Casa response didn't match the expected schema") from err
        except CasaApiError as err:
            raise UpdateFailed(f"Casa returned HTTP {err.status}") from err
        self._schema_warned = False
        return data
