"""Coordinator that polls Casa's account-wide bootstrap endpoint."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from casahome import (
    CasaApi,
    CasaApiError,
    CasaAuthError,
    CasaConnectionError,
    CasaResponseError,
)
from casahome.models import Bootstrap
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, REQUEST_TIMEOUT_SECONDS, UPDATE_INTERVAL

if TYPE_CHECKING:
    from . import CasaConfigEntry
    from .home_coordinator import CasaHomeCoordinator

_LOGGER = logging.getLogger(__name__)


class CasaCoordinator(DataUpdateCoordinator[Bootstrap]):
    """Fetch the member bootstrap every 15 minutes."""

    config_entry: CasaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CasaConfigEntry,
        api: CasaApi,
        *,
        resume_token_persistence: Callable[[], None],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self._schema_warned = False
        self.token_persistence_active = True
        self._resume_token_persistence = resume_token_persistence
        # Context-driven reads, one coordinator per home in the first bootstrap.
        self.home_coordinators: dict[str, CasaHomeCoordinator] = {}

    @callback
    def async_resume_token_persistence(self) -> None:
        """Reconcile the current SDK token after an unsuccessful unload."""
        self._resume_token_persistence()

    async def _async_update_data(self) -> Bootstrap:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                data = await self.api.bootstrap()
        except CasaAuthError:
            raise ConfigEntryAuthFailed("Casa rejected the refresh token") from None
        except (CasaConnectionError, TimeoutError):
            raise UpdateFailed("Casa unreachable") from None
        except CasaResponseError:
            if not self._schema_warned:
                _LOGGER.warning("Casa bootstrap response didn't match the expected schema")
                self._schema_warned = True
            raise UpdateFailed("Casa response didn't match the expected schema") from None
        except CasaApiError as err:
            raise UpdateFailed(f"Casa returned HTTP {err.status}") from None
        self._schema_warned = False
        return data
