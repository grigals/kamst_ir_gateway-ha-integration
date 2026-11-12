"""Tests for cost tracking: the cost sensor, the prices and the schedule."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import ATTR_STATE_CLASS, SensorStateClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.kamstir.const import (
    CONF_ENERGY_UNIT,
    CONF_METER_TYPE,
    DOMAIN,
    METER_WATER,
)
from custom_components.kamstir.cost import (
    PriceSchedule,
    cost_spec,
    entity_price,
    price_per_unit,
    storage_key,
)
from custom_components.kamstir.diagnostics import async_get_config_entry_diagnostics

from .conftest import HOST2, MAC, MAC2, load_payload, make_entry, setup_entry, url

COST = "sensor.kamstrup_k1_heat_cost"
PRICE = "number.kamstrup_k1_heat_price"
NEXT_PRICE = "number.kamstrup_k1_next_heat_price"
NEXT_DATE = "date.kamstrup_k1_next_heat_price_from"
EXTERNAL_PRICE = "input_number.heat_price"

# The heat fixture reads 1234.567 MWh.
START = 1234.567
SINCE = "2026-10-01T00:00:00+00:00"


def cost(hass: HomeAssistant, entity_id: str = COST) -> float:
    """Return the cost sensor's state as a number."""
    return float(hass.states.get(entity_id).state)


async def poll(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    *,
    fixture: str = "read_heat.json",
    host_url: str | None = None,
    error: bool = False,
    **fields: str,
) -> None:
    """Serve a payload with some fields changed and let one poll happen."""
    aioclient_mock.clear_requests()
    if error:
        aioclient_mock.get(host_url or url(), exc=aiohttp.ClientError())
    else:
        aioclient_mock.get(host_url or url(), json={**load_payload(fixture), **fields})
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def set_price(hass: HomeAssistant, value: float, entity_id: str = PRICE) -> None:
    """Set a price number."""
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )


async def set_date(hass: HomeAssistant, value: date) -> None:
    """Set the next-price date."""
    await hass.services.async_call(
        "date",
        "set_value",
        {"entity_id": NEXT_DATE, "date": value.isoformat()},
        blocking=True,
    )


def stored_prices(entry_id: str, **data: Any) -> dict[str, Any]:
    """Return a storage file for an entry's price schedule."""
    return {
        "version": 1,
        "minor_version": 1,
        "key": storage_key(entry_id),
        "data": data,
    }


async def test_off_by_default(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
) -> None:
    """Without the option there are no cost entities."""
    entry = await setup_entry(hass, make_entry())
    domains = {
        e.domain
        for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    }
    assert "number" not in domains
    assert "date" not in domains
    assert hass.states.get(COST) is None
    assert entry.runtime_data.prices is None


async def test_entities(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
) -> None:
    """Cost tracking adds a cost sensor, two prices and a date."""
    await setup_entry(hass, make_entry(cost_tracking=True))

    state = hass.states.get(COST)
    assert state.state == "0.0"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == hass.config.currency
    assert state.attributes[ATTR_DEVICE_CLASS] == "monetary"
    assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.TOTAL
    assert entity_registry.async_get(COST).unique_id == f"{MAC}_heat_cost"

    for entity_id in (PRICE, NEXT_PRICE):
        price = hass.states.get(entity_id)
        assert price.state == STATE_UNKNOWN
        assert price.attributes[ATTR_UNIT_OF_MEASUREMENT] == (
            f"{hass.config.currency}/MWh"
        )
        assert entity_registry.async_get(entity_id).entity_category == "config"
    assert hass.states.get(NEXT_DATE).state == STATE_UNKNOWN


