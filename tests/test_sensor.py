"""Tests for sensors, REST polling, setup, diagnostics and repairs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import ATTR_STATE_CLASS, SensorStateClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.kamstir.const import DOMAIN, ISSUE_LEGACY_YAML, METER_WATER
from custom_components.kamstir.diagnostics import async_get_config_entry_diagnostics

from .conftest import HOST, HOST2, MAC, MAC2, load_payload, make_entry, setup_entry, url

METER_SENSORS = (
    "heat_energy",
    "cooling_energy",
    "power",
    "supply_temperature",
    "return_temperature",
    "temperature_difference",
    "flow",
    "volume",
    "operating_hours",
    "info_code",
    "water_temperature",
    "battery_days_left",
    "max_flow_month",
    "max_flow_date_year",
    "meter_clock",
)


async def test_heat_values(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """Values, units and classes match the spec table."""
    await setup_entry(hass, make_entry())

    state = hass.states.get("sensor.kamstrup_k1_heat_energy")
    assert state.state == "1234.567"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "MWh"
    assert state.attributes[ATTR_DEVICE_CLASS] == "energy"
    assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.TOTAL_INCREASING

    power = hass.states.get("sensor.kamstrup_k1_power")
    assert power.state == "12.3"
    assert power.attributes[ATTR_UNIT_OF_MEASUREMENT] == "kW"
    assert (
        hass.states.get("sensor.kamstrup_k1_supply_temperature").attributes[
            ATTR_UNIT_OF_MEASUREMENT
        ]
        == "°C"
    )
    assert (
        hass.states.get("sensor.kamstrup_k1_flow").attributes[ATTR_UNIT_OF_MEASUREMENT]
        == "L/h"
    )
    assert hass.states.get("sensor.kamstrup_k1_info_code").state == "0"
    assert hass.states.get("binary_sensor.kamstrup_k1_connected").state == STATE_ON


async def test_enabled_by_meter_type(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
) -> None:
    """The meter type picks which entities are enabled by default."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    aioclient_mock.get(url(HOST2), json=load_payload("read_water.json"))
    await setup_entry(hass, make_entry())
    await setup_entry(
        hass,
        make_entry(title="Kamstrup K2", host=HOST2, mac=MAC2, meter_type=METER_WATER),
    )

    def enabled(entity_id: str) -> bool:
        entry = entity_registry.async_get(entity_id)
        assert entry is not None, entity_id
        return not entry.disabled

    assert enabled("sensor.kamstrup_k1_heat_energy")
    assert enabled("sensor.kamstrup_k1_power")
    assert not enabled("sensor.kamstrup_k1_water_temperature")
    assert not enabled("sensor.kamstrup_k1_max_flow_month")
    assert not enabled("sensor.kamstrup_k1_wifi_signal")

    assert not enabled("sensor.kamstrup_k2_heat_energy")
    assert enabled("sensor.kamstrup_k2_volume")
    assert enabled("sensor.kamstrup_k2_water_temperature")
    assert enabled("sensor.kamstrup_k2_battery_days_left")
    assert enabled("sensor.kamstrup_k2_leak_burst")
    assert hass.states.get("sensor.kamstrup_k2_volume").state == "312.456"


