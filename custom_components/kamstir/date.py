"""Next-price date for the Kamst-IR gateway's cost tracking."""

from __future__ import annotations

from datetime import date

from homeassistant.components.date import DateEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import KamstirConfigEntry, KamstirCoordinator, merged_config
from .cost import PriceSchedule, cost_spec
from .entity import KamstirEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KamstirConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the next-price date when the integration keeps the price itself."""
    coordinator = entry.runtime_data
    if (schedule := coordinator.prices) is None:
        return
    spec = cost_spec(merged_config(entry))
    async_add_entities(
        [KamstirNextPriceDate(coordinator, schedule, spec.next_date_key)]
    )


class KamstirNextPriceDate(KamstirEntity, DateEntity):
    """The date from which the next price applies (from local midnight)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: KamstirCoordinator, schedule: PriceSchedule, key: str
    ) -> None:
        """Initialise the date."""
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._schedule = schedule

    async def async_added_to_hass(self) -> None:
        """Follow changes made by the schedule, such as the midnight switch."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._schedule.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """A date is a setting; it does not depend on the gateway."""
        return True

    @property
    def native_value(self) -> date | None:
        """Return the date; None shows as unknown."""
        return self._schedule.next_date

    async def async_set_value(self, value: date) -> None:
        """Store the new date."""
        self._schedule.async_set_next_date(value)
