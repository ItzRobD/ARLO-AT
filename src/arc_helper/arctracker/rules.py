"""Parse the owner's rules.yaml (keep targets, actions, loadout groups)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn

import yaml

ACTIONS = frozenset({"keep", "sell", "recycle", "buy_on_demand"})
CATEGORIES = frozenset(
    {"weapons", "consumables", "medical", "crafting", "base", "arc_rare"}
)
RESERVED = frozenset({"loadout", "keys"})
DEFAULT_TIER = 5

if TYPE_CHECKING:
    from arc_helper.arctracker.catalogue import Catalogue


def _fail(msg: str) -> NoReturn:
    raise ValueError(msg)


@dataclass(frozen=True)
class Target:
    min: int
    ideal: int
    tier: int


@dataclass(frozen=True)
class LoadoutGroup:
    name: str
    items: tuple[str, ...]
    copies: int
    tier: int


@dataclass(frozen=True)
class Rules:
    targets: dict[str, Target]
    actions: dict[str, str]
    notes: dict[str, str]
    categories: dict[str, str]
    loadout: tuple[LoadoutGroup, ...]
    keep_key_rarities: frozenset[str]
    errors: tuple[str, ...]


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"expected an integer, got {value!r}")
    if value < 0:
        _fail(f"must not be negative: {value}")
    return value


def _pair(value: Any) -> tuple[int, int]:
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            _fail(f"expected [min, max], got {value!r}")
        lo, hi = _int(value[0]), _int(value[1])
        if hi < lo:
            _fail(f"max below min: {value!r}")
        return lo, hi
    n = _int(value)
    return n, n


def _target(spec: dict[str, Any], stack: int | None) -> tuple[int, int] | None:
    """Return (min, ideal) units, or None when the entry has no quantity field."""
    if "target" in spec:
        n = _int(spec["target"])
        return n, n
    if "rows" in spec or "stacks" in spec:
        if stack is None:
            _fail("stack size unknown")
        if "rows" in spec:
            lo, hi = _pair(spec["rows"])
            mult = 4 * stack
        else:
            lo, hi = _pair(spec["stacks"])
            mult = stack
        return lo * mult, hi * mult
    if "min" in spec or "ideal" in spec:
        lo = _int(spec["min"]) if "min" in spec else None
        hi = _int(spec["ideal"]) if "ideal" in spec else None
        if lo is None:
            lo = hi
        if hi is None:
            hi = lo
        if lo is None or hi is None:
            _fail("min/ideal missing")
        if hi < lo:
            _fail(f"ideal {hi} below min {lo}")
        return lo, hi
    return None


def parse_rules(text: str, catalogue: Catalogue) -> Rules:
    """Parse rules text. Bad entries are collected in ``errors``, never raised.

    Raises yaml.YAMLError only when the text is not valid YAML at all.
    """
    data = yaml.safe_load(text)
    errors: list[str] = []
    targets: dict[str, Target] = {}
    actions: dict[str, str] = {}
    notes: dict[str, str] = {}
    categories: dict[str, str] = {}
    loadout: list[LoadoutGroup] = []
    rarities: frozenset[str] = frozenset()

    if data is None:
        data = {}
    if not isinstance(data, dict):
        errors.append("rules file must be a mapping at the top level")
        data = {}

    for raw_key, spec in data.items():
        key = str(raw_key)
        if key == "loadout":
            loadout.extend(_parse_loadout(spec, catalogue, errors))
        elif key == "keys":
            rarities = _parse_keys(spec, errors)
        else:
            _parse_entry(
                key, spec, catalogue, targets, actions, notes, categories, errors
            )

    return Rules(
        targets=targets,
        actions=actions,
        notes=notes,
        categories=categories,
        loadout=tuple(loadout),
        keep_key_rarities=rarities,
        errors=tuple(errors),
    )


def _parse_entry(
    key: str,
    spec: Any,
    catalogue: Catalogue,
    targets: dict[str, Target],
    actions: dict[str, str],
    notes: dict[str, str],
    categories: dict[str, str],
    errors: list[str],
) -> None:
    item = catalogue.items.get(key)
    if item is None:
        errors.append(f"{key}: unknown item id")
        return
    if not isinstance(spec, dict):
        errors.append(f"{key}: entry must be a mapping")
        return
    try:
        tier = DEFAULT_TIER
        if "tier" in spec:
            tier = _int(spec["tier"])
            if not 4 <= tier <= 6:
                _fail(f"tier must be 4..6, got {tier}")
        action = spec.get("action")
        if action is not None and action not in ACTIONS:
            _fail(f"unknown action {action!r}")
        category = spec.get("category")
        if category is not None and category not in CATEGORIES:
            _fail(f"unknown category {category!r}")
        units = _target(spec, item.stack_size)
    except ValueError as exc:
        errors.append(f"{key}: {exc}")
        return
    if units is not None:
        targets[key] = Target(units[0], units[1], tier)
    if action is not None:
        actions[key] = action
    if spec.get("note") is not None:
        notes[key] = str(spec["note"])
    if category is not None:
        categories[key] = category


def _parse_loadout(
    spec: Any, catalogue: Catalogue, errors: list[str]
) -> list[LoadoutGroup]:
    groups: list[LoadoutGroup] = []
    if not isinstance(spec, dict):
        errors.append("loadout: must be a mapping of groups")
        return groups
    for raw_name, g in spec.items():
        name = str(raw_name)
        if not isinstance(g, dict) or not isinstance(g.get("items"), list):
            errors.append(f"loadout.{name}: needs an items list")
            continue
        try:
            copies = _int(g.get("copies", 1))
        except ValueError as exc:
            errors.append(f"loadout.{name}: copies: {exc}")
            continue
        ids: list[str] = []
        for item_id in g["items"]:
            if str(item_id) in catalogue.items:
                ids.append(str(item_id))
            else:
                errors.append(f"loadout.{name}: unknown item id {item_id}")
        groups.append(
            LoadoutGroup(name, tuple(ids), copies, 2 if name == "weapons" else 3)
        )
    return groups


def _parse_keys(spec: Any, errors: list[str]) -> frozenset[str]:
    if not isinstance(spec, dict):
        errors.append("keys: must be a mapping")
        return frozenset()
    vals = spec.get("keep_rarities", [])
    if not isinstance(vals, list):
        errors.append("keys.keep_rarities: must be a list")
        return frozenset()
    return frozenset(str(v) for v in vals)


class RulesWatcher:
    """Reloads rules when the file's mtime changes; keeps old rules on YAML errors."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._mtime_ns: int | None = None
        self._rules: Rules | None = None

    def current(self, catalogue: Catalogue) -> Rules | None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return self._rules
        if mtime != self._mtime_ns:
            self._mtime_ns = mtime
            with contextlib.suppress(yaml.YAMLError, OSError, UnicodeDecodeError):
                self._rules = parse_rules(
                    self.path.read_text(encoding="utf-8"), catalogue
                )
        return self._rules
