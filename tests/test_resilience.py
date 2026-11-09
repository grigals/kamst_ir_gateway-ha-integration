"""Robustness tests: garbage input, failure modes, lifecycle and consistency.

The other test files check that the integration does the right thing with
good input. These check that it keeps working when the gateway, the broker
or the user does something unexpected.
"""

from __future__ import annotations

from datetime import timedelta
import json
import logging
import math
from pathlib import Path
import tomllib
from typing import Any
from unittest.mock import MagicMock

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_mqtt_message,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.kamstir.api import FIELD_TYPES, KamstirParser
from custom_components.kamstir.const import (
    CONF_ENERGY_UNIT,
    CONF_MODE,
    CONF_MQTT_PREFIX,
    DOMAIN,
    ENERGY_UNIT_MWH,
    ENERGY_UNITS,
    ISSUE_LEGACY_YAML,
    METER_TYPES,
    MODE_MQTT,
    MODE_REST,
    MODES,
)
from custom_components.kamstir.diagnostics import async_get_config_entry_diagnostics
from custom_components.kamstir.sensor import SENSORS

from .conftest import (
    FIXTURES,
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

REPO = Path(__file__).parents[1]
COMPONENT = REPO / "custom_components" / "kamstir"

# Fields that describe the gateway itself; garbage tests leave them intact so
# the entry can still be identified.
GATEWAY_FIELDS = {"mac_address", "gateway_model", "firmware_running"}

NASTY_VALUES = [
    "nan",
    "NaN",
    "inf",
    "-inf",
    "Infinity",
    "1e400",
    "-1e400",
    "",
    " ",
    "-",
    "--",
    "abc",
    "0",
    "0.00",
    "-1",
    "99999999999999999999",
    "24:00:00",
    "0000-00-00",
    "991399",
    "true",
    "None",
    "null",
    "💥",
    "1,5",
    "12.3 kW",
]


def entity_id(hass: HomeAssistant, platform: str, key: str, mac: str = MAC) -> str:
    """Return the entity ID for a unique-ID key."""
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{mac}_{key}")
    assert found is not None, f"no {platform} entity for {key}"
    return found


def state_of(hass: HomeAssistant, platform: str, key: str, mac: str = MAC) -> str:
    """Return the state string for a unique-ID key."""
    state = hass.states.get(entity_id(hass, platform, key, mac))
    assert state is not None, key
    return state.state


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float = 61
) -> None:
    """Move time forward and let timers run."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def integration_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return logged errors and tracebacks, wherever they came from."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR or record.exc_info
    ]


# --------------------------------------------------------------------------
# Parser: nothing the gateway sends may raise or produce a non-finite number
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["nan", "NaN", "-nan", "inf", "-inf", "Infinity"])
def test_non_finite_numbers_are_rejected(value: str) -> None:
    """NaN and infinity are not readings; they must become unknown."""
    assert KamstirParser().parse_field("power", value) is None
    assert KamstirParser().parse_field("infoevent", value) is None


def test_overflowing_number_is_rejected() -> None:
    """A number too large for a float parses to infinity in Python."""
    assert KamstirParser().parse_field("heat_energy", "1e400") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 12.5 ", 12.5),
        ("1e3", 1000.0),
        ("-0.5", -0.5),
        ("+7", 7.0),
        ("0", 0.0),
        ("1,5", None),
        ("0x10", None),
        ("12.3 kW", None),
        ("", None),
        ("-", None),
        ("   ", None),
    ],
)
def test_number_formats(raw: str, expected: float | None) -> None:
    """Only plain decimal numbers are accepted."""
    assert KamstirParser().parse_field("power", raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TRUE", True),
        (" true ", True),
        ("False", False),
        ("1", None),
        ("yes", None),
        ("on", None),
    ],
)
def test_bool_formats(raw: str, expected: bool | None) -> None:
    """Booleans are true/false in any case; nothing else is guessed."""
    assert KamstirParser().parse_field("mqtt_configured", raw) is expected


@pytest.mark.parametrize("value", [*NASTY_VALUES, None, 0, 1.5, True, [], {}])
def test_parser_never_raises(value: Any) -> None:
    """Every field survives every odd value, and numbers stay finite."""
    parser = KamstirParser()
    for field in FIELD_TYPES:
        result = parser.parse_field(field, value)
        if isinstance(result, float):
            assert math.isfinite(result), (field, value)


@pytest.mark.parametrize("value", NASTY_VALUES)
def test_sensor_values_never_raise(value: str) -> None:
    """No sensor's value function raises on a payload full of one odd value."""
    data = KamstirParser().parse(dict.fromkeys(FIELD_TYPES, value))
    for description in SENSORS:
        result = description.value_fn(data)
        if isinstance(result, float):
            assert math.isfinite(result), (description.key, value)


