"""Actions of the Kamst-IR Gateway integration."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
import voluptuous as vol

from .backfill import async_backfill_cost
from .const import DOMAIN, SERVICE_BACKFILL_COST
from .coordinator import KamstirConfigEntry

ATTR_PRICE = "price"
ATTR_START = "start"
ATTR_END = "end"

BACKFILL_COST_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_PRICE): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional(ATTR_START): cv.date,
        vol.Optional(ATTR_END): cv.date,
    }
)


@callback
def _loaded_entry(hass: HomeAssistant, device_id: str) -> KamstirConfigEntry:
    """Return the loaded gateway entry a device belongs to."""
    device = dr.async_get(hass).async_get(device_id)
    for entry_id in device.config_entries if device else ():
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is not None
            and entry.domain == DOMAIN
            and entry.state is ConfigEntryState.LOADED
        ):
            return entry
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="backfill_no_gateway"
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions."""
    # One backfill at a time: each reads the statistics it then rewrites.
    lock = asyncio.Lock()

    async def backfill_cost(call: ServiceCall) -> ServiceResponse:
        entry = _loaded_entry(hass, call.data[ATTR_DEVICE_ID])
        async with lock:
            return await async_backfill_cost(
                hass,
                entry,
                entry.runtime_data,
                price=call.data.get(ATTR_PRICE),
                start=call.data.get(ATTR_START),
                end=call.data.get(ATTR_END),
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL_COST,
        backfill_cost,
        schema=BACKFILL_COST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
