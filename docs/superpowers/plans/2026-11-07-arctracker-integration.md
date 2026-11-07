# ARC Tracker Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ARLO's generic popup advice with a personal verdict (Keep / Keep · building / Keep · low / Sell N / Recycle N / No rule / Unknown) computed from the owner's ARC Tracker data and their `rules.yaml`.

**Architecture:** A new `arc_helper.arctracker` package. A background `AdvisorService` thread fetches the public item catalogue (REST, daily) and the player's data (MCP, every 5 min). It caches both on disk and builds an immutable `Plan` (ingredient allocation done once per refresh). The scanner thread only calls `AdvisorService.lookup(ocr_name)`, which is an in-memory dictionary read. `OverlayWindow` gains a verdict layout. With no key or no data, the existing `items.db` path is used unchanged.

**Tech Stack:** Python 3.14, Tk, PyYAML, official `mcp` Python SDK (streamable HTTP), `requests` (already a dependency).

**Spec:** `docs/superpowers/specs/2026-11-07-arctracker-integration-design.md`

## Global Constraints

- Python `>=3.11,<3.15`. Windows code paths must keep working (no Linux-only imports at module top level in shared modules).
- No network or disk I/O on the scanner thread. `AdvisorService.lookup()` must take < 5 ms (tested).
- ARC Tracker is read-only: never call `preview_changes` / `apply_changes`.
- MCP endpoint `https://mcp.arctracker.io/mcp`, auth `Authorization: Bearer <ARCTRACKER_API_KEY>`. Free plan limit is 150 calls / 10 min, so one full refresh must stay ≤ 8 calls.
- Catalogue: `GET https://arctracker.io/api/items` (no auth), refreshed when the cache is older than 24 h.
- Row = 4 slots: `rows: N` → `N × 4 × stackSize`; `stacks: N` → `N × stackSize`. Unknown stack size → target unknown.
- Name matching: exact space/case-insensitive key first, then `difflib` cutoff **0.85**, and never across a different trailing Roman numeral. No substring (`LIKE`) matching.
- Verdict colours: Keep `#52cb89`, Keep · building `#f5d75e`, Keep · low `#f49b50`, Sell `#FFD700`, Recycle `#00CED1`, No rule / Unknown `#888888`.
- Category colours: weapons `#7dd3fc`, consumables `#60a5fa`, medical `#f9a8d4`, crafting `#e7c66b`, base `#53c8ae`, arc_rare `#bf9af7`.
- Sync age label `⟳ 12m` / `⟳ 10h` / `⟳ 3d` / `⟳ —`; orange `#f49b50` when older than **3 h**.
- Git: one feature branch `feat/arctracker`, commit after every task, push.

## Review Focus

1. **The owner owns an item the catalogue doesn't know** (newly added to the game): that item's verdict is Unknown, with no crash and no effect on other items. Test in Task 5.
2. **`rules.yaml` contains a typo'd id, a bad field, or invalid YAML:** the bad entry is skipped and logged, the rest still apply, and invalid YAML keeps the previous rules. Tests in Task 3.
3. **The API key is wrong, or the network drops mid-session:** the last snapshot is kept, the age keeps growing, and the service retries with backoff instead of every scan. Test in Task 6.
4. **An OCR read of one tier matches a neighbouring tier or the blueprint** ("RENEGADEIII" vs Renegade IV, "ANVILI" vs Anvil I Blueprint): it must resolve exactly or not at all. Test in Task 1.
5. **Hovering an item the owner has 0 of** (trader, in-raid loot): the verdict is still meaningful ("Keep · low", "Own 0 · min 15 · ideal 50"), and there are no divide-by-zero or negative counts. Test in Task 5.

---

## File Structure

```
src/arc_helper/arctracker/
  __init__.py        # re-exports AdvisorService, Verdict
  catalogue.py       # CatalogueItem, Catalogue, fetch/cache, name resolution, categories
  snapshot.py        # PlayerSnapshot, Need, parse_* from MCP JSON, cache, format_age
  mcp_client.py      # ArcTrackerClient: fetch raw tool JSON (paged)
  rules.py           # Rules, Target, LoadoutGroup, load_rules, RulesWatcher
  planner.py         # build_plan(): tiered allocation -> Plan
  verdict.py         # Verdict model + verdict_for(plan, item_id)
  service.py         # AdvisorService: background refresh, lookup()
src/arc_helper/overlay.py   # + VerdictView rendering inside OverlayWindow
src/arc_helper/main.py      # scanner uses AdvisorService, refresh on INVENTORY
src/arc_helper/config.py    # + ArcTracker settings
rules.yaml                  # owner's rules (committed)
tests/arctracker/           # unit tests + fixtures/
```

---

### Task 1: Settings, dependencies and the item catalogue

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `src/arc_helper/config.py`, `.env.example`, `.gitignore`
- Create: `src/arc_helper/arctracker/__init__.py`, `src/arc_helper/arctracker/catalogue.py`
- Create: `tests/arctracker/__init__.py`, `tests/arctracker/fixtures/catalogue_subset.json`, `tests/arctracker/test_catalogue.py`

