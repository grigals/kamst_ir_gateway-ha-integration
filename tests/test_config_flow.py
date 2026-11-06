"""Tests for the config, reconfigure and options flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import aiohttp
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.kamstir.const import (
    CONF_METER_TYPE,
    CONF_MODE,
    CONF_MQTT_PREFIX,
    DOMAIN,
    METER_HEAT,
    METER_WATER,
    MODE_MQTT,
    MODE_REST,
)

from .conftest import (
    HOST,
    HOST2,
    MAC,
    MAC2,
    PORT,
    PREFIX,
    load_payload,
    make_entry,
    setup_entry,
    url,
)


def user_input(**overrides: Any) -> dict[str, Any]:
    """Return a complete user step input."""
    return {
        CONF_HOST: HOST,
        CONF_PORT: PORT,
        CONF_NAME: "Kamstrup K1",
        CONF_METER_TYPE: METER_HEAT,
        CONF_MODE: MODE_REST,
        **overrides,
    }


async def start_flow(hass: HomeAssistant) -> dict[str, Any]:
    """Start the user flow."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def test_user_rest(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """A REST gateway is added with its MAC as unique ID."""
    result = await start_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(**{CONF_HOST: f" {HOST} ", CONF_PORT: 82.0})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kamstrup K1"
    assert result["data"] == {
        CONF_HOST: HOST,
        CONF_PORT: 82,
        CONF_METER_TYPE: METER_HEAT,
    }
    assert result["options"] == {CONF_SCAN_INTERVAL: 60, CONF_MODE: MODE_REST}
    assert result["result"].unique_id == MAC


async def test_blank_name_uses_default(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """An empty name falls back to "Kamstrup"."""
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(**{CONF_NAME: "  "})
    )
    assert result["title"] == "Kamstrup"


@pytest.mark.parametrize(
    ("mock_kwargs", "error"),
    [
        ({"exc": aiohttp.ClientError()}, "cannot_connect"),
        ({"text": "not json"}, "invalid_response"),
        ({"json": {"power": "1"}}, "invalid_response"),
    ],
)
async def test_user_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_kwargs: dict[str, Any],
    error: str,
) -> None:
    """Connection problems show an error and the user can retry."""
    aioclient_mock.get(url(), **mock_kwargs)
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    aioclient_mock.clear_requests()
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input()
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_unknown_error(hass: HomeAssistant) -> None:
    """Unexpected exceptions map to unknown."""
    result = await start_flow(hass)
    with patch(
        "custom_components.kamstir.config_flow.async_read_mac",
        side_effect=RuntimeError("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input()
        )
    assert result["errors"] == {"base": "unknown"}


async def test_already_configured_updates_host(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Adding the same gateway again aborts and picks up the new address."""
    entry = make_entry(host="10.0.0.9")
    entry.add_to_hass(hass)
    aioclient_mock.get(url(HOST2), json=load_payload("read_heat.json"))

    result = await start_flow(hass)
    with patch("custom_components.kamstir.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input(**{CONF_HOST: HOST2})
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == HOST2


async def test_user_mqtt(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """MQTT mode asks for the prefix and stores it without trailing slash."""
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input(**{CONF_MODE: MODE_MQTT, CONF_METER_TYPE: METER_WATER}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mqtt"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MQTT_PREFIX: "home/#"}
    )
    assert result["errors"] == {CONF_MQTT_PREFIX: "invalid_prefix"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MQTT_PREFIX: f" {PREFIX}/ "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_METER_TYPE] == METER_WATER
    assert result["options"] == {
        CONF_SCAN_INTERVAL: 60,
        CONF_MODE: MODE_MQTT,
        CONF_MQTT_PREFIX: PREFIX,
    }


async def test_user_mqtt_default_and_duplicate_prefix(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """The prefix defaults to kamstir and can't be shared by two gateways."""
    make_entry(mac=MAC2, host=HOST2, mode=MODE_MQTT, prefix="kamstir").add_to_hass(hass)
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))

    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(**{CONF_MODE: MODE_MQTT})
    )
    schema = result["data_schema"].schema
    default = next(k for k in schema if k == CONF_MQTT_PREFIX).default()
    assert default == "kamstir"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MQTT_PREFIX: "kamstir"}
    )
    assert result["errors"] == {CONF_MQTT_PREFIX: "prefix_in_use"}

    with patch("custom_components.kamstir.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MQTT_PREFIX: PREFIX}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_options_prefix_in_use(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """Options refuse a prefix another gateway uses."""
    make_entry(mac=MAC2, host=HOST2, mode=MODE_MQTT, prefix=PREFIX).add_to_hass(hass)
    entry = await setup_entry(hass, make_entry())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 60, CONF_MODE: MODE_MQTT, CONF_MQTT_PREFIX: PREFIX},
    )
    assert result["errors"] == {CONF_MQTT_PREFIX: "prefix_in_use"}


async def test_user_mqtt_not_configured(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """MQTT mode aborts when the MQTT integration is missing."""
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(**{CONF_MODE: MODE_MQTT})
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "mqtt_not_configured"


async def test_reconfigure_rest(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The host can change while the MAC stays the same."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    aioclient_mock.get(url(HOST2), json=load_payload("read_heat.json"))
    entry = await setup_entry(hass, make_entry())

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    assert CONF_MQTT_PREFIX not in result["data_schema"].schema

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST2, CONF_PORT: PORT}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == HOST2


async def test_reconfigure_wrong_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Pointing the entry at another gateway aborts."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    aioclient_mock.get(url(HOST2), json=load_payload("read_water.json"))
    entry = await setup_entry(hass, make_entry())

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST2, CONF_PORT: PORT}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data[CONF_HOST] == HOST


async def test_reconfigure_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An unreachable new host shows an error."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    aioclient_mock.get(url(HOST2), exc=aiohttp.ClientError())
    entry = await setup_entry(hass, make_entry())

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST2, CONF_PORT: PORT}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_mqtt_prefix(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """In MQTT mode the prefix can be changed too."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))

    result = await entry.start_reconfigure_flow(hass)
    assert CONF_MQTT_PREFIX in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_PORT: PORT, CONF_MQTT_PREFIX: "+"}
    )
    assert result["errors"] == {CONF_MQTT_PREFIX: "invalid_prefix"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: HOST, CONF_PORT: PORT, CONF_MQTT_PREFIX: "new/prefix/"},
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.options[CONF_MQTT_PREFIX] == "new/prefix"
    assert entry.options[CONF_MODE] == MODE_MQTT


async def test_options_rest(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """The scan interval can be changed."""
    entry = await setup_entry(hass, make_entry())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120.0, CONF_MODE: MODE_REST}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCAN_INTERVAL: 120, CONF_MODE: MODE_REST}
    await hass.async_block_till_done()
    assert entry.runtime_data.update_interval.total_seconds() == 120


async def test_options_switch_to_mqtt(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """Switching to MQTT requires a valid prefix."""
    entry = await setup_entry(hass, make_entry())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, CONF_MODE: MODE_MQTT}
    )
    assert result["errors"] == {CONF_MQTT_PREFIX: "invalid_prefix"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 60, CONF_MODE: MODE_MQTT, CONF_MQTT_PREFIX: PREFIX},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.mode == MODE_MQTT


async def test_options_mqtt_not_configured(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """Switching to MQTT without the MQTT integration shows an error."""
    entry = await setup_entry(hass, make_entry())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 60, CONF_MODE: MODE_MQTT, CONF_MQTT_PREFIX: PREFIX},
    )
    assert result["errors"] == {"base": "mqtt_not_configured"}
