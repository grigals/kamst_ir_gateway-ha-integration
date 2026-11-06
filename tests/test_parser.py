"""Tests for the payload parser and REST client."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
import logging
from unittest.mock import MagicMock

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.kamstir.api import (
    CannotConnect,
    InvalidResponse,
    KamstirClient,
    KamstirParser,
    normalize_mac,
)

from .conftest import HOST, PORT, load_payload, url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30_76_F5_00_00_01", "30:76:f5:00:00:01"),
        ("30:76:F5:00:00:01", "30:76:f5:00:00:01"),
        ("3076f5000001", "30:76:f5:00:00:01"),
        ("30_76_F5", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_mac(raw: str | None, expected: str | None) -> None:
    """MAC addresses are normalised to lower case with colons."""
    assert normalize_mac(raw) == expected


def test_parse_empty_payload() -> None:
    """Missing meter values become None; gateway values are parsed."""
    parsed = KamstirParser().parse(load_payload("read_empty.json"))
    assert parsed["heat_energy"] is None
    assert parsed["minflow_m"] is None
    assert parsed["device_date"] is None
    assert parsed["mac_address"] == "30:76:f5:00:00:01"
    assert parsed["firmware_available"] is None
    assert parsed["firmware_running"] == "2026071601"
    assert parsed["firmware_update_available"] is False
    assert parsed["mqtt_configured"] is True
    assert parsed["wifi_rssi"] == -61.0
    assert parsed["startup_time"] == datetime(2026, 11, 6, 7, 12, 45, tzinfo=UTC)


def test_parse_heat_payload() -> None:
    """Numbers are floats, meter dates and times are parsed."""
    parsed = KamstirParser().parse(load_payload("read_heat.json"))
    assert parsed["heat_energy"] == 1234.567
    assert parsed["power"] == 12.3
    assert parsed["flow"] == 390.0
    assert parsed["maxflowdate_y"] == date(2026, 2, 3)
    assert parsed["device_time"] == time(14, 35, 12)
    assert parsed["firmware_available"] == "2026101501"


def test_parse_real_format_payload() -> None:
    """A payload in the real gateway format (firmware 2026071601) parses fully."""
    parsed = KamstirParser().parse(load_payload("read_real_format.json"))
    assert parsed["heat_energy"] == 987.654
    assert parsed["power"] == 12.3
    assert parsed["flow"] == 450.0
    assert parsed["minflowdate_m"] == date(2026, 11, 2)
    assert parsed["maxpowerdate_y"] == date(2026, 2, 10)
    assert parsed["device_date"] == date(2026, 11, 5)
    assert parsed["device_time"] == time(9, 30, 0)
    assert parsed["battery_days_left"] == 0.0
    assert parsed["infoevent"] == 16.0
    assert parsed["leak_burst"] == "0"
    assert parsed["firmware_available"] is None
    # Every field in the payload is known and none failed to parse.
    raw = load_payload("read_real_format.json")
    assert set(parsed) == set(raw)
    assert all(v is not None for k, v in parsed.items() if k != "firmware_available")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0 years and 0 days", 0.0),
        ("2 years and 45 days", 775.0),
        ("1 year and 1 day", 366.0),
        ("120 days", 120.0),
        ("3 years", 1095.0),
        ("2950", 2950.0),
        ("soon", None),
    ],
)
def test_parse_battery_days(raw: str, expected: float | None) -> None:
    """The battery field comes as text like "0 years and 0 days"."""
    assert KamstirParser().parse_field("battery_days_left", raw) == expected


def test_unknown_and_missing_keys() -> None:
    """Unknown keys are dropped; missing ones are simply absent."""
    parsed = KamstirParser().parse({"power": "1.5", "brand_new_field": "x"})
    assert parsed == {"power": 1.5}
    assert KamstirParser().parse_field("brand_new_field", "x") is None
    assert KamstirParser().parse_field("power", None) is None


def test_bad_values_log_once(caplog: pytest.LogCaptureFixture) -> None:
    """An unparsable value becomes None and is logged once per field."""
    parser = KamstirParser()
    with caplog.at_level(logging.DEBUG, logger="custom_components.kamstir.api"):
        assert parser.parse_field("power", "abc") is None
        assert parser.parse_field("power", "xyz") is None
        assert parser.parse_field("mqtt_configured", "maybe") is None
        assert parser.parse_field("startup_time", "yesterday") is None
        assert parser.parse_field("mac_address", "nope") is None
    messages = [r.getMessage() for r in caplog.records]
    assert sum("power" in m for m in messages) == 1
    assert len(messages) == 4


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("261106", date(2026, 11, 6)),  # Kamstrup YYMMDD
        ("261106.0", date(2026, 11, 6)),
        ("261101.00", date(2026, 11, 1)),  # as the gateway sends it
        ("0.00", None),
        ("061126", date(2006, 11, 26)),  # still read year-first when valid
        ("150640", date(2040, 6, 15)),  # YYMMDD invalid -> DDMMYY
        ("20261106", date(2026, 11, 6)),
        ("06112026", date(2026, 11, 6)),
        ("2026-11-06", date(2026, 11, 6)),
        ("06-11-2026", date(2026, 11, 6)),
        ("06.11.2026", date(2026, 11, 6)),
        ("000000", None),
        ("0", None),
        ("123", None),
        ("999999", None),
        ("someday", None),
    ],
)
def test_parse_date(raw: str, expected: date | None) -> None:
    """Meter dates in the formats seen on Kamstrup meters."""
    assert KamstirParser().parse_field("maxflowdate_m", raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("143512", time(14, 35, 12)),
        ("81530", time(8, 15, 30)),
        ("1435", time(14, 35)),
        ("14:35:12", time(14, 35, 12)),
        ("14:35", time(14, 35)),
        ("256000", None),
        ("noon", None),
    ],
)
def test_parse_time(raw: str, expected: time | None) -> None:
    """Meter clock times."""
    assert KamstirParser().parse_field("device_time", raw) == expected


def test_mqtt_aliases() -> None:
    """MQTT names under smart_gateways/ map to the REST names."""
    parsed = KamstirParser().parse(
        {
            "running_firmware_version": "2026071601",
            "available_firmware_version": "2026101501",
            "update_available": "true",
        }
    )
    assert parsed == {
        "firmware_running": "2026071601",
        "firmware_available": "2026101501",
        "firmware_update_available": True,
    }


def test_naive_timestamp_is_utc() -> None:
    """A timestamp without zone is taken as UTC."""
    value = KamstirParser().parse_field("startup_time", "2026-11-06T07:12:45")
    assert value == datetime(2026, 11, 6, 7, 12, 45, tzinfo=UTC)


async def test_client_read(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The client returns every value as a string."""
    aioclient_mock.get(url(), json={"power": 1.5, "temp1": None, "mac_address": "x"})
    client = KamstirClient(async_get_clientsession(hass), HOST, PORT)
    assert client.base_url == f"http://{HOST}:{PORT}"
    assert await client.read() == {"power": "1.5", "temp1": "", "mac_address": "x"}


def test_ipv6_base_url() -> None:
    """IPv6 hosts are bracketed."""
    client = KamstirClient(MagicMock(), "fe80::1", 82)
    assert client.base_url == "http://[fe80::1]:82"


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"exc": aiohttp.ClientError()}, CannotConnect),
        ({"exc": TimeoutError()}, CannotConnect),
        ({"status": 500}, CannotConnect),
        ({"text": "<html>not json</html>"}, InvalidResponse),
        ({"json": ["a", "list"]}, InvalidResponse),
    ],
)
async def test_client_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict,
    error: type[Exception],
) -> None:
    """Transport and format problems map to the client's errors."""
    aioclient_mock.get(url(), **kwargs)
    client = KamstirClient(async_get_clientsession(hass), HOST, PORT)
    with pytest.raises(error):
        await client.read()