**Interfaces:**
- Produces:
  - `Settings.arctracker_api_key: str | None` (env `ARCTRACKER_API_KEY`), `Settings.arctracker_mcp_url: str = "https://mcp.arctracker.io/mcp"`, `Settings.rules_path: Path = APP_DIR / "rules.yaml"`, `Settings.arctracker_cache_dir: Path = APP_DIR / "cache"`
  - `@dataclass(frozen=True) class CatalogueItem: id: str; name: str; type: str | None; category: str; rarity: str | None; value: int; stack_size: int | None; recipe: dict[str, int]; benches: tuple[str, ...]; station_level: int | None; recycles_into: dict[str, int]; upgrade_cost: dict[str, int]; upgrades_to: str | None; blueprint_locked: bool`
  - `class Catalogue: items: dict[str, CatalogueItem]; version: str`; `Catalogue.from_api_json(data: dict) -> Catalogue`; `Catalogue.resolve(ocr_text: str) -> str | None`; `Catalogue.weapon_chain(item_id: str) -> list[str]` (tier I → … → item_id via `upgrades_to`); `Catalogue.produced_by(ingredient_id: str) -> list[str]` (items whose `recipe` contains it)
  - `fetch_catalogue(cache_dir: Path, *, max_age_s: int = 86400, session=None) -> Catalogue | None`: uses `cache_dir/catalogue.json` when fresh, else downloads; on download failure returns the stale cache, or None.
  - `category_for(raw: dict) -> str`: one of `weapons, consumables, medical, crafting, base, arc_rare`

- [ ] **Step 1: Add dependencies and settings**

Run: `uv add "pyyaml>=6.0" "mcp>=1.10"`. Expected: lock updates and `uv run python -c "import yaml, mcp"` exits 0 on Python 3.14.
Add the four settings to `Settings` in `config.py` with the defaults above. Add `ARCTRACKER_API_KEY=` (commented explanation: "ARC Tracker → Settings → Developer Access") to `.env.example`. Add `cache/` to `.gitignore`.

- [ ] **Step 2: Create the fixture**

Generate `tests/arctracker/fixtures/catalogue_subset.json` from the live API, keeping the real records for these ids: `simple_gun_parts light_gun_parts medium_gun_parts heavy_gun_parts renegade_i renegade_ii renegade_iii renegade_iv advanced_mechanical_components mechanical_components oil metal_parts canister plastic_parts chemicals smoke_grenade herbal_bandage bombardier_cell arc_alloy wires anvil_i anvil_i_blueprint medium_shield light_ammo` plus one `type == "Key"` item of rarity `Common` and one of rarity `Epic`, and one `type == "Blueprint"`. Keep the top-level `version`, `generatedAt` and `itemCount` (set it to the subset length) keys.

```bash
curl -s https://arctracker.io/api/items | uv run python -c "import json,sys; d=json.load(sys.stdin); ids=set(sys.argv[1:]); L=d['items']; keep=[i for i in L if i['id'] in ids]; keep+= [next(i for i in L if i.get('type')=='Key' and i.get('rarity')==r) for r in ('Common','Epic')]; keep.append(next(i for i in L if i.get('type')=='Blueprint' and i['id'] not in ids)); json.dump({'version':d['version'],'generatedAt':d['generatedAt'],'itemCount':len(keep),'items':keep}, open('tests/arctracker/fixtures/catalogue_subset.json','w'))" simple_gun_parts light_gun_parts medium_gun_parts heavy_gun_parts renegade_i renegade_ii renegade_iii renegade_iv advanced_mechanical_components mechanical_components oil metal_parts canister plastic_parts chemicals smoke_grenade herbal_bandage bombardier_cell arc_alloy wires anvil_i anvil_i_blueprint medium_shield light_ammo
```

If an id doesn't exist, look up its real id in the full list and substitute it; note any substitution in the commit message.

- [ ] **Step 3: Write failing tests** in `tests/arctracker/test_catalogue.py` (load the fixture with `Catalogue.from_api_json`):

```python
def test_parses_recipe_and_stack(cat):
    lgp = cat.items["light_gun_parts"]
    assert lgp.recipe == {"simple_gun_parts": 4}
    assert lgp.stack_size == 5 and lgp.value == 700
    assert lgp.benches == ("refiner",) and lgp.station_level == 2
    assert lgp.blueprint_locked is True

def test_bench_list_normalised(cat):            # craftBench may be str or list
    assert all(isinstance(i.benches, tuple) for i in cat.items.values())

def test_resolve_exact_ocr_forms(cat):
    assert cat.resolve("CANISTER") == "canister"
    assert cat.resolve("LIGHTGUNPARTS") == "light_gun_parts"
    assert cat.resolve("RENEGADEIV") == "renegade_iv"

def test_resolve_never_crosses_tier_or_blueprint(cat):   # Review Focus 4
    assert cat.resolve("RENEGADEIII") == "renegade_iii"
    assert cat.resolve("RENEGADEVI") is None            # no tier VI
    assert cat.resolve("ANVILI") == "anvil_i"

def test_resolve_fuzzy_typo(cat):
    assert cat.resolve("CANISTFR") == "canister"
    assert cat.resolve("XQZWV") is None

def test_weapon_chain(cat):
    assert cat.weapon_chain("renegade_iv") == ["renegade_i", "renegade_ii", "renegade_iii", "renegade_iv"]

def test_produced_by(cat):
    assert set(cat.produced_by("simple_gun_parts")) >= {"light_gun_parts", "medium_gun_parts", "heavy_gun_parts"}

def test_categories(cat):
    assert cat.items["light_gun_parts"].category == "weapons"
    assert cat.items["renegade_iv"].category == "weapons"
    assert cat.items["herbal_bandage"].category == "medical"
    assert cat.items["smoke_grenade"].category == "consumables"
    assert cat.items["metal_parts"].category == "base"
    assert cat.items["canister"].category == "crafting"

def test_fetch_uses_fresh_cache_and_falls_back(tmp_path, monkeypatch): ...
    # fresh cache: no HTTP call made; stale cache + HTTP error: returns stale; nothing + error: None
```

- [ ] **Step 4: Run tests and confirm they fail**

Run: `uv run pytest tests/arctracker/test_catalogue.py -v`. Expected: ImportError / FAIL.

- [ ] **Step 5: Implement `catalogue.py`**

