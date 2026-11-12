"""Tests for the backfill_cost action and the plan behind it."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
from pytest_homeassistant_custom_component.typing import (
    RecorderInstanceContextManager,
)
import voluptuous as vol

from custom_components.kamstir.backfill import HOUR, plan_backfill
from custom_components.kamstir.const import DOMAIN, METER_WATER, SERVICE_BACKFILL_COST

from .conftest import HOST2, MAC, MAC2, load_payload, make_entry, setup_entry, url

HEAT = "sensor.kamstrup_k1_heat_energy"
COST = "sensor.kamstrup_k1_heat_cost"
PRICE = "number.kamstrup_k1_heat_price"


@pytest.fixture
async def mock_recorder_before_hass(
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """Prepare the recorder before hass, as its fixtures require."""


# Meter sums at the end of five hours: 0, 1, 3, 6 and 10 units used.
H0 = 1_700_000_000 // HOUR * HOUR
METER = [100.0, 101.0, 103.0, 106.0, 110.0]


def rows(sums: list[float], *, first: int = H0, states: bool = False) -> list[dict]:
    """Return hourly statistics rows with these sums, one per hour."""
    return [
        {
            "start": float(first + index * HOUR),
            "sum": total,
            "state": total if states else None,
        }
        for index, total in enumerate(sums)
    ]


def apply(existing: list[dict], plan: Any) -> list[dict]:
    """Return the cost rows as they are after writing a plan."""
    merged = {int(row["start"]): dict(row) for row in existing}
    for row in plan.rows:
        merged[row.start] = {
            "start": float(row.start),
            "sum": row.sum,
            "state": row.state,
        }
    return [merged[start] for start in sorted(merged)]


def hourly_cost(cost_rows: list[dict]) -> dict[int, float]:
    """Return each hour's cost: the growth of the sum, by hour number."""
    ordered = sorted(cost_rows, key=lambda row: row["start"])
    return {
        (int(after["start"]) - H0) // HOUR: pytest.approx(after["sum"] - before["sum"])
        for before, after in zip(ordered, ordered[1:], strict=False)
    }


def test_plan_prices_every_hour() -> None:
    """Each hour costs its consumption times the price; the last sum is 0."""
    plan = plan_backfill(rows(METER), [], price=2.0)

    assert [(row.start, row.sum) for row in plan.rows] == [
        (H0, -20.0),
        (H0 + HOUR, -18.0),
        (H0 + 2 * HOUR, -14.0),
        (H0 + 3 * HOUR, -8.0),
        (H0 + 4 * HOUR, 0.0),
    ]
    assert all(row.state is None for row in plan.rows)
    assert plan.consumption == pytest.approx(10.0)
    assert plan.cost == pytest.approx(20.0)
    assert (plan.first, plan.end, plan.hours) == (H0, H0 + 5 * HOUR, 5)


def test_plan_continues_into_live_rows() -> None:
    """Live cost rows after the range are left alone and follow on.

    The sensor counted 0.5 and 1.5 in hours 3 and 4 before the backfill; the
    newest row keeps its sum, so the next live row continues from it.
    """
    live = rows([0.5, 1.5], first=H0 + 3 * HOUR, states=True)
    plan = plan_backfill(rows(METER), live, price=2.0)

    written = {row.start: row for row in plan.rows}
    assert H0 + 4 * HOUR not in written  # unchanged, so not rewritten
    assert written[H0 + 3 * HOUR].sum == pytest.approx(1.5 - 8.0)
    assert written[H0 + 3 * HOUR].state == 0.5  # what the sensor showed stays
    assert written[H0].sum == pytest.approx(1.5 - 20.0)
    assert hourly_cost(apply(live, plan)) == {1: 2.0, 2: 4.0, 3: 6.0, 4: 8.0}


def test_plan_is_repeatable() -> None:
    """Running the same backfill again changes nothing."""
    first = plan_backfill(rows(METER), [], price=2.0)
    written = apply([], first)
    again = plan_backfill(rows(METER), written, price=2.0)
    assert again.rows == []
    assert again.cost == pytest.approx(20.0)


def test_plan_new_price_replaces_old() -> None:
    """A second run with another price replaces the first, not adds to it."""
    written = apply([], plan_backfill(rows(METER), [], price=2.0))
    plan = plan_backfill(rows(METER), written, price=3.0)
    assert hourly_cost(apply(written, plan)) == {1: 3.0, 2: 6.0, 3: 9.0, 4: 12.0}
    # The newest row is the fixed point.
    assert apply(written, plan)[-1]["sum"] == written[-1]["sum"]


