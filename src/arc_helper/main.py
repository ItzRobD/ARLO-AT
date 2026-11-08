"""
ARLO - Main Application.
Coordinates OCR scanning and overlay display.
"""

import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from enum import auto
from pathlib import Path
from threading import Thread

from dotenv import load_dotenv

from arc_helper.arctracker import AdvisorService
from arc_helper.arctracker.verdict import Verdict
from arc_helper.arctracker.verdict import VerdictKind
from arc_helper.clickthrough import make_click_through
from arc_helper.clickthrough import make_outline
from arc_helper.config import APP_DIR
from arc_helper.config import SettingsManager
from arc_helper.config import get_settings
from arc_helper.config import logger
from arc_helper.database import Database
from arc_helper.database import Item
from arc_helper.database import get_database
from arc_helper.hotkey import HotkeyMonitor
from arc_helper.hotkey import get_hotkey_monitor
from arc_helper.ocr import OCREngineManager
from arc_helper.ocr import get_ocr_engine
from arc_helper.overlay import OverlayWindow
from arc_helper.overlay import StatusWindow
from arc_helper.resolution_profiles import get_profile_manager
from arc_helper.screen import ScreenCaptureUnavailable

load_dotenv(Path(__file__).with_name(".env"), override=False)

CONNECTION_POLL_MS = 2000


class ScannerState(Enum):
    """State machine for the scanner."""

    IDLE = auto()  # Scanning for trigger (INVENTORY)
    ACTIVE = auto()  # Inventory detected, scanning tooltip
    PAUSED = auto()  # Temporarily paused
    STOPPED = auto()  # Fully stopped


class DebugOverlay:
    """Semi-transparent overlay showing the tooltip capture area."""

    def __init__(self, root: tk.Tk, settings):
        self.root = root
        self.settings = settings

        self.window = tk.Toplevel(root)
        self.window.title("Capture Area")
        self.window.attributes("-topmost", True)  # noqa: FBT003
        self.window.attributes("-alpha", 0.3)  # ignored by KWin; see make_outline
        self.window.overrideredirect(boolean=True)
        self.window.config(bg="red")
        self._shaped_size: tuple[int, int] | None = None
        make_click_through(self.window)

        # Update position periodically
        self._update_position()

    def _update_position(self):
        """Update overlay position to follow cursor."""
        try:
            from arc_helper.ocr import get_cursor_position
            from arc_helper.screen import phys_to_tk
            from arc_helper.screen import phys_to_tk_pos

            if self.settings.tooltip_detection.lower() != "cursor":
                # Frame mode: outline the tooltip that was actually found
                bbox = get_ocr_engine().last_tooltip_bbox
                if bbox is None:
                    self.window.withdraw()
                    self.root.after(50, self._update_position)
                    return
                self.window.deiconify()
                left, top, right, bottom = bbox
                x, y = phys_to_tk_pos(left, top)
                w, h = phys_to_tk(right - left), phys_to_tk(bottom - top)
            else:
                cursor = get_cursor_position()
                x, y = phys_to_tk_pos(
                    cursor.x + self.settings.tooltip_capture.offset_x,
                    cursor.y + self.settings.tooltip_capture.offset_y,
                )
                w = phys_to_tk(self.settings.tooltip_capture.width)
                h = phys_to_tk(self.settings.tooltip_capture.height)

            self.window.geometry(f"{w}x{h}+{x}+{y}")
            if self._shaped_size != (w, h):
                # Hollow box: a solid one would cover (and be captured
                # instead of) the tooltip it is meant to show.
                make_outline(self.window, w, h)
                make_click_through(self.window)
                self._shaped_size = (w, h)
        except Exception:  # noqa: BLE001
            pass

        # Schedule next update (every 50ms for smooth following)
        self.root.after(50, self._update_position)

    def destroy(self):
        self.window.destroy()


