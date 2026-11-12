"""Constants for the Kamst-IR Gateway integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "kamstir"
MANUFACTURER: Final = "Smart Gateways"

CONF_METER_TYPE: Final = "meter_type"
CONF_MODE: Final = "mode"
CONF_MQTT_PREFIX: Final = "mqtt_prefix"
CONF_ENERGY_UNIT: Final = "energy_unit"

# The gateway passes on the meter's energy register as-is, in whatever unit the
# meter is configured for. The user picks the unit shown on the meter display.
ENERGY_UNIT_MWH: Final = "mwh"
ENERGY_UNIT_GJ: Final = "gj"
ENERGY_UNIT_KWH: Final = "kwh"
ENERGY_UNITS: Final = (ENERGY_UNIT_MWH, ENERGY_UNIT_GJ, ENERGY_UNIT_KWH)
DEFAULT_ENERGY_UNIT: Final = ENERGY_UNIT_MWH

# Cost tracking (optional): a running cost total from the meter reading and a
# price. The price comes from the integration's own price entities, or from an
# entity the user picks.
CONF_COST_TRACKING: Final = "cost_tracking"
CONF_PRICE_ENTITY: Final = "price_entity"
PRICE_ENTITY_DOMAINS: Final = ("sensor", "number", "input_number")
# A reading below the last one is ignored until it has been seen this many
# times in a row; only then does it count as a replaced or reset meter.
COST_RESET_CONFIRMATIONS: Final = 3
SERVICE_BACKFILL_COST: Final = "backfill_cost"

METER_HEAT: Final = "heat"
METER_WATER: Final = "water"
METER_TYPES: Final = (METER_HEAT, METER_WATER)

MODE_REST: Final = "rest"
MODE_MQTT: Final = "mqtt"
MODES: Final = (MODE_REST, MODE_MQTT)

DEFAULT_NAME: Final = "Kamstrup"
# Topic base when the PREFIX field in the gateway web UI is left blank.
DEFAULT_MQTT_PREFIX: Final = "kamstir"
DEFAULT_PORT: Final = 82
DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 3600

API_PATH: Final = "/kamst-ir/api/read"
REBOOT_PATH: Final = "/reboot"
REQUEST_TIMEOUT: Final = 10

# REST: the gateway counts as connected while the last good poll is younger
# than this many scan intervals.
REST_CONNECTED_FACTOR: Final = 3
# MQTT: data older than this is stale and the entities go unavailable.
MQTT_STALE_AFTER: Final = timedelta(minutes=10)
MQTT_WATCHDOG_INTERVAL: Final = timedelta(seconds=60)
# Messages arrive one field per topic; batch them before notifying entities.
MQTT_DEBOUNCE_SECONDS: Final = 1.0

MQTT_TOPIC_GROUPS: Final = ("reading", "smart_gateways")
MQTT_INSTALL_TOPIC: Final = "smart_gateways/install_update"
MQTT_INSTALL_PAYLOAD: Final = "yes"

ISSUE_LEGACY_YAML: Final = "legacy_yaml_entities"
LEGACY_UNIQUE_ID_PREFIX: Final = "kamstir_"
