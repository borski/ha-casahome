"""Config flow for Casa."""

from __future__ import annotations

from casahome import CasaApi, CasaClient, FirebaseAuth
from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN

__all__ = ["CasaApi", "CasaClient", "CasaConfigFlow", "FirebaseAuth"]


class CasaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Casa config flow."""

    VERSION = 1