def test_parser_handles_non_string_json_values() -> None:
    """JSON numbers and booleans work the same as their string forms."""
    parsed = KamstirParser().parse(
        {"power": 12.3, "infoevent": 0, "mqtt_configured": True, "temp1": None}
    )
    assert parsed == {
        "power": 12.3,
        "infoevent": 0.0,
        "mqtt_configured": True,
        "temp1": None,
    }


# --------------------------------------------------------------------------
# REST: garbage payloads and failure modes, end to end
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("value", ["nan", "inf", "1e400", "garbage", "💥", ""])
async def test_garbage_payload_end_to_end(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    """A payload of rubbish loads cleanly: unknown states, no errors logged."""
    payload = {
        key: raw if key in GATEWAY_FIELDS else value
        for key, raw in load_payload("read_heat.json").items()
    }
    aioclient_mock.get(url(), json=payload)
    entry = await setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.LOADED

    sensors = hass.states.async_all("sensor")
    assert len(sensors) > 30
    # leak_burst and mqtt_server are free text and show whatever was sent.
    text_ids = {
        entity_id(hass, "sensor", "leak_burst"),
        entity_id(hass, "sensor", "mqtt_server"),
    }
    wrong = {
        s.entity_id: s.state
        for s in sensors
        if s.entity_id not in text_ids and s.state != STATE_UNKNOWN
    }
    assert wrong == {}
    assert state_of(hass, "binary_sensor", "connected") == STATE_ON
    assert integration_errors(caplog) == []


@pytest.mark.parametrize(
    "failure",
    [
        {"exc": aiohttp.ClientConnectionError()},
        {"exc": TimeoutError()},
        {"status": 500},
        {"status": 404},
        {"text": "<html>busy</html>"},
        {"json": ["not", "an", "object"]},
        {"text": ""},
    ],
)
async def test_poll_failure_modes_recover(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    failure: dict[str, Any],
) -> None:
    """Every kind of bad answer makes the sensors unavailable, then recovers."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    entry = await setup_entry(hass, make_entry())
    power = entity_id(hass, "sensor", "power")

    aioclient_mock.clear_requests()
    aioclient_mock.get(url(), **failure)
    await tick(hass, freezer)
    assert hass.states.get(power).state == STATE_UNAVAILABLE
    assert entry.state is ConfigEntryState.LOADED
    # The last good payload is kept for diagnostics.
    assert entry.runtime_data.raw["power"] == "12.3"

    payload = load_payload("read_heat.json")
    payload["power"] = "3.3"
    aioclient_mock.clear_requests()
    aioclient_mock.get(url(), json=payload)
    await tick(hass, freezer)
    assert hass.states.get(power).state == "3.3"
    assert not [r for r in caplog.records if r.exc_info]


@pytest.mark.parametrize(
    "failure",
    [{"text": "<html>login</html>"}, {"json": []}, {"status": 503}],
)
async def test_bad_answer_at_startup_retries(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    failure: dict[str, Any],
) -> None:
    """A bad first answer is retried, and setup completes once it is good."""
    aioclient_mock.get(url(), **failure)
    entry = await setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.states.async_all("sensor") == []

    aioclient_mock.clear_requests()
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    await tick(hass, freezer, 120)
    # The retry runs as a background task.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    assert state_of(hass, "sensor", "power") == "12.3"


async def test_value_turning_missing_is_unknown(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A reading that drops to "-" is unknown, not unavailable, and comes back."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    await setup_entry(hass, make_entry())
    power = entity_id(hass, "sensor", "power")
    heat = entity_id(hass, "sensor", "heat_energy")

    payload = load_payload("read_heat.json")
    payload["power"] = "-"
    del payload["heat_energy"]
    aioclient_mock.clear_requests()
    aioclient_mock.get(url(), json=payload)
    await tick(hass, freezer)
    assert hass.states.get(power).state == STATE_UNKNOWN
    assert hass.states.get(heat).state == STATE_UNKNOWN

    aioclient_mock.clear_requests()
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    await tick(hass, freezer)
    assert hass.states.get(power).state == "12.3"
    assert hass.states.get(heat).state == "1234.567"


async def test_polling_follows_scan_interval(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The gateway is asked once per scan interval, not more."""
    await setup_entry(hass, make_entry(scan_interval=300))
    assert mock_gateway.call_count == 1

    await tick(hass, freezer, 120)
    await tick(hass, freezer, 120)
    assert mock_gateway.call_count == 1
    await tick(hass, freezer, 120)
    assert mock_gateway.call_count == 2


async def test_diagnostics_after_failure_is_json(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Diagnostics stay downloadable while the gateway is down."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    entry = await setup_entry(hass, make_entry())
    aioclient_mock.clear_requests()
    aioclient_mock.get(url(), exc=aiohttp.ClientError())
    for _ in range(4):
        await tick(hass, freezer)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag)
    assert diag["coordinator"]["last_update_success"] is False
    assert diag["coordinator"]["connected"] is False
    assert diag["raw"]["heat_energy"] == "1234.567"
    assert "30_76_F5_00_00_01" not in text
    assert "192.168.1.10" not in text  # the broker address


async def test_diagnostics_mqtt(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """MQTT diagnostics name the prefix and have no poll interval."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    diag = await async_get_config_entry_diagnostics(hass, entry)
    json.dumps(diag)
    assert diag["coordinator"]["mode"] == MODE_MQTT
    assert diag["coordinator"]["mqtt_prefix"] == PREFIX
    assert diag["coordinator"]["update_interval"] is None
    assert diag["coordinator"]["connected"] is True


async def test_legacy_yaml_issue_lists_at_most_ten(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Many old entities are counted in full but the list is cut short."""
    for index in range(12):
        entity_registry.async_get_or_create("sensor", "mqtt", f"kamstir_old_{index:02}")
    # Other people's MQTT entities are not ours to complain about.
    entity_registry.async_get_or_create("sensor", "mqtt", "shelly_kamstir_1")
    await setup_entry(hass, make_entry())

    issue = issue_registry.async_get_issue(DOMAIN, ISSUE_LEGACY_YAML)
    assert issue is not None
    assert issue.translation_placeholders["count"] == "12"
    shown = issue.translation_placeholders["entities"]
    assert shown.endswith(" …")
    assert shown.count("sensor.") == 10
    assert "shelly" not in shown


# --------------------------------------------------------------------------
# Lifecycle: reload, reconfigure, mode switches, removal
# --------------------------------------------------------------------------


async def test_remove_entry_cleans_up(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Removing the gateway removes its device, entities and polling."""
    entry = await setup_entry(hass, make_entry())
    assert er.async_entries_for_config_entry(entity_registry, entry.entry_id)

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert er.async_entries_for_config_entry(entity_registry, entry.entry_id) == []
    assert device_registry.async_get_device(identifiers={(DOMAIN, MAC)}) is None

    calls = mock_gateway.call_count
    await tick(hass, freezer, 600)
    assert mock_gateway.call_count == calls


async def test_reload_keeps_entities(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
) -> None:
    """A reload gives the same entities under the same IDs."""
    entry = await setup_entry(hass, make_entry())

    def snapshot() -> dict[str, str]:
        return {
            e.unique_id: e.entity_id
            for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        }

    before = snapshot()
    for _ in range(3):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert snapshot() == before
    assert entry.state is ConfigEntryState.LOADED
    assert state_of(hass, "sensor", "power") == "12.3"


async def test_reconfigure_moves_polling_and_device_link(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    device_registry: dr.DeviceRegistry,
) -> None:
    """After a new address the old one is left alone and the device link moves."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    moved = load_payload("read_heat.json")
    moved["power"] = "7.7"
    aioclient_mock.get(url(HOST2), json=moved)
    entry = await setup_entry(hass, make_entry())

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST2, CONF_PORT: PORT}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    assert state_of(hass, "sensor", "power") == "7.7"
    device = device_registry.async_get_device(identifiers={(DOMAIN, MAC)})
    assert device.configuration_url == f"http://{HOST2}:{PORT}"
    assert len(device_registry.devices) == 1

    aioclient_mock.mock_calls.clear()
    await tick(hass, freezer)
    hosts = {call[1].host for call in aioclient_mock.mock_calls}
    assert hosts == {HOST2}


async def test_switching_modes_keeps_entities(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """REST -> MQTT -> REST keeps every entity and only changes the transport."""
    entry = await setup_entry(hass, make_entry())
    firmware = entity_id(hass, "update", "firmware")
    power = entity_id(hass, "sensor", "power")

    def unique_ids() -> set[str]:
        return {
            e.unique_id
            for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        }

    def can_install() -> bool:
        features = hass.states.get(firmware).attributes[ATTR_SUPPORTED_FEATURES]
        return bool(features & UpdateEntityFeature.INSTALL)

    async def set_options(**options: Any) -> None:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: 60,
                CONF_ENERGY_UNIT: ENERGY_UNIT_MWH,
                **options,
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    before = unique_ids()
    assert not can_install()

    await set_options(**{CONF_MODE: MODE_MQTT, CONF_MQTT_PREFIX: PREFIX})
    assert entry.runtime_data.mode == MODE_MQTT
    assert can_install()
    assert unique_ids() == before
    async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", "8.8")
    await tick(hass, freezer, 2)
    assert hass.states.get(power).state == "8.8"
    calls = mock_gateway.call_count
    await tick(hass, freezer, 300)
    assert mock_gateway.call_count == calls  # MQTT mode does not poll

    await set_options(**{CONF_MODE: MODE_REST, CONF_MQTT_PREFIX: PREFIX})
    assert entry.runtime_data.mode == MODE_REST
    assert not can_install()
    assert unique_ids() == before
    assert hass.states.get(power).state == "12.3"
    # Back on REST the topics are no longer listened to.
    async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", "99")
    await tick(hass, freezer, 2)
    assert hass.states.get(power).state == "12.3"
    await tick(hass, freezer)
    assert mock_gateway.call_count > calls + 1


# --------------------------------------------------------------------------
# MQTT edge cases
# --------------------------------------------------------------------------


async def test_mqtt_burst_gives_one_update(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A burst of topics is one update; the last value of a field wins."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    updates: list[None] = []
    entry.async_on_unload(
        entry.runtime_data.async_add_listener(lambda: updates.append(None))
    )

    for value in ("1.0", "2.0", "3.0"):
        async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", value)
    for field in ("temp1", "temp2", "flow", "volume"):
        async_fire_mqtt_message(hass, f"{PREFIX}/reading/{field}", "5")
    await tick(hass, freezer, 2)

    assert len(updates) == 1
    assert state_of(hass, "sensor", "power") == "3.0"
    assert state_of(hass, "sensor", "flow") == "5.0"


async def test_mqtt_steady_stream_is_not_starved(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Messages that keep coming do not push the update further away."""
    await setup_entry(hass, make_entry(mode=MODE_MQTT))
    power = entity_id(hass, "sensor", "power")

    for index in range(8):
        async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", f"{index}.5")
        await tick(hass, freezer, 0.4)
    assert hass.states.get(power).state != "12.3"


async def test_mqtt_odd_payloads(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing, broken and unknown values over MQTT do no harm."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    coordinator = entry.runtime_data

    async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", "-")
    async_fire_mqtt_message(hass, f"{PREFIX}/reading/temp1", "nan")
    async_fire_mqtt_message(hass, f"{PREFIX}/reading/temp2", "")
    async_fire_mqtt_message(hass, f"{PREFIX}/reading/infoevent", "inf")
    async_fire_mqtt_message(hass, f"{PREFIX}/reading/flow", "soon")
    async_fire_mqtt_message(hass, f"{PREFIX}/reading/brand_new_field", "42")
    async_fire_mqtt_message(hass, f"{PREFIX}/reading/volume", "4568.01")
    await tick(hass, freezer, 2)

    for key in ("power", "temp1", "temp2", "infoevent", "flow"):
        assert state_of(hass, "sensor", key) == STATE_UNKNOWN, key
    assert state_of(hass, "sensor", "volume") == "4568.01"
    assert state_of(hass, "sensor", "heat_energy") == "1234.567"  # untouched
    # Unknown fields are kept raw for diagnostics but create nothing.
    assert coordinator.raw["brand_new_field"] == "42"
    assert "brand_new_field" not in coordinator.data
    assert integration_errors(caplog) == []


async def test_mqtt_bytes_payload(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A raw bytes payload, even invalid UTF-8, is decoded without raising."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    coordinator = entry.runtime_data

    coordinator._handle_message(
        MagicMock(topic=f"{PREFIX}/reading/power", payload=b"4.2")
    )
    coordinator._handle_message(
        MagicMock(topic=f"{PREFIX}/reading/temp1", payload=b"\xff\xfe")
    )
    await tick(hass, freezer, 2)
    assert state_of(hass, "sensor", "power") == "4.2"
    assert state_of(hass, "sensor", "temp1") == STATE_UNKNOWN


async def test_mqtt_two_gateways_do_not_mix(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Gateways on look-alike prefixes each get only their own messages."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    aioclient_mock.get(url(HOST2), json=load_payload("read_heat.json"))
    await setup_entry(hass, make_entry(mode=MODE_MQTT, prefix="home/k"))
    await setup_entry(
        hass,
        make_entry(
            title="Kamstrup K2", host=HOST2, mac=MAC2, mode=MODE_MQTT, prefix="home/k2"
        ),
    )

    async_fire_mqtt_message(hass, "home/k/reading/power", "1.1")
    async_fire_mqtt_message(hass, "home/k2/reading/power", "2.2")
    await tick(hass, freezer, 2)
    assert state_of(hass, "sensor", "power", MAC) == "1.1"
    assert state_of(hass, "sensor", "power", MAC2) == "2.2"


async def test_mqtt_prefix_with_slash_and_spaces(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A stored prefix with a trailing slash still matches the topics."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT, prefix=" kamstir/ "))
    assert entry.runtime_data.prefix == "kamstir"
    async_fire_mqtt_message(hass, "kamstir/reading/power", "6.6")
    await tick(hass, freezer, 2)
    assert state_of(hass, "sensor", "power") == "6.6"


async def test_mqtt_nothing_runs_after_unload(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After unload no message, debounce or watchdog touches the coordinator."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    coordinator = entry.runtime_data
    async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", "1.0")
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    data = dict(coordinator.data)

    async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", "2.0")
    await tick(hass, freezer, 2)
    await tick(hass, freezer, 3600)
    assert coordinator.data == data
    assert coordinator.data["power"] == 12.3
    assert "No MQTT data" not in caplog.text
    assert integration_errors(caplog) == []


async def test_mqtt_mode_without_mqtt_integration(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """With the MQTT integration gone, setup is retried instead of crashing."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_mqtt_rest_down_creates_all_sensors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """With no first payload every sensor exists, and MQTT fills the device."""
    aioclient_mock.get(url(), exc=aiohttp.ClientError())
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    sensors = [
        e
        for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if e.domain == "sensor"
    ]
    assert len(sensors) == len(SENSORS)
    device = device_registry.async_get_device(identifiers={(DOMAIN, MAC)})
    assert device.model is None
    assert state_of(hass, "update", "firmware") == STATE_UNAVAILABLE

    base = f"{PREFIX}/smart_gateways"
    async_fire_mqtt_message(hass, f"{base}/gateway_model", "kamst-ir_v1.0")
    async_fire_mqtt_message(hass, f"{base}/running_firmware_version", "2026071601")
    async_fire_mqtt_message(hass, f"{base}/available_firmware_version", "2026101501")
    await tick(hass, freezer, 2)

    device = device_registry.async_get_device(identifiers={(DOMAIN, MAC)})
    assert device.model == "kamst-ir_v1.0"
    assert device.sw_version == "2026071601"
    assert state_of(hass, "update", "firmware") == STATE_ON
    # Readings that have not been published yet are unknown, not unavailable.
    assert state_of(hass, "sensor", "power") == STATE_UNKNOWN


async def test_mqtt_watchdog_boundary(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    mqtt_mock: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Data every 9 minutes keeps the gateway connected for hours."""
    await setup_entry(hass, make_entry(mode=MODE_MQTT))
    for index in range(10):
        for _ in range(9):
            await tick(hass, freezer, 60)
        assert state_of(hass, "binary_sensor", "connected") == STATE_ON
        async_fire_mqtt_message(hass, f"{PREFIX}/reading/power", f"{index}.0")
        await tick(hass, freezer, 2)
    assert state_of(hass, "sensor", "power") == "9.0"


# --------------------------------------------------------------------------
# Buttons and the Connected sensor while the gateway is down
# --------------------------------------------------------------------------


async def test_restart_available_while_gateway_is_down(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Restart and Connected stay usable when every reading is unavailable."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    await setup_entry(hass, make_entry())
    aioclient_mock.clear_requests()
    aioclient_mock.get(url(), exc=aiohttp.ClientError())
    aioclient_mock.get(f"http://{HOST}:{PORT}/reboot", text="ok")
    for _ in range(4):
        await tick(hass, freezer)

    assert state_of(hass, "sensor", "power") == STATE_UNAVAILABLE
    assert state_of(hass, "binary_sensor", "connected") == STATE_OFF
    restart = entity_id(hass, "button", "restart")
    assert hass.states.get(restart).state != STATE_UNAVAILABLE
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: restart}, blocking=True
    )


async def test_restart_forbidden_and_mqtt_mode(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """HTTP 403 asks for a login too, and the button exists in MQTT mode."""
    mock_gateway.get(f"http://{HOST}:{PORT}/reboot", status=403)
    await setup_entry(hass, make_entry(mode=MODE_MQTT))
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: entity_id(hass, "button", "restart")},
            blocking=True,
        )
    assert err.value.translation_key == "restart_auth_required"


# --------------------------------------------------------------------------
# Config and options flow edge cases
# --------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [0, 5, 29, 3601, 100000])
async def test_options_reject_scan_interval_out_of_range(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, interval: int
) -> None:
    """The scan interval must stay within 30 s and an hour."""
    entry = await setup_entry(hass, make_entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: interval,
                CONF_ENERGY_UNIT: ENERGY_UNIT_MWH,
                CONF_MODE: MODE_REST,
            },
        )
    assert entry.options[CONF_SCAN_INTERVAL] == 60


async def test_options_rest_with_bad_prefix(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """A wildcard prefix is refused even while the mode is REST."""
    entry = await setup_entry(hass, make_entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 60,
            CONF_ENERGY_UNIT: ENERGY_UNIT_MWH,
            CONF_MODE: MODE_REST,
            CONF_MQTT_PREFIX: "home/+/kamstir",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MQTT_PREFIX: "invalid_prefix"}
    assert CONF_MQTT_PREFIX not in entry.options


async def test_options_keep_own_prefix(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """Saving the options again with the gateway's own prefix is not a clash."""
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 60,
            CONF_ENERGY_UNIT: ENERGY_UNIT_MWH,
            CONF_MODE: MODE_MQTT,
            CONF_MQTT_PREFIX: PREFIX,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MQTT_PREFIX] == PREFIX


async def test_reconfigure_prefix_of_other_gateway(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker, mqtt_mock: MagicMock
) -> None:
    """Reconfigure refuses another gateway's prefix but accepts its own."""
    make_entry(mac=MAC2, host=HOST2, mode=MODE_MQTT, prefix="taken").add_to_hass(hass)
    entry = await setup_entry(hass, make_entry(mode=MODE_MQTT))

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: HOST, CONF_PORT: PORT, CONF_MQTT_PREFIX: "taken/"},
    )
    assert result["errors"] == {CONF_MQTT_PREFIX: "prefix_in_use"}
    assert entry.options[CONF_MQTT_PREFIX] == PREFIX

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: HOST, CONF_PORT: PORT, CONF_MQTT_PREFIX: PREFIX},
    )
    assert result["reason"] == "reconfigure_successful"


