"""Shared fixtures for the Kamst-IR tests."""

from __future__ import annotations

from collections.abc import Generator
import json
from pathlib import Path
from typing import Any
from unittest.mock import PropertyMock, patch

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.kamstir.const import (
    CONF_METER_TYPE,
    CONF_MODE,
    CONF_MQTT_PREFIX,
    DOMAIN,
    METER_HEAT,
    MODE_MQTT,
    MODE_REST,
)

FIXTURES = Path(__file__).parent / "fixtures"

HOST = "192.168.1.50"
HOST2 = "192.168.1.51"
PORT = 82
MAC = "30:76:f5:00:00:01"
MAC2 = "30:76:f5:a1:22:9c"
PREFIX = "home/heating/kamstir"


def url(host: str = HOST, port: int = PORT) -> str:
    """Return the gateway's read URL."""
    return f"http://{host}:{port}/kamst-ir/api/read"


def load_payload(name: str) -> dict[str, Any]:
    """Load a fixture payload."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Create entities that are disabled by default as enabled."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        new_callable=PropertyMock,
        return_value=True,
    ):
        yield


@pytest.fixture
def mock_gateway(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Serve the heat fixture on the default host."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    return aioclient_mock


def make_entry(
    *,
    title: str = "Kamstrup K1",
    host: str = HOST,
    mac: str = MAC,
    meter_type: str = METER_HEAT,
    mode: str = MODE_REST,
    prefix: str | None = None,
    scan_interval: int = 60,
) -> MockConfigEntry:
    """Build a config entry like the config flow creates."""
    options: dict[str, Any] = {CONF_SCAN_INTERVAL: scan_interval, CONF_MODE: mode}
    if mode == MODE_MQTT:
        options[CONF_MQTT_PREFIX] = prefix or PREFIX
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=mac,
        data={CONF_HOST: host, CONF_PORT: PORT, CONF_METER_TYPE: meter_type},
        options=options,
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    """Add and set up an entry."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
