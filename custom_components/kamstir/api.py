"""REST client and payload parser for the Kamst-IR gateway.

The gateway reports every value as a string, over both REST and MQTT. The
parser in this module turns those strings into Python values and is shared by
both transports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from enum import StrEnum
import json
import logging
import re
from typing import Any

import aiohttp

from .const import API_PATH, REBOOT_PATH, REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)

MISSING_VALUES = frozenset({"", "-"})


class FieldType(StrEnum):
    """How a raw gateway string is parsed."""

    NUMBER = "number"
    BOOL = "bool"
    STRING = "string"
    TIMESTAMP = "timestamp"
    DATE = "date"
    TIME = "time"
    DAYS = "days"
    MAC = "mac"
    FIRMWARE = "firmware"


# The MQTT topics under smart_gateways/ use other names than the REST payload
# (see the vendor's MQTT YAML). Map them to the REST names.
FIELD_ALIASES: dict[str, str] = {
    "running_firmware_version": "firmware_running",
    "available_firmware_version": "firmware_available",
    "update_available": "firmware_update_available",
}


def canonical_field(name: str) -> str:
    """Return the REST field name for a REST or MQTT field name."""
    return FIELD_ALIASES.get(name, name)


_STAT_NUMBERS = ("minflow", "maxflow", "minpower", "maxpower", "avgtemp1", "avgtemp2")
_STAT_DATES = ("minflowdate", "maxflowdate", "minpowerdate", "maxpowerdate")

FIELD_TYPES: dict[str, FieldType] = {
    # Gateway
    "mac_address": FieldType.MAC,
    "gateway_model": FieldType.STRING,
    "startup_time": FieldType.TIMESTAMP,
    "firmware_running": FieldType.STRING,
    "firmware_available": FieldType.FIRMWARE,
    "firmware_update_available": FieldType.BOOL,
    "wifi_rssi": FieldType.NUMBER,
    "mqtt_configured": FieldType.BOOL,
    "mqtt_server": FieldType.STRING,
    # Meter, live
    "heat_energy": FieldType.NUMBER,
    "cooling_energy": FieldType.NUMBER,
    "power": FieldType.NUMBER,
    "temp1": FieldType.NUMBER,
    "temp2": FieldType.NUMBER,
    "tempdiff": FieldType.NUMBER,
    "flow": FieldType.NUMBER,
    "volume": FieldType.NUMBER,
    "water_temp": FieldType.NUMBER,
    "meter_temp": FieldType.NUMBER,
    "infoevent": FieldType.NUMBER,
    "hourcounter": FieldType.NUMBER,
    # Sent as text, e.g. "0 years and 0 days".
    "battery_days_left": FieldType.DAYS,
    "device_time": FieldType.TIME,
    "device_date": FieldType.DATE,
    "leak_burst": FieldType.STRING,
    "temp1xm3": FieldType.NUMBER,
    "temp2xm3": FieldType.NUMBER,
}
for _suffix in ("_m", "_y"):
    FIELD_TYPES.update({f"{name}{_suffix}": FieldType.NUMBER for name in _STAT_NUMBERS})
    FIELD_TYPES.update({f"{name}{_suffix}": FieldType.DATE for name in _STAT_DATES})


class KamstirError(Exception):
    """Base error for the gateway client."""


class CannotConnect(KamstirError):
    """The gateway could not be reached."""


class InvalidResponse(KamstirError):
    """The gateway answered with something that is not a readings object."""


class AuthRequired(KamstirError):
    """The gateway wants a login for this request."""


def normalize_mac(value: str | None) -> str | None:
    """Turn ``30_76_F5_00_00_01`` (or any separator) into ``30:76:f5:00:00:01``."""
    if not value:
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    if len(digits) != 12:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def _parse_timestamp(value: str) -> datetime | None:
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _whole_number(value: str) -> str:
    """Drop a zero fraction: the gateway sends dates like ``261101.00``."""
    match = re.fullmatch(r"(\d+)\.0+", value)
    return match.group(1) if match else value


def _parse_days(value: str) -> float | None:
    """Parse ``"2 years and 45 days"`` (or a plain number) into days."""
    try:
        return float(value)
    except ValueError:
        pass
    match = re.fullmatch(
        r"(?:(\d+)\s*years?)?\s*(?:and\s*)?(?:(\d+)\s*days?)?", value.lower()
    )
    if not match or not any(match.groups()):
        return None
    years, days = (int(group or 0) for group in match.groups())
    return float(years * 365 + days)


def _build_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


_SEPARATED_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d")


def _parse_date(value: str) -> date | None:
    """Parse a meter date.

    Kamstrup meters report dates as YYMMDD (``261106`` is 6 Nov 2026). Also
    accept YYYYMMDD and common separated forms, and fall back to day-first
    when the year-first reading is not a valid date. All zeros means unset.
    """
    text = _whole_number(value)
    if text.isdigit():
        if len(text) == 6:
            yy, mm, dd = int(text[:2]), int(text[2:4]), int(text[4:])
            return _build_date(2000 + yy, mm, dd) or _build_date(2000 + dd, mm, yy)
        if len(text) == 8:
            return _build_date(
                int(text[:4]), int(text[4:6]), int(text[6:])
            ) or _build_date(int(text[4:]), int(text[2:4]), int(text[:2]))
        return None
    for fmt in _SEPARATED_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(value: str) -> time | None:
    """Parse a meter time: ``hhmmss``, ``hhmm`` or ``hh:mm[:ss]``."""
    text = _whole_number(value)
    if text.isdigit() and len(text) in (5, 6):
        text = text.zfill(6)
        text = f"{text[:2]}:{text[2:4]}:{text[4:]}"
    elif text.isdigit() and len(text) in (3, 4):
        text = text.zfill(4)
        text = f"{text[:2]}:{text[2:]}"
    try:
        return time.fromisoformat(text)
    except ValueError:
        return None


class KamstirParser:
    """Parse gateway strings, logging each unparsable field only once."""

    def __init__(self) -> None:
        """Initialise the parser."""
        self._logged: set[str] = set()

    def parse_field(self, field: str, raw: Any) -> Any:
        """Parse one field. Unknown fields and missing values return ``None``."""
        field_type = FIELD_TYPES.get(field)
        if field_type is None or raw is None:
            return None
        value = str(raw).strip()
        if value in MISSING_VALUES:
            return None

        result: Any
        match field_type:
            case FieldType.NUMBER:
                try:
                    result = float(value)
                except ValueError:
                    result = None
            case FieldType.BOOL:
                result = {"true": True, "false": False}.get(value.lower())
            case FieldType.FIRMWARE:
                # "0" means the gateway knows of no newer firmware.
                return None if value == "0" else value
            case FieldType.MAC:
                result = normalize_mac(value)
            case FieldType.TIMESTAMP:
                result = _parse_timestamp(value)
            case FieldType.DATE:
                if _whole_number(value).strip("0") == "":
                    return None  # all zeros: date not set, not an error
                result = _parse_date(value)
            case FieldType.DAYS:
                result = _parse_days(value)
            case FieldType.TIME:
                result = _parse_time(value)
            case _:
                return value

        if result is None and field not in self._logged:
            self._logged.add(field)
            _LOGGER.debug("Could not parse %s value %r", field, value)
        return result

    def parse(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Parse a full payload. Unknown keys are dropped, missing keys are absent.

        MQTT field names are mapped to their REST names first.
        """
        parsed: dict[str, Any] = {}
        for name, value in raw.items():
            field = canonical_field(name)
            if field in FIELD_TYPES:
                parsed[field] = self.parse_field(field, value)
        return parsed


