
## Configuration

All configuration is stored in the `.env` file in the application directory. You can edit this file with any text editor.

### Configuration Options

```env
# =============================================================================
# TRIGGER REGIONS - Where "INVENTORY" text appears
# =============================================================================

# Region 1 - Menu mode
TRIGGER_REGION_X=1450
TRIGGER_REGION_Y=23
TRIGGER_REGION_WIDTH=173
TRIGGER_REGION_HEIGHT=44

# Region 2 - In-raid mode
TRIGGER_REGION2_X=1295
TRIGGER_REGION2_Y=38
TRIGGER_REGION2_WIDTH=173
TRIGGER_REGION2_HEIGHT=44

# =============================================================================
# TOOLTIP CAPTURE - How the item tooltip is found
# =============================================================================
TOOLTIP_DETECTION=frame          # frame: search the whole screen for the tooltip
                                 # cursor: fixed box at the offsets below
# Used only with TOOLTIP_DETECTION=cursor:

TOOLTIP_CAPTURE_WIDTH=550
TOOLTIP_CAPTURE_HEIGHT=550
TOOLTIP_CAPTURE_OFFSET_X=50      # Horizontal offset from cursor
TOOLTIP_CAPTURE_OFFSET_Y=-500    # Vertical offset from cursor (negative = above)

# =============================================================================
# OVERLAY SETTINGS - How the recommendation overlay appears
# =============================================================================

OVERLAY_ANCHOR=tooltip           # tooltip: beside the game's tooltip; fixed: at X/Y
OVERLAY_SCALE=1.0                # Popup size multiplier (0.5 - 4.0)
OVERLAY_X=100                    # Overlay X position (OVERLAY_ANCHOR=fixed)
OVERLAY_Y=100                    # Overlay Y position (OVERLAY_ANCHOR=fixed)
OVERLAY_DISPLAY_TIME=4.0         # How long the overlay stays visible (seconds)
OVERLAY_COOLDOWN=2.0             # Minimum time between showing same item (seconds)

# =============================================================================
# SCANNING SETTINGS - How often the app scans the screen
# =============================================================================

TRIGGER_SCAN_INTERVAL=0.5        # Seconds between trigger scans
TOOLTIP_SCAN_INTERVAL=0.3        # Seconds between tooltip scans

# =============================================================================
# DEBUG SETTINGS
# =============================================================================

DEBUG_MODE=false                 # Enable to save debug images and logs
SHOW_CAPTURE_AREA=false          # Show red rectangle around capture area
```

### Debug Mode

Enable debug mode to troubleshoot OCR issues:

1. Set `DEBUG_MODE=true` in `.env`
2. Run the application
3. Check the `debug/` folder for:
   - `trigger_raw.png` / `trigger_processed.png` - Trigger region captures
   - `tooltip_capture_raw.png` / `tooltip_capture_processed.png` - Tooltip captures
   - `tooltip_mask.png`, `tooltip_cropped.png`, etc. - OCR preprocessing steps
   - `arc_helper.log` - Detailed log file with all operations
