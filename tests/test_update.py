"""Tests for the firmware update entity."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.update import (
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
    DOMAIN as UPDATE_DOMAIN,
    SERVICE_INSTALL,
    UpdateEntityFeature,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.kamstir.const import MODE_MQTT

from .conftest import PREFIX, load_payload, make_entry, setup_entry, url

FIRMWARE = "update.kamstrup_k1_firmware"


async def test_rest_shows_versions(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """REST mode shows the versions but cannot install."""
    await setup_entry(hass, make_entry())
    state = hass.states.get(FIRMWARE)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_INSTALLED_VERSION] == "2026071601"
    assert state.attributes[ATTR_LATEST_VERSION] == "2026101501"
    assert not state.attributes[ATTR_SUPPORTED_FEATURES] & UpdateEntityFeature.INSTALL

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: FIRMWARE}, blocking=True
        )


async def test_rest_install_guard(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """Calling install directly in REST mode raises a translated error."""
    entry = await setup_entry(hass, make_entry())
    entity = hass.data["entity_components"][UPDATE_DOMAIN].get_entity(FIRMWARE)
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_install(None, False)
    assert err.value.translation_key == "install_needs_mqtt"
    assert entry.runtime_data.can_install is False


async def test_no_newer_firmware(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """firmware_available "0" means up to date."""
    aioclient_mock.get(url(), json=load_payload("read_empty.json"))
    await setup_entry(hass, make_entry())
    state = hass.states.get(FIRMWARE)
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_LATEST_VERSION] == "2026071601"


async def test_mqtt_install_publishes(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """In MQTT mode Install publishes yes to the install topic."""
    await setup_entry(hass, make_entry(mode=MODE_MQTT))
    state = hass.states.get(FIRMWARE)
    assert state.attributes[ATTR_SUPPORTED_FEATURES] & UpdateEntityFeature.INSTALL

    await hass.services.async_call(
        UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: FIRMWARE}, blocking=True
    )
    mqtt_mock.async_publish.assert_called_once_with(
        f"{PREFIX}/smart_gateways/install_update", "yes", 0, False
    )
