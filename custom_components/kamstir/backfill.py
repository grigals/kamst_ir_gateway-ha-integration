"""Backfill the cost sensor's history from the meter's statistics.

The cost sensor only counts from the moment cost tracking is turned on. The
meter reading has hourly long-term statistics that go back further. The
backfill prices those hours and writes the result as the cost sensor's own
hourly statistics.

How the sums are kept continuous
--------------------------------
A statistic's ``sum`` is a running total; only differences between rows mean
anything. The recorder continues the cost sensor's sum from its newest
5-minute row, which a backfill cannot change. So the backfill never touches
the rows from its end onwards. It works backwards from the sum the sensor has
at the end of the backfilled range (0 if it has none yet) and subtracts each
hour's cost. Backfilled hours therefore have sums below that anchor, usually
negative. Every hour-to-hour difference is the cost of that hour, and the
live rows follow on without a jump.

Hours outside the requested range keep the cost they had, so the action can
be run again for another period or with another price.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import KamstirCoordinator
from .cost import cost_spec, cost_tracking_enabled, current_price

HOUR = 3600
# Sums closer than this are the same row; avoids rewriting unchanged history.
_SAME = 1e-9


@dataclass(frozen=True)
class CostRow:
    """One hourly cost statistics row to write."""

    start: int
    sum: float
    state: float | None


@dataclass(frozen=True)
class BackfillPlan:
    """The rows to write and what they add up to."""

    rows: list[CostRow]
    # Hours that were priced, and the range they span (end is exclusive).
    hours: int
    first: int
    end: int
    consumption: float
    cost: float


def _error(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


def _sums(rows: Iterable[Mapping[str, Any]]) -> list[tuple[int, float, float | None]]:
    """Return (hour start, sum, state) for the rows that have a sum, oldest first."""
    return sorted(
        (int(row["start"]), float(row["sum"]), row.get("state"))
        for row in rows
        if row.get("sum") is not None
    )


def plan_backfill(
    source_rows: Iterable[Mapping[str, Any]],
    cost_rows: Iterable[Mapping[str, Any]],
    *,
    price: float,
    start: int | None = None,
    end: int | None = None,
) -> BackfillPlan | None:
    """Work out the cost rows for the hours from ``start`` up to ``end``.

    ``source_rows`` and ``cost_rows`` are hourly statistics rows (``start`` as
    a UTC timestamp, ``sum``, ``state``) of the meter reading and of the cost
    sensor. ``start`` and ``end`` are UTC timestamps on the hour; ``end`` is
    exclusive. Returns None when there is no hour to price.
    """
    source = _sums(source_rows)
    if len(source) < 2:
        return None

    # Never go past the newest finished hour of the meter: later hours are
    # still being compiled by the recorder.
    limit = source[-1][0] + HOUR
    if end is not None:
        limit = min(limit, end)

    # Consumption per hour is the growth of the meter's sum. The oldest row
    # is only the starting point: what its sum is measured from is unknown.
    used: dict[int, float] = {source[0][0]: 0.0}
    for (_, before, _), (hour, after, _) in pairwise(source):
        used[hour] = after - before
    used = {hour: amount for hour, amount in used.items() if hour < limit}

    existing = {
        hour: (total, state) for hour, total, state in _sums(cost_rows) if hour < limit
    }
    # What each existing hour cost so far, to keep for hours out of range.
    had: dict[int, float] = {}
    previous = 0.0
    for hour in sorted(existing):
        had[hour] = existing[hour][0] - previous
        previous = existing[hour][0]

    def in_range(hour: int) -> bool:
        return start is None or hour >= start

    priced = sorted(hour for hour in used if in_range(hour))
    if not priced:
        return None

    hours = sorted(used.keys() | existing.keys())
    # Work back from the sum at the end of the range, which must not move.
    running = existing[max(existing)][0] if existing else 0.0
    rows: list[CostRow] = []
    consumption = cost = 0.0
    for hour in reversed(hours):
        if hour not in existing or abs(existing[hour][0] - running) > _SAME:
            state = existing[hour][1] if hour in existing else None
            rows.append(CostRow(hour, running, state))
        if in_range(hour):
            # A cost row for an hour the meter has no row for costs nothing.
            amount = used.get(hour, 0.0)
            consumption += amount
            cost += amount * price
            running -= amount * price
        else:
            running -= had.get(hour, 0.0)
    rows.reverse()
    return BackfillPlan(
        rows=rows,
        hours=len(priced),
        first=priced[0],
        end=priced[-1] + HOUR,
        consumption=consumption,
        cost=cost,
    )


def _hour_timestamp(day: date | None) -> int | None:
    """Return local midnight of ``day`` as a UTC timestamp, on the hour."""
    if day is None:
        return None
    moment = dt_util.as_utc(dt_util.start_of_local_day(day))
    return int(moment.timestamp()) // HOUR * HOUR


async def async_backfill_cost(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: KamstirCoordinator,
    *,
    price: float | None = None,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Price the meter's statistics and write them as cost statistics."""
    if "recorder" not in hass.config.components:
        raise _error("backfill_no_recorder")
    # Imported here: the recorder is optional for the rest of the integration.
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.models import (  # noqa: PLC0415
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        async_import_statistics,
        get_metadata,
        statistics_during_period,
    )
    from homeassistant.components.recorder.tasks import (  # noqa: PLC0415
        SynchronizeTask,
    )

    conf = {**entry.data, **entry.options}
    if not cost_tracking_enabled(conf):
        raise _error("backfill_cost_tracking_off")
    spec = cost_spec(conf)

    registry = er.async_get(hass)
    mac = coordinator.mac
    source_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{mac}_{spec.source_key}"
    )
    cost_id = registry.async_get_entity_id("sensor", DOMAIN, f"{mac}_{spec.cost_key}")
    if source_id is None or cost_id is None:
        raise _error("backfill_no_entity")

    if price is None:
        price = current_price(hass, entry, coordinator.prices)
    if price is None:
        raise _error("backfill_no_price")

    start_ts = _hour_timestamp(start)
    end_ts = _hour_timestamp(end)
    if start_ts is not None and end_ts is not None and start_ts >= end_ts:
        raise _error("backfill_bad_range")

    recorder = get_instance(hass)

    async def written() -> None:
        """Wait until everything queued so far has been written.

        The recorder works through its queue in order, so a marker queued
        now completes after the tasks queued before it.
        """
        future: asyncio.Future[None] = hass.loop.create_future()
        recorder.queue_task(SynchronizeTask(future))
        await future

    # Let rows that are queued but not yet written land before reading.
    await written()
    stats = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {source_id, cost_id},
        "hour",
        {spec.unit_class: spec.unit},
        {"sum", "state"},
    )
    plan = plan_backfill(
        stats.get(source_id, []),
        stats.get(cost_id, []),
        price=price,
        start=start_ts,
        end=end_ts,
    )
    if plan is None:
        raise _error("backfill_no_statistics", entity_id=source_id)

    known = await recorder.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={cost_id})
    )
    metadata: StatisticMetaData
    if cost_id in known:
        metadata = known[cost_id][1]
    else:
        # The same metadata the recorder would create for the cost sensor.
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=cost_id,
            unit_class=None,
            unit_of_measurement=hass.config.currency,
        )

    if plan.rows:
        rows: list[StatisticData] = []
        for row in plan.rows:
            data = StatisticData(
                start=dt_util.utc_from_timestamp(row.start), sum=row.sum
            )
            if row.state is not None:
                data["state"] = row.state
            rows.append(data)
        async_import_statistics(hass, metadata, rows)
        await written()

    return {
        "entity_id": cost_id,
        "source_entity_id": source_id,
        "start": dt_util.utc_from_timestamp(plan.first).isoformat(),
        "end": dt_util.utc_from_timestamp(plan.end).isoformat(),
        "hours": plan.hours,
        "consumption": round(plan.consumption, 6),
        "consumption_unit": spec.unit,
        "price": price,
        "cost": round(plan.cost, 2),
        "currency": metadata["unit_of_measurement"],
    }
