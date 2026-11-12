"""Cost tracking: what is priced, in which unit, and at which price.

The cost sensor multiplies the growth of one meter register (heat energy or
water volume) by a price per unit of that register. The price is either kept
by the integration (``PriceSchedule``, shown as number and date entities) or
read from an entity the user picked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    UnitOfEnergy,
    UnitOfVolume,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .const import (
    CONF_COST_TRACKING,
    CONF_ENERGY_UNIT,
    CONF_METER_TYPE,
    CONF_PRICE_ENTITY,
    DEFAULT_ENERGY_UNIT,
    DOMAIN,
    ENERGY_UNIT_GJ,
    ENERGY_UNIT_KWH,
    ENERGY_UNIT_MWH,
    METER_HEAT,
    METER_WATER,
)

STORAGE_VERSION = 1

_ENERGY_UNIT_SYMBOLS: dict[str, str] = {
    ENERGY_UNIT_MWH: UnitOfEnergy.MEGA_WATT_HOUR,
    ENERGY_UNIT_GJ: UnitOfEnergy.GIGA_JOULE,
    ENERGY_UNIT_KWH: UnitOfEnergy.KILO_WATT_HOUR,
}
_PRICE_UNIT_CONVERTERS = (EnergyConverter, VolumeConverter)
_UNIT_SPELLINGS = {"m3": UnitOfVolume.CUBIC_METERS}


@dataclass(frozen=True)
class CostSpec:
    """What one gateway's cost sensor prices."""

    kind: str
    # Field in the coordinator data, and the key of the sensor that shows it.
    source_key: str
    # The unit the source is read in; the price is per one of these.
    unit: str
    # Unit class to ask the recorder to convert the source statistics with.
    unit_class: str

    @property
    def cost_key(self) -> str:
        """Return the key of the cost sensor."""
        return f"{self.kind}_cost"

    @property
    def price_key(self) -> str:
        """Return the key of the price number."""
        return f"{self.kind}_price"

    @property
    def next_price_key(self) -> str:
        """Return the key of the next-price number."""
        return f"next_{self.kind}_price"

    @property
    def next_date_key(self) -> str:
        """Return the key of the next-price date."""
        return f"next_{self.kind}_price_date"


def cost_spec(conf: dict[str, Any] | Any) -> CostSpec:
    """Return what is priced for an entry's merged config."""
    if conf.get(CONF_METER_TYPE, METER_HEAT) == METER_WATER:
        return CostSpec(
            kind="water",
            source_key="volume",
            unit=UnitOfVolume.CUBIC_METERS,
            unit_class=VolumeConverter.UNIT_CLASS,
        )
    energy_unit = conf.get(CONF_ENERGY_UNIT, DEFAULT_ENERGY_UNIT)
    return CostSpec(
        kind="heat",
        source_key="heat_energy",
        unit=_ENERGY_UNIT_SYMBOLS.get(
            energy_unit, _ENERGY_UNIT_SYMBOLS[DEFAULT_ENERGY_UNIT]
        ),
        unit_class=EnergyConverter.UNIT_CLASS,
    )


def cost_tracking_enabled(conf: dict[str, Any] | Any) -> bool:
    """Return True if the entry has cost tracking turned on."""
    return bool(conf.get(CONF_COST_TRACKING))


def uses_own_prices(conf: dict[str, Any] | Any) -> bool:
    """Return True if the integration keeps the price itself."""
    return cost_tracking_enabled(conf) and not conf.get(CONF_PRICE_ENTITY)


def finite(value: Any) -> float | None:
    """Return value as a finite float, else None."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def price_per_unit(price: float, price_unit: str | None, unit: str) -> float:
    """Convert a price to "per ``unit``" using the price entity's unit.

    ``EUR/kWh`` with a meter in MWh gives the price per MWh. A unit that
    names no known quantity after the slash is taken as already per ``unit``.
    """
    if not price_unit or "/" not in price_unit:
        return price
    per = price_unit.rsplit("/", 1)[1].strip()
    per = _UNIT_SPELLINGS.get(per, per)
    if per == unit:
        return price
    for converter in _PRICE_UNIT_CONVERTERS:
        if per in converter.VALID_UNITS and unit in converter.VALID_UNITS:
            # How many ``per`` units fit in one ``unit``.
            return price * converter.convert(1, unit, per)
    return price


def entity_price(hass: HomeAssistant, entity_id: str, unit: str) -> float | None:
    """Return the price entity's current value per ``unit``; None if unusable."""
    state = hass.states.get(entity_id)
    if state is None or (price := finite(state.state)) is None:
        return None
    return price_per_unit(price, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT), unit)


