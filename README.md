# ARLO - Arc Raiders Loot Overlay

> **A big thanks to [Pabosik](https://github.com/Pabosik) for creating the original [PabsArcTooltip](https://github.com/Pabosik/PabsArcTooltip) that this project is built on. The core idea - an OCR-based screen overlay that reads item tooltips and tells you what to do with them - is entirely his. ARLO just builds on that foundation with some extra features.**

A screen overlay tool for [Arc Raiders](https://store.steampowered.com/app/2073750/ARC_Raiders/) that detects items in your inventory and displays recommended actions (**keep**, **recycle**, **sell**) along with sell prices, stack sizes, and crafting details. The item database auto-syncs from the [MetaForge API](https://metaforge.app/arc-raiders) on every launch.

[![Example 1](https://github.com/Soygen/ARLO/raw/master/static/ARLO_01.png)](/Soygen/ARLO/blob/master/static/ARLO_01.png) [![Example 2](https://github.com/Soygen/ARLO/raw/master/static/ARLO_02.png)](/Soygen/ARLO/blob/master/static/ARLO_02.png) [![Example 3](https://github.com/Soygen/ARLO/raw/master/static/ARLO_03.png)](/Soygen/ARLO/blob/master/static/ARLO_03.png)

---

## Features

- **Auto-sync on launch** - The item database updates from the MetaForge API every time you start the app (throttled to once per 24 hours). No manual steps needed.
- **Sell price & stack size on the overlay** - See at a glance what an item sells for and how high it stacks
- **440+ items** - Full item coverage including keys, mods, augments, shields, ammo, consumables, and recycle components
- **Quest-aware recommendations** - Items needed for quests are flagged to keep, with quest names shown
- **Smart action generation** - Items are auto-categorized (keep materials, sell trinkets, recycle junk) based on API data
- **Merge mode** - New items are added without overwriting your manual action overrides
- **Dual data sources** - MetaForge API as primary, Arc Raiders Wiki as fallback
- **GitHub Actions** - Optional scheduled workflow that auto-updates the database weekly

---

## How It Works

The tool runs two detection phases while you play:

1. **Trigger Detection** (every 500ms) - Scans for the word "INVENTORY" on screen to know when your inventory is open
2. **Tooltip Detection** (every 300ms) - When inventory is open, captures the area around your cursor, reads the item name via OCR, looks it up in the database, and shows an overlay

The overlay popup displays:

```
+--------------------------------------+
|  ARC Alloy                           |
|  -> Keep                             |
|  Sell: 200   Stack: 15               |
|  For: Workshop Explosives Station 1  |
+--------------------------------------+
```

- **Item name** at the top
- **Action** color-coded (green = keep, gold = sell, turquoise = recycle, pink = use)
- **Sell price** and **stack size** on the info line
- **Details** - what it recycles into, or why you're keeping it

---

## Requirements

### Pre-built Release

- Windows 10/11
- Arc Raiders running in **borderless windowed** or **windowed** mode (not exclusive fullscreen)

### Running from Source

- Windows 10/11, or Linux (X11 or Wayland)
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) package manager
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) — on Linux: `sudo apt install tesseract-ocr` / `nix-env -i tesseract`

---

## Installation

### Option 1: Pre-built Release (Recommended)

1. Download the latest release from the [Releases](https://github.com/Soygen/ARLO/releases) page
2. Extract the zip to a folder of your choice
3. Run `ARLO.exe`

> **This fork:** the upstream release doesn't include this fork's ARC Tracker verdicts or its Linux support. Run from source (Option 2), or build your own `ARLO.exe` on Windows with [docs/BUILD.md](docs/BUILD.md).

The release includes all dependencies, including Tesseract OCR. The item database updates automatically on launch, no more than once per 24 hours.

### Option 2: From Source

1. Clone the repository:

```
git clone https://github.com/ItzRobD/ARLO-AT.git
cd ARLO-AT
```

2. Install dependencies:

```
uv sync --all-extras
```

3. Install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki):

   - **Windows:** install to `C:\Program Files\Tesseract-OCR\` (or set `TESSERACT_PATH` in `.env`)
   - **Linux:** `sudo apt install tesseract-ocr` or `nix-env -i tesseract` (must be on `$PATH`)
   - **NixOS:** Use the provided flake (see [NixOS](#nixos) below)

4. Copy the example config:

```
copy .env.example .env
```

   To get personal verdicts, put your ARC Tracker key in `.env` (see [Personal Verdicts](#personal-verdicts-arc-tracker-optional)).

5. Launch the app:

```
uv run arc-helper
```

The item database updates automatically on first launch. No separate step needed.

### NixOS

The project includes a `flake.nix` so you can build the Python dependency `evdev` (required by `pynput`) with kernel headers and a C compiler, and get X11 libraries for screen capture. From the repo root:

1. Enter the dev shell (first time will create `flake.lock`):

```
nix develop
```

2. Install dependencies and run the app:

```
uv sync --all-extras
cp -n .env.example .env   # if you don't have .env yet
uv run arc-helper
```

To run without entering the shell each time: `nix develop --command bash -c 'uv run arc-helper'`.

### Linux: Wayland

On a Wayland session ARLO captures the screen through the desktop portal
(xdg-desktop-portal + PipeWire) instead of X11. Requirements:

- System packages (Arch): `sudo pacman -S --needed cairo pkgconf gcc gstreamer gst-plugins-base gst-plugin-pipewire` (Debian/Ubuntu: `sudo apt install libcairo2-dev libgirepository1.0-dev pkg-config gcc gstreamer1.0-pipewire gstreamer1.0-plugins-base`)
- `xdg-desktop-portal` plus your desktop's backend (`xdg-desktop-portal-kde`,
  `-gnome` or `-wlr`) — preinstalled on KDE/GNOME.
- Python deps: `uv sync --all-extras` (builds PyGObject).
- Hold-to-scan hotkey: reading the keyboard needs your user in the `input` group: `sudo usermod -aG input $USER` (log out and back in). Without it the
  hotkey is disabled but everything else works. The key is `HOTKEY_KEY` in
  `.env` (default `KEY_RIGHTCTRL` — hold Right Ctrl).

On first launch your desktop shows a screen-share dialog once; pick your
monitor. The approval is remembered (`.screencast_restore_token`).

Overlay and cursor positions are correctly mapped under fractional display
scaling (e.g. 4K @ 175%) — no extra configuration needed.

Notes and limitations:

- The game must run under XWayland (Proton's default) for cursor-relative
  tooltip detection; cursor position over native Wayland windows is stale.
- Tooltip capture is cursor-relative, while the game's info panel anchors to
  the hovered item, so framing isn't pixel-perfect on Wayland — it works,
  but you may see occasional "clamped" warnings in the log.
- Force a backend with `SCREEN_BACKEND=x11|wayland` in `.env`.
- Diagnostic: `uv run python -m arc_helper.screen.diag` prints the backend,
  resolution, scale and cursor, and saves a test capture to `diag_frame.png`.

---

## Item Database

### Automatic Updates

The app fetches item data from the [MetaForge API](https://metaforge.app/arc-raiders) every time it starts, throttled to once per 24 hours. This pulls from three MetaForge data sources:

- **Items API** - Names, categories, sell prices, stack sizes, rarity (440+ items)
- **Recycle components** - What each item breaks down into when recycled
- **Quests API** - Which items are needed for which quests

If MetaForge is unreachable, the updater falls back to scraping the [Arc Raiders Wiki](https://arcraiders.wiki/wiki/Loot). If both are down, the app continues normally with whatever database it already has.

Updates always run in merge mode, so any manual action overrides you've made are preserved.

### Manual Update

You can also trigger a database update from the command line:

```
uv run python update_db.py                # Full update from MetaForge API
uv run python update_db.py --merge        # Keep your manual overrides
uv run python update_db.py --dry-run      # Preview changes without writing
uv run python update_db.py --source wiki  # Force wiki scraper instead of API
```

Or use the makefile shortcuts:

```
make update-db          # Full update
make update-db-merge    # Merge mode
make update-db-dry      # Dry run preview
```

### GitHub Actions (Optional)

The included workflow at `.github/workflows/update-db.yml` can also sync the database automatically:

- Runs weekly on Mondays at 6:00 UTC in merge mode
- Auto-commits updated `items.csv` and `items.db` if changes are detected
- Can be triggered manually from the **Actions** tab on GitHub

No secrets or extra configuration needed.

For more details on how the updater works, CSV format, and manual database management, see [docs/ITEMS.md](https://github.com/Soygen/ARLO/blob/master/docs/ITEMS.md).

---

## Building the Standalone Executable

To produce a distributable package with everything bundled:

```
uv sync --all-extras
uv run python build.py
```

Output lands in `dist/ArcRaidersHelper/` containing the exe, calibration tool, bundled Tesseract, config files, item database, and the updater script. Zip that folder to share with anyone - no Python install needed on their end.

See [docs/BUILD.md](https://github.com/Soygen/ARLO/blob/master/docs/BUILD.md) for full build details.

---

## First Run Setup

On first launch the app detects your screen resolution and loads a matching profile if one exists:

| Resolution | Aspect Ratio        | Status       |
| ---------- | -------------------- | ------------ |
| 5120x2160  | 21:9 DQHD Ultrawide  | ✅ Configured |
| 3840x2160  | 16:9 4K UHD          | ✅ Configured |
| 3440x1440  | 21:9 Ultrawide QHD   | ✅ Configured |
| 2560x1440  | 16:9 QHD             | ✅ Configured |
| 2560x1080  | 21:9 Ultrawide FHD   | ✅ Configured |
| 1920x1080  | 16:9 Full HD         | ✅ Configured |

If your resolution isn't listed, run the [Calibration tool](https://github.com/Soygen/ARLO/blob/master/docs/CALIBRATION.md):

```
uv run arc-calibrate
```

---

## Configuration

All settings live in the `.env` file. See [docs/CONFIGURATION.md](https://github.com/Soygen/ARLO/blob/master/docs/CONFIGURATION.md) for details on scan regions, overlay position, display time, and debug mode.

**Debugging:** In `.env` set `DEBUG_MODE=true` for verbose logs and `SHOW_CAPTURE_AREA=true` to show the trigger and tooltip regions on screen. Restart ARLO after changing `.env`.

**Log file:** ARLO always writes `arc_helper.log` next to the app (5 MB, two old copies kept), so connection problems can be looked up after a session even when it was started from Steam with no terminal. `DEBUG_MODE=true` adds debug detail.

**Quitting:** If you started ARLO from a terminal, press **Ctrl+C**. You can also **right-click the status box** (the "Scanning…" / "ARLO Status" window) to quit, or run `pkill -f arc-helper` in a terminal.

## Personal Verdicts (ARC Tracker, optional)

By default ARLO shows the generic action from `items.csv`. You can connect your [ARC Tracker](https://arctracker.io) account to get verdicts based on your own stash, quests, projects and workshop instead. For example, ARLO stops telling you to recycle a weapon you have equipped.

1. In ARC Tracker, go to **Settings → Developer Access** and create an API key.
2. Put it in `.env` as `ARCTRACKER_API_KEY=...`. Never commit `.env`.
3. Optional: `cp rules.example.yaml rules.yaml` and edit it with your own keep targets, loadout and actions. `rules.yaml` is gitignored and reloads while ARLO runs. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#arc-tracker-rules).

How it syncs:

- ARLO pulls your stash, needs and progress when you open the inventory, at most once every 3 minutes.
- The item catalogue is cached for a day in `cache/`.
- With no key, ARLO makes no ARC Tracker requests at all.
- ARC Tracker data is read-only: ARLO never changes your account.

### Reading the popup

| Bar colour | Verdict | Meaning |
| ---------- | ------- | ------- |
| Green | **Keep** | Target met; this copy is needed |
| Yellow | **Keep · building** | Above minimum, still short of the ideal |
| Orange | **Keep · low** | Below the minimum you set |
| Gold | **Sell N** | N spare copies; selling beats recycling |
| Turquoise | **Recycle N** | N spare copies; the recycled parts are worth more |
| Purple | **Consume N** | Research item; hand spares in at the Research Station |
| Grey | **No rule** / **Unknown** | No target for this item, or the name wasn't found in ARC Tracker |

- **Below the title:** the reason, coloured by category (quest, project, workshop, loadout, rule), and your counts (for example `Own 5 · crafts need 160`).
- **Craft rows:** how many you can craft now.
- **Made-from rows:** the ingredients still needed for the target. They're hidden once the target is met.
- **⟳ age (top-right corner):** how long ago ARC Tracker last synced. It changes colour once the data is more than 3 hours old.

### Crafting screen

When a workshop station's **CRAFTING** tab is open, ARLO adds a popup beside the hovered blueprint's tooltip and another beside the selected blueprint's panel on the right:

- **Craft N:** crafts still needed to reach your ideal target: ARC Tracker needs, plus your `rules.yaml` or loadout ideal, plus what your other unmet targets still need of this item as an ingredient (listed in rows on the popup). Recipes that make several per craft are counted per craft.
- **Target met:** you already have enough.
- **No target:** nothing asks for this item.

These popups need an ARC Tracker key. Counts come from ARC Tracker, so right after you craft they lag until its next sync.

### Trader screen

A trader's **TRADES** tab works the same way: popups beside the hovered item's tooltip and the selected item's panel, with the same targets as the crafting screen. The title is **Buy N** (single items, since traders sell one at a time) instead of **Craft N**. A trader's QUESTS tab also shows TRADES in its header, so hovering a quest reward there shows a popup too.

**BP status unknown** on a popup means ARC Tracker's list of learned blueprints was cut off before reaching this item, so ARLO can't tell whether you've learned it. Its API returns at most 30 names.

### Connection icon

With an ARC Tracker key set, a wifi icon sits beside the status text in the top-left window: green when the last sync worked, grey while connecting, red when offline or the key was rejected (popups then use the last saved data). Without a key the icon is hidden.

### Contributing to a public fork

Enable the bundled pre-commit hook once per clone:

```
git config core.hooksPath .githooks
```

It refuses commits that contain API keys, tokens, `.env`, `rules.yaml`, `cache/`, `screenshots/` or `debug/` output.

---

## Project Structure

```
ARLO/
├── update_db.py                    # MetaForge API updater (with wiki fallback)
├── items.csv                       # Item data (auto-generated or hand-edited)
├── items.db                        # SQLite database (built from CSV)
├── pyproject.toml                  # Package config & dependencies
├── makefile                        # Convenience targets
├── build.py                        # PyInstaller build script
├── .env.example                    # Configuration template
├── .github/workflows/
│   └── update-db.yml               # Scheduled database sync
├── src/arc_helper/
│   ├── main.py                     # Main app entry point
│   ├── config.py                   # Settings & logging
│   ├── database.py                 # SQLite + CSV import + fuzzy matching
│   ├── ocr.py                      # Screen capture & Tesseract OCR
│   ├── overlay.py                  # Tkinter overlay (action + price + stack)
│   ├── calibrate.py                # GUI calibration tool
│   ├── resolution_profiles.py      # Resolution presets
│   ├── resolutions.json            # Pre-configured resolution profiles
│   └── screen/                     # Screen capture backends (mss / Wayland portal)
├── docs/
│   ├── ITEMS.md                    # Item database & updater docs
│   ├── CALIBRATION.md              # Calibration guide
│   ├── CONFIGURATION.md            # Settings reference
│   ├── BUILD.md                    # Build instructions
│   ├── TROUBLESHOOTING.md          # Common issues
│   └── CHANGELOG.md                # Version history
└── static/                         # Screenshots
```

---

## Contributing

### Item Database

Run `uv run python update_db.py --dry-run` to preview the API data. If you spot incorrect auto-generated actions, edit `items.csv` directly and use `--merge` on future updates to preserve your fixes.

### Resolution Profiles

If you calibrate for a resolution that isn't pre-configured, add it to `resolutions.json` and submit a PR.

### Bug Reports

Please include your screen resolution, debug images from the `debug/` folder (enable `DEBUG_MODE=true` in `.env`), and `arc_helper.log`. On Linux, please also note whether you're running X11 or Wayland (`echo $XDG_SESSION_TYPE`).

---

## Credits

- **[PabsArcTooltip](https://github.com/Pabosik/PabsArcTooltip)** - Original project by [Pabosik](https://github.com/Pabosik)
- **[MetaForge](https://metaforge.app/arc-raiders)** - Primary item data source (API and community database)
- **[Arc Raiders Wiki](https://arcraiders.wiki)** - Fallback item data source
- **[Tesseract OCR](https://github.com/tesseract-ocr/tesseract)** - Text recognition engine
- **[PyInstaller](https://pyinstaller.org/)** - Executable packaging

---

## License

MIT License - Free to use and modify.