@pytest.mark.parametrize("mac", ["", "-", "not-a-mac", "30_76_F5", None])
async def test_user_flow_needs_a_usable_mac(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mac: str | None
) -> None:
    """Without a real MAC there is no unique ID, so no entry is created."""
    payload = load_payload("read_heat.json")
    payload["mac_address"] = mac
    aioclient_mock.get(url(), json=payload)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: HOST,
            CONF_PORT: PORT,
            "name": "K1",
            "meter_type": "heat",
            CONF_ENERGY_UNIT: ENERGY_UNIT_MWH,
            CONF_MODE: MODE_REST,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_response"}
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize("port", [0, 65536, -1])
async def test_user_flow_rejects_bad_port(hass: HomeAssistant, port: int) -> None:
    """Ports outside 1-65535 never reach the gateway client."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: HOST,
                CONF_PORT: port,
                "name": "K1",
                "meter_type": "heat",
                CONF_ENERGY_UNIT: ENERGY_UNIT_MWH,
                CONF_MODE: MODE_REST,
            },
        )


# --------------------------------------------------------------------------
# Consistency between code, translations, fixtures and packaging
# --------------------------------------------------------------------------


def _strings() -> dict[str, Any]:
    return json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))


def test_sensor_keys_are_unique_and_parsed() -> None:
    """Each sensor has its own key, and the parser knows the field behind it."""
    keys = [d.key for d in SENSORS]
    assert len(keys) == len(set(keys))
    assert set(keys) - {"meter_clock"} <= set(FIELD_TYPES)


def test_every_parsed_field_is_used() -> None:
    """A field the parser accepts ends up in an entity or the device info."""
    used_elsewhere = {
        "mac_address",  # unique ID
        "gateway_model",  # device info
        "firmware_running",  # update entity, device info
        "firmware_available",  # update entity
        "firmware_update_available",  # implied by the two versions
        "mqtt_configured",  # binary sensor
        "device_date",  # meter clock
        "device_time",  # meter clock
    }
    assert set(FIELD_TYPES) - {d.key for d in SENSORS} == used_elsewhere


def test_fixtures_only_use_known_fields() -> None:
    """Fixture payloads and the parser agree on the field names."""
    for path in FIXTURES.glob("*.json"):
        unknown = set(load_payload(path.name)) - set(FIELD_TYPES)
        assert unknown == set(), path.name


def test_icons_belong_to_real_entities() -> None:
    """icons.json has no entry for an entity that does not exist."""
    icons = json.loads((COMPONENT / "icons.json").read_text(encoding="utf-8"))
    names = _strings()["entity"]
    for platform, entries in icons["entity"].items():
        assert set(entries) <= set(names[platform]), platform


def test_translation_keys_used_in_code_exist() -> None:
    """Every error, abort and exception key the code raises is translated."""
    strings = _strings()
    assert {
        "cannot_connect",
        "invalid_response",
        "invalid_prefix",
        "prefix_in_use",
        "unknown",
    } <= set(strings["config"]["error"])
    assert {
        "already_configured",
        "mqtt_not_configured",
        "wrong_device",
        "reconfigure_successful",
    } <= set(strings["config"]["abort"])
    assert {"invalid_prefix", "prefix_in_use", "mqtt_not_configured"} <= set(
        strings["options"]["error"]
    )
    assert {"install_needs_mqtt", "restart_failed", "restart_auth_required"} <= set(
        strings["exceptions"]
    )
    assert ISSUE_LEGACY_YAML in strings["issues"]
    for name in ("meter_type", "energy_unit", "mode"):
        assert strings["selector"][name]["options"]


def test_selector_options_are_translated() -> None:
    """Each choice in the flows has a label."""
    selectors = _strings()["selector"]
    assert set(selectors["meter_type"]["options"]) == set(METER_TYPES)
    assert set(selectors["energy_unit"]["options"]) == set(ENERGY_UNITS)
    assert set(selectors["mode"]["options"]) == set(MODES)


def test_issue_placeholders_match_code() -> None:
    """The repair text uses exactly the placeholders the code fills in."""
    description = _strings()["issues"][ISSUE_LEGACY_YAML]["description"]
    assert "{count}" in description
    assert "{entities}" in description


def test_versions_agree() -> None:
    """manifest.json and pyproject.toml carry the same version and domain."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["version"] == project["project"]["version"]
    assert manifest["domain"] == DOMAIN == COMPONENT.name
    assert manifest["config_flow"] is True
    # MQTT is optional: it must not be a hard dependency.
    assert "mqtt" not in manifest.get("dependencies", [])
    assert "mqtt" in manifest["after_dependencies"]
