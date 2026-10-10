"""Shared fixtures. Mocks sit at the SDK boundary. No HTTP, no live Casa."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from casahome.models import Bootstrap, MemberCredits, WeatherResponse
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.casahome.const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_EXPERIMENTAL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

FIXTURES = Path(__file__).parent / "fixtures"

USER_ID = "fake-firebase-user-0001"
REFRESH_TOKEN = "fake-refresh-token-not-real"
DEVICE_ID = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
HOME_ID = "00000000-0000-4000-8000-000000000002"


def load_fixture_json(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def bootstrap_model(**overrides: Any) -> Bootstrap:
    data = load_fixture_json("bootstrap.json")
    data.update(overrides)
    return Bootstrap.model_validate(data)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let HA load custom_components/casahome in every test."""


@pytest.fixture
def mock_api() -> Generator[MagicMock]:
    """Patch CasaApi everywhere the integration constructs it."""
    api = MagicMock()
    api.bootstrap = AsyncMock(return_value=bootstrap_model())
    api.badge_counts = AsyncMock()
    # Real models, so an options flip to experimental never feeds MagicMocks to entities.
    api.weather = AsyncMock(
        return_value=WeatherResponse(
            temperature_degrees=61.5, temperature_units="fahrenheit", condition_type="cloudy"
        )
    )
    api.credits_summary = AsyncMock(
        return_value=MemberCredits(
            home_id=HOME_ID, total_available=3, handyman_hourly_rate_cents=8500
        )
    )
    api.device_id = DEVICE_ID
    with (
        patch("custom_components.casahome.CasaApi", return_value=api) as init_cls,
        patch("custom_components.casahome.config_flow.CasaApi", return_value=api) as flow_cls,
    ):
        api.init_cls = init_cls
        api.flow_cls = flow_cls
        yield api


@pytest.fixture
def mock_auth() -> Generator[MagicMock]:
    """Patch FirebaseAuth so no token refresh happens. Captures the rotation callback."""
    auth = MagicMock()
    auth.user_id = USER_ID
    auth.get_id_token = AsyncMock(return_value="fake-id-token")
    captured: dict[str, Any] = {}

    def _factory(*args: Any, **kwargs: Any) -> MagicMock:
        captured["args"] = args
        captured["kwargs"] = kwargs
        # Real FirebaseAuth exposes the token it holds. Echo the one we were given.
        auth.refresh_token = args[1] if len(args) > 1 else kwargs.get("refresh_token")
        return auth

    with (
        patch("custom_components.casahome.FirebaseAuth", side_effect=_factory),
        patch("custom_components.casahome.config_flow.FirebaseAuth", side_effect=_factory),
    ):
        auth.captured = captured
        yield auth


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Michael",
        unique_id=USER_ID,
        data={CONF_REFRESH_TOKEN: REFRESH_TOKEN, CONF_DEVICE_ID: DEVICE_ID},
        options={CONF_ENABLE_EXPERIMENTAL: False},
    )
