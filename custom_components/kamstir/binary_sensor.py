"""Binary sensors for the Kamst-IR gateway."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .coordinator import KamstirConfigEntry, KamstirCoordinator
from .entity import KamstirEntity

PARALLEL_UPDATES = 0

# The coordinator does not notify listeners on repeated failed polls, so the
# Connected sensor re-checks the age of the last good update on its own.
CONNECTED_RECHECK = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KamstirConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the binary sensors for one gateway."""
    coordinator = entry.runtime_data
    async_add_entities(
        [KamstirConnectedSensor(coordinator), KamstirMqttConfiguredSensor(coordinator)]
    )


class KamstirConnectedSensor(KamstirEntity, BinarySensorEntity):
    """On while the gateway delivered data recently. Always available."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connected"

    def __init__(self, coordinator: KamstirCoordinator) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, "connected")

    async def async_added_to_hass(self) -> None:
        """Start the periodic re-check."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._recheck, CONNECTED_RECHECK)
        )

    @callback
    def _recheck(self, _now: datetime) -> None:
        # An unchanged state does not fire a state_changed event.
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Stay available so it can report the gateway as down."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True while the last good update is recent."""
        return self.coordinator.is_connected


class KamstirMqttConfiguredSensor(KamstirEntity, BinarySensorEntity):
    """Whether MQTT is configured on the gateway itself."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "mqtt_configured"

    def __init__(self, coordinator: KamstirCoordinator) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, "mqtt_configured")

    @property
    def is_on(self) -> bool | None:
        """Return the gateway's MQTT setting."""
        return (self.coordinator.data or {}).get("mqtt_configured")
