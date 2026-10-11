"""Config flow for Casa."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import probatio
from casahome import (
    CasaApi,
    CasaAuthError,
    CasaClient,
    CasaConnectionError,
    FirebaseAuth,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import CONF_DEVICE_ID, CONF_REFRESH_TOKEN, DOMAIN, REQUEST_TIMEOUT_SECONDS

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


class _WrongAccount(Exception):
    """Authenticated identity doesn't match the entry. Never bootstrap it."""


async def _validate(
    hass: HomeAssistant,
    token: str,
    device_id: str,
    *,
    expected_user_id: str | None = None,
    on_refresh_token_rotated: Callable[[str], None] | None = None,
) -> _Validated:
    """Authenticate before bootstrap, which registers the stable device_id."""
    session = async_get_clientsession(hass)
    auth = FirebaseAuth(session, token, on_refresh_token_rotated=on_refresh_token_rotated)
    async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
        await auth.get_id_token()
        user_id = auth.user_id
        if not user_id:
            raise CasaAuthError("no user id after refresh")
        if expected_user_id is not None and user_id != expected_user_id:
            raise _WrongAccount
        api = CasaApi(
            CasaClient(session, auth), device_id=device_id, timezone=hass.config.time_zone
        )
        bootstrap = await api.bootstrap()
    # Bootstrap can trigger another refresh on a 401. Save the live token on success.
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
    # 2 dropped the experimental option. See async_migrate_entry.
    MINOR_VERSION = 2

    def __init__(self) -> None:
        super().__init__()
        self._device_id = str(uuid.uuid4()).upper()
        self._submitted_token: str | None = None
        self._pending_token: str | None = None
        self._validation_lock = asyncio.Lock()
        self._removed = False

    @callback
    def _clear_pending_token(self) -> None:
        self._submitted_token = None
        self._pending_token = None

    @callback
    def async_remove(self) -> None:
        """Discard pending credentials when HA removes or cancels the flow."""
        self._removed = True
        self._clear_pending_token()
        super().async_remove()

    async def _async_validate_token(
        self, submitted: str, device_id: str, *, expected_user_id: str | None = None
    ) -> _Validated:
        """Retain rotations only in this flow, for retries of the same submitted token."""
        if self._removed:
            raise asyncio.CancelledError
        if submitted != self._submitted_token:
            self._clear_pending_token()
            self._submitted_token = submitted
            self._pending_token = submitted

        active = True

        @callback
        def _remember_rotated_token(new_token: str) -> None:
            # Sync assignment only, inside the SDK refresh lock. No entry writes or logs.
            # Completed attempts and removed flows can't restore or overwrite pending state.
            if active and not self._removed and self._submitted_token == submitted:
                self._pending_token = new_token

        assert self._pending_token is not None
        try:
            result = await _validate(
                self.hass,
                self._pending_token,
                device_id,
                expected_user_id=expected_user_id,
                on_refresh_token_rotated=_remember_rotated_token,
            )
            if self._removed:
                raise asyncio.CancelledError
        except (CasaAuthError, _WrongAccount, asyncio.CancelledError):
            self._clear_pending_token()
            raise
        finally:
            active = False
        # Connection/timeout/response failures retain the rotation for a form retry.
        self._clear_pending_token()
        return result

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # HA can submit the same flow concurrently. Serialize cache and result handling too.
        async with self._validation_lock:
            errors: dict[str, str] = {}
            if user_input is not None:
                device_id = self._device_id
                try:
                    result = await self._async_validate_token(
                        user_input[CONF_REFRESH_TOKEN], device_id
                    )
                except Exception as err:  # mapped to a form error
                    errors["base"] = _error_key(err)
                else:
                    await self.async_set_unique_id(result.user_id)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=result.first_name or "Casa",
                        data={CONF_REFRESH_TOKEN: result.refresh_token, CONF_DEVICE_ID: device_id},
                    )
            return self.async_show_form(step_id="user", data_schema=TOKEN_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        async with self._validation_lock:
            errors: dict[str, str] = {}
            entry = self._get_reauth_entry()
            if user_input is not None:
                try:
                    result = await self._async_validate_token(
                        user_input[CONF_REFRESH_TOKEN],
                        entry.data[CONF_DEVICE_ID],
                        expected_user_id=entry.unique_id or "",
                    )
                except _WrongAccount:
                    return self.async_abort(reason="wrong_account")
                except Exception as err:  # mapped to a form error
                    errors["base"] = _error_key(err)
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_REFRESH_TOKEN: result.refresh_token}
                    )
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=TOKEN_SCHEMA, errors=errors
            )
