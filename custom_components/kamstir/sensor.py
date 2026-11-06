"""Sensors for the Kamst-IR gateway."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .const import CONF_METER_TYPE, METER_HEAT, METER_WATER
from .coordinator import KamstirConfigEntry, KamstirCoordinator
from .entity import KamstirEntity

PARALLEL_UPDATES = 0

HEAT = frozenset({METER_HEAT})
WATER = frozenset({METER_WATER})
BOTH = HEAT | WATER
NONE: frozenset[str] = frozenset()


def _get(key: str) -> Callable[[Mapping[str, Any]], Any]:
    return lambda data: data.get(key)


def _get_int(key: str) -> Callable[[Mapping[str, Any]], Any]:
    def value(data: Mapping[str, Any]) -> int | None:
        raw = data.get(key)
        return None if raw is None else int(raw)

    return value


def _has(key: str) -> Callable[[Mapping[str, Any]], bool]:
    return lambda data: key in data


def _meter_clock(data: Mapping[str, Any]) -> datetime | None:
    day: date | None = data.get("device_date")
    clock: time | None = data.get("device_time")
    if day is None or clock is None:
        return None
    return datetime.combine(day, clock, tzinfo=dt_util.get_default_time_zone())


@dataclass(frozen=True, kw_only=True)
class KamstirSensorEntityDescription(SensorEntityDescription):
    """Describe one gateway sensor."""

    value_fn: Callable[[Mapping[str, Any]], StateType | datetime]
    exists_fn: Callable[[Mapping[str, Any]], bool]
    # Meter types for which the entity is enabled when first created.
    enabled_for: frozenset[str] = field(default=NONE)


def _sensor(
    key: str,
    *,
    enabled_for: frozenset[str] = NONE,
    value_fn: Callable[[Mapping[str, Any]], StateType | datetime] | None = None,
    **kwargs: Any,
) -> KamstirSensorEntityDescription:
    return KamstirSensorEntityDescription(
        key=key,
        translation_key=key,
        value_fn=value_fn or _get(key),
        exists_fn=_has(key),
        enabled_for=enabled_for,
        **kwargs,
    )


_TEMPERATURE: dict[str, Any] = {
    "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
    "device_class": SensorDeviceClass.TEMPERATURE,
    "state_class": SensorStateClass.MEASUREMENT,
}
_FLOW: dict[str, Any] = {
    "native_unit_of_measurement": UnitOfVolumeFlowRate.LITERS_PER_HOUR,
    "device_class": SensorDeviceClass.VOLUME_FLOW_RATE,
    "state_class": SensorStateClass.MEASUREMENT,
}
_POWER: dict[str, Any] = {
    "native_unit_of_measurement": UnitOfPower.KILO_WATT,
    "device_class": SensorDeviceClass.POWER,
    "state_class": SensorStateClass.MEASUREMENT,
}
_DIAGNOSTIC: dict[str, Any] = {"entity_category": EntityCategory.DIAGNOSTIC}

_STAT_KINDS: dict[str, dict[str, Any]] = {
    "minflow": _FLOW,
    "maxflow": _FLOW,
    "minpower": _POWER,
    "maxpower": _POWER,
    "avgtemp1": _TEMPERATURE,
    "avgtemp2": _TEMPERATURE,
}
_STAT_DATES = ("minflowdate", "maxflowdate", "minpowerdate", "maxpowerdate")


SENSORS: tuple[KamstirSensorEntityDescription, ...] = (
    # Meter, live
    _sensor(
        "heat_energy",
        enabled_for=HEAT,
        native_unit_of_measurement=UnitOfEnergy.GIGA_JOULE,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    _sensor(
        "cooling_energy",
        native_unit_of_measurement=UnitOfEnergy.GIGA_JOULE,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    _sensor("power", enabled_for=HEAT, **_POWER),
    _sensor("temp1", enabled_for=HEAT, **_TEMPERATURE),
    _sensor("temp2", enabled_for=HEAT, **_TEMPERATURE),
    _sensor(
        "tempdiff",
        enabled_for=HEAT,
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _sensor("flow", enabled_for=BOTH, **_FLOW),
    _sensor(
        "volume",
        enabled_for=BOTH,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    _sensor(
        "hourcounter",
        enabled_for=BOTH,
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        **_DIAGNOSTIC,
    ),
    _sensor(
        "infoevent", enabled_for=BOTH, value_fn=_get_int("infoevent"), **_DIAGNOSTIC
    ),
    _sensor("water_temp", enabled_for=WATER, **_TEMPERATURE),
    _sensor("meter_temp", **_TEMPERATURE, **_DIAGNOSTIC),
    _sensor(
        "battery_days_left",
        enabled_for=WATER,
        native_unit_of_measurement=UnitOfTime.DAYS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        **_DIAGNOSTIC,
    ),
    _sensor("leak_burst", enabled_for=WATER, **_DIAGNOSTIC),
    # Volume-weighted temperature sums: temp1xm3 / volume = average supply temp.
    *(
        _sensor(
            key,
            native_unit_of_measurement="°C·m³",
            state_class=SensorStateClass.TOTAL_INCREASING,
            **_DIAGNOSTIC,
        )
        for key in ("temp1xm3", "temp2xm3")
    ),
    # Month and year statistics
    *(
        _sensor(f"{name}{suffix}", **kind)
        for suffix in ("_m", "_y")
        for name, kind in _STAT_KINDS.items()
    ),
    *(
        _sensor(f"{name}{suffix}", device_class=SensorDeviceClass.DATE, **_DIAGNOSTIC)
        for suffix in ("_m", "_y")
        for name in _STAT_DATES
    ),
    # The meter's own clock: device_date + device_time, in HA's time zone.
    KamstirSensorEntityDescription(
        key="meter_clock",
        translation_key="meter_clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_meter_clock,
        exists_fn=_has("device_date"),
        **_DIAGNOSTIC,
    ),
    # Gateway
    _sensor(
        "wifi_rssi",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        **_DIAGNOSTIC,
    ),
    _sensor("startup_time", device_class=SensorDeviceClass.TIMESTAMP, **_DIAGNOSTIC),
    _sensor("mqtt_server", **_DIAGNOSTIC),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KamstirConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the sensors for one gateway."""
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    meter_type = entry.data.get(CONF_METER_TYPE, METER_HEAT)
    # Create every known field the payload carries. With no payload yet (MQTT
    # mode, gateway not answering REST) create them all.
    async_add_entities(
        KamstirSensor(coordinator, description, meter_type)
        for description in SENSORS
        if not data or description.exists_fn(data)
    )


class KamstirSensor(KamstirEntity, SensorEntity):
    """A value read from the gateway or the meter."""

    entity_description: KamstirSensorEntityDescription

    def __init__(
        self,
        coordinator: KamstirCoordinator,
        description: KamstirSensorEntityDescription,
        meter_type: str,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_entity_registry_enabled_default = (
            meter_type in description.enabled_for
        )

    @property
    def native_value(self) -> StateType | datetime:
        """Return the parsed value; None shows as unknown."""
        return self.entity_description.value_fn(self.coordinator.data or {})
