"""Check that translations cover every entity and match each other."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from custom_components.kamstir.const import (
    CONF_METER_TYPE,
    METER_HEAT,
    METER_WATER,
    SERVICE_BACKFILL_COST,
)
from custom_components.kamstir.cost import cost_spec
from custom_components.kamstir.sensor import SENSORS
from custom_components.kamstir.services import BACKFILL_COST_SCHEMA

ROOT = Path(__file__).parents[1] / "custom_components" / "kamstir"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _keys(tree: dict[str, Any], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        keys |= _keys(value, path) if isinstance(value, dict) else {path}
    return keys


def test_en_matches_strings() -> None:
    """translations/en.json is a copy of strings.json."""
    assert _load(ROOT / "strings.json") == _load(ROOT / "translations" / "en.json")


def test_lv_has_same_keys() -> None:
    """The Latvian file translates every key."""
    assert _keys(_load(ROOT / "translations" / "lv.json")) == _keys(
        _load(ROOT / "strings.json")
    )


def test_every_sensor_has_a_name() -> None:
    """Every sensor description has a translated name."""
    names = _load(ROOT / "strings.json")["entity"]["sensor"]
    cost = {"heat_cost", "water_cost"}
    assert {d.translation_key for d in SENSORS} | cost == set(names)


def test_cost_entities_have_names_and_icons() -> None:
    """Every cost tracking entity has a translated name and an icon."""
    entities = _load(ROOT / "strings.json")["entity"]
    icons = _load(ROOT / "icons.json")["entity"]
    for meter_type in (METER_HEAT, METER_WATER):
        spec = cost_spec({CONF_METER_TYPE: meter_type})
        expected = {
            "sensor": {spec.cost_key},
            "number": {spec.price_key, spec.next_price_key},
            "date": {spec.next_date_key},
        }
        for platform, keys in expected.items():
            assert keys <= set(entities[platform]), platform
            assert keys <= set(icons[platform]), platform


def test_action_is_described() -> None:
    """The backfill action has a name, fields and an icon everywhere."""
    services = _load(ROOT / "strings.json")["services"]
    fields = set(services[SERVICE_BACKFILL_COST]["fields"])
    assert fields == set(BACKFILL_COST_SCHEMA.schema)
    assert SERVICE_BACKFILL_COST in _load(ROOT / "icons.json")["services"]
    described = (ROOT / "services.yaml").read_text(encoding="utf-8")
    assert described.startswith(f"{SERVICE_BACKFILL_COST}:")
    for field in fields:
        assert f"    {field}:" in described
