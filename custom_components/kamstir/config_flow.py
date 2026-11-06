"""Config, reconfigure and options flows for the Kamst-IR gateway."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
import voluptuous as vol

from .api import CannotConnect, InvalidResponse, KamstirClient, normalize_mac
from .const import (
    CONF_METER_TYPE,
    CONF_MODE,
    CONF_MQTT_PREFIX,
    DEFAULT_MQTT_PREFIX,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    METER_HEAT,
    METER_TYPES,
    MIN_SCAN_INTERVAL,
    MODE_MQTT,
    MODE_REST,
    MODES,
)
from .coordinator import merged_config

_LOGGER = logging.getLogger(__name__)

MQTT_DOMAIN = "mqtt"
PREFIX_EXAMPLE = "home/heating/kamstir"

PORT_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
)
METER_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(METER_TYPES),
        translation_key=CONF_METER_TYPE,
        mode=SelectSelectorMode.LIST,
    )
)
MODE_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(MODES), translation_key=CONF_MODE, mode=SelectSelectorMode.LIST
    )
)
SCAN_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL,
        max=MAX_SCAN_INTERVAL,
        step=1,
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="s",
    )
)


async def async_read_mac(hass: HomeAssistant, host: str, port: int) -> str:
    """Read the gateway once and return its normalised MAC address."""
    client = KamstirClient(async_get_clientsession(hass), host, port)
    raw = await client.read()
    mac = normalize_mac(raw.get("mac_address"))
    if mac is None:
        raise InvalidResponse("Payload has no usable mac_address")
    return mac


def clean_prefix(value: str | None) -> str | None:
    """Return the topic prefix without a trailing slash, or None if invalid."""
    prefix = (value or "").strip().rstrip("/")
    if not prefix or "#" in prefix or "+" in prefix:
        return None
    return prefix


def prefix_in_use(
    hass: HomeAssistant, prefix: str, exclude_entry_id: str | None = None
) -> bool:
    """Return True if another gateway already listens on this topic prefix."""
    return any(
        entry.entry_id != exclude_entry_id
        and entry.options.get(CONF_MODE) == MODE_MQTT
        and entry.options.get(CONF_MQTT_PREFIX) == prefix
        for entry in hass.config_entries.async_entries(DOMAIN)
    )


def prefix_error(
    hass: HomeAssistant, prefix: str | None, exclude_entry_id: str | None = None
) -> str | None:
    """Return the error key for a prefix, or None if it can be used."""
    if prefix is None:
        return "invalid_prefix"
    if prefix_in_use(hass, prefix, exclude_entry_id):
        return "prefix_in_use"
    return None


def mqtt_available(hass: HomeAssistant) -> bool:
    """Return True if the HA MQTT integration is set up."""
    return bool(hass.config_entries.async_loaded_entries(MQTT_DOMAIN))


async def _connect_errors(
    hass: HomeAssistant, host: str, port: int
) -> tuple[str | None, dict[str, str]]:
    """Return (mac, errors) for a connection attempt."""
    try:
        return await async_read_mac(hass, host, port), {}
    except CannotConnect:
        return None, {"base": "cannot_connect"}
    except InvalidResponse:
        return None, {"base": "invalid_response"}
    except Exception:
        _LOGGER.exception("Unexpected error talking to %s:%s", host, port)
        return None, {"base": "unknown"}


class KamstirConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Kamst-IR gateway."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._data: dict[str, Any] = {}
        self._name = DEFAULT_NAME

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KamstirOptionsFlow:
        """Return the options flow."""
        return KamstirOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the gateway's address, name, meter type and mode."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            mac, errors = await _connect_errors(self.hass, host, port)
            if mac is not None:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: host, CONF_PORT: port}
                )
                self._name = user_input[CONF_NAME].strip() or DEFAULT_NAME
                self._data = {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_METER_TYPE: user_input[CONF_METER_TYPE],
                }
                if user_input[CONF_MODE] == MODE_MQTT:
                    return await self.async_step_mqtt()
                return self._create({CONF_MODE: MODE_REST})

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                vol.Required(
                    CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
                ): PORT_SELECTOR,
                vol.Required(
                    CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)
                ): str,
                vol.Required(
                    CONF_METER_TYPE, default=defaults.get(CONF_METER_TYPE, METER_HEAT)
                ): METER_SELECTOR,
                vol.Required(
                    CONF_MODE, default=defaults.get(CONF_MODE, MODE_REST)
                ): MODE_SELECTOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the gateway's MQTT topic prefix."""
        if not mqtt_available(self.hass):
            return self.async_abort(reason="mqtt_not_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            prefix = clean_prefix(user_input.get(CONF_MQTT_PREFIX))
            if error := prefix_error(self.hass, prefix):
                errors[CONF_MQTT_PREFIX] = error
            else:
                return self._create({CONF_MODE: MODE_MQTT, CONF_MQTT_PREFIX: prefix})

        return self.async_show_form(
            step_id="mqtt",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MQTT_PREFIX, default=DEFAULT_MQTT_PREFIX
                    ): TextSelector()
                }
            ),
            errors=errors,
            description_placeholders={"example": PREFIX_EXAMPLE},
        )

    def _create(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._name,
            data=self._data,
            options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL, **options},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change host, port or MQTT prefix without removing the device."""
        entry = self._get_reconfigure_entry()
        conf = merged_config(entry)
        is_mqtt = conf.get(CONF_MODE) == MODE_MQTT
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            prefix = clean_prefix(user_input.get(CONF_MQTT_PREFIX)) if is_mqtt else None
            error = prefix_error(self.hass, prefix, entry.entry_id) if is_mqtt else None
            if error:
                errors[CONF_MQTT_PREFIX] = error
            else:
                mac, errors = await _connect_errors(self.hass, host, port)
                if mac is not None:
                    await self.async_set_unique_id(mac)
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                    options = dict(entry.options)
                    if prefix is not None:
                        options[CONF_MQTT_PREFIX] = prefix
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={CONF_HOST: host, CONF_PORT: port},
                        options=options,
                    )

        values = user_input or conf
        fields: dict[Any, Any] = {
            vol.Required(CONF_HOST, default=values.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_PORT, default=values.get(CONF_PORT, DEFAULT_PORT)
            ): PORT_SELECTOR,
        }
        if is_mqtt:
            fields[
                vol.Required(CONF_MQTT_PREFIX, default=values.get(CONF_MQTT_PREFIX, ""))
            ] = TextSelector()
        return self.async_show_form(
            step_id="reconfigure", data_schema=vol.Schema(fields), errors=errors
        )


class KamstirOptionsFlow(OptionsFlowWithReload):
    """Scan interval, transport and MQTT prefix."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options."""
        errors: dict[str, str] = {}
        current = merged_config(self.config_entry)

        if user_input is not None:
            mode = user_input[CONF_MODE]
            options: dict[str, Any] = {
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                CONF_MODE: mode,
            }
            raw_prefix = user_input.get(CONF_MQTT_PREFIX)
            prefix = clean_prefix(raw_prefix)
            if prefix is not None:
                options[CONF_MQTT_PREFIX] = prefix
            if mode == MODE_MQTT and not mqtt_available(self.hass):
                errors["base"] = "mqtt_not_configured"
            elif (mode == MODE_MQTT or raw_prefix) and (
                error := prefix_error(self.hass, prefix, self.config_entry.entry_id)
            ):
                errors[CONF_MQTT_PREFIX] = error
            if not errors:
                return self.async_create_entry(data=options)

        values = user_input or current
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=values.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): SCAN_SELECTOR,
                vol.Required(
                    CONF_MODE, default=values.get(CONF_MODE, MODE_REST)
                ): MODE_SELECTOR,
                vol.Optional(
                    CONF_MQTT_PREFIX,
                    description={"suggested_value": values.get(CONF_MQTT_PREFIX)},
                ): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"example": PREFIX_EXAMPLE},
        )
