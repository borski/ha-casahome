"""Diagnostics for Casa. Redacts everything on the never-expose list."""

from __future__ import annotations

from typing import Any

from casahome.models import Bootstrap
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import CasaConfigEntry
from .const import CONF_DEVICE_ID, CONF_REFRESH_TOKEN

TO_REDACT_ENTRY = {CONF_REFRESH_TOKEN, CONF_DEVICE_ID}

TO_REDACT_DATA = {
    # Account
    "id",
    "phone_number",
    "email",
    "last_name",
    "avatar_url",
    "avatar_url_cache_key",
    "calendar_sync_email",
    "calendar_sync_options",
    "user_prefs",
    "waitlist_entries",
    # Home location and media
    "address_street_number",
    "address_street",
    "address_subpremise",
    "address_city",
    "address_state",
    "address_postal_code",
    "latitude",
    "longitude",
    "home_image_url",
    "home_image_url_cache_key",
    "matterport_model_id",
    "matterport_model_password",
    "route",
    # Billing and membership
    "card_surcharge_rate",
    "payment_description",
    "payment_method_type",
    "payment_method_last4",
    "payment_method_brand",
    "membership_name",
    "membership_plan",
    "membership_amount",
    "membership_next_renewal",
    "past_due_until",
    "scheduled_change",
    # Agreement text
    "content",
}


def _declared_only(bootstrap: Bootstrap) -> dict[str, Any]:
    """Dump declared fields only. SDK models keep unknown upstream fields, which a
    denylist can't anticipate, so new Casa fields never reach diagnostics."""
    data = bootstrap.model_dump(mode="json", include=set(type(bootstrap).model_fields))
    homes = []
    for home in bootstrap.homes:
        dumped = home.model_dump(mode="json", include=set(type(home).model_fields))
        dumped["pending_agreements"] = [
            agreement.model_dump(mode="json", include=set(type(agreement).model_fields))
            for agreement in home.pending_agreements
        ]
        if home.badge_counts is not None:
            dumped["badge_counts"] = home.badge_counts.model_dump(
                mode="json", include=set(type(home.badge_counts).model_fields)
            )
        homes.append(dumped)
    data["homes"] = homes
    # app_update has a known model. Untyped calendar options are redacted as a whole.
    if bootstrap.app_update is not None:
        data["app_update"] = bootstrap.app_update.model_dump(
            mode="json", include=set(type(bootstrap.app_update).model_fields)
        )
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CasaConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = entry.runtime_data
    bootstrap = _declared_only(coordinator.data) if coordinator.data else None
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT_ENTRY),
            "options": dict(entry.options),
        },
        "last_update_success": coordinator.last_update_success,
        "bootstrap": async_redact_data(bootstrap, TO_REDACT_DATA) if bootstrap else None,
    }
