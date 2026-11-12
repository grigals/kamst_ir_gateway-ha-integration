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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_UNIT,
    CONF_METER_TYPE,
    CONF_PRICE_ENTITY,
    COST_RESET_CONFIRMATIONS,
    DEFAULT_ENERGY_UNIT,
    DOMAIN,
    ENERGY_UNIT_GJ,
    ENERGY_UNIT_KWH,
    ENERGY_UNIT_MWH,
    METER_HEAT,
    METER_WATER,
)
from .coordinator import KamstirConfigEntry, KamstirCoordinator, merged_config
from .cost import (
    CostSpec,
    cost_spec,
    cost_tracking_enabled,
    entity_price,
    finite,
)
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

# Energy sensors take their unit from the entry; precision matches the meter
# display (MULTICAL shows MWh and GJ with 3 decimals, kWh as whole numbers).
_ENERGY_KEYS = frozenset({"heat_energy", "cooling_energy"})
_ENERGY_UNITS: dict[str, tuple[str, int]] = {
    ENERGY_UNIT_MWH: (UnitOfEnergy.MEGA_WATT_HOUR, 3),
    ENERGY_UNIT_GJ: (UnitOfEnergy.GIGA_JOULE, 3),
    ENERGY_UNIT_KWH: (UnitOfEnergy.KILO_WATT_HOUR, 0),
}

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
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    _sensor(
        "cooling_energy",
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
    energy_unit = merged_config(entry).get(CONF_ENERGY_UNIT, DEFAULT_ENERGY_UNIT)
    _async_reset_energy_display_unit(hass, coordinator.mac, energy_unit)
    # Create every known field the payload carries. With no payload yet (MQTT
    # mode, gateway not answering REST) create them all.
    entities: list[SensorEntity] = [
        KamstirSensor(coordinator, description, meter_type, energy_unit)
        for description in SENSORS
        if not data or description.exists_fn(data)
    ]
    conf = merged_config(entry)
    if cost_tracking_enabled(conf):
        entities.append(
            KamstirCostSensor(
                coordinator,
                cost_spec(conf),
                hass.config.currency,
                conf.get(CONF_PRICE_ENTITY),
            )
        )
    async_add_entities(entities)


def _energy_unit(energy_unit: str) -> tuple[str, int]:
    return _ENERGY_UNITS.get(energy_unit, _ENERGY_UNITS[DEFAULT_ENERGY_UNIT])


@callback
def _async_reset_energy_display_unit(
    hass: HomeAssistant, mac: str, energy_unit: str
) -> None:
    """Show energy in the configured unit, not the unit the entity had before.

    When a sensor's native unit changes, HA stores the old unit as the
    suggested display unit and converts to it. Here the unit is a setting
    that says what the meter really sends, so the old unit is wrong: a value
    sent in MWh would be shown converted to GJ. A display unit the user chose
    in the entity settings is kept.
    """
    registry = er.async_get(hass)
    unit, _ = _energy_unit(energy_unit)
    private_key = "sensor.private"
    for key in _ENERGY_KEYS:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{mac}_{key}")
        if entity_id is None or (entry := registry.async_get(entity_id)) is None:
            continue
        private = dict(entry.options.get(private_key) or {})
        if private.get("suggested_unit_of_measurement") == unit:
            continue
        private["suggested_unit_of_measurement"] = unit
        registry.async_update_entity_options(entity_id, private_key, private)


class KamstirSensor(KamstirEntity, SensorEntity):
    """A value read from the gateway or the meter."""

    entity_description: KamstirSensorEntityDescription

    def __init__(
        self,
        coordinator: KamstirCoordinator,
        description: KamstirSensorEntityDescription,
        meter_type: str,
        energy_unit: str = DEFAULT_ENERGY_UNIT,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_entity_registry_enabled_default = (
            meter_type in description.enabled_for
        )
        if description.key in _ENERGY_KEYS:
            unit, precision = _energy_unit(energy_unit)
            self._attr_native_unit_of_measurement = unit
            self._attr_suggested_display_precision = precision

    @property
    def native_value(self) -> StateType | datetime:
        """Return the parsed value; None shows as unknown."""
        return self.entity_description.value_fn(self.coordinator.data or {})


@dataclass
class CostData(ExtraStoredData):
    """What the cost sensor keeps across restarts."""

    total: float = 0.0
    # When the total started from zero. Reported as last_reset, so that a
    # total that had to start over is a new cycle in the statistics instead
    # of a drop.
    since: datetime | None = None
    # The meter reading up to which consumption has been priced.
    last_reading: float | None = None
    last_time: datetime | None = None
    # Last usable value of the user's price entity, per unit of the reading.
    last_price: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the stored form."""
        return {
            "total": self.total,
            "since": self.since.isoformat() if self.since else None,
            "last_reading": self.last_reading,
            "last_time": self.last_time.isoformat() if self.last_time else None,
            "last_price": self.last_price,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CostData:
        """Read the stored form, ignoring anything unusable."""
        last_time = data.get("last_time")
        since = data.get("since")
        total = finite(data.get("total"))
        return cls(
            total=total or 0.0,
            # Without a usable total the count starts over, from now.
            since=(
                dt_util.parse_datetime(since)
                if isinstance(since, str) and total is not None
                else None
            ),
            last_reading=finite(data.get("last_reading")),
            last_time=(
                dt_util.parse_datetime(last_time)
                if isinstance(last_time, str)
                else None
            ),
            last_price=finite(data.get("last_price")),
        )


class KamstirCostSensor(KamstirEntity, RestoreEntity, SensorEntity):
    """Running cost: growth of the meter reading times the price in force.

    The state counts from the moment cost tracking was turned on. Each new
    reading adds (reading - last reading) x price. A missing reading adds
    nothing and keeps the last reading, so the consumption is priced once the
    meter answers again.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: KamstirCoordinator,
        spec: CostSpec,
        currency: str,
        price_entity: str | None,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, spec.cost_key)
        self._attr_translation_key = spec.cost_key
        self._attr_native_unit_of_measurement = currency
        self._spec = spec
        self._price_entity = price_entity
        self._cost = CostData()
        self._low_readings = 0

    async def async_added_to_hass(self) -> None:
        """Restore the total, then price what was used while HA was down."""
        await super().async_added_to_hass()
        if (stored := await self.async_get_last_extra_data()) is not None:
            self._cost = CostData.from_dict(stored.as_dict())
        if self._cost.since is None:
            self._cost.since = dt_util.utcnow()
        self._accrue()

    @property
    def available(self) -> bool:
        """The total stays known while the gateway is unreachable."""
        return True

    @property
    def native_value(self) -> float:
        """Return the cost so far."""
        return round(self._cost.total, 5)

    @property
    def last_reset(self) -> datetime | None:
        """Return when the total started from zero."""
        return self._cost.since

    @property
    def extra_restore_state_data(self) -> CostData:
        """Return what to keep across restarts."""
        return self._cost

    @callback
    def _handle_coordinator_update(self) -> None:
        self._accrue()
        super()._handle_coordinator_update()

    def _price(self, since: datetime | None, now: datetime) -> float | None:
        """Return the price for consumption between two readings."""
        if self._price_entity is None:
            prices = self.coordinator.prices
            return prices.average_price(since, now) if prices else None
        # A price entity that is briefly unavailable keeps its last value.
        price = entity_price(self.hass, self._price_entity, self._spec.unit)
        if price is not None:
            self._cost.last_price = price
        return self._cost.last_price

    @callback
    def _accrue(self) -> None:
        """Price the consumption since the last reading."""
        reading = finite((self.coordinator.data or {}).get(self._spec.source_key))
        if reading is None:
            return
        cost = self._cost
        now = dt_util.utcnow()
        # Read the price on every reading, so the last value of a price
        # entity is remembered even while nothing is being used.
        price = self._price(cost.last_time, now)
        if cost.last_reading is not None and reading < cost.last_reading:
            # A single low value is more likely a bad read than a new meter.
            self._low_readings += 1
            if self._low_readings < COST_RESET_CONFIRMATIONS:
                return
        elif cost.last_reading is not None and price is not None:
            cost.total += (reading - cost.last_reading) * price
        self._low_readings = 0
        cost.last_reading = reading
        cost.last_time = now