async def test_price_unit_follows_energy_unit(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """The price is per unit of the meter reading."""
    await setup_entry(hass, make_entry(cost_tracking=True, energy_unit="gj"))
    assert hass.states.get(PRICE).attributes[ATTR_UNIT_OF_MEASUREMENT] == (
        f"{hass.config.currency}/GJ"
    )


async def test_cost_accrues(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Each new reading adds its growth times the price."""
    await setup_entry(hass, make_entry(cost_tracking=True))
    await set_price(hass, 100)
    assert hass.states.get(PRICE).state == "100.0"

    await poll(hass, mock_gateway, freezer, heat_energy="1234.667")
    assert cost(hass) == pytest.approx(10.0)

    # No growth, no cost.
    await poll(hass, mock_gateway, freezer, heat_energy="1234.667")
    assert cost(hass) == pytest.approx(10.0)

    # A new price applies to what is used from then on.
    await set_price(hass, 50)
    await poll(hass, mock_gateway, freezer, heat_energy="1235.667")
    assert cost(hass) == pytest.approx(60.0)


async def test_no_price_adds_nothing(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """What is used before a price is set is not priced later."""
    await setup_entry(hass, make_entry(cost_tracking=True))

    await poll(hass, mock_gateway, freezer, heat_energy="1235.567")
    assert cost(hass) == 0

    await set_price(hass, 100)
    await poll(hass, mock_gateway, freezer, heat_energy="1235.567")
    assert cost(hass) == 0
    await poll(hass, mock_gateway, freezer, heat_energy="1235.667")
    assert cost(hass) == pytest.approx(10.0)


async def test_missing_reading_is_caught_up(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An unknown reading adds nothing; the gap is priced when it returns."""
    await setup_entry(hass, make_entry(cost_tracking=True))
    await set_price(hass, 100)

    await poll(hass, mock_gateway, freezer, heat_energy="-")
    assert cost(hass) == 0
    await poll(hass, mock_gateway, freezer, heat_energy="nan")
    assert cost(hass) == 0
    await poll(hass, mock_gateway, freezer, heat_energy="1234.767")
    assert cost(hass) == pytest.approx(20.0)


async def test_gateway_down(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The cost and the prices stay available while the gateway is down."""
    await setup_entry(hass, make_entry(cost_tracking=True))
    await set_price(hass, 100)
    await poll(hass, mock_gateway, freezer, heat_energy="1234.667")

    await poll(hass, mock_gateway, freezer, error=True)
    assert hass.states.get("sensor.kamstrup_k1_heat_energy").state == STATE_UNAVAILABLE
    assert cost(hass) == pytest.approx(10.0)
    assert hass.states.get(PRICE).state == "100.0"
    assert hass.states.get(NEXT_DATE).state == STATE_UNKNOWN

    await poll(hass, mock_gateway, freezer, heat_energy="1234.867")
    assert cost(hass) == pytest.approx(30.0)


async def test_single_low_reading_is_ignored(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One bad low value must not make the next good one look like a jump."""
    await setup_entry(hass, make_entry(cost_tracking=True))
    await set_price(hass, 100)

    await poll(hass, mock_gateway, freezer, heat_energy="12.3")
    assert cost(hass) == 0
    await poll(hass, mock_gateway, freezer, heat_energy="0")
    assert cost(hass) == 0
    await poll(hass, mock_gateway, freezer, heat_energy="1234.667")
    assert cost(hass) == pytest.approx(10.0)


async def test_replaced_meter_restarts_the_count(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A lower reading seen three times in a row is a new meter."""
    await setup_entry(hass, make_entry(cost_tracking=True))
    await set_price(hass, 100)
    await poll(hass, mock_gateway, freezer, heat_energy="1234.667")

    for reading in ("0.1", "0.2", "0.3"):
        await poll(hass, mock_gateway, freezer, heat_energy=reading)
        assert cost(hass) == pytest.approx(10.0)
    await poll(hass, mock_gateway, freezer, heat_energy="0.8")
    assert cost(hass) == pytest.approx(60.0)


async def test_reload_keeps_total_and_prices(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Reloading the entry loses neither the total nor the prices."""
    entry = await setup_entry(hass, make_entry(cost_tracking=True))
    await set_price(hass, 100)
    await set_price(hass, 120, NEXT_PRICE)
    await set_date(hass, date(2099, 1, 1))
    await poll(hass, mock_gateway, freezer, heat_energy="1234.667")
    since = hass.states.get(COST).attributes["last_reset"]

    # The reading moves on while the entry is being reloaded.
    mock_gateway.clear_requests()
    mock_gateway.get(
        url(), json={**load_payload("read_heat.json"), "heat_energy": "1234.767"}
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert cost(hass) == pytest.approx(20.0)
    assert hass.states.get(COST).attributes["last_reset"] == since
    assert hass.states.get(PRICE).state == "100.0"
    assert hass.states.get(NEXT_PRICE).state == "120.0"
    assert hass.states.get(NEXT_DATE).state == "2099-01-01"


async def test_restart_prices_the_downtime(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """After a restart the total continues and the downtime is priced."""
    entry = make_entry(cost_tracking=True)
    hass_storage[storage_key(entry.entry_id)] = stored_prices(entry.entry_id, price=100)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(COST, "12.5"),
                {
                    "total": 12.5,
                    "since": SINCE,
                    "last_reading": START - 4,
                    "last_time": "2026-11-01T00:00:00+00:00",
                    "last_price": None,
                },
            )
        ],
    )
    await setup_entry(hass, entry)

    assert hass.states.get(PRICE).state == "100.0"
    assert cost(hass) == pytest.approx(412.5)
    # The same total carries on, so it is still the same cycle.
    assert hass.states.get(COST).attributes["last_reset"] == SINCE


async def test_unusable_stored_data_starts_clean(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Broken stored values are dropped instead of breaking the setup."""
    entry = make_entry(cost_tracking=True)
    hass_storage[storage_key(entry.entry_id)] = stored_prices(
        entry.entry_id,
        price="abc",
        previous_price=True,
        changed_at=5,
        next_price=float("nan"),
        next_date="soon",
    )
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(COST, "x"),
                {"total": "x", "since": SINCE, "last_reading": None, "last_time": 3},
            )
        ],
    )
    await setup_entry(hass, entry)

    assert cost(hass) == 0
    # A total that starts over is a new cycle, not a drop to zero.
    assert hass.states.get(COST).attributes["last_reset"] == (
        dt_util.utcnow().isoformat()
    )
    assert hass.states.get(PRICE).state == STATE_UNKNOWN
    assert hass.states.get(NEXT_PRICE).state == STATE_UNKNOWN
    assert hass.states.get(NEXT_DATE).state == STATE_UNKNOWN


async def test_scheduled_price(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """At midnight on the date the next price takes over and the pair clears.

    Consumption measured across the change is split by time.
    """
    day = date(2026, 11, 26)
    midnight = dt_util.as_utc(dt_util.start_of_local_day(day))
    freezer.move_to(midnight - timedelta(hours=1))
    await setup_entry(hass, make_entry(cost_tracking=True))
    await set_price(hass, 100)
    await set_price(hass, 200, NEXT_PRICE)
    await set_date(hass, day)
    assert hass.states.get(NEXT_DATE).state == "2026-11-26"
    assert hass.states.get(PRICE).state == "100.0"

    # One reading an hour before midnight (at setup), the next an hour after.
    mock_gateway.clear_requests()
    mock_gateway.get(
        url(), json={**load_payload("read_heat.json"), "heat_energy": "1235.567"}
    )
    freezer.move_to(midnight + timedelta(hours=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(PRICE).state == "200.0"
    assert hass.states.get(NEXT_PRICE).state == STATE_UNKNOWN
    assert hass.states.get(NEXT_DATE).state == STATE_UNKNOWN
    assert cost(hass) == pytest.approx(150.0)

    await poll(hass, mock_gateway, freezer, heat_energy="1236.567")
    assert cost(hass) == pytest.approx(350.0)


async def test_schedule_needs_both_values(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A date without a price changes nothing; adding the price applies it."""
    await setup_entry(hass, make_entry(cost_tracking=True))
    await set_price(hass, 100)
    await set_date(hass, date(2000, 1, 1))
    assert hass.states.get(PRICE).state == "100.0"
    assert hass.states.get(NEXT_DATE).state == "2000-01-01"

    await set_price(hass, 200, NEXT_PRICE)
    assert hass.states.get(PRICE).state == "200.0"
    assert hass.states.get(NEXT_PRICE).state == STATE_UNKNOWN
    assert hass.states.get(NEXT_DATE).state == STATE_UNKNOWN


async def test_schedule_due_while_down(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """A change that came due while HA was off applies at its own date."""
    day = date(2026, 11, 20)
    midnight = dt_util.as_utc(dt_util.start_of_local_day(day))
    freezer.move_to(midnight + timedelta(days=2))
    entry = make_entry(cost_tracking=True)
    hass_storage[storage_key(entry.entry_id)] = stored_prices(
        entry.entry_id, price=100, next_price=200, next_date=day.isoformat()
    )
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(COST, "0"),
                {
                    "total": 0,
                    "last_reading": START - 1,
                    "last_time": (midnight - timedelta(days=2)).isoformat(),
                },
            )
        ],
    )
    await setup_entry(hass, entry)

    assert hass.states.get(PRICE).state == "200.0"
    assert hass.states.get(NEXT_DATE).state == STATE_UNKNOWN
    # Two days at 100 and two days at 200 for 1 MWh.
    assert cost(hass) == pytest.approx(150.0)
    assert entry.runtime_data.prices.changed_at == midnight


async def test_price_entity(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """A chosen price entity is used, converted, and replaces the own prices."""
    hass.states.async_set(EXTERNAL_PRICE, "0.1", {"unit_of_measurement": "EUR/kWh"})
    entry = await setup_entry(
        hass, make_entry(cost_tracking=True, price_entity=EXTERNAL_PRICE)
    )
    assert entry.runtime_data.prices is None
    assert hass.states.get(PRICE) is None
    assert hass.states.get(NEXT_DATE) is None

    # 0.1 EUR/kWh is 100 EUR/MWh.
    await poll(hass, mock_gateway, freezer, heat_energy="1234.667")
    assert cost(hass) == pytest.approx(10.0)

    # While the price entity is unavailable its last value is used.
    hass.states.async_set(EXTERNAL_PRICE, STATE_UNAVAILABLE)
    await poll(hass, mock_gateway, freezer, heat_energy="1234.767")
    assert cost(hass) == pytest.approx(20.0)

    hass.states.async_set(EXTERNAL_PRICE, "50", {"unit_of_measurement": "EUR/MWh"})
    await poll(hass, mock_gateway, freezer, heat_energy="1235.767")
    assert cost(hass) == pytest.approx(70.0)


async def test_price_entity_never_seen(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A price entity that never had a value adds no cost."""
    await setup_entry(hass, make_entry(cost_tracking=True, price_entity=EXTERNAL_PRICE))
    await poll(hass, mock_gateway, freezer, heat_energy="1235.567")
    assert cost(hass) == 0


async def test_water_meter(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A water meter prices its volume per cubic metre."""
    aioclient_mock.get(url(HOST2), json=load_payload("read_water.json"))
    await setup_entry(
        hass,
        make_entry(
            title="Kamstrup K2",
            host=HOST2,
            mac=MAC2,
            meter_type=METER_WATER,
            cost_tracking=True,
        ),
    )
    price = "number.kamstrup_k2_water_price"
    assert hass.states.get(price).attributes[ATTR_UNIT_OF_MEASUREMENT] == (
        f"{hass.config.currency}/m³"
    )
    assert hass.states.get("date.kamstrup_k2_next_water_price_from") is not None
    assert hass.states.get("sensor.kamstrup_k2_heat_cost") is None

    await set_price(hass, 2.5, price)
    await poll(
        hass,
        aioclient_mock,
        freezer,
        fixture="read_water.json",
        host_url=url(HOST2),
        volume="316.456",
    )
    assert cost(hass, "sensor.kamstrup_k2_water_cost") == pytest.approx(10.0)


async def test_diagnostics_and_removal(
    hass: HomeAssistant,
    mock_gateway: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Diagnostics show the prices; removing the gateway deletes them."""
    entry = await setup_entry(hass, make_entry(cost_tracking=True))
    await set_price(hass, 100)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["prices"]["price"] == 100
    assert diag["prices"]["next_date"] is None

    key = storage_key(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass_storage[key]["data"]["price"] == 100
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_diagnostics_without_own_prices(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """Without own prices the diagnostics say so."""
    entry = await setup_entry(hass, make_entry())
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["prices"] is None


@pytest.mark.parametrize(
    ("price", "price_unit", "unit", "expected"),
    [
        (0.1, "EUR/kWh", "MWh", 100.0),
        (85.0, "EUR/MWh", "MWh", 85.0),
        (100.0, "€/MWh", "kWh", 0.1),
        (36.0, "EUR/MWh", "GJ", 10.0),
        (2.5, "EUR/m3", "m³", 2.5),
        (0.0025, "EUR/L", "m³", 2.5),
        # No unit, or one that names no quantity: taken as per meter unit.
        (85.0, None, "MWh", 85.0),
        (85.0, "EUR", "MWh", 85.0),
        (85.0, "EUR/month", "MWh", 85.0),
        # A volume price cannot be converted to an energy price.
        (2.5, "EUR/m³", "MWh", 2.5),
    ],
)
def test_price_per_unit(
    price: float, price_unit: str | None, unit: str, expected: float
) -> None:
    """Prices are converted by the unit after the slash."""
    assert price_per_unit(price, price_unit, unit) == pytest.approx(expected)


@pytest.mark.parametrize("state", ["unknown", "unavailable", "abc", "inf", "nan", ""])
def test_entity_price_unusable(hass: HomeAssistant, state: str) -> None:
    """A price entity without a finite number has no price."""
    assert entity_price(hass, EXTERNAL_PRICE, "MWh") is None
    hass.states.async_set(EXTERNAL_PRICE, state)
    assert entity_price(hass, EXTERNAL_PRICE, "MWh") is None


def test_cost_spec() -> None:
    """Heat is priced per energy unit, water per cubic metre."""
    heat = cost_spec({CONF_ENERGY_UNIT: "kwh"})
    assert (heat.kind, heat.source_key, heat.unit) == ("heat", "heat_energy", "kWh")
    assert cost_spec({}).unit == "MWh"
    assert cost_spec({CONF_ENERGY_UNIT: "bogus"}).unit == "MWh"
    water = cost_spec({CONF_METER_TYPE: METER_WATER, CONF_ENERGY_UNIT: "gj"})
    assert (water.kind, water.source_key, water.unit) == ("water", "volume", "m³")
    assert water.next_date_key == "next_water_price_date"


def test_average_price(hass: HomeAssistant) -> None:
    """The price between two readings is weighted by time around a change."""
    schedule = PriceSchedule(hass, "test")
    t0 = datetime(2026, 11, 1, tzinfo=dt_util.UTC)
    hour = timedelta(hours=1)
    assert schedule.average_price(t0, t0 + hour) is None

    schedule.price = 200.0
    assert schedule.average_price(None, t0) == 200.0
    assert schedule.average_price(t0, t0 + hour) == 200.0

    schedule.previous_price = 100.0
    schedule.changed_at = t0 + hour
    # The change is inside the interval: one hour at 100, three at 200.
    assert schedule.average_price(t0, t0 + 4 * hour) == pytest.approx(175.0)
    # The change is at or outside the ends: only the current price counts.
    assert schedule.average_price(t0 + hour, t0 + 2 * hour) == 200.0
    assert schedule.average_price(t0 - hour, t0 + hour) == 200.0
    assert schedule.average_price(t0 + 2 * hour, t0 + 3 * hour) == 200.0

    # Before the first price was ever set nothing is charged.
    schedule.previous_price = None
    assert schedule.average_price(t0, t0 + 4 * hour) == pytest.approx(150.0)


async def test_same_price_keeps_change_time(hass: HomeAssistant) -> None:
    """Setting the price it already has is not a price change."""
    schedule = PriceSchedule(hass, "test")
    schedule.async_set_price(100)
    changed_at = schedule.changed_at
    assert changed_at is not None
    schedule.async_set_price(100)
    assert schedule.changed_at == changed_at
    assert schedule.previous_price is None
    await schedule.async_unload()


async def test_entry_ids_are_separate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Two gateways keep their own prices."""
    aioclient_mock.get(url(), json=load_payload("read_heat.json"))
    aioclient_mock.get(url(HOST2), json=load_payload("read_heat.json"))
    await setup_entry(hass, make_entry(cost_tracking=True))
    await setup_entry(
        hass, make_entry(title="Kamstrup K2", host=HOST2, mac=MAC2, cost_tracking=True)
    )
    await set_price(hass, 100)
    assert hass.states.get(PRICE).state == "100.0"
    assert hass.states.get("number.kamstrup_k2_heat_price").state == STATE_UNKNOWN
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{MAC2}_heat_cost")