@dataclass
class ScannerStats:
    """Statistics for the scanner."""

    trigger_scans: int = 0
    tooltip_scans: int = 0
    items_detected: int = 0
    items_found_in_db: int = 0
    last_item: str | None = None
    last_item_time: float = 0


@dataclass
class Scanner:
    """
    Main scanner that coordinates OCR and overlay.

    Uses a two-phase approach:
    1. Low-frequency scan for "INVENTORY" text (trigger)
    2. When triggered, high-frequency scan for item names
    """

    root: tk.Tk
    overlay: OverlayWindow
    status: StatusWindow
    db: Database = field(default_factory=get_database)
    state: ScannerState = ScannerState.IDLE
    stats: ScannerStats = field(default_factory=ScannerStats)
    hotkey: HotkeyMonitor = field(default_factory=get_hotkey_monitor)
    # Personal verdicts from ARC Tracker; None (or no data) -> generic db
    advisor: AdvisorService | None = None

    # UI updates are posted here for the main (GUI) thread to apply. tkinter
    # is not thread-safe, and on Linux/Tcl root.after() called from this
    # background thread silently never fires - so we marshal via a queue that
    # Application drains on a main-thread timer.
    ui_queue: "queue.Queue[tuple]" = field(default_factory=queue.Queue)

    # Cooldown tracking
    _last_shown_item: str = ""
    _last_shown_time: float = 0
    _last_trigger_check: float = 0.0
    _overlay_visible: bool = False
    # Item lookups by OCR'd name (frame mode re-shows every scan)
    _lookup_cache: dict = field(default_factory=dict)

    # Hotkey override (Ctrl+Shift to force tooltip scanning)
    _hotkey_override: bool = False
    # Active because a workshop station's CRAFTING tab is open (not INVENTORY)
    _crafting: bool = False

    # Thread control
    _running: bool = False
    _scan_thread: Thread | None = None

    def start(self) -> None:
        """Start the scanner in a background thread."""
        if self._running:
            return

        self._running = True
        self.state = ScannerState.IDLE
        self.hotkey.start()
        self._scan_thread = Thread(target=self._scan_loop, daemon=True)
        self._scan_thread.start()
        logger.info("Scanner started")

    def stop(self) -> None:
        """Stop the scanner."""
        self._running = False
        self.state = ScannerState.STOPPED
        if self._scan_thread:
            self._scan_thread.join(timeout=2.0)
        self.hotkey.stop()
        logger.info("Scanner stopped")

    def pause(self) -> None:
        """Pause scanning."""
        self.state = ScannerState.PAUSED

    def resume(self) -> None:
        """Resume scanning."""
        self.state = ScannerState.IDLE

    def _scan_loop(self) -> None:
        """Main scanning loop running in background thread."""
        settings = get_settings()
        ocr = get_ocr_engine()

        while self._running:
            try:
                if self.state == ScannerState.PAUSED:
                    time.sleep(0.1)
                    continue

                if self.state == ScannerState.STOPPED:
                    break

                # Check hotkey override (Ctrl+Shift)
                hotkey_now = self.hotkey.is_held()

                # Phase 1: Check for trigger (INVENTORY) or hotkey
                if self.state == ScannerState.IDLE:
                    self._update_status("scanning")

                    if hotkey_now:
                        # Hotkey pressed - enter active mode via override
                        self._hotkey_override = True
                        self.state = ScannerState.ACTIVE
                        self._update_status("hotkey")
                        logger.info("Hotkey override - activating tooltip scanner")
                    elif ocr.check_trigger_any(
                        [settings.trigger_region, settings.trigger_region2]
                    ):
                        # Trigger detected! Switch to active mode
                        self._hotkey_override = False
                        self._crafting = False
                        self.state = ScannerState.ACTIVE
                        self._update_status("active")
                        logger.info("INVENTORY detected - activating tooltip scanner")
                        if self.advisor is not None:
                            self.advisor.request_refresh()  # throttled
                    elif self.advisor is not None and ocr.check_crafting():
                        # Crafting popups need personal data (crafts to target)
                        self._hotkey_override = False
                        self._crafting = True
                        self.state = ScannerState.ACTIVE
                        self._update_status("active")
                        logger.info("CRAFTING detected - activating panel scanner")
                        self.advisor.request_refresh()  # throttled
                    else:
                        # Wait before next trigger scan
                        time.sleep(settings.scan.trigger_scan_interval)

                    self.stats.trigger_scans += 1

                # Phase 2: Active mode - scan tooltip
                elif self.state == ScannerState.ACTIVE:
                    if self._hotkey_override:
                        # Hotkey mode: stay active as long as hotkey is held
                        if not hotkey_now:
                            self._hotkey_override = False
                            self.state = ScannerState.IDLE
                            self._update_status("scanning")
                            self._hide_overlay()
                            logger.info("Hotkey released - returning to idle")
                            continue
                    else:
                        # Inventory mode: re-check the trigger on a timer
                        # (it costs an OCR call; tooltip scans are cheaper)
                        now = time.monotonic()
                        should_check_trigger = (
                            now - self._last_trigger_check
                            >= settings.scan.trigger_scan_interval
                        )
                        if should_check_trigger:
                            self._last_trigger_check = now
                        if should_check_trigger and not (
                            ocr.check_crafting()
                            if self._crafting
                            else ocr.check_trigger_any(
                                [settings.trigger_region, settings.trigger_region2]
                            )
                        ):
                            # Inventory / crafting closed, go back to idle
                            screen = "CRAFTING" if self._crafting else "INVENTORY"
                            self._crafting = False
                            self.state = ScannerState.IDLE
                            self._update_status("scanning")
                            self._hide_overlay()
                            logger.info(f"{screen} closed - returning to idle")
                            continue

                    if self._crafting:
                        self._show_panels(ocr.read_panel_names())
                        self.stats.tooltip_scans += 1
                        time.sleep(settings.scan.tooltip_scan_interval)
                        continue

                    item_name = ocr.read_hovered_item_name()
                    self.stats.tooltip_scans += 1

                    if item_name:
                        self._handle_detected_item(item_name)
                    elif ocr.detection != "cursor" and ocr.last_tooltip_bbox is None:
                        # Tooltip closed (or another one animating in)
                        self._hide_overlay()

                    # Wait before next tooltip scan
                    time.sleep(settings.scan.tooltip_scan_interval)

            except ScreenCaptureUnavailable as e:
                logger.error(f"Screen capture lost permanently: {e}")
                self._update_status("error")
                self.state = ScannerState.STOPPED
                break  # latch: no point scanning without capture
            except Exception as e:  # noqa: BLE001
                logger.error(f"Scanner error: {e}")
                self._update_status("error")
                time.sleep(1.0)  # Back off on error

    def _handle_detected_item(self, item_name: str) -> None:
        """Handle a detected item name."""
        settings = get_settings()
        current_time = time.time()
        ocr = get_ocr_engine()

        if ocr.detection != "cursor" and item_name == self._last_shown_item:
            # Frame mode: the tooltip is still up, so keep the popup up
            # (an unchanged popup only repositions and extends its timer).
            if self._show_verdict(item_name, ocr.last_tooltip_bbox) is None:
                self._show_overlay(
                    item_name, self._db_lookup(item_name), ocr.last_tooltip_bbox
                )
            self._last_shown_time = current_time
            return

        # Check cooldown - don't spam the same item
        if (
            item_name == self._last_shown_item
            and current_time - self._last_shown_time < settings.overlay.cooldown
        ):
            return

        self.stats.items_detected += 1
        self.stats.last_item = item_name
        self.stats.last_item_time = current_time

        # Personal verdict first; the generic database is the fallback only
        # without personal data (with it, an unresolved name shows Unknown)
        verdict = self._show_verdict(item_name, ocr.last_tooltip_bbox)
        if verdict is not None:
            if verdict.kind is VerdictKind.UNKNOWN:
                logger.debug(f"Not in ARC Tracker data: {item_name}")
                self.db.log_missing_item(item_name)
            else:
                self.stats.items_found_in_db += 1  # a resolved item is found
                logger.debug(f"Verdict: {item_name}")
            self._last_shown_item = item_name
            self._last_shown_time = current_time
            return

        recommendation = self._db_lookup(item_name)

        if recommendation:
            self.stats.items_found_in_db += 1
            logger.debug(f"Found: {item_name} → {recommendation.action}")
        else:
            logger.debug(f"Unknown item: {item_name}")
            # Log to missing items file for easier database updates
            self.db.log_missing_item(item_name)

        # Show overlay (must be done on main thread), beside the tooltip
        self._show_overlay(item_name, recommendation, ocr.last_tooltip_bbox)

        # Update cooldown tracking
        self._last_shown_item = item_name
        self._last_shown_time = current_time

    def _show_panels(
        self, panels: list[tuple[str, tuple[int, int, int, int]]]
    ) -> None:
        """Crafting screen: a crafts-to-target popup beside each panel."""
        entries = []
        for name, bbox in panels:
            res = self.advisor.lookup(name, crafting=True) if self.advisor else None
            if res is not None:
                entries.append((name, *res, bbox))
        if entries:
            self._overlay_visible = True
            self.ui_queue.put(("panels", entries))
        else:
            self._hide_overlay()

    def _db_lookup(self, item_name: str) -> Item | None:
        """Generic database lookup, memoised by OCR'd name."""
        if item_name not in self._lookup_cache:
            self._lookup_cache[item_name] = self.db.lookup(item_name)
        return self._lookup_cache[item_name]

    def _show_verdict(
        self, item_name: str, anchor: tuple[int, int, int, int] | None
    ) -> Verdict | None:
        """Queue the personal verdict and return it; None when there is none.

        The advisor memoises verdicts per data set, so calling this every
        frame-mode scan is cheap, and the overlay skips the redraw while
        (item, verdict, age, stale) is unchanged.
        """
        if self.advisor is None:
            return None
        res = self.advisor.lookup(item_name)
        if res is None:
            return None
        verdict, age, stale = res
        self._overlay_visible = True
        self.ui_queue.put(("verdict", item_name, verdict, age, stale, anchor))
        return verdict

    def _show_overlay(
        self,
        item_name: str,
        recommendation: Item | None,
        anchor: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Queue an overlay update for the main (GUI) thread to apply."""
        self._overlay_visible = True
        self.ui_queue.put(("overlay", item_name, recommendation, anchor))

    def _hide_overlay(self) -> None:
        """Queue hiding the popup (once) for the main thread."""
        if self._overlay_visible:
            self._overlay_visible = False
            self._last_shown_item = ""
            self.ui_queue.put(("hide",))

    def _update_status(self, status: str) -> None:
        """Queue a status update for the main (GUI) thread to apply."""
        self.ui_queue.put(("status", status))


def _near(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """Same panel: top-left corners within 40 px."""
    return abs(a[0] - b[0]) <= 40 and abs(a[1] - b[1]) <= 40


class Application:
    """Main application controller."""

    def __init__(self):
        """Initialize the application."""
        # Load settings
        self.settings = get_settings()

        # Initialize database
        self.db = get_database()

        # Personal verdicts (no-op without ARCTRACKER_API_KEY)
        self.advisor = AdvisorService(get_settings())

        # Create main Tk root (hidden)
        self.root = tk.Tk()
        self.root.withdraw()  # Hide main window

        # Create overlay and status windows
        self.overlay = OverlayWindow(self.root)
        # Crafting screen: hovered tooltip and selected panel get one each
        self.overlay2 = OverlayWindow(self.root)
        self.status = StatusWindow(self.root, on_quit=self.quit)

        # Debug overlay for visualizing capture area (separate from debug_mode)
        self.debug_overlay = None
        if self.settings.show_capture_area:
            self.debug_overlay = DebugOverlay(self.root, self.settings)

        # Create scanner. The scanner thread posts UI updates to this queue;
        # the main thread drains it in _pump_ui_queue (tkinter is single-thread).
        self.ui_queue: queue.Queue[tuple] = queue.Queue()
        self.scanner = Scanner(
            root=self.root,
            overlay=self.overlay,
            status=self.status,
            db=self.db,
            advisor=self.advisor,
            ui_queue=self.ui_queue,
        )

        # Bind close handler
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    def _apply_status(self, status: str) -> None:
        """Apply a status update to the StatusWindow (main thread only)."""
        if status == "scanning":
            self.status.set_scanning()
        elif status == "active":
            self.status.set_active()
        elif status == "hotkey":
            self.status.set_hotkey()
        elif status == "error":
            self.status.set_error("Error")

    def _poll_connection(self) -> None:
        """Keep the status window's ARC Tracker icon current."""
        self.status.set_connection(self.advisor.connection())
        self.root.after(CONNECTION_POLL_MS, self._poll_connection)

    def _pump_ui_queue(self) -> None:
        """Drain scanner UI messages on the main thread, then reschedule."""
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                if msg[0] == "status":
                    self._apply_status(msg[1])
                elif msg[0] == "overlay":
                    self.overlay.show(msg[1], msg[2], anchor=msg[3])
                elif msg[0] == "verdict":
                    self.overlay.show_verdict(*msg[1:5], anchor=msg[5])
                elif msg[0] == "panels":
                    self._show_panels(msg[1])
                elif msg[0] == "hide":
                    self.overlay.hide()
                    self.overlay2.hide()
        except queue.Empty:
            pass
        self.root.after(20, self._pump_ui_queue)

    def _show_panels(self, entries: list[tuple]) -> None:
        """One popup per crafting panel; a popup stays with its panel.

        Entries are (name, verdict, age, stale, anchor). Each keeps the popup
        already beside the same panel, so hovering in and out does not swap
        the two popups' contents.
        """
        free = [self.overlay, self.overlay2]
        pending = []
        for entry in entries:
            anchor = entry[4]
            match = next(
                (
                    ow
                    for ow in free
                    if ow.placed_anchor is not None
                    and _near(ow.placed_anchor, anchor)
                ),
                None,
            )
            if match is None:
                pending.append(entry)
                continue
            free.remove(match)
            match.show_verdict(*entry[:4], anchor=anchor)
        for entry in pending:
            if free:
                free.pop(0).show_verdict(*entry[:4], anchor=entry[4])
        for ow in free:
            ow.hide()

    def run(self) -> None:
        """Start the application."""
        logger.info("=" * 50)
        logger.info("ARLO - Started")
        logger.info("=" * 50)
        logger.info(f"Database: {self.db.count()} items loaded")
        logger.info(f"Debug mode: {self.settings.debug_mode}")
        logger.info("=" * 50)
        logger.info("Trigger Region:")
        logger.info(
            f"  Position: ({self.settings.trigger_region.x}, {self.settings.trigger_region.y})"
        )
        logger.info(
            f"  Size: {self.settings.trigger_region.width}x{self.settings.trigger_region.height}"
        )
        logger.info("Trigger Region 2:")
        logger.info(
            f"  Position: ({self.settings.trigger_region2.x}, {self.settings.trigger_region2.y})"
        )
        logger.info(
            f"  Size: {self.settings.trigger_region2.width}x{self.settings.trigger_region2.height}"
        )
        logger.info("=" * 50)
        logger.info(
            f"Trigger scan interval: {self.settings.scan.trigger_scan_interval}s"
        )
        logger.info(
            f"Tooltip scan interval: {self.settings.scan.tooltip_scan_interval}s"
        )
        logger.info("=" * 50)
        logger.info("Looking for INVENTORY screen...")
        logger.info(
            "Hold the hotkey to force tooltip scanning (Ctrl+Shift; "
            f"{get_settings().hotkey_key} on Wayland)"
        )
        logger.info("Press Ctrl+C in terminal to quit")
        logger.info("=" * 50)

        # Start the ARC Tracker refresher, then the scanner
        self.advisor.start()
        self.scanner.start()

        # Start the main-thread UI pump (must be scheduled from the main thread)
        self._pump_ui_queue()
        self._poll_connection()

        # Run Tk mainloop
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.quit()

    def quit(self) -> None:
        """Clean shutdown."""
        logger.info("\nShutting down...")
        try:
            self.scanner.stop()
        finally:
            # Even when a second Ctrl+C interrupts the scanner join
            self.advisor.stop(timeout=2.0)
        from arc_helper.screen import reset_backend

        reset_backend()  # release the portal session / PipeWire stream
        self.root.quit()
        self.root.destroy()


def check_first_run() -> bool:
    """
    Check if this is a first run or uncalibrated state.

    Returns True if ready to run, False if calibration needed.
    """
    profile_manager = get_profile_manager()
    resolution = profile_manager.get_resolution_key()

    if profile_manager.is_uncalibrated():
        logger.info(f"Detected resolution: {resolution}")
        logger.info("Settings are uncalibrated, checking for profile...")

        if profile_manager.has_profile():
            logger.info(f"Found profile for {resolution}, applying...")
            profile_manager.apply_profile()

            # Force complete reload of everything
            SettingsManager.reload()
            OCREngineManager.reset()

            # Verify the reload worked
            new_settings = SettingsManager.get()
            logger.info(
                f"After reload - Trigger region: ({new_settings.trigger_region.x}, {new_settings.trigger_region.y})"
            )

            logger.info("Profile applied successfully!")
            return True
        supported = profile_manager.get_supported_resolutions()
        logger.warning(f"No pre-configured profile found for {resolution}.")
        logger.info(
            f"Supported resolutions: {', '.join(supported) if supported else 'None yet'}"
        )
        logger.info("Please run the Calibration tool to configure screen regions.")
        return False

    return True


def main() -> None:
    """Entry point for ARLO."""

    # Log any unhandled exception before exit
    def exception_hook(exc_type, exc_value, exc_tb):
        logger.error("=" * 50)
        logger.error("UNHANDLED EXCEPTION - APP CRASHING")
        logger.error("=" * 50)
        logger.error("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        # Also write to a crash log file
        crash_log = APP_DIR / "crash.log"
        Path(crash_log).write_text(
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
            encoding="utf-8",
        )
        logger.error(f"Crash log written to: {crash_log}")
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = exception_hook

    def thread_exception_hook(args):
        logger.error("=" * 50)
        logger.error(f"THREAD EXCEPTION in {args.thread.name}")
        logger.error("=" * 50)
        logger.error(
            "".join(
                traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback
                )
            )
        )
        crash_log = APP_DIR / "crash.log"
        with Path(crash_log).open("a", encoding="utf-8") as f:
            f.write(f"\n\nTHREAD {args.thread.name}:\n")
            f.write(
                "".join(
                    traceback.format_exception(
                        args.exc_type, args.exc_value, args.exc_traceback
                    )
                )
            )

    threading.excepthook = thread_exception_hook

    try:
        # Check first run / calibration status
        if not check_first_run():
            input("\nPress Enter to exit...")
            return

        # Auto-update item database from wiki (throttled to once per 24h)
        try:
            sys.path.insert(0, str(APP_DIR))
            from update_db import auto_update

            auto_update()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Wiki auto-update unavailable: {e}")

        settings = get_settings()
        logger.info(
            f"Settings loaded for resolution, trigger at ({settings.trigger_region.x}, {settings.trigger_region.y})"
        )

        _ = get_ocr_engine()
        app = Application()
        app.run()

    except Exception as e:  # noqa: BLE001
        logger.error(f"Fatal error in main: {e}")
        logger.error(traceback.format_exc())
        input("\nPress Enter to exit after error...")


if __name__ == "__main__":
    main()
