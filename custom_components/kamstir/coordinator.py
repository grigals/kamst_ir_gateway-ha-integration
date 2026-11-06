"""Data coordinators for the Kamst-IR gateway.

Both coordinators expose the same ``data`` dict (field name -> parsed value),
so the entities do not know which transport fed them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import KamstirClient, KamstirError, KamstirParser, canonical_field
from .const import (
    DOMAIN,
    MODE_MQTT,
    MODE_REST,
    MQTT_DEBOUNCE_SECONDS,
    MQTT_INSTALL_PAYLOAD,
    MQTT_INSTALL_TOPIC,
    MQTT_STALE_AFTER,
    MQTT_TOPIC_GROUPS,
    MQTT_WATCHDOG_INTERVAL,
    REST_CONNECTED_FACTOR,
)

_LOGGER = logging.getLogger(__name__)

type KamstirConfigEntry = ConfigEntry[KamstirCoordinator]


class KamstirCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Shared state for one gateway, whichever transport feeds it."""

    config_entry: KamstirConfigEntry
    mode: str
    can_install: bool = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: KamstirConfigEntry,
        client: KamstirClient,
        update_interval: timedelta | None,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=update_interval,
        )
        self.client = client
        self.parser = KamstirParser()
        self.mac: str = entry.unique_id or entry.entry_id
        self.raw: dict[str, str] = {}
        self.last_seen: datetime | None = None

    @property
    def connected_timeout(self) -> timedelta:
        """Return how old the last good update may be while still connected."""
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        """Return True while the last good update is recent enough."""
        if self.last_seen is None:
            return False
        return dt_util.utcnow() - self.last_seen < self.connected_timeout

    def _mark_seen(self) -> None:
        self.last_seen = dt_util.utcnow()


class RestCoordinator(KamstirCoordinator):
    """Poll the gateway's REST API."""

    mode = MODE_REST

    def __init__(
        self,
        hass: HomeAssistant,
        entry: KamstirConfigEntry,
        client: KamstirClient,
        scan_interval: timedelta,
    ) -> None:
        """Initialise the polling coordinator."""
        super().__init__(hass, entry, client, scan_interval)
        self._scan_interval = scan_interval

    @property
    def connected_timeout(self) -> timedelta:
        """Return three scan intervals."""
        return self._scan_interval * REST_CONNECTED_FACTOR

    async def _async_update_data(self) -> dict[str, Any]:
        # DataUpdateCoordinator logs once when this starts failing and once
        # when it recovers, and marks the entities unavailable in between.
        try:
            raw = await self.client.read()
        except KamstirError as err:
            raise UpdateFailed(str(err)) from err
        self.raw = raw
        self._mark_seen()
        return self.parser.parse(raw)


class MqttCoordinator(KamstirCoordinator):
    """Receive pushed readings over MQTT."""

    mode = MODE_MQTT
    can_install = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: KamstirConfigEntry,
        client: KamstirClient,
        prefix: str,
    ) -> None:
        """Initialise the push coordinator."""
        super().__init__(hass, entry, client, None)
        self.prefix = prefix.strip().rstrip("/")
        self._pending: dict[str, str] = {}
        self._cancel_flush: CALLBACK_TYPE | None = None

    @property
    def connected_timeout(self) -> timedelta:
        """Return the MQTT staleness window."""
        return MQTT_STALE_AFTER

    async def _async_update_data(self) -> dict[str, Any]:
        # Push only: nothing schedules a refresh, but keep the contract.
        return self.data or {}

    async def async_start(self) -> None:
        """Fill the data once over REST, then subscribe to the gateway's topics."""
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            raise ConfigEntryNotReady("The MQTT integration is not available")

        try:
            raw = await self.client.read()
        except KamstirError as err:
            # Not fatal in MQTT mode: values arrive with the next publish.
            _LOGGER.debug("Initial REST read for %s failed: %s", self.name, err)
            self.data = {}
            self.last_update_success = False
        else:
            self.raw = raw
            self._mark_seen()
            self.async_set_updated_data(self.parser.parse(raw))

        entry = self.config_entry
        entry.async_on_unload(
            await mqtt.async_subscribe(
                self.hass, f"{self.prefix}/#", self._handle_message
            )
        )
        entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._check_stale, MQTT_WATCHDOG_INTERVAL
            )
        )
        entry.async_on_unload(self._cancel_pending_flush)

    def field_from_topic(self, topic: str) -> str | None:
        """Map ``<prefix>/reading/<field>`` to ``<field>``; None for other topics."""
        if not topic.startswith(f"{self.prefix}/"):
            return None
        parts = topic[len(self.prefix) + 1 :].split("/")
        if len(parts) != 2 or parts[0] not in MQTT_TOPIC_GROUPS:
            return None
        if "/".join(parts) == MQTT_INSTALL_TOPIC:
            return None
        return parts[1] or None

    @callback
    def _handle_message(self, msg: mqtt.ReceiveMessage) -> None:
        field = self.field_from_topic(msg.topic)
        if field is None:
            return
        payload = msg.payload
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        self._pending[canonical_field(field)] = str(payload)
        if self._cancel_flush is None:
            self._cancel_flush = async_call_later(
                self.hass, MQTT_DEBOUNCE_SECONDS, self._flush
            )

    @callback
    def _flush(self, _now: datetime | None = None) -> None:
        self._cancel_flush = None
        if not self._pending:
            return
        pending, self._pending = self._pending, {}
        self.raw.update(pending)
        data = dict(self.data or {})
        data.update(self.parser.parse(pending))
        if not self.last_update_success and self.last_seen is not None:
            _LOGGER.info("MQTT data from %s is arriving again", self.name)
        self._mark_seen()
        self.async_set_updated_data(data)

    @callback
    def _check_stale(self, _now: datetime) -> None:
        if self.last_update_success and not self.is_connected:
            _LOGGER.warning(
                "No MQTT data from %s for %s, marking it unavailable",
                self.name,
                self.connected_timeout,
            )
            self.last_update_success = False
            self.async_update_listeners()

    @callback
    def _cancel_pending_flush(self) -> None:
        if self._cancel_flush is not None:
            self._cancel_flush()
            self._cancel_flush = None

    async def async_install_firmware(self) -> None:
        """Ask the gateway to install the available firmware."""
        await mqtt.async_publish(
            self.hass, f"{self.prefix}/{MQTT_INSTALL_TOPIC}", MQTT_INSTALL_PAYLOAD
        )


def merged_config(entry: ConfigEntry) -> Mapping[str, Any]:
    """Return entry data overlaid with options."""
    return {**entry.data, **entry.options}
