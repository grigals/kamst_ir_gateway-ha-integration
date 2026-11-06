"""Tests for MQTT push mode."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_mqtt_message,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.kamstir.const import MODE_MQTT

from .conftest import PREFIX, make_entry, setup_entry, url

POWER = "sensor.kamstrup_k1_power"
CONNECTED = "binary_sensor.kamstrup_k1_connected"


async def flush(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float = 2
) -> None:
    """Let the debounce timer fire."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_push_updates(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Published values update the entities after one REST read at startup."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    assert entry.runtime_data.mode == MODE_MQTT
    assert hass.states.get(POWER).state == "12.3"
    assert len(mock_gateway.mock_calls) == 1

    async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", "15.5")
    async_fire_mqtt_message(hass, f"{PREFIX}/reading/temp1", "70.1")
    async_fire_mqtt_message(hass, f"{PREFIX}/smart_gateways/wifi_rssi", "-55")
    await hass.async_block_till_done()
    assert hass.states.get(POWER).state == "12.3"  # still debouncing

    await flush(hass, freezer)
    assert hass.states.get(POWER).state == "15.5"
    assert hass.states.get("sensor.kamstrup_k1_supply_temperature").state == "70.1"
    assert entry.runtime_data.raw["wifi_rssi"] == "-55"
    assert len(mock_gateway.mock_calls) == 1  # no polling in MQTT mode


async def test_mqtt_firmware_topic_names(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The MQTT firmware topics use other names than REST; both feed the entity."""
    await setup_entry(hass, make_entry(mode=MODE_MQTT))
    base = f"{PREFIX}/smart_gateways"
    async_fire_mqtt_message(hass, f"{base}/running_firmware_version", "2026101501")
    async_fire_mqtt_message(hass, f"{base}/available_firmware_version", "0")
    async_fire_mqtt_message(hass, f"{base}/update_available", "false")
    await flush(hass, freezer)

    state = hass.states.get("update.kamstrup_k1_firmware")
    assert state.attributes["installed_version"] == "2026101501"
    assert state.attributes["latest_version"] == "2026101501"
    assert state.state == "off"


@pytest.mark.parametrize(
    "topic",
    [
        f"{PREFIX}/smart_gateways/install_update",
        f"{PREFIX}/other/power",
        f"{PREFIX}/reading/power/extra",
        f"{PREFIX}/reading/",
        "somewhere/else/reading/power",
    ],
)
async def test_ignored_topics(
    hass: HomeAssistant,
    mqtt_mock: MagicMock,
    mock_gateway: AiohttpClientMocker,
    topic: str,
) -> None:
    """Only <prefix>/reading|smart_gateways/<field> is used."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    assert entry.runtime_data.field_from_topic(topic) is None


async def test_rest_down_at_startup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without REST the entry loads, entities wait for the first publish."""
    aioclient_mock.get(url(), exc=aiohttp.ClientError())
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(POWER).state == STATE_UNAVAILABLE
    assert hass.states.get(CONNECTED).state == STATE_OFF

    async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", b"9.9")
    await flush(hass, freezer)
    assert hass.states.get(POWER).state == "9.9"
    assert hass.states.get(CONNECTED).state == STATE_ON


async def test_watchdog_marks_stale(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No data for 10 minutes makes the entities unavailable until data returns."""
    await setup_entry(hass, make_entry(mode=MODE_MQTT))

    for _ in range(11):
        await flush(hass, freezer, 60)
    assert hass.states.get(POWER).state == STATE_UNAVAILABLE
    assert hass.states.get(CONNECTED).state == STATE_OFF
    assert sum("No MQTT data" in r.getMessage() for r in caplog.records) == 1

    async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", "1.0")
    await flush(hass, freezer)
    assert hass.states.get(POWER).state == "1.0"
    assert hass.states.get(CONNECTED).state == STATE_ON
    assert sum("arriving again" in r.getMessage() for r in caplog.records) == 1


async def test_mqtt_client_unavailable(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """If the MQTT client never comes up, setup is retried."""
    with patch(
        "custom_components.kamstir.coordinator.mqtt.async_wait_for_mqtt_client",
        return_value=False,
    ):
        entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_cancels_pending(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """Unloading with a pending debounce leaves nothing scheduled."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", "1.0")
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
