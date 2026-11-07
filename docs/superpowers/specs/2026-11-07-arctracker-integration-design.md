# ARC Tracker Integration — Personal Keep/Sell/Recycle Popup

Date: 2026-11-07
Status: draft for approval
Branch: `feat/arctracker` (off `feat/overlay-polish`)

## Intent

ARLO's popup currently gives generic, everyone-gets-the-same advice from a
scraped CSV (`items.csv` → `items.db`). It told the owner to recycle the
Renegade IV and Stitcher IV in their own loadout, and its prices are stale
(Canister shows 300; the game says 3,000).

The owner is a PvP player who keeps a personal stash ruleset
(`gpt-plan/stash-audit-rules.md`). When they hover an item, they want a
one-second answer to **"do I need this, and why?"** — needed for quests,
crafting or upgrades, or within the reserve thresholds they keep as a PvP
player — and otherwise whether it is surplus to sell or recycle.

ARC Tracker (arctracker.io) already knows their stash (synced from the game
by the ARC Tracker Link app they launch with the game) and their open
quests, workshop levels and projects. This project makes the popup
personal by combining that data with the owner's own rules.

## Goals

1. Personal verdict per hovered item: Keep / Keep · building / Keep · low /
   Sell N / Recycle N / Unknown, with a one-line reason and counts.
2. Protection order (from the ruleset): active quests → projects, research
   and workshop → preferred-loadout crafting → reserve targets → policy
   rules → surplus.
3. Crafting awareness in both directions, one level deep:
   - on an **ingredient**: what it crafts into that is below target, and
     how many of this ingredient that takes;
   - on a **craftable item below target**: what it is made from and how
     much of the shortfall can be crafted now.
4. Owner's reserve targets and rules live in an editable `rules.yaml`.
5. Stash age shown on every popup; data refreshes while playing.
6. Works offline from a local cache; degrades to today's generic advice
   with no key or no data.

## Non-goals

- The full chatbot audit report from the ruleset (sections 11–14: daily
  trader checklist, Scrappy feeding, farming routes, six-section report,
  multi-step crafting plans). Possibly a separate command later.
- Writing anything to ARC Tracker. Its MCP server and REST API cannot set
  keep amounts or tracked items (verified 2026-11-07), and the owner
  doesn't want to depend on the ARC Tracker developers adding it.
- Credits, wallet or financial tracking (excluded by the ruleset).
- Multi-level crafting chains in the popup (only one level each way).
- Outpost weapon racks or other storage ARC Tracker doesn't report.

## Anti-goals (a failure even if it "works")

- Telling the owner to sell or recycle something an **active quest** needs.
  Quest protection is absolute (ruleset §2). When in doubt, keep.
