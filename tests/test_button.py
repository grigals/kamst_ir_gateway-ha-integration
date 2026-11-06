"""Tests for the restart button."""

from __future__ import annotations

import aiohttp
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from .conftest import HOST, PORT, load_payload, make_entry, setup_entry, url

RESTART = "button.kamstrup_k1_restart"
REBOOT_URL = f"http://{HOST}:{PORT}/reboot"


async def press(hass: HomeAssistant) -> None:
    """Press the restart button."""
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: RESTART}, blocking=True
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "Rebooting..."},
        {"exc": aiohttp.ServerDisconnectedError()},
        {"exc": TimeoutError()},
    ],
)
async def test_restart(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, kwargs: dict
) -> None:
    """Pressing Restart calls GET /reboot; a dropped connection is fine."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    aioclient_mock.get(REBOOT_URL, **kwargs)
    await setup_entry(hass, make_entry())
    assert hass.states.get(RESTART) is not None

    await press(hass)
    assert any(str(call[1]) == REBOOT_URL for call in aioclient_mock.mock_calls)


@pytest.mark.parametrize(
    ("kwargs", "key"),
    [
        ({"status": 401}, "restart_auth_required"),
        ({"exc": aiohttp.ClientConnectionError()}, "restart_failed"),
        ({"status": 500}, "restart_failed"),
    ],
)
async def test_restart_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, kwargs: dict, key: str
) -> None:
    """Login prompts and connection failures show a translated error."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    aioclient_mock.get(REBOOT_URL, **kwargs)
    await setup_entry(hass, make_entry())

    with pytest.raises(HomeAssistantError) as err:
        await press(hass)
    assert err.value.translation_key == key
