"""Price numbers for the Kamst-IR gateway's cost tracking."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
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
    """Create the price numbers when the integration keeps the price itself."""
    coordinator = entry.runtime_data
    if (schedule := coordinator.prices) is None:
        return
    spec = cost_spec(merged_config(entry))
    unit = f"{hass.config.currency}/{spec.unit}"
    async_add_entities(
        [
            KamstirPriceNumber(coordinator, schedule, spec.price_key, unit, False),
            KamstirPriceNumber(coordinator, schedule, spec.next_price_key, unit, True),
        ]
    )


class KamstirPriceNumber(KamstirEntity, NumberEntity):
    """The current price, or the price that applies from the next-price date."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 1_000_000
    _attr_native_step = 0.00001

    def __init__(
        self,
        coordinator: KamstirCoordinator,
        schedule: PriceSchedule,
        key: str,
        unit: str,
        is_next: bool,
    ) -> None:
        """Initialise the number."""
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._attr_native_unit_of_measurement = unit
        self._schedule = schedule
        self._is_next = is_next

    async def async_added_to_hass(self) -> None:
        """Follow changes made by the schedule, such as the midnight switch."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._schedule.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """A price is a setting; it does not depend on the gateway."""
        return True

    @property
    def native_value(self) -> float | None:
        """Return the price; None shows as unknown."""
        if self._is_next:
            return self._schedule.next_price
        return self._schedule.price

    async def async_set_native_value(self, value: float) -> None:
        """Store the new price."""
        if self._is_next:
            self._schedule.async_set_next_price(value)
        else:
            self._schedule.async_set_price(value)
