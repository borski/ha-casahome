"""Constants for the Casa integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "casahome"

CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_DEVICE_ID: Final = "device_id"

# Removed in config entry minor version 2. Kept only so migration can drop it.
LEGACY_CONF_ENABLE_EXPERIMENTAL: Final = "enable_experimental"

# Bootstrap is about 92 KB. Every 15 minutes is polite.
UPDATE_INTERVAL: Final = timedelta(minutes=15)
REQUEST_TIMEOUT_SECONDS: Final = 30

MANUFACTURER: Final = "Casa"
MODEL: Final = "Casa membership"
CONFIGURATION_URL: Final = "https://getcasa.com"