def test_plan_end_leaves_later_hours() -> None:
    """Hours from the end date on are not written."""
    live = rows([4.0, 9.0], first=H0 + 3 * HOUR)
    plan = plan_backfill(rows(METER), live, price=2.0, end=H0 + 3 * HOUR)

    assert [row.start for row in plan.rows] == [H0, H0 + HOUR, H0 + 2 * HOUR]
    # No cost row before the end: the sum there is 0, where live rows began.
    assert plan.rows[-1].sum == 0.0
    assert (plan.hours, plan.end) == (3, H0 + 3 * HOUR)
    assert plan.consumption == pytest.approx(3.0)
    # Hour 3 keeps the 4.0 the sensor counted, hour 4 its 5.0.
    assert hourly_cost(apply(live, plan)) == {1: 2.0, 2: 4.0, 3: 4.0, 4: 5.0}


def test_plan_end_after_data_is_capped() -> None:
    """An end beyond the meter's newest hour stops at that hour."""
    plan = plan_backfill(rows(METER), [], price=2.0, end=H0 + 50 * HOUR)
    assert plan.end == H0 + 5 * HOUR
    assert plan.cost == pytest.approx(20.0)


def test_plan_start_keeps_earlier_hours() -> None:
    """Hours before the start keep the cost they had."""
    written = apply([], plan_backfill(rows(METER), [], price=2.0))
    plan = plan_backfill(rows(METER), written, price=5.0, start=H0 + 3 * HOUR)

    assert (plan.hours, plan.first) == (2, H0 + 3 * HOUR)
    assert plan.consumption == pytest.approx(7.0)
    assert plan.cost == pytest.approx(35.0)
    assert hourly_cost(apply(written, plan)) == {1: 2.0, 2: 4.0, 3: 15.0, 4: 20.0}


@pytest.mark.parametrize("later_period_first", [False, True])
def test_plan_two_periods_in_any_order(later_period_first: bool) -> None:
    """Two tariff periods can be backfilled in either order."""
    split = H0 + 3 * HOUR
    runs = [{"price": 2.0, "end": split}, {"price": 5.0, "start": split}]
    if later_period_first:
        runs.reverse()

    written: list[dict] = []
    for run in runs:
        written = apply(written, plan_backfill(rows(METER), written, **run))
    assert hourly_cost(written) == {1: 2.0, 2: 4.0, 3: 15.0, 4: 20.0}
    assert written[-1]["sum"] == 0.0


def test_plan_gap_in_meter_statistics() -> None:
    """What was used while the meter had no rows lands on the next row."""
    source = rows(METER)
    del source[2]  # hour 2 is missing; hour 3 holds 103 -> 106 plus 101 -> 103
    plan = plan_backfill(source, [], price=2.0)
    assert [row.start for row in plan.rows] == [
        H0,
        H0 + HOUR,
        H0 + 3 * HOUR,
        H0 + 4 * HOUR,
    ]
    assert hourly_cost(apply([], plan)) == {1: 2.0, 3: 10.0, 4: 8.0}


def test_plan_cost_row_without_meter_row() -> None:
    """A cost hour the meter has no row for costs nothing once repriced."""
    source = rows(METER)
    del source[2]
    stale = rows([7.0], first=H0 + 2 * HOUR)
    plan = plan_backfill(source, stale, price=2.0)
    costs = hourly_cost(apply(stale, plan))
    assert costs == {1: 2.0, 2: 0.0, 3: 10.0, 4: 8.0}


def test_plan_ignores_rows_without_sum() -> None:
    """Rows without a sum are skipped."""
    source = rows(METER)
    source[1]["sum"] = None
    plan = plan_backfill(source, [{"start": float(H0), "sum": None}], price=1.0)
    assert plan.consumption == pytest.approx(10.0)
    assert len(plan.rows) == 4


@pytest.mark.parametrize(
    ("source", "kwargs"),
    [
        ([], {}),
        (rows(METER[:1]), {}),
        (rows(METER), {"start": H0 + 5 * HOUR}),
        (rows(METER), {"end": H0}),
    ],
)
def test_plan_nothing_to_price(source: list[dict], kwargs: dict[str, int]) -> None:
    """No statistics, or none in the period, gives no plan."""
    assert plan_backfill(source, [], price=2.0, **kwargs) is None


# --- The action, against a real recorder -----------------------------------


def device_id(hass: HomeAssistant, mac: str = MAC) -> str:
    """Return the device id of a gateway."""
    return dr.async_get(hass).async_get_device(identifiers={(DOMAIN, mac)}).id


