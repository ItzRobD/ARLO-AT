"""ARC Tracker item catalogue: parsing, OCR name resolution and cached fetch."""

from __future__ import annotations

import contextlib
import difflib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

import requests

from arc_helper.database import _match_key

logger = logging.getLogger(__name__)

API_URL = "https://arctracker.io/api/items"
USER_AGENT = "ARLO-AT/1.0"

# Trailing run of roman-numeral letters on an upper-cased match key. Taking the
# whole run (not just a valid numeral) keeps a bogus "VI" distinct from "I".
_TIER_RE = re.compile(r"[IVX]+$")


@dataclass(frozen=True)
class CatalogueItem:
    id: str
    name: str
    type: str | None
    category: str
    rarity: str | None
    value: int
    stack_size: int | None
    recipe: dict[str, int]
    benches: tuple[str, ...]
    station_level: int | None
    recycles_into: dict[str, int]
    upgrade_cost: dict[str, int]
    upgrades_to: str | None
    blueprint_locked: bool
    is_weapon: bool = False  # classification Firearm.*
    craft_quantity: int = 1  # units one craft yields (ammo: 5..25)


def _classification(raw: dict[str, Any]) -> str:
    cls = (raw.get("mechanics") or {}).get("classification") or {}
    return cls.get("category") or ""


def category_for(raw: dict[str, Any]) -> str:
    """Map a raw API item to one of our six verdict categories."""
    cat = _classification(raw)
    typ = raw.get("type")
    item_id = raw.get("id") or ""
    if (
        cat.startswith(("Firearm.", "Modification."))
        or typ == "Ammunition"
        or "gun_parts" in item_id
    ):
        return "weapons"
    if cat == "Utility.Regenerative":
        return "medical"
    if cat.startswith(("Utility.", "Gadget", "Augment")) or typ in {
        "Quick Use",
        "Shield",
        "Key",
        "Augment",
    }:
        return "consumables"
    if cat == "CraftingMaterial.Basic":
        return "base"
    if item_id.startswith("arc_") or raw.get("rarity") in {"Epic", "Legendary"}:
        return "arc_rare"
    return "crafting"


def _benches(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _parse_item(raw: dict[str, Any]) -> CatalogueItem:
    upgrade_cost = raw.get("upgradeCost")
    if not upgrade_cost and raw.get("upgrades"):
        upgrade_cost = raw["upgrades"][0].get("cost")
    names = raw.get("name") or {}
    name = names.get("en") if isinstance(names, dict) else str(names)
    return CatalogueItem(
        id=raw["id"],
        name=name or raw["id"],
        type=raw.get("type"),
        category=category_for(raw),
        rarity=raw.get("rarity"),
        value=int(raw.get("value") or 0),
        stack_size=raw.get("stackSize"),
        recipe=dict(raw.get("recipe") or {}),
        benches=_benches(raw.get("craftBench")),
        station_level=raw.get("stationLevelRequired"),
        recycles_into=dict(raw.get("recyclesInto") or {}),
        upgrade_cost=dict(upgrade_cost or {}),
        upgrades_to=raw.get("upgradesTo"),
        blueprint_locked=bool(raw.get("blueprintLocked")),
        is_weapon=_classification(raw).startswith("Firearm."),
        craft_quantity=max(1, int(raw.get("craftQuantity") or 1)),
    )


def _tier(key: str) -> str:
    m = _TIER_RE.search(key.upper())
    return m.group(0) if m else ""


@dataclass
class Catalogue:
    items: dict[str, CatalogueItem]
    version: str = ""
    _keys: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # match key -> item id; on a collision prefer the non-Blueprint item
        keys: dict[str, str] = {}
        for item in self.items.values():
            key = _match_key(item.name)
            if not key:
                continue
            prev = self.items.get(keys.get(key, ""))
            if prev is None or (prev.type == "Blueprint" and item.type != "Blueprint"):
                keys[key] = item.id
        self._keys = keys

    @classmethod
    def from_api_json(cls, data: dict[str, Any]) -> Catalogue:
        items = {}
        for raw in data.get("items", []):
            if raw.get("id"):
                items[raw["id"]] = _parse_item(raw)
        return cls(items=items, version=str(data.get("version", "")))

    def resolve(self, ocr_text: str) -> str | None:
        """Resolve OCR'd tooltip text to an item id, never crossing tiers."""
        key = _match_key(ocr_text)
        if not key:
            return None
        if key in self._keys:
            return self._keys[key]
        want = _tier(key)
        for cand in difflib.get_close_matches(key, self._keys, n=3, cutoff=0.85):
            if _tier(cand) == want:
                return self._keys[cand]
        return None

    def weapon_chain(self, item_id: str) -> list[str]:
        """Tier I -> ... -> item_id following `upgrades_to` links backwards."""
        prev = {i.upgrades_to: i.id for i in self.items.values() if i.upgrades_to}
        chain = [item_id]
        seen = {item_id}
        while chain[0] in prev and prev[chain[0]] not in seen:
            chain.insert(0, prev[chain[0]])
            seen.add(chain[0])
        return chain

    def produced_by(self, ingredient_id: str) -> list[str]:
        """Ids of items whose crafting recipe contains the ingredient."""
        return [i.id for i in self.items.values() if ingredient_id in i.recipe]


def _load(path: Path) -> Catalogue | None:
    try:
        return Catalogue.from_api_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("Unreadable catalogue cache %s", path, exc_info=True)
        return None


def _downloaded(data: dict[str, Any]) -> Catalogue:
    """Parse a download; one with no items counts as failed (cache kept)."""
    catalogue = Catalogue.from_api_json(data)
    if not catalogue.items:
        msg = "catalogue download has no items"
        raise ValueError(msg)
    return catalogue


def fetch_catalogue(
    cache_dir: Path,
    *,
    max_age_s: int = 86400,
    session: Any = None,
    current: tuple[Catalogue, int] | None = None,
) -> Catalogue | None:
    """Return the catalogue from a fresh cache, else download it.

    ``current`` is (catalogue, cache mtime_ns it was read from): while the
    cache is fresh and unchanged it is returned as is, without re-parsing.
    A download with no items counts as failed. On download failure falls back
    to the stale cache, or None.
    """
    path = cache_dir / "catalogue.json"
    cached = path.exists()
    if cached and time.time() - path.stat().st_mtime < max_age_s:
        if current is not None and path.stat().st_mtime_ns == current[1]:
            return current[0]
        fresh = _load(path)
        if fresh is not None:
            return fresh
    try:
        if session is None:
            session = requests
        resp = session.get(API_URL, timeout=30, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        catalogue = _downloaded(data)
        cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            Path(tmp).replace(path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise
    except (requests.RequestException, ValueError, KeyError, TypeError, OSError):
        logger.warning("ARC Tracker catalogue download failed", exc_info=True)
        return _load(path) if cached else None
    return catalogue