- Counting the same ingredient for two recipes (ruleset §4: "never
  allocate the same ingredient to multiple recipes").
- Counting trader stock or purchases as owned (ruleset §11).
- A verbose popup. The owner wants "a small amount of at-a-glance data".
- Stating a count or recipe that wasn't verified from data; show unknown
  instead (ruleset §15).
- Noticeable game performance impact or input delay (current popup latency
  is ~0.2–0.3 s after the tooltip settles; don't regress it).

## Constraints and current state

- Python 3.14 (system interpreter; uv `python-preference = "system"`),
  Tk overlay, Linux KDE Wayland is the owner's platform. Windows support in
  the codebase must not break.
- The scanner thread produces an OCR'd item name (e.g. `"CANISTER"`,
  `"HULLCRACKERIV"` — spaces are dropped, uppercase only). Today
  `Database.lookup()` maps it to an `Item` (fuzzy matching) and
  `OverlayWindow.show(name, item, anchor)` renders it. The new verdict
  replaces what `show()` renders; tooltip detection, OCR and positioning
  stay as they are.
- Popup lookups happen on the scanner thread at ~10 Hz; per-hover work
  must be in-memory (no network, no disk) — target well under 5 ms.
- ARC Tracker free plan: MCP 150 tool calls / 10 min per account.
- Per-project git rule: a branch per feature/fix; commit and push freely.

## Decisions made with the owner

### Source of truth split

- **ARC Tracker = facts**, read-only: stash counts and their sync time,
  what open quests / workshop levels / projects / tracked research still
  need, keep amounts and craft goals if the owner ever sets them, and the
  game item catalogue.
- **`rules.yaml` in ARLO = the owner's policy**: every reserve target and
  non-numeric rule from the ruleset. It is the *only* place targets live.
  The owner changes it by asking Claude/GPT, which edits the file.
- ARC Tracker **keep amounts** (none set today) are honoured as an extra
  floor: effective target = max(rules.yaml target, keep amount). ARLO never
  writes them.
- `gpt-plan/stash-audit-rules.md` stays as the human-readable rationale.

### Rules file: YAML

Chosen over JSON (no comments) and a Markdown table (rules have different
shapes; fragile to parse). Keyed by ARC Tracker item id. Each entry uses
only the fields it needs. Illustrative shape (final schema is the
builder's, but must express all of these):

```yaml
# Gun parts: never below 15, aim for 50
light_gun_parts:   { min: 15, ideal: 50 }
smoke_grenade:     { rows: 1 }              # rows × 4 slots × stack size
complex_gun_parts: { stacks: [2, 4] }       # range: min 2 stacks, ideal 4
wolfpack:          { rows: 1 }
bombardier_cell:   { action: recycle, unless: needed }
light_ammo:        { action: buy_on_demand }
herbal_bandage:    { note: "preferred efficient healing" }

hand_drill:        { action: keep }          # "retain for Anvil crafting"

loadout:            # ruleset §3, one group per item type
  weapons:
    items: [renegade_iv, anvil_iv, il_toro_iv, stitcher_iv]
    copies: 1       # keep N of each; below N protects their ingredients
  shields:
    items: [medium_shield]
    copies: 0       # craft on demand (owner's usual preference)
  augments:
    items: [tactical_mk3_smoke, combat_mk3_flanking]
    copies: 0

keys:               # ruleset §9
  keep_rarities: [Rare, Epic, Legendary]   # blue, purple, yellow
```

Unit conversion (ruleset §7): a **row = 4 slots**, so
`rows: N` → `N × 4 × stackSize` units; `stacks: N` → `N × stackSize`.
Stack sizes come from ARC Tracker's catalogue. If a stack size is unknown,
the target is unknown (not guessed).

Ranges: the lower bound is `min`, the upper is `ideal`. A single number is
both min and ideal.

The first `rules.yaml` is a translation of every rule in ruleset §§3–9 that
applies to a single item. The owner reviews it before it is used.

### Verdict logic

For the hovered item, evaluated with ingredient allocation done once per
data refresh (see below), not per hover:

1. **Protected need** (quests, workshop, projects, tracked research/
   upgrades/builds, craft goals — everything ARC Tracker's needs report
   gives): if `owned ≤ needed` → **Keep**, reason e.g.
   `Quest · Tick the Box ×6`. Show the highest-priority reason first.
2. **Loadout** (owner decision): `rules.yaml` has a loadout group per item
   type (weapons, shields, augments, …), each with a preferred list and a
   `copies: N` threshold. Shields and augments usually use `copies: 0`
   (craft on demand). A loadout item is a reserve target of N copies of
   itself: hovering it shows Keep while owned ≤ N. Below N, the
   ingredients to craft the missing copies are protected and appear in
   the Craft / Made from sections like any other short target.
   *Claude's call:* owned copies = stash + current loadout; a lower tier
   of a preferred weapon counts as a copy, and its remaining
   `upgradeCost` to the preferred tier is the ingredient demand (ruleset
   §3 "prefer upgrading"); a missing copy needs tier I `recipe` plus each
   tier's `upgradeCost`.
3. **Reserve target** (rules.yaml, max'd with keep amount):
   - `owned < min` → **Keep · low**
   - `min ≤ owned < ideal` → **Keep · building**
   - `owned ≥ ideal` → only the excess above ideal is surplus.
4. **Ingredient for a short target**: if the item is an ingredient of
   something whose target is unmet, the units needed to craft that
   shortfall are protected (verdict follows that target's state).
5. **Policy rule** (`action:` in rules.yaml): e.g. recycle/sell unless
   needed, buy on demand, key rarity rules.
6. **Surplus**: the unprotected amount → **Sell N** or **Recycle N**,
   whichever ARC Tracker data says is worth more (compare sell value vs
   value of recycle outputs).

**No double counting.** Each owned unit is claimed once, following the
ruleset's §4 tiers (*Claude's call*, from the ruleset): (1) ARC Tracker
needs — quests, then projects/research/workshop; (2) loadout weapons;
(3) loadout shields/augments; (4) PvP consumables (§6 items);
(5) general material reserves; (6) everything else. Within a tier, an
item's own reserve and the ingredient demand of short targets in that tier
compete; ingredients are claimed first up to each output's **min**, and
only then toward **ideal** from what is left (ruleset §4: "do not exhaust
scarce materials merely to reach an ideal target"). An ingredient claimed
by a higher tier shows as not spare to lower ones. ARC Tracker's needs
report already counts each owned item once across its needs; ARLO
allocates only what remains.

**Items with no numeric rule** (*Claude's call*):
- Ruleset lines that say keep without a number ("Retain", "As needed",
  "Planned crafting", "Reserve for planned modifications", "Preferred …
  healing") become `action: keep` in the first `rules.yaml` → verdict
  **Keep** (all), reason from the rule's note. Errs on retaining (§2).
- Items the ruleset never mentions: protected needs still apply; the rest
  is surplus (Sell/Recycle), as agreed with the owner.
- Item classes ARC Tracker never computes surplus for (blueprints, gear,
  modifications) with no rule and no need: neutral verdict **No rule**
  (Unknown grey bar) with sell value only — never Sell/Recycle by default.
- Keys: `keep_rarities` keys → Keep (all); other keys → **Sell**, reason
  "use or sell" (ruleset §9).

**Crafting hints are gated** (*Claude's call*, ruleset §4/§15): protection
of ingredients does not depend on whether the owner can craft the output,
but `Craft N now` is shown only when the blueprint is known and the bench
level is met (via MCP `get_my_progress`); otherwise the hint reads e.g.
`Needs Refiner 2` or `Blueprint not learned`. If progress data is missing,
show no craft-now hint.

**In-raid context** (owner decision): the in-raid INVENTORY trigger
(trigger region 2) marks raid mode. The verdict answers "is this worth
bringing home?" using stash counts. The counts line adds what the last
sync saw in the backpack: `Own 21 · +3 carried`. Items picked up during
the raid are not known (ARC Tracker reads the loadout from the game, normally
before the raid); the owner accounts for those mentally. Sell/Recycle N
in raid refers to the stash surplus.

### Popup content (approved mockup v5, with the change below)

No item name (the game tooltip already shows it). Top to bottom:

- **Top bar** (5 px) in the verdict colour:
  Keep `#52cb89`, Keep · building `#f5d75e`, Keep · low `#f49b50`,
  Sell `#FFD700`, Recycle `#00CED1`, Unknown `#888888`.
- **Verdict** (large, verdict colour) with **sync age** small in the
  top-right corner: `⟳ 12m`, `⟳ 10h`, `⟳ —` (no personal data). Orange
  (`#f49b50`) when stale.
- **Reason line** in the item's **category colour** from ruleset §14:
  Weapons & Gun Parts `#7dd3fc`, Consumables & Utility `#60a5fa`,
  Medical & Healing `#f9a8d4`, Crafting Materials `#e7c66b`,
  Base Components `#53c8ae`, ARC & Rare Materials `#bf9af7`.
- **Counts line**: e.g. `Own 21 · min 15 · ideal 50`, `Own 20 · 14 spare`,
  `Own 13 · ideal 8 · 3,000₡ ea`.
- **Craft section** (ingredients only, when they feed an unmet target):
  up to 3 rows `Light Gun Parts ×29 … 116` — output item, its shortfall,
  units of this ingredient needed. A small pip per row in that target's
  state colour (orange below min, yellow building, green met).
- **Made from section** (craftable items **below target only** — omitted
  when the target is met): each ingredient with per-craft quantity, owned
  and spare (`own 40 · 24 spare`), pip green/yellow/orange for
  covers/partly/doesn't cover the shortfall, and one hint line:
  `Craft 6 now → 27 / 50` or `Farm Simple Gun Parts`.

Mockups: `.superpowers/brainstorm/40717-1794066306/content/`
(`popup-layout-v3.html`, `popup-crafting-v4.html`, `popup-upstream-v5.html`).
Existing `OVERLAY_SCALE`, font selection and tooltip anchoring apply.

### Staleness

Showing the age is enough; minute-accurate counts are not required. Data
refreshes in the background while playing. The verdict is still shown when
stale; only the age turns orange.

## Data sources (verified 2026-11-07)

- **Catalogue — public REST, no auth**: `GET https://arctracker.io/api/items`
  (~4 MB, 992 items, has `version`/`generatedAt`). Per item: `id`,
  localized `name`, `type`, `rarity`, `value` (sell), `stackSize`,
  `recipe` (`{"simple_gun_parts": 4}`), `craftBench`,
  `stationLevelRequired`, `recyclesInto`, `upgradeCost`/`upgradesTo`,
  `vendors`, `mechanics.classification.category`, `questItem`.
  Replaces MetaForge/wiki as the item database and fixes stale prices.
- **Personal data — ARC Tracker MCP** at `https://mcp.arctracker.io/mcp`,
  authenticated with a **personal API key as a Bearer token** (created in
  ARC Tracker Settings → Developer Access; stored as `ARCTRACKER_API_KEY`
  in `.env`, which is git-ignored):
  - `get_my_inventory` — counts per item id, `readFromGame.at` (sync
    time), per-item `surplus` and sell/recycle `way` hints, loadout.
    Paged, max 100 per page (owner has 158 distinct items).
  - `get_my_needs` — still-needed counts grouped by quest / workshop /
    outpost / project / goal / stock / research / upgrade / build.
  - `get_my_tracking` — keep amounts and craft goals (owner has none yet).
  - `get_my_progress` — known blueprints and workshop levels, for gating
    craft-now hints.
  ARC Tracker's own `surplus` must not be used as the verdict by itself
  (ruleset §13: it ignores personal reserve targets); it can be used as a
  cross-check.
- The REST user endpoints (`/api/v2/user/stash` etc.) need a registered
  app key plus a user key; not used.

## Behaviour details decided as defaults (owner may override)

These are Claude's calls, stated to the owner, not owner decisions:

- **MCP budget**: a full refresh is ~5–6 calls (inventory pages, needs
  pages, tracking, progress); at the cadence below that is far under the
  free 150 / 10 min.
- **Refresh**: personal data on startup, every 5 minutes, and when the
  INVENTORY screen is first detected (throttled to once per minute). The
  catalogue once per 24 h (replacing the current MetaForge auto-update).
  All network work off the scanner thread; the scanner reads an immutable
  snapshot that is swapped atomically.
- **Cache**: last catalogue and personal snapshot stored locally (e.g.
  under the app dir, git-ignored) so a restart works offline and still
  shows the real sync age.
- **Fallback**: no key, or no snapshot ever fetched → current generic
  `items.db` behaviour with `⟳ —` in the corner. Network failures keep the
  last snapshot.
- **Name matching**: OCR text → catalogue id by the same space- and
  case-insensitive key the current database uses
  (`_match_key`), built from English names. No `LIKE '%…%'`
  substring fallback (it can map a garbled read to the wrong item); keep a
  conservative fuzzy fallback (e.g. difflib ≥ 0.85) and show Unknown
  otherwise.
- **Rules reload**: `rules.yaml` is re-read when its modification time
  changes, so edits apply without restarting ARLO. Invalid YAML keeps the
  previous rules and logs the error.
- **Category colour**: derived from catalogue `type` /
  `mechanics.classification.category` with an override field in
  `rules.yaml` for items that land in the wrong bucket.

## Left to the builder

- Module layout, MCP client library (e.g. official `mcp` Python SDK over
  streamable HTTP), cache file format.
- Exact `rules.yaml` schema beyond the shapes above, and validation.
- Allocation algorithm details within the stated priority order.
- How sell vs recycle value is computed when recycle outputs have no value.
- Tests: verdict logic must be unit-tested with fixture snapshots
  (quest-protected, below min, building, above ideal, ingredient demand,
  shared ingredient not double-counted, stale, no data).
- Migration path for `update_db.py`, `items.csv`/`items.db` and the
  weekly GitHub Action (keep generic fallback working).
