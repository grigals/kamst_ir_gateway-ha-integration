"""Firmware update entity for the Kamst-IR gateway."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import KamstirConfigEntry, KamstirCoordinator, MqttCoordinator
from .entity import KamstirEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KamstirConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the firmware update entity."""
    async_add_entities([KamstirFirmwareUpdate(entry.runtime_data)])


class KamstirFirmwareUpdate(KamstirEntity, UpdateEntity):
    """Gateway firmware. Installable only when the gateway is reached over MQTT."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_translation_key = "firmware"

    def __init__(self, coordinator: KamstirCoordinator) -> None:
        """Initialise the update entity."""
        super().__init__(coordinator, "firmware")
        if coordinator.can_install:
            self._attr_supported_features = UpdateEntityFeature.INSTALL

    @property
    def installed_version(self) -> str | None:
        """Return the running firmware."""
        return (self.coordinator.data or {}).get("firmware_running")

    @property
    def latest_version(self) -> str | None:
        """Return the newest firmware the gateway knows of."""
        data = self.coordinator.data or {}
        return data.get("firmware_available") or data.get("firmware_running")

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Tell the gateway to install the available firmware."""
        if not isinstance(self.coordinator, MqttCoordinator):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="install_needs_mqtt"
            )
        await self.coordinator.async_install_firmware()
