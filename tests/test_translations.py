"""Check that translations cover every entity and match each other."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from custom_components.kamstir.sensor import SENSORS

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
    assert {d.translation_key for d in SENSORS} == set(names)
