"""The Kamst-IR Gateway integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KamstirClient
from .const import (
    CONF_MODE,
    CONF_MQTT_PREFIX,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_LEGACY_YAML,
    LEGACY_UNIQUE_ID_PREFIX,
    MODE_MQTT,
)
from .coordinator import (
    KamstirConfigEntry,
    KamstirCoordinator,
    MqttCoordinator,
    RestCoordinator,
    merged_config,
)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.UPDATE,
]


async def async_setup_entry(hass: HomeAssistant, entry: KamstirConfigEntry) -> bool:
    """Set up one gateway."""
    conf = merged_config(entry)
    client = KamstirClient(
        async_get_clientsession(hass),
        conf[CONF_HOST],
        conf.get(CONF_PORT, DEFAULT_PORT),
    )

    coordinator: KamstirCoordinator
    if conf.get(CONF_MODE) == MODE_MQTT:
        coordinator = MqttCoordinator(hass, entry, client, conf[CONF_MQTT_PREFIX])
        await coordinator.async_start()
    else:
        scan_interval = timedelta(
            seconds=conf.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        coordinator = RestCoordinator(hass, entry, client, scan_interval)
        # Raises ConfigEntryNotReady when the gateway is down; HA retries.
        await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(
        coordinator.async_add_listener(lambda: _async_update_device(hass, coordinator))
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_check_legacy_yaml(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KamstirConfigEntry) -> bool:
    """Unload one gateway."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


@callback
def _async_update_device(hass: HomeAssistant, coordinator: KamstirCoordinator) -> None:
    """Keep model and firmware on the device page current, e.g. after an update."""
    data = coordinator.data or {}
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, coordinator.mac)})
    if device is None:
        return
    model = data.get("gateway_model") or device.model
    sw_version = data.get("firmware_running") or device.sw_version
    if (model, sw_version) != (device.model, device.sw_version):
        registry.async_update_device(device.id, model=model, sw_version=sw_version)


@callback
def async_check_legacy_yaml(hass: HomeAssistant) -> None:
    """Raise a repair issue while the old MQTT YAML entities still exist.

    Nothing is deleted automatically; the user removes their packages file.
    """
    legacy = sorted(
        entity.entity_id
        for entity in er.async_get(hass).entities.values()
        if entity.platform == "mqtt"
        and entity.unique_id.startswith(LEGACY_UNIQUE_ID_PREFIX)
    )
    if not legacy:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_LEGACY_YAML)
        return
    shown = ", ".join(legacy[:10]) + (" …" if len(legacy) > 10 else "")
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_LEGACY_YAML,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_LEGACY_YAML,
        translation_placeholders={"count": str(len(legacy)), "entities": shown},
    )