class KamstirClient:
    """Read the gateway's REST API."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int) -> None:
        """Initialise the client."""
        self._session = session
        self.host = host
        self.port = port

    @property
    def base_url(self) -> str:
        """Return the gateway's web UI address."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    async def read(self) -> dict[str, str]:
        """Fetch the raw readings. Values are returned as strings."""
        url = f"{self.base_url}{API_PATH}"
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                response = await self._session.get(url)
                response.raise_for_status()
                text = await response.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(f"Cannot reach {url}: {err}") from err

        try:
            payload = json.loads(text)
        except ValueError as err:
            raise InvalidResponse(f"{url} did not return JSON") from err
        if not isinstance(payload, dict):
            raise InvalidResponse(f"{url} did not return a JSON object")
        return {str(k): "" if v is None else str(v) for k, v in payload.items()}

    async def reboot(self) -> None:
        """Restart the gateway with ``GET /reboot``.

        The gateway may drop the connection or stop answering while it goes
        down; that counts as success. Not reaching it at all does not.
        """
        url = f"{self.base_url}{REBOOT_PATH}"
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                response = await self._session.get(url)
                if response.status in (401, 403):
                    raise AuthRequired(f"{url} asks for a login")
                response.raise_for_status()
        except (aiohttp.ServerDisconnectedError, TimeoutError):
            return
        except aiohttp.ClientError as err:
            raise CannotConnect(f"Cannot reach {url}: {err}") from err