async def test_two_gateways_are_separate_devices(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Two gateways give two devices with their own entities."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    aioclient_mock.get(url(HOST2), json=load_payload("read_water.json"))
    k1 = await setup_entry(hass, make_entry())
    k2 = await setup_entry(
        hass,
        make_entry(title="Kamstrup K2", host=HOST2, mac=MAC2, meter_type=METER_WATER),
    )

    d1 = device_registry.async_get_device(identifiers={(DOMAIN, MAC)})
    d2 = device_registry.async_get_device(identifiers={(DOMAIN, MAC2)})
    assert d1 and d2 and d1.id != d2.id
    assert d1.name == "Kamstrup K1"
    assert d1.model == "connectix_kamst-ir_gateway_v1.0"
    assert d1.sw_version == "2026071601"
    assert d1.configuration_url == f"http://{HOST}:82"
    assert (dr.CONNECTION_NETWORK_MAC, MAC) in d1.connections

    e1 = er.async_entries_for_config_entry(entity_registry, k1.entry_id)
    e2 = er.async_entries_for_config_entry(entity_registry, k2.entry_id)
    assert {e.unique_id for e in e1}.isdisjoint({e.unique_id for e in e2})
    assert entity_registry.async_get("sensor.kamstrup_k1_heat_energy").unique_id == (
        f"{MAC}_heat_energy"
    )


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_empty_payload_is_unknown(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Before the first meter read every meter sensor is unknown, not unavailable."""
    aioclient_mock.get(url(), json=load_payload("read_empty.json"))
    entry = await setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.LOADED

    for name in METER_SENSORS:
        state = hass.states.get(f"sensor.kamstrup_k1_{name}")
        assert state is not None, name
        assert state.state == STATE_UNKNOWN, name
    assert hass.states.get("sensor.kamstrup_k1_wifi_signal").state == "-61.0"
    assert hass.states.get("sensor.kamstrup_k1_last_restart").state == (
        "2026-11-06T07:12:45+00:00"
    )
    assert (
        hass.states.get("binary_sensor.kamstrup_k1_mqtt_configured").state == STATE_ON
    )


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_real_format_payload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A payload in the real format gives a state for every entity."""
    aioclient_mock.get(url(), json=load_payload("read_real_format.json"))
    await setup_entry(hass, make_entry())

    def state(name: str) -> str:
        return hass.states.get(f"sensor.kamstrup_k1_{name}").state

    assert state("heat_energy") == "987.654"
    assert state("power") == "12.3"
    assert state("flow") == "450.0"
    assert state("min_flow_date_month") == "2026-11-02"
    assert state("max_power_date_year") == "2026-02-10"
    assert state("battery_days_left") == "0.0"
    assert state("info_code") == "16"
    sums_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{MAC}_temp1xm3"
    )
    sums = hass.states.get(sums_id)
    assert sums.attributes[ATTR_UNIT_OF_MEASUREMENT] == "°C·m³"
    unknown = [
        s.entity_id
        for s in hass.states.async_all("sensor")
        if s.state in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    ]
    assert unknown == []


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_dates_and_meter_clock(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """Date fields are date sensors; the meter clock is a local timestamp."""
    await setup_entry(hass, make_entry())

    max_flow_date = hass.states.get("sensor.kamstrup_k1_max_flow_date_year")
    assert max_flow_date.state == "2026-02-03"
    assert max_flow_date.attributes[ATTR_DEVICE_CLASS] == "date"

    expected = datetime(
        2026, 11, 6, 14, 35, 12, tzinfo=dt_util.get_default_time_zone()
    ).astimezone(UTC)
    assert hass.states.get("sensor.kamstrup_k1_meter_clock").state == (
        expected.isoformat()
    )


async def test_only_known_fields_in_payload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Fields missing from the payload get no entity."""
    payload = load_payload("read_heat.json")
    del payload["cooling_energy"]
    aioclient_mock.get(url(), json=payload)
    await setup_entry(hass, make_entry())
    er_ = er.async_get(hass)
    assert er_.async_get_entity_id("sensor", DOMAIN, f"{MAC}_cooling_energy") is None
    assert er_.async_get_entity_id("sensor", DOMAIN, f"{MAC}_heat_energy")


async def test_gateway_down_at_startup(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An unreachable gateway at startup makes HA retry."""
    aioclient_mock.get(url(), exc=aiohttp.ClientError())
    entry = await setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unavailable_and_recovery(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entities go unavailable, Connected turns off, and both recover."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    await setup_entry(hass, make_entry())
    heat = "sensor.kamstrup_k1_heat_energy"
    connected = "binary_sensor.kamstrup_k1_connected"

    async def tick() -> None:
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    aioclient_mock.clear_requests()
    aioclient_mock.get(url(), exc=aiohttp.ClientError())
    await tick()
    assert hass.states.get(heat).state == STATE_UNAVAILABLE
    assert hass.states.get(connected).state == STATE_ON  # within 3 intervals

    await tick()
    await tick()
    assert hass.states.get(connected).state == STATE_OFF
    assert sum("Error fetching" in r.getMessage() for r in caplog.records) == 1

    aioclient_mock.clear_requests()
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    await tick()
    assert hass.states.get(heat).state == "1234.567"
    assert hass.states.get(connected).state == STATE_ON
    assert sum("recovered" in r.getMessage() for r in caplog.records) == 1


async def test_firmware_change_updates_device(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A new running firmware shows on the device page."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    await setup_entry(hass, make_entry())

    payload = load_payload("read_heat.json")
    payload["firmware_running"] = "2026101501"
    aioclient_mock.clear_requests()
    aioclient_mock.get(url(), json=payload)
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    device = device_registry.async_get_device(identifiers={(DOMAIN, MAC)})
    assert device.sw_version == "2026101501"


async def test_unload(hass: HomeAssistant, mock_gateway: AiohttpClientMocker) -> None:
    """The entry unloads cleanly."""
    entry = await setup_entry(hass, make_entry())
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """Diagnostics include the raw payload with private fields redacted."""
    entry = await setup_entry(hass, make_entry())
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["raw"]["mac_address"] == "**REDACTED**"
    assert diag["raw"]["mqtt_server"] == "**REDACTED**"
    assert diag["raw"]["heat_energy"] == "1234.567"
    assert diag["entry"]["options"]["mode"] == "rest"
    assert diag["coordinator"]["connected"] is True
    assert diag["coordinator"]["update_interval"] == 60


async def test_legacy_yaml_repair(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Old MQTT YAML entities raise a repair issue; nothing is deleted."""
    old = entity_registry.async_get_or_create(
        "sensor", "mqtt", "kamstir_k1_heat_energy", suggested_object_id="k1_heat"
    )
    await setup_entry(hass, make_entry())

    issue = issue_registry.async_get_issue(DOMAIN, ISSUE_LEGACY_YAML)
    assert issue is not None
    assert issue.translation_placeholders["count"] == "1"
    assert entity_registry.async_get(old.entity_id) is not None

    entity_registry.async_remove(old.entity_id)
    entry2 = make_entry(title="Kamstrup K2", host=HOST2, mac=MAC2)
    mock_gateway.get(url(HOST2), json=load_payload("read_water.json"))
    await setup_entry(hass, entry2)
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_LEGACY_YAML) is None


@pytest.mark.parametrize(
    ("energy_unit", "unit", "precision"),
    [("mwh", "MWh", 3), ("gj", "GJ", 3), ("kwh", "kWh", 0)],
)
async def test_energy_unit_option(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
    energy_unit: str,
    unit: str,
    precision: int,
) -> None:
    """Energy sensors use the configured unit and the meter's precision."""
    await setup_entry(hass, make_entry(energy_unit=energy_unit))

    entity_id = "sensor.kamstrup_k1_heat_energy"
    entry = entity_registry.async_get(entity_id)
    assert entry.options["sensor"]["suggested_display_precision"] == precision
    state = hass.states.get(entity_id)
    assert state.state == "1234.567"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == unit