class PriceSchedule:
    """The price the integration keeps itself, with one scheduled change.

    Holds the current price and an optional next price with the date it takes
    effect. At local midnight on that date the next price becomes the current
    one. The time of the last change is kept, so consumption measured across
    a change can be split between the old and the new price.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise an empty schedule."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry_id)
        )
        self.price: float | None = None
        self.previous_price: float | None = None
        self.changed_at: datetime | None = None
        self.next_price: float | None = None
        self.next_date: date | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self._cancel_switch: CALLBACK_TYPE | None = None

    async def async_load(self) -> None:
        """Load the stored prices and apply or schedule a pending change."""
        data = await self._store.async_load() or {}
        self.price = finite(data.get("price"))
        self.previous_price = finite(data.get("previous_price"))
        self.next_price = finite(data.get("next_price"))
        changed_at = data.get("changed_at")
        self.changed_at = (
            dt_util.parse_datetime(changed_at) if isinstance(changed_at, str) else None
        )
        next_date = data.get("next_date")
        self.next_date = (
            dt_util.parse_date(next_date) if isinstance(next_date, str) else None
        )
        self._async_schedule()

    async def async_unload(self) -> None:
        """Stop the timer and write the prices to disk now."""
        self.async_cancel()
        await self._store.async_save(self.as_dict())

    @callback
    def async_cancel(self) -> None:
        """Cancel the scheduled price change timer."""
        if self._cancel_switch is not None:
            self._cancel_switch()
            self._cancel_switch = None

    def as_dict(self) -> dict[str, Any]:
        """Return the schedule in its stored form."""
        return {
            "price": self.price,
            "previous_price": self.previous_price,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
            "next_price": self.next_price,
            "next_date": self.next_date.isoformat() if self.next_date else None,
        }

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``listener`` whenever a value changes; return the remover."""
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    @callback
    def async_set_price(self, value: float) -> None:
        """Set the current price, effective now."""
        self._change_price(value, dt_util.utcnow())
        self._async_changed()

    @callback
    def async_set_next_price(self, value: float) -> None:
        """Set the price that applies from the next-price date."""
        self.next_price = value
        self._async_schedule()
        self._async_changed()

    @callback
    def async_set_next_date(self, value: date) -> None:
        """Set the date from which the next price applies."""
        self.next_date = value
        self._async_schedule()
        self._async_changed()

    @callback
    def average_price(self, since: datetime | None, now: datetime) -> float | None:
        """Return the time-weighted price for consumption between two readings.

        If the price changed between ``since`` and ``now`` the old and the new
        price are weighted by how long each applied. Time before the first
        price was set counts as free. None while no price is set.
        """
        # A reading can arrive in the same moment as a due change, before the
        # timer has run (after the clock jumped, for example). Apply it first.
        if self._cancel_switch is not None and self._due():
            self._async_switch(now)
        if self.price is None:
            return None
        changed_at = self.changed_at
        if since is None or changed_at is None or not since < changed_at < now:
            return self.price
        before = (changed_at - since) / (now - since)
        return before * (self.previous_price or 0.0) + (1 - before) * self.price

    def _change_price(self, value: float, when: datetime) -> None:
        if value == self.price:
            return
        self.previous_price = self.price
        self.price = value
        self.changed_at = when

    def _switch_time(self) -> datetime:
        """Return when the next price takes effect: local midnight of its date."""
        assert self.next_date is not None
        return dt_util.as_utc(dt_util.start_of_local_day(self.next_date))

    def _due(self) -> bool:
        """Return True if a complete next price has reached its date."""
        return (
            self.next_price is not None
            and self.next_date is not None
            and self._switch_time() <= dt_util.utcnow()
        )

    @callback
    def _async_schedule(self) -> None:
        """Apply the next price if it is due, else set a timer for it."""
        self.async_cancel()
        if self.next_price is None or self.next_date is None:
            return
        when = self._switch_time()
        if self._due():
            self._change_price(self.next_price, when)
            self.next_price = None
            self.next_date = None
            return
        self._cancel_switch = async_track_point_in_time(
            self.hass, self._async_switch, when
        )

    @callback
    def _async_switch(self, _now: datetime) -> None:
        self._cancel_switch = None
        self._async_schedule()
        self._async_changed()

    @callback
    def _async_changed(self) -> None:
        self._store.async_delay_save(self.as_dict, 1)
        for listener in list(self._listeners):
            listener()


def storage_key(entry_id: str) -> str:
    """Return the storage key of an entry's price schedule."""
    return f"{DOMAIN}.{entry_id}.prices"


def current_price(
    hass: HomeAssistant, entry: ConfigEntry, schedule: PriceSchedule | None
) -> float | None:
    """Return the price in force right now, per unit of the priced register."""
    conf = {**entry.data, **entry.options}
    if price_entity := conf.get(CONF_PRICE_ENTITY):
        return entity_price(hass, price_entity, cost_spec(conf).unit)
    return schedule.price if schedule is not None else None
