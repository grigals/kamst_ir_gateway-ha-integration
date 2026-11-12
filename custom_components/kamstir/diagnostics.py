"""Diagnostics download for the Kamst-IR gateway."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import KamstirConfigEntry, MqttCoordinator

TO_REDACT = {"mac_address", "mqtt_server", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: KamstirConfigEntry
) -> dict[str, Any]:
    """Return the raw payload, the entry options, coordinator state and prices."""
    coordinator = entry.runtime_data
    state: dict[str, Any] = {
        "mode": coordinator.mode,
        "last_update_success": coordinator.last_update_success,
        "last_seen": coordinator.last_seen.isoformat()
        if coordinator.last_seen
        else None,
        "connected": coordinator.is_connected,
        "update_interval": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None
        ),
    }
    if isinstance(coordinator, MqttCoordinator):
        state["mqtt_prefix"] = coordinator.prefix
    return {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "coordinator": state,
        "prices": coordinator.prices.as_dict() if coordinator.prices else None,
        "raw": async_redact_data(coordinator.raw, TO_REDACT),
    }
