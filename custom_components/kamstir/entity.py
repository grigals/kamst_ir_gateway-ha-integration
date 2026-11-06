"""Base entity for the Kamst-IR gateway."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import KamstirCoordinator


def build_device_info(coordinator: KamstirCoordinator) -> DeviceInfo:
    """Return the device info for one gateway."""
    data = coordinator.data or {}
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.mac)},
        connections={(CONNECTION_NETWORK_MAC, coordinator.mac)},
        name=coordinator.config_entry.title,
        manufacturer=MANUFACTURER,
        model=data.get("gateway_model"),
        sw_version=data.get("firmware_running"),
        configuration_url=coordinator.client.base_url,
    )


class KamstirEntity(CoordinatorEntity[KamstirCoordinator]):
    """An entity that belongs to one gateway."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: KamstirCoordinator, key: str) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.mac}_{key}"
        self._attr_device_info = build_device_info(coordinator)