async def backfill(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    """Run the action for the first gateway and return its response."""
    return await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKFILL_COST,
        {"device_id": device_id(hass), **data},
        blocking=True,
        return_response=True,
    )


async def import_meter(
    hass: HomeAssistant,
    first: datetime,
    sums: list[float],
    *,
    entity_id: str = HEAT,
    unit: str = "MWh",
    unit_class: str = "energy",
) -> None:
    """Give the meter sensor hourly statistics, one row per sum."""
    async_import_statistics(
        hass,
        {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": None,
            "source": "recorder",
            "statistic_id": entity_id,
            "unit_class": unit_class,
            "unit_of_measurement": unit,
        },
        [
            {"start": first + timedelta(hours=index), "sum": total, "state": total}
            for index, total in enumerate(sums)
        ],
    )
    await async_wait_recording_done(hass)


async def read_stats(
    hass: HomeAssistant, entity_id: str = COST, period: str = "hour"
) -> list[dict]:
    """Return all statistics rows of an entity."""
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {entity_id},
        period,
        None,
        {"sum", "state"},
    )
    return stats.get(entity_id, [])


def sums(stat_rows: list[dict]) -> list[float]:
    """Return the sums of statistics rows."""
    return [pytest.approx(row["sum"]) for row in stat_rows]


def old_hour(hours_ago: int) -> datetime:
    """Return the start of a full hour in the past."""
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    return now - timedelta(hours=hours_ago)


def next_hour(minute: int) -> datetime:
    """Return a 5-minute period start in the coming hour.

    The recorder compiles any period it is asked for from the newest states,
    so the tests pick the periods instead of depending on the clock. Only a
    period starting at minute 55 also compiles the hourly row.
    """
    return old_hour(-1) + timedelta(minutes=minute)


async def test_backfill_writes_history(
    recorder_mock: Recorder, hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """The action prices the meter's history with the current price."""
    await setup_entry(hass, make_entry(cost_tracking=True))
    first = old_hour(48)
    await import_meter(hass, first, METER)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": PRICE, "value": 2}, blocking=True
    )

    response = await backfill(hass)

    assert response == {
        "entity_id": COST,
        "source_entity_id": HEAT,
        "start": first.isoformat(),
        "end": (first + timedelta(hours=5)).isoformat(),
        "hours": 5,
        "consumption": 10.0,
        "consumption_unit": "MWh",
        "price": 2.0,
        "cost": 20.0,
        "currency": hass.config.currency,
    }
    written = await read_stats(hass)
    assert sums(written) == [-20.0, -18.0, -14.0, -8.0, 0.0]
    assert [row["start"] for row in written] == [
        (first + timedelta(hours=index)).timestamp() for index in range(5)
    ]

    # Again with the same price: nothing changes.
    assert (await backfill(hass))["cost"] == 20.0
    assert sums(await read_stats(hass)) == [-20.0, -18.0, -14.0, -8.0, 0.0]

    # A given price replaces what was written.
    assert (await backfill(hass, price=3))["cost"] == 30.0
    assert sums(await read_stats(hass)) == [-30.0, -27.0, -21.0, -12.0, 0.0]