- Name: `raw["name"]["en"]`.
- Match key: reuse `arc_helper.database._match_key`.
- `resolve()`: exact key lookup first. If that fails, `difflib.get_close_matches(key, keys, n=3, cutoff=0.85)`, keeping the first candidate whose trailing Roman numeral (regex `(I{1,3}|IV|V)$` on the key's uppercase form, or none) equals the input's. If two catalogue items share a key, prefer the one whose type is not `Blueprint`.
- `category_for`, first rule that matches wins:
  - `mechanics.classification.category` starts with `Firearm.` or `Modification.`, or `type == "Ammunition"`, or `"gun_parts"` in the id → `weapons`
  - category `Utility.Regenerative` → `medical`
  - category starts with `Utility.`, or `Gadget` / `Augment`, or `type` in `{"Quick Use", "Shield", "Key", "Augment"}` → `consumables`
  - `CraftingMaterial.Basic` → `base`
  - id starts with `arc_`, or rarity in `{"Epic", "Legendary"}` → `arc_rare`
  - else → `crafting`
- `fetch_catalogue`: use `requests` with the same User-Agent style as `update_db.py` (`ARLO-AT/1.0`) and a 30 s timeout. Write the cache atomically (temp file + `os.replace`).

- [ ] **Step 6: Run tests and confirm they pass**

Run: `uv run pytest tests/arctracker/test_catalogue.py -v`. Expected: all PASS. Then `uv run pytest -q`: all existing tests still pass.

- [ ] **Step 7: Commit and push**

```bash
git add pyproject.toml uv.lock .env.example .gitignore src/arc_helper/config.py src/arc_helper/arctracker tests/arctracker
git commit -m "Add ARC Tracker item catalogue with cached fetch and OCR name resolution"
git push
```

---

### Task 2: Player snapshot model and MCP JSON parsing

**Files:**
- Create: `src/arc_helper/arctracker/snapshot.py`, `tests/arctracker/test_snapshot.py`, `tests/arctracker/fixtures/{inventory_p1,inventory_p2,needs_p1,needs_p2,tracking,progress_blueprints,progress_workshop}.json`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Need: item_id: str; needed: int; kind: str; label: str` (`kind` is one of `quest workshop outpost project goal stock research upgrade build`; `label` is the group name)
  - `@dataclass(frozen=True) class PlayerSnapshot: synced_at: datetime | None; fetched_at: datetime; owned: dict[str, int]; needs: tuple[Need, ...]; keep_amounts: dict[str, int]; learned_blueprints: frozenset[str]; blueprints_complete: bool; bench_levels: dict[str, int]`
  - `parse_inventory(pages: list[dict]) -> tuple[dict[str, int], datetime | None]`: sums `items[].count` and every list in `loadout.items.*` by id. Returns (owned, `readFromGame.at`).
  - `parse_needs(pages: list[dict]) -> tuple[Need, ...]`: one `Need` per group item with `needed > 0`, in page order.
  - `parse_tracking(data: dict) -> dict[str, int]`: keep amounts (entries of shape `keep`).
  - `parse_progress(blueprints: dict, workshop: dict) -> tuple[frozenset[str], bool, dict[str, int]]`: `blueprints_complete = not blueprints["blueprints"].get("namesTruncated", False)`.
  - `snapshot_to_json(s) -> dict` / `snapshot_from_json(d) -> PlayerSnapshot`; `save_snapshot(s, path)` / `load_snapshot(path) -> PlayerSnapshot | None` (atomic write; returns None on a missing or corrupt file)
  - `format_age(synced_at: datetime | None, now: datetime) -> tuple[str, bool]`: returns (label, stale).

- [ ] **Step 1: Write fixtures**

Trimmed copies of real MCP responses, in the shapes captured on 2026-11-07:
- `inventory_p1.json`: `readFromGame.at = "2026-11-07T05:23:00.117Z"`; items `light_gun_parts` count 21, `simple_gun_parts` count 40, `canister` count 10; `loadout.items.weapons` = `[{"id":"renegade_iv","count":1}]`; `loadout.items.backpack` = `[{"id":"light_ammo","count":100},{"id":"light_ammo","count":58}]`; `truncated: true`, `nextOffset: 3`.
- `inventory_p2.json`: items `metal_parts` count 40; `truncated: false`.
- `needs_p1.json`: a quest group "Tracks of the Beast" needing `skulker_targeter` ×1, and a workshop group "Gear Bench 3" needing `canister` ×10, with `more.nextOffset: 2`. `needs_p2.json`: a project group "Weather Monitor System" needing `canister` ×5.
- `tracking.json`: the real empty response (`entries: []`) plus one entry `{"shape":"keep","item":{"id":"oil"},"amount":20}`. Mirror the field names in the real `get_my_tracking` entry schema, checking one live entry if available. If the real shape differs, adapt the parser and the fixture together.
- `progress_blueprints.json`: `items` with `anvil_i` and `heavy_gun_parts`, `namesTruncated: true`. `progress_workshop.json`: modules `refiner` level 3 and `weapon_bench` level 3.

- [ ] **Step 2: Write failing tests**

```python
def test_inventory_sums_stash_and_loadout_across_pages(inv_pages):
    owned, at = parse_inventory(inv_pages)
    assert owned["light_gun_parts"] == 21 and owned["metal_parts"] == 40
    assert owned["renegade_iv"] == 1 and owned["light_ammo"] == 158
    assert at == datetime(2026, 11, 7, 5, 23, 0, 117000, tzinfo=UTC)

def test_needs_flattened_in_order(needs_pages):
    needs = parse_needs(needs_pages)
    assert [(n.item_id, n.needed, n.kind) for n in needs] == [
        ("skulker_targeter", 1, "quest"), ("canister", 10, "workshop"), ("canister", 5, "project")]
    assert needs[0].label == "Tracks of the Beast"

def test_progress_marks_truncated_blueprints(bp, ws):
    learned, complete, benches = parse_progress(bp, ws)
    assert "anvil_i" in learned and complete is False and benches["refiner"] == 3

def test_snapshot_roundtrip_and_corrupt_cache(tmp_path): ...  # save/load equal; garbage file -> None

@pytest.mark.parametrize("delta,label,stale", [
    (timedelta(minutes=12), "⟳ 12m", False), (timedelta(hours=10), "⟳ 10h", True),
    (timedelta(days=3), "⟳ 3d", True), (timedelta(seconds=20), "⟳ 0m", False)])
def test_format_age(delta, label, stale): ...
def test_format_age_none(): assert format_age(None, NOW) == ("⟳ —", False)
```

- [ ] **Step 3: Run tests and confirm they fail.** `uv run pytest tests/arctracker/test_snapshot.py -v` → FAIL.
- [ ] **Step 4: Implement `snapshot.py`.** Thresholds: `< 60 min → "{m}m"`, `< 48 h → "{h}h"`, else `"{d}d"`. Stale when older than 3 h.
- [ ] **Step 5: Run tests and confirm they pass.**
- [ ] **Step 6: Commit and push** (`"Add ARC Tracker player snapshot parsing and cache"`).

---

### Task 3: Rules file loading

**Files:**
- Create: `src/arc_helper/arctracker/rules.py`, `tests/arctracker/test_rules.py`

**Interfaces:**
- Consumes: `Catalogue`, `CatalogueItem.stack_size` (Task 1)
- Produces:
  - `@dataclass(frozen=True) class Target: min: int; ideal: int; tier: int`
  - `@dataclass(frozen=True) class LoadoutGroup: name: str; items: tuple[str, ...]; copies: int; tier: int` (`weapons` → tier 2, any other group → tier 3)
  - `@dataclass(frozen=True) class Rules: targets: dict[str, Target]; actions: dict[str, str]; notes: dict[str, str]; categories: dict[str, str]; loadout: tuple[LoadoutGroup, ...]; keep_key_rarities: frozenset[str]; errors: tuple[str, ...]`
  - `parse_rules(text: str, catalogue: Catalogue) -> Rules` (never raises on bad entries; collects `errors`)
  - `class RulesWatcher(path: Path)`: `.current(catalogue) -> Rules | None` reloads when mtime changes and keeps the previous `Rules` if the YAML fails to parse.

YAML schema (top-level keys other than `loadout` / `keys` are item ids):

| field | meaning |
|---|---|
| `min`, `ideal` | units; either alone sets both |
| `target: N` | min = ideal = N |
| `rows: N or [a,b]` | ×4×stack_size |
| `stacks: N or [a,b]` | ×stack_size |
| `tier: 4..6` | default 5 (general reserve); §6 consumables use 4 |
| `action: keep\|sell\|recycle\|buy_on_demand` | policy |
| `note: str` | free text, shown as the reason when it's the deciding rule |
| `category: weapons\|consumables\|medical\|crafting\|base\|arc_rare` | override |

`loadout: {<group>: {items: [...], copies: N}}`; `keys: {keep_rarities: [...]}`.

- [ ] **Step 1: Write failing tests**

```python
def test_units_conversion(cat):
    r = parse_rules("light_gun_parts: {min: 15, ideal: 50}\nsmoke_grenade: {rows: 1}\noil: {target: 3}", cat)
    assert r.targets["light_gun_parts"] == Target(15, 50, 5)
    assert r.targets["oil"] == Target(3, 3, 5)
    assert r.targets["smoke_grenade"].ideal == 4 * cat.items["smoke_grenade"].stack_size

def test_range_stacks(cat):
    r = parse_rules("canister: {stacks: [1, 2]}", cat)
    s = cat.items["canister"].stack_size
    assert r.targets["canister"] == Target(s, 2 * s, 5)

def test_unknown_id_and_bad_field_collected_not_fatal(cat):   # Review Focus 2
    r = parse_rules("not_an_item: {target: 5}\ncanister: {rows: banana}\noil: {target: 20}", cat)
    assert "oil" in r.targets and "canister" not in r.targets and len(r.errors) == 2

def test_actions_loadout_keys(cat):
    r = parse_rules("bombardier_cell: {action: recycle}\nloadout:\n  weapons: {items: [renegade_iv], copies: 1}\n  shields: {items: [medium_shield], copies: 0}\nkeys: {keep_rarities: [Rare, Epic]}", cat)
    assert r.actions["bombardier_cell"] == "recycle"
    assert r.loadout[0] == LoadoutGroup("weapons", ("renegade_iv",), 1, 2)
    assert r.loadout[1].tier == 3 and r.keep_key_rarities == {"Rare", "Epic"}

def test_watcher_keeps_previous_on_invalid_yaml(tmp_path, cat): ...  # write valid, load, write "a: [", touch mtime, current() still old
def test_watcher_reloads_on_change(tmp_path, cat): ...
```

- [ ] **Step 2: Run tests and confirm they fail.**
- [ ] **Step 3: Implement `rules.py`** with `yaml.safe_load`. A target whose stack size is unknown (needed for rows/stacks) goes to `errors` as "stack size unknown", never a guessed number.
- [ ] **Step 4: Run tests and confirm they pass.**
- [ ] **Step 5: Commit and push** (`"Add rules.yaml loader with unit conversion and hot reload"`).

---

### Task 4: Allocation planner

**Files:**
- Create: `src/arc_helper/arctracker/planner.py`, `tests/arctracker/test_planner.py`

**Interfaces:**
- Consumes: `Catalogue` (Task 1), `PlayerSnapshot`/`Need` (Task 2), `Rules`/`Target`/`LoadoutGroup` (Task 3)
- Produces:
  - `@dataclass(frozen=True) class TargetState: item_id: str; owned: int; min: int; ideal: int; tier: int; craft_inputs: dict[str, int]` (per-craft ingredient quantities; empty if not craftable). Properties: `.state -> "low"|"building"|"met"` and `.shortfall -> int` (to ideal, ≥ 0).
  - `@dataclass(frozen=True) class Plan: catalogue; snapshot; rules; owned: dict[str,int]; need_claims: dict[str,int]; need_reasons: dict[str, Need]` (the first need per item) `; targets: dict[str, TargetState]; claims: dict[str, dict[str, int]]` (ingredient → {claimant target id or "__need__" or "__self__": units}) `; demand: dict[str, dict[str, int]]` (ingredient → {target id: units needed to craft its shortfall, regardless of availability}) `; unclaimed: dict[str, int]`
  - `build_plan(catalogue: Catalogue, snapshot: PlayerSnapshot | None, rules: Rules) -> Plan`
  - `effective_target(rules, snapshot, item_id) -> Target | None`: the rules target with min/ideal raised to the ARC keep amount when that is higher (keep amount alone → `Target(k, k, 5)`).

Allocation algorithm, which the tests pin:

```
remaining = copy(owned)
# tier 1: ARC Tracker needs (sum per item, claim what is owned)
for item, need_total in summed needs: c = min(remaining[item], need_total); claim(item, "__need__", c)
# loadout groups become targets: weapon copies count across the weapon_chain tiers;
#   craft_inputs for a weapon = remaining upgrade costs from the best owned tier, else tier I recipe + all upgrade costs
# tiers 2..6 in order; within a tier, targets in rules-file order, two passes: pass "min" then pass "ideal"
for tier in 2..6:
  for level in ("min", "ideal"):
    for t in targets_in(tier):
      goal = t.min if level == "min" else t.ideal
      self_claim = min(remaining[t], goal - claimed_self(t)); claim(t, "__self__", self_claim)
      short = goal - (owned_self(t))   # owned units of t claimed for itself
      if short > 0 and t.craft_inputs:
        for ing, q in t.craft_inputs: demand[ing][t] = max(demand[ing][t], short*q)  # record need
                                      claim(ing, t, min(remaining[ing], short*q - claimed(ing,t)))
unclaimed = remaining
```

- [ ] **Step 1: Write failing tests** (build tiny snapshots and rules inline; use the Task 1 catalogue fixture)

```python
def test_need_claims_first(cat):            # canister: own 10, needs 10+5 -> all claimed by needs
    p = build_plan(cat, snap(owned={"canister": 10}, needs=[("canister",10,"workshop"),("canister",5,"project")]), rules(""))
    assert p.need_claims["canister"] == 10 and p.unclaimed["canister"] == 0

def test_ingredient_not_double_counted(cat):  # 40 simple; light short 29 (min 15 met), heavy short to min
    p = build_plan(cat, snap(owned={"simple_gun_parts":40,"light_gun_parts":21,"heavy_gun_parts":8}),
                   rules("light_gun_parts: {min: 15, ideal: 50}\nheavy_gun_parts: {min: 15, ideal: 50}"))
    claimed = sum(p.claims["simple_gun_parts"].values())
    assert claimed == 40                                   # never more than owned
    assert p.claims["simple_gun_parts"]["heavy_gun_parts"] == 28   # min pass first: (15-8)*4
    assert p.claims["simple_gun_parts"]["light_gun_parts"] == 12   # leftover goes to ideal pass, file order
    assert p.demand["simple_gun_parts"] == {"light_gun_parts": 116, "heavy_gun_parts": 168}

def test_higher_tier_wins(cat):             # loadout weapon (tier 2) claims before reserve (tier 5)
    ...  # renegade_iv copies 1, owned 0; medium_gun_parts reserve ideal 50; owned medium 3 -> weapon claims first

def test_lower_weapon_tier_counts_as_copy(cat):
    p = build_plan(cat, snap(owned={"renegade_iii":1}), rules("loadout:\n  weapons: {items: [renegade_iv], copies: 1}"))
    t = p.targets["renegade_iv"]
    assert t.owned == 1 and t.craft_inputs == cat.items["renegade_iii"].upgrade_cost

def test_keep_amount_raises_target(cat):    # ARC keep 20 > rules ideal 10
    ...

def test_no_snapshot_yields_empty_plan(cat):
    p = build_plan(cat, None, rules("oil: {target: 5}"))
    assert p.owned == {} and p.targets["oil"].owned == 0
```

- [ ] **Step 2: Run tests and confirm they fail.**
- [ ] **Step 3: Implement `planner.py`.**
- [ ] **Step 4: Run tests and confirm they pass.**
- [ ] **Step 5: Commit and push** (`"Add tiered allocation planner for needs, loadout and reserves"`).

---

### Task 5: Verdicts

**Files:**
- Create: `src/arc_helper/arctracker/verdict.py`, `tests/arctracker/test_verdict.py`

**Interfaces:**
- Consumes: `Plan`, `TargetState` (Task 4)
- Produces:
  - `class VerdictKind(Enum): KEEP, KEEP_BUILDING, KEEP_LOW, SELL, RECYCLE, NO_RULE, UNKNOWN`
  - `@dataclass(frozen=True) class CraftRow: label: str; right: str; state: str` (state: `low|building|met|covers|partial|none`)
  - `@dataclass(frozen=True) class Verdict: kind: VerdictKind; title: str; reason: str; counts: str; category: str; crafts: tuple[CraftRow, ...] = (); made_from: tuple[CraftRow, ...] = (); hint: str | None = None; hint_state: str | None = None`
  - `verdict_for(plan: Plan, item_id: str) -> Verdict`

Decision order, first match wins (spec §Verdict logic). Copy strings exactly:

| case | kind / title | reason | counts |
|---|---|---|---|
| id not in catalogue | UNKNOWN / `Unknown` | `Not in ARC Tracker data` | `` |
| ARC need, owned ≤ need total | KEEP / `Keep` | `{Kind} · {label} ×{needed}` (Kind: Quest, Workshop, Outpost, Project, Craft goal, Keep amount, Research, Upgrade, Build) | `Own {o} · need {n}` |
| loadout item, owned ≤ copies | KEEP / `Keep` | `Loadout · {copies} cop{y/ies}` | `Own {o} · keep {copies}` |
| own target, owned < min | KEEP_LOW / `Keep · low` | note or `Reserve target` | `Own {o} · min {m} · ideal {i}` (`· target {i}` when min == ideal) |
| own target, owned < ideal | KEEP_BUILDING / `Keep · building` | note or `Reserve target` | same |
| claimed as ingredient > 0 | KEEP_LOW if any claimant state is `low`, else KEEP_BUILDING / `Keep all` when claims ≥ owned, else `Keep {claimed}` | `Crafting ingredient` | `Own {o} · crafts need {sum demand}` |
| action keep | KEEP / `Keep` | note or `Rule · keep` | `Own {o}` |
| Key, rarity in keep set | KEEP / `Keep` | `Key · {rarity}` | `Own {o}` |
| Key, other rarity | SELL / `Sell {spare}` | `Key · use or sell` | `Own {o}` |
| spare = unclaimed > 0, type in {Blueprint, Modification, Augment, Shield} or category weapons with `isWeapon`, and no rule | NO_RULE / `No rule` | `No rule for this item` | `Own {o} · {value:,}₡ ea` |
| spare > 0 | SELL or RECYCLE (`action` forces; else recycle when Σ recycle-output values > value) / `Sell {spare}` / `Recycle {spare}` | `Above reserve` if target else `Rule · {action}` if action else `Surplus` | target: `Own {o} · ideal {i} · {value:,}₡ ea`; recycle: `→ ` + `, `.join(`{q}× {name}`) + ` each` |
| owned 0, nothing applies | SELL / `Sell` | `Surplus` | `Own 0 · {value:,}₡ ea` |
| everything claimed (spare 0) | KEEP / `Keep` | the top claimant's reason | `Own {o}` |

Crafts (ingredients): rows for the claimant targets in `plan.demand[item]` whose state ≠ met, sorted low→building and then by demand descending, max 3. `label = "{output name} ×{shortfall}"`, `right = str(demand units)`, `state` = target state.
Made from (only when the item's own target state ≠ met and `craft_inputs` exists): one row per ingredient, `label = "{name} ×{q}"`, `right = "own {owned} · {spare} spare"`, where spare = claims for this target + unclaimed. `craft_now = min(spare // q)`, capped at the shortfall. Row state: `covers` if craft_now ≥ shortfall, `partial` if > 0, else `none`.
Hint:
- `craft_now > 0` and craftable (bench level ≥ station level when known; blueprint learned, or not `blueprint_locked`) → `Craft {n} now → {owned+n} / {ideal}`, hint_state `partial` or `covers`.
- craft_now == 0 → `Farm {name of the ingredient with the least spare}`, hint_state `none`.
- Bench too low → `Needs {Bench name} {level}`.
- Blueprint locked and not in the learned set: if `blueprints_complete` → `Blueprint not learned`, else no hint.
- No progress data → no hint.

Bench display names: `refiner: Refiner, weapon_bench: Gunsmith, equipment_bench: Gear Bench, utility_bench: Utility Station, explosives_bench: Explosives Station, med_station: Medical Lab, workbench: Workbench`.

- [ ] **Step 1: Write failing tests**, one per table row plus these:

```python
def test_quest_protected_even_when_rule_says_recycle(cat):   # anti-goal: never sell quest items
    v = verdict_for(plan(owned={"bombardier_cell":2}, needs=[("bombardier_cell",3,"quest","Taking Stock")],
                         rules="bombardier_cell: {action: recycle}"), "bombardier_cell")
    assert v.kind is VerdictKind.KEEP and v.reason == "Quest · Taking Stock ×3"

def test_ingredient_crafts_rows(cat):  # mockup v4 case
    v = verdict_for(<simple 40, light 21 (15/50), heavy 8 (15/50)>, "simple_gun_parts")
    assert v.title == "Keep all" and v.kind is VerdictKind.KEEP_LOW
    assert [r.label for r in v.crafts] == ["Heavy Gun Parts ×42", "Light Gun Parts ×29"]
    assert v.counts == "Own 40 · crafts need 284"

def test_made_from_hidden_when_met(cat): ...   # light owned 50 -> made_from == ()
def test_made_from_and_hint(cat): ...          # light 21, 24 spare simple, refiner 3, blueprint learned -> "Craft 6 now → 27 / 50"
def test_hint_hidden_when_blueprint_unknown(cat): ...  # blueprints_complete False and not learned -> hint None
def test_zero_owned_is_meaningful(cat):        # Review Focus 5
    v = verdict_for(plan(owned={}, rules="light_gun_parts: {min: 15, ideal: 50}"), "light_gun_parts")
    assert v.kind is VerdictKind.KEEP_LOW and v.counts == "Own 0 · min 15 · ideal 50"
def test_unknown_owned_item_does_not_break_others(cat):   # Review Focus 1
    p = plan(owned={"brand_new_item": 3, "oil": 5}); assert verdict_for(p, "brand_new_item").kind is VerdictKind.UNKNOWN
    assert verdict_for(p, "oil").kind in (VerdictKind.SELL, VerdictKind.RECYCLE)
def test_sell_vs_recycle_by_value(cat): ...
```

Compute the expected numbers in the test from the fixture's real recipes. The 42 / 284 above assume heavy shortfall 50−8 and the 4:1 recipe. Correct them in the test if the fixture says otherwise.

- [ ] **Step 2: Run tests and confirm they fail.**
- [ ] **Step 3: Implement `verdict.py`.**
- [ ] **Step 4: Run tests and confirm they pass.**
- [ ] **Step 5: Commit and push** (`"Add personal verdicts with craft and made-from rows"`).

---

### Task 6: MCP client and AdvisorService

**Files:**
- Create: `src/arc_helper/arctracker/mcp_client.py`, `src/arc_helper/arctracker/service.py`, `tests/arctracker/test_service.py`, `tests/arctracker/test_mcp_live.py`
- Modify: `src/arc_helper/arctracker/__init__.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5
- Produces:
  - `class ArcTrackerClient(api_key: str, url: str)`: `fetch_raw() -> dict` returning `{"inventory": [pages], "needs": [pages], "tracking": dict, "progress_blueprints": dict, "progress_workshop": dict}`. It opens one MCP session per refresh (`mcp.client.streamable_http.streamablehttp_client(url, headers={"Authorization": f"Bearer {key}"})` + `ClientSession`), calls `get_my_inventory(limit=100, offset=…)` until `truncated` is false, `get_my_needs(limit=30, offset=…)` until `more` is absent, `get_my_tracking(limit=50)`, and `get_my_progress(section="blueprints")` / `(section="workshop")`, then parses `result.content[0].text` as JSON. That's ≤ 8 calls. It's a synchronous wrapper (`asyncio.run`), called only from the service thread. On an auth failure it raises `ArcTrackerAuthError`; on anything else, `ArcTrackerError`.
  - `class AdvisorService(settings, *, client=None, clock=time.time, catalogue_loader=fetch_catalogue)`: `start()`, `stop()`, `request_refresh()` (throttled to once per 60 s), `lookup(ocr_name: str) -> tuple[Verdict, str, bool] | None` (verdict, age label, stale; None when no catalogue or no snapshot ever loaded), `status() -> str`.
  - Refresh loop: refresh personal data on start, every 300 s, and on `request_refresh`; after a failure, back off 60 → 120 → 300 s and keep the last snapshot. On auth failure, log once and stop retrying until `ARCTRACKER_API_KEY` changes (restart). The catalogue is refreshed daily. Rules are re-checked via `RulesWatcher` every loop tick (1 s). The `Plan` is rebuilt whenever the catalogue, snapshot or rules change and swapped in as one attribute assignment.
  - Startup: load `cache/catalogue.json` and `cache/snapshot.json` first, so `lookup` works offline immediately.

- [ ] **Step 1: Write failing tests** with a fake client (no network):

```python
def test_lookup_uses_cached_snapshot_offline(tmp_path): ...  # pre-write cache files, client raises -> lookup works, age from cache
def test_keeps_last_snapshot_and_backs_off_on_error(tmp_path):     # Review Focus 3
    ...  # fake client: ok, then raises ArcTrackerError x3; assert fetch count over simulated 10 min <= 6 and lookup still returns
def test_auth_error_stops_retrying(tmp_path): ...
def test_request_refresh_throttled(tmp_path): ...       # 5 calls within 60 s -> 1 fetch
def test_rules_edit_rebuilds_plan(tmp_path): ...        # change rules.yaml target -> next lookup reflects it
def test_lookup_is_fast(tmp_path):
    ...  # 1000 lookups of known names < 5 s total (< 5 ms each)
def test_no_key_means_no_lookup(tmp_path): assert AdvisorService(settings_without_key).lookup("CANISTER") is None
```

`tests/arctracker/test_mcp_live.py`: `pytest.mark.skipif(not os.environ.get("ARCTRACKER_API_KEY"))`. Calls `fetch_raw()` once and asserts the inventory has a `readFromGame` key, then that `parse_inventory` returns a non-empty dict.

- [ ] **Step 2: Run tests and confirm they fail.**
- [ ] **Step 3: Implement `mcp_client.py` and `service.py`.** The service thread is a daemon named `arctracker-advisor`; `lookup` only reads `self._plan` and `self._verdict_cache`. Verdicts are memoised per item id and cleared on each plan swap.
- [ ] **Step 4: Run tests and confirm they pass**, including the full suite.
- [ ] **Step 5: Commit and push** (`"Add ARC Tracker MCP client and background advisor service"`).

---

### Task 7: Popup rendering and scanner wiring

**Files:**
- Modify: `src/arc_helper/overlay.py`, `src/arc_helper/main.py`
- Test: `tests/test_overlay_verdict.py`

**Interfaces:**
- Consumes: `AdvisorService.lookup`, `.request_refresh`, `.start/.stop` (Task 6); `Verdict`, `CraftRow`, `VerdictKind` (Task 5)
- Produces:
  - `OverlayWindow.show_verdict(item_name: str, verdict: Verdict, age: str, stale: bool, anchor: tuple[int,int,int,int] | None) -> None`, which uses the same visibility, timer and anchoring behaviour as `show()`.
  - `VERDICT_COLORS: dict[VerdictKind, str]`, `CATEGORY_COLORS: dict[str, str]`, `PIP_COLORS = {"low": "#f49b50", "building": "#f5d75e", "met": "#52cb89", "covers": "#52cb89", "partial": "#f5d75e", "none": "#f49b50"}`

Layout (spec §Popup content): 5 px top bar in the verdict colour. Row 1 is the title (the action label's font) left and the age (info font, `#7d7d99`, or `#f49b50` when stale) right. Row 2 is the reason in the category colour, bold. Row 3 is the counts (`#c8c8dc`). Then an optional separator with craft rows (4 px pip + label left, right text right-aligned in `#9999bb`). Then an optional `MADE FROM` small caps label with its rows and the hint line in its pip colour. Hidden sections are `pack_forget`-ed. The item name is not shown.

Scanner wiring in `main.py`:
- `Application.__init__` creates `AdvisorService(get_settings())` and passes it to `Scanner(advisor=…)`. `run()` calls `advisor.start()`; `quit()` calls `advisor.stop()`.
- `Scanner._handle_detected_item`: `res = self.advisor.lookup(item_name)`. If `res` is set, queue `("verdict", item_name, verdict, age, stale, anchor)`. Otherwise keep the existing `db.lookup` path and queue `("overlay", …)` as today, and the old layout shows a small `⟳ —` age in its top-right corner. Unknown items still go through `db.log_missing_item`.
- When the state changes to ACTIVE from the INVENTORY trigger: `self.advisor.request_refresh()`.
- `_pump_ui_queue` handles `"verdict"` → `overlay.show_verdict(...)`.
- In frame mode, re-showing the same item every scan (existing behaviour) must reuse the memoised verdict (no re-render if unchanged).

- [ ] **Step 1: Write failing tests** (skip the module when `tkinter.Tk()` fails, i.e. no display):

```python
def test_renders_verdict_sections(root):
    ov = OverlayWindow(root)
    v = Verdict(VerdictKind.KEEP_LOW, "Keep all", "Crafting ingredient", "Own 40 · crafts need 284", "weapons",
                crafts=(CraftRow("Heavy Gun Parts ×42", "168", "low"),))
    ov.show_verdict("SIMPLEGUNPARTS", v, "⟳ 12m", False, None)
    assert ov.title_label.cget("text") == "Keep all"
    assert ov.title_label.cget("fg").lower() == "#f49b50"
    assert ov.reason_label.cget("fg").lower() == "#7dd3fc"
    assert ov.age_label.cget("text") == "⟳ 12m"
    assert ov.crafts_frame.winfo_manager() == "pack" and ov.made_from_frame.winfo_manager() == ""

def test_stale_age_orange(root): ...
def test_fallback_show_has_age_dash(root): ...   # show(name, Item(...)) -> age label "⟳ —"
```

- [ ] **Step 2: Run tests and confirm they fail.**
- [ ] **Step 3: Implement the rendering and wiring.**
- [ ] **Step 4: Run tests and confirm they pass**, then run the full suite and `uv run ruff check src tests` (no new findings beyond the 9 pre-existing ones).
- [ ] **Step 5: Offline smoke check.** Set `ARCTRACKER_API_KEY=` empty and run `timeout -s INT 10 uv run arc-helper`. Expected: it starts, logs `ARC Tracker: no API key; using generic item database`, and doesn't crash.
- [ ] **Step 6: Commit and push** (`"Show personal verdict popup and wire advisor into scanner"`).

---

### Task 8: Owner's initial rules.yaml

**Files:**
- Create: `rules.yaml`, `tests/arctracker/test_owner_rules.py`

**Interfaces:**
- Consumes: `parse_rules` (Task 3), the full live catalogue (cached in `cache/catalogue.json` by Task 6, or fetched in the test with skip-if-offline)

- [ ] **Step 1: Translate `gpt-plan/stash-audit-rules.md` §§3–9 into `rules.yaml`.** Use real catalogue ids and keep one comment per section header citing the ruleset section. Mappings:
  - §5 gun parts → `{min: 15, ideal: 50}`.
  - §6 consumables → `tier: 4`. "N" → `target: N`, "a–b" → `min: a, ideal: b`, "1 row" → `rows: 1`, "1–2 stacks" → `stacks: [1, 2]`.
  - "Preferred …", "Retain", "As needed", "Planned crafting", "Reserve for planned modifications" → `action: keep` with the phrase as `note`.
  - "Buy on demand", "Purchase on demand" → `action: buy_on_demand`.
  - "Recycle unless required" → `action: recycle`. "Sell or refine …" → `action: sell` with the note.
  - §8 base materials → `target: 100`.
  - `loadout`: weapons `[renegade_iv, anvil_iv, il_toro_iv, stitcher_iv]` copies 1 (placeholder, flagged in a comment), shields `[medium_shield]` copies 0, augments `[tactical_mk3_smoke, combat_mk3_flanking]` copies 0.
  - `keys: {keep_rarities: [Rare, Epic, Legendary]}`.
  - Group names like "Exodus Materials", "Base materials" or "Other healing" that map to several items: list each real item. If unsure of membership, leave it out and list it in the commit message for the owner.
- [ ] **Step 2: Write the test:** `parse_rules(Path("rules.yaml").read_text(), live_catalogue).errors == ()`, and spot checks: `targets["light_gun_parts"] == Target(15, 50, 5)`; `targets["smoke_grenade"].ideal == 20`.
- [ ] **Step 3: Run it.** `uv run pytest tests/arctracker/test_owner_rules.py -v` → PASS (skips when offline with no cache).
- [ ] **Step 4: Commit and push** (`"Add owner's rules.yaml translated from stash audit ruleset"`).
- [ ] **Step 5: Owner review.** Show the owner the file grouped by section, with the flagged placeholders and uncertain group memberships, and apply their corrections. This blocks Task 9.

---

### Task 9: Live verification with the owner

- [ ] **Step 1:** The owner creates a personal API key (ARC Tracker → Settings → Developer Access, read scopes) and adds `ARCTRACKER_API_KEY=…` to `.env`.
- [ ] **Step 2:** Run `ARCTRACKER_API_KEY=… uv run pytest tests/arctracker/test_mcp_live.py -v` → PASS.
- [ ] **Step 3:** The owner runs `uv run arc-helper` in-game and hovers: a quest item, a gun part below ideal, Simple Gun Parts, a surplus material, a key, and an item not in the catalogue. Compare each popup against the spec table. Log mismatches as fixes on this branch.
- [ ] **Step 4:** Update `README.md` (setup: API key, `rules.yaml`, popup legend) and `docs/CONFIGURATION.md` (new settings). Commit, push, and open a PR from `feat/arctracker` into `master`.
