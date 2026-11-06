"""Restart button for the Kamst-IR gateway."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import AuthRequired, KamstirError
from .const import DOMAIN
from .coordinator import KamstirConfigEntry, KamstirCoordinator
from .entity import KamstirEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KamstirConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the restart button."""
    async_add_entities([KamstirRestartButton(entry.runtime_data)])


class KamstirRestartButton(KamstirEntity, ButtonEntity):
    """Restart the gateway over its web server (works in REST and MQTT mode)."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: KamstirCoordinator) -> None:
        """Initialise the button."""
        super().__init__(coordinator, "restart")

    @property
    def available(self) -> bool:
        """Allow a restart even while the readings are stale."""
        return True

    async def async_press(self) -> None:
        """Send the restart request."""
        try:
            await self.coordinator.client.reboot()
        except AuthRequired as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="restart_auth_required"
            ) from err
        except KamstirError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="restart_failed",
                translation_placeholders={"error": str(err)},
            ) from err