async def test_backfill_period(
    recorder_mock: Recorder, hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """Start and end dates are local days; the end day is not included."""
    await setup_entry(hass, make_entry(cost_tracking=True))
    today = dt_util.now().date()
    yesterday = today - timedelta(days=1)
    midnight = dt_util.as_utc(dt_util.start_of_local_day(yesterday))
    # Three hours before yesterday's midnight ... two hours after it.
    first = midnight - timedelta(hours=3)
    await import_meter(hass, first, [100.0, 101.0, 103.0, 106.0, 110.0, 115.0])

    # Everything before yesterday at 2 per MWh: hours 0-2.
    response = await backfill(hass, price=2, end=yesterday.isoformat())
    assert (response["hours"], response["cost"]) == (3, 6.0)
    assert response["end"] == midnight.isoformat()
    assert sums(await read_stats(hass)) == [-6.0, -4.0, 0.0]

    # Yesterday itself at 10 per MWh: hours 3-5.
    response = await backfill(
        hass, price=10, start=yesterday.isoformat(), end=today.isoformat()
    )
    assert (response["hours"], response["consumption"]) == (3, 12.0)
    assert sums(await read_stats(hass)) == [-126.0, -124.0, -120.0, -90.0, -50.0, 0.0]


async def test_backfill_continues_into_live_statistics(
    recorder_mock: Recorder, hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """The recorder's own rows for the cost sensor follow the backfill.

    The backfilled history ends at sum 0; the sensor's first compiled row must
    start there and grow with the cost it counts.
    """
    await setup_entry(hass, make_entry(cost_tracking=True))
    await import_meter(hass, old_hour(48), METER)
    await backfill(hass, price=2)
    assert sums(await read_stats(hass)) == [-20.0, -18.0, -14.0, -8.0, 0.0]

    await async_wait_recording_done(hass)
    do_adhoc_statistics(hass, start=next_hour(50))
    await async_wait_recording_done(hass)
    assert sums(await read_stats(hass, period="5minute")) == [0.0]

    # The sensor counts 0.1 MWh at 100: the next row is 10 above the first.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": PRICE, "value": 100}, blocking=True
    )
    # One reading after the price was set, then one with the consumption.
    for seconds, reading in ((120, "1234.567"), (240, "1234.667")):
        mock_gateway.clear_requests()
        mock_gateway.get(
            url(), json={**load_payload("read_heat.json"), "heat_energy": reading}
        )
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
        await hass.async_block_till_done()
    assert float(hass.states.get(COST).state) == pytest.approx(10.0)

    # Minute 55 also compiles the hour: the sensor's own hourly row follows
    # the backfilled ones without a jump.
    await async_wait_recording_done(hass)
    do_adhoc_statistics(hass, start=next_hour(55))
    await async_wait_recording_done(hass)
    assert sums(await read_stats(hass, period="5minute")) == [0.0, 10.0]
    hourly = await read_stats(hass)
    assert sums(hourly) == [-20.0, -18.0, -14.0, -8.0, 0.0, 10.0]
    assert hourly[-1]["start"] == old_hour(-1).timestamp()


async def test_backfill_after_live_statistics(
    recorder_mock: Recorder, hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """A backfill run after the sensor has statistics leaves the live rows be.

    This is the usual order: cost tracking has been on for a while, then the
    history is filled in behind it.
    """
    await setup_entry(hass, make_entry(cost_tracking=True))
    await hass.services.async_call(
        "number", "set_value", {"entity_id": PRICE, "value": 100}, blocking=True
    )

    async def use(reading: str, seconds: int) -> None:
        mock_gateway.clear_requests()
        mock_gateway.get(
            url(), json={**load_payload("read_heat.json"), "heat_energy": reading}
        )
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
        await hass.async_block_till_done()

    async def compile_period(start: datetime) -> list[float]:
        await async_wait_recording_done(hass)
        do_adhoc_statistics(hass, start=start)
        await async_wait_recording_done(hass)
        return sums(await read_stats(hass, period="5minute"))

    assert await compile_period(next_hour(5)) == [0.0]
    await use("1234.567", 120)
    await use("1234.667", 240)
    assert await compile_period(next_hour(10)) == [0.0, 10.0]

    # The meter has five old hours; the sensor "counted" in two of them.
    first = old_hour(48)
    await import_meter(hass, first, METER)
    async_import_statistics(
        hass,
        {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": None,
            "source": "recorder",
            "statistic_id": COST,
            "unit_class": None,
            "unit_of_measurement": hass.config.currency,
        },
        [
            {"start": first + timedelta(hours=2), "sum": 1.0, "state": 1.0},
            {"start": first + timedelta(hours=4), "sum": 4.0, "state": 4.0},
        ],
    )
    await async_wait_recording_done(hass)

    response = await backfill(hass, price=2)
    assert response["cost"] == 20.0
    assert response["currency"] == hass.config.currency
    hourly = await read_stats(hass)
    # The newest of those rows keeps its sum; the rest is chained back from it.
    assert sums(hourly) == [-16.0, -14.0, -10.0, -4.0, 4.0]
    assert [row["state"] for row in hourly] == [None, None, 1.0, None, 4.0]

    # The live rows did not move, and the sensor carries on from them.
    assert sums(await read_stats(hass, period="5minute")) == [0.0, 10.0]
    await use("1234.767", 360)
    assert await compile_period(next_hour(15)) == [0.0, 10.0, 20.0]


async def test_lost_total_is_a_new_cycle(
    recorder_mock: Recorder, hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """A cost sensor that starts over from zero does not subtract its old total.

    Without last_reset the recorder would book the fall from 10 to 0 as a
    cost of -10.
    """
    entry = await setup_entry(hass, make_entry(cost_tracking=True))
    await hass.services.async_call(
        "number", "set_value", {"entity_id": PRICE, "value": 100}, blocking=True
    )

    async def use(reading: str, seconds: int) -> None:
        mock_gateway.clear_requests()
        mock_gateway.get(
            url(), json={**load_payload("read_heat.json"), "heat_energy": reading}
        )
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
        await hass.async_block_till_done()

    async def compile_period(minute: int) -> list[float]:
        await async_wait_recording_done(hass)
        do_adhoc_statistics(hass, start=next_hour(minute))
        await async_wait_recording_done(hass)
        return sums(await read_stats(hass, period="5minute"))

    assert await compile_period(5) == [0.0]
    await use("1234.567", 120)
    await use("1234.667", 240)
    assert await compile_period(10) == [0.0, 10.0]

    # The stored total is lost: the sensor comes back at zero.
    assert await hass.config_entries.async_unload(entry.entry_id)
    mock_restore_cache_with_extra_data(hass, [])
    mock_gateway.clear_requests()
    mock_gateway.get(
        url(), json={**load_payload("read_heat.json"), "heat_energy": "1234.667"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert float(hass.states.get(COST).state) == 0
    assert await compile_period(15) == [0.0, 10.0, 10.0]

    await use("1234.667", 120)
    await use("1234.767", 240)
    assert await compile_period(20) == [0.0, 10.0, 10.0, 20.0]


async def test_backfill_price_entity_and_unit(
    recorder_mock: Recorder, hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """The price entity gives the default price; statistics are converted."""
    hass.states.async_set(
        "input_number.heat_price", "0.002", {"unit_of_measurement": "EUR/kWh"}
    )
    await setup_entry(
        hass, make_entry(cost_tracking=True, price_entity="input_number.heat_price")
    )
    # The statistics are kept in kWh; the meter (and the price) are per MWh.
    await import_meter(
        hass, old_hour(48), [value * 1000 for value in METER], unit="kWh"
    )

    response = await backfill(hass)
    assert response["price"] == pytest.approx(2.0)
    assert response["consumption"] == pytest.approx(10.0)
    assert response["cost"] == 20.0


async def test_backfill_water(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A water meter backfills from its volume."""
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
    await import_meter(
        hass,
        old_hour(48),
        METER,
        entity_id="sensor.kamstrup_k2_volume",
        unit="m³",
        unit_class="volume",
    )
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKFILL_COST,
        {"device_id": device_id(hass, MAC2), "price": 2.5},
        blocking=True,
        return_response=True,
    )
    assert response["entity_id"] == "sensor.kamstrup_k2_water_cost"
    assert response["consumption_unit"] == "m³"
    assert response["cost"] == 25.0


async def test_backfill_errors(
    recorder_mock: Recorder, hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """Each thing that can be missing has its own error."""
    entry = await setup_entry(hass, make_entry(cost_tracking=True))

    async def error(**data: Any) -> str:
        with pytest.raises(ServiceValidationError) as raised:
            await backfill(hass, **data)
        return raised.value.translation_key

    assert await error() == "backfill_no_price"
    assert await error(price=2) == "backfill_no_statistics"
    assert await error(price=2, start="2026-02-01", end="2026-01-01") == (
        "backfill_bad_range"
    )
    with pytest.raises(vol.Invalid):
        await backfill(hass, price=-1)

    await import_meter(hass, old_hour(48), METER)
    assert await error(price=2, start=dt_util.now().date().isoformat()) == (
        "backfill_no_statistics"
    )
    assert await read_stats(hass) == []

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKFILL_COST,
            {"device_id": "nope", "price": 2},
            blocking=True,
            return_response=True,
        )
    assert raised.value.translation_key == "backfill_no_gateway"

    # Cost tracking off: the cost sensor is not there to fill.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "cost_tracking": False}
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert await error(price=2) == "backfill_cost_tracking_off"

    gateway = device_id(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKFILL_COST,
            {"device_id": gateway, "price": 2},
            blocking=True,
        )
    assert raised.value.translation_key == "backfill_no_gateway"


async def test_backfill_needs_the_meter_sensor(
    recorder_mock: Recorder, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without the meter sensor there is nothing to price."""
    payload = load_payload("read_heat.json")
    del payload["heat_energy"]
    aioclient_mock.get(url(), json=payload)
    await setup_entry(hass, make_entry(cost_tracking=True))
    with pytest.raises(ServiceValidationError) as raised:
        await backfill(hass, price=2)
    assert raised.value.translation_key == "backfill_no_entity"


async def test_backfill_without_recorder(
    hass: HomeAssistant, mock_gateway: AiohttpClientMocker
) -> None:
    """Without the recorder the action explains why it cannot run."""
    await setup_entry(hass, make_entry(cost_tracking=True))
    with pytest.raises(ServiceValidationError) as raised:
        await backfill(hass, price=2)
    assert raised.value.translation_key == "backfill_no_recorder"
