"""Config flow for Casa."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from typing import Any

import probatio
from casahome import (
    CasaApi,
    CasaAuthError,
    CasaClient,
    CasaConnectionError,
    FirebaseAuth,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_EXPERIMENTAL,
    CONF_REFRESH_TOKEN,
    DEFAULT_ENABLE_EXPERIMENTAL,
    DOMAIN,
    REQUEST_TIMEOUT_SECONDS,
)

__all__ = ["CasaApi", "CasaClient", "CasaConfigFlow", "FirebaseAuth"]

_LOGGER = logging.getLogger(__name__)

TOKEN_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_REFRESH_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


class _Validated:
    """What a successful token check hands back. Never logged."""

    def __init__(self, user_id: str, refresh_token: str, first_name: str | None) -> None:
        self.user_id = user_id
        self.refresh_token = refresh_token
        self.first_name = first_name


async def _validate(hass: HomeAssistant, token: str, device_id: str) -> _Validated:
    """Run one bootstrap with this token. Bootstrap registers the device, so reuse device_id."""
    session = async_get_clientsession(hass)
    auth = FirebaseAuth(session, token)
    api = CasaApi(CasaClient(session, auth), device_id=device_id, timezone=hass.config.time_zone)
    async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
        bootstrap = await api.bootstrap()
    user_id = auth.user_id
    if not user_id:
        raise CasaAuthError("no user id after refresh")
    # The token can rotate during the refresh that bootstrap triggered. Keep the live one.
    return _Validated(user_id, auth.refresh_token or token, bootstrap.first_name)


def _error_key(err: BaseException) -> str:
    if isinstance(err, CasaAuthError):
        return "invalid_auth"
    if isinstance(err, CasaConnectionError | TimeoutError):
        return "cannot_connect"
    _LOGGER.error("Unexpected error validating Casa token: %s", type(err).__name__)
    return "unknown"


class CasaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Casa config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            device_id = str(uuid.uuid4()).upper()
            try:
                result = await _validate(self.hass, user_input[CONF_REFRESH_TOKEN], device_id)
            except Exception as err:  # mapped to a form error
                errors["base"] = _error_key(err)
            else:
                await self.async_set_unique_id(result.user_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=result.first_name or "Casa",
                    data={CONF_REFRESH_TOKEN: result.refresh_token, CONF_DEVICE_ID: device_id},
                    options={CONF_ENABLE_EXPERIMENTAL: DEFAULT_ENABLE_EXPERIMENTAL},
                )
        return self.async_show_form(step_id="user", data_schema=TOKEN_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                result = await _validate(
                    self.hass, user_input[CONF_REFRESH_TOKEN], entry.data[CONF_DEVICE_ID]
                )
            except Exception as err:  # mapped to a form error
                errors["base"] = _error_key(err)
            else:
                if result.user_id != entry.unique_id:
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_REFRESH_TOKEN: result.refresh_token}
                )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=TOKEN_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> CasaOptionsFlow:
        return CasaOptionsFlow()


class CasaOptionsFlow(OptionsFlow):
    """One switch: poll endpoints nobody has seen in real traffic yet."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(
            CONF_ENABLE_EXPERIMENTAL, DEFAULT_ENABLE_EXPERIMENTAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_ENABLE_EXPERIMENTAL, default=current): bool}
            ),
        )
