"""
Overlay UI for ARLO (Arc Raiders Loot Overlay).
Displays item recommendations as a styled popup with action colors.
"""

import tkinter as tk
from tkinter import font as tkfont
from typing import Callable

from .clickthrough import make_click_through
from .config import get_dpi_scale
from .config import get_settings
from .database import Item
from .screen import get_backend
from .screen import phys_to_tk_pos

# Action colors
ACTION_COLORS: dict[str, str] = {
    "SELL": "#FFD700",
    "RECYCLE": "#00CED1",
    "KEEP": "#32CD32",
    "USE": "#FF69B4",
    "TRASH": "#FF4444",
    "UNKNOWN": "#888888",
}

# Background palette
BG_DARK = "#12121f"
BG_PANEL = "#1a1a2e"
BORDER_COLOR = "#3a3a5c"
TEXT_DIM = "#9999bb"
TEXT_INFO = "#c4a0ff"

# Gap between the game's tooltip and our popup, in physical pixels.
ANCHOR_GAP = 16

# First installed family wins; Segoe UI only exists on Windows.
FONT_CANDIDATES = (
    "Segoe UI",
    "Inter",
    "Noto Sans",
    "Adwaita Sans",
    "Cantarell",
    "DejaVu Sans",
)
_font_family: str | None = None


def ui_font_family(root: tk.Misc) -> str:
    """Pick a sans-serif family that is actually installed."""
    global _font_family  # noqa: PLW0603 - cached per process
    if _font_family is None:
        installed = set(tkfont.families(root))
        _font_family = next(
            (f for f in FONT_CANDIDATES if f in installed), "TkDefaultFont"
        )
    return _font_family


def place_beside(
    anchor: tuple[int, int, int, int],
    popup_size: tuple[int, int],
    screen_size: tuple[int, int],
    gap: int = ANCHOR_GAP,
) -> tuple[int, int]:
    """Top-left (physical px) for a popup next to the tooltip ``anchor``.

    Prefers the right side, top-aligned with the tooltip (over the loadout
    panel rather than the item grid); falls back to the left side, and is
    always clamped on-screen.
    """
    left, top, right, _ = anchor
    pw, ph = popup_size
    sw, sh = screen_size
    x = right + gap
    if x + pw > sw:
        x = left - gap - pw
    x = max(0, min(x, sw - pw))
    y = max(0, min(top, sh - ph))
    return x, y


class OverlayWindow:
    """Transparent overlay window for showing recommendations."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = get_settings()
        self.dpi_scale = get_dpi_scale()
        self.ui_scale = self.settings.overlay.scale
        self.font_family = ui_font_family(root)

        self.window = tk.Toplevel(root)
        self.window.title("ARLO")
        self.window.attributes("-topmost", True)  # noqa: FBT003
        self.window.attributes("-alpha", 0.92)
        self.window.overrideredirect(boolean=True)
        self.window.withdraw()

        self.overlay_x = self.settings.overlay.x
        self.overlay_y = self.settings.overlay.y

        self._setup_ui()
        # The popup must never swallow game clicks
        make_click_through(self.window)

        self._hide_after_id: str | None = None
        self._current_item: str | None = None

    def _scale(self, value: float) -> int:
        return max(1, round(value * self.ui_scale / self.dpi_scale))

    def _setup_ui(self) -> None:
        """Build the overlay layout."""
        padx = self._scale(18)
        pady = self._scale(12)
        border_w = max(1, self._scale(2))

        # Font sizes - bigger and bolder
        name_size = max(10, self._scale(18))
        action_size = max(12, self._scale(26))
        info_size = max(9, self._scale(13))
        detail_size = max(9, self._scale(12))

        # Outer frame with border
        self.outer_frame = tk.Frame(
            self.window,
            bg=BORDER_COLOR,
            padx=border_w,
            pady=border_w,
        )
        self.outer_frame.pack()

        # Inner content area
        self.frame = tk.Frame(
            self.outer_frame,
            bg=BG_PANEL,
            padx=padx,
            pady=pady,
        )
        self.frame.pack()

        # Colored accent bar at the top (changes color with action)
        self.accent_bar = tk.Frame(
            self.frame,
            bg="#32CD32",
            height=max(2, self._scale(4)),
        )
        self.accent_bar.pack(fill="x", pady=(0, self._scale(8)))

        # Item name
        self.name_label = tk.Label(
            self.frame,
            text="Item Name",
            font=(self.font_family, name_size, "bold"),
            fg="white",
            bg=BG_PANEL,
            anchor="w",
        )
        self.name_label.pack(anchor="w")

        # Action label (large, colored)
        self.action_label = tk.Label(
            self.frame,
            text="ACTION",
            font=(self.font_family, action_size, "bold"),
            fg="#FFD700",
            bg=BG_PANEL,
            anchor="w",
        )
        self.action_label.pack(anchor="w", pady=(self._scale(4), 0))

        # Sell price / stack size info line
        self.info_label = tk.Label(
            self.frame,
            text="",
            font=(self.font_family, info_size),
            fg=TEXT_INFO,
            bg=BG_PANEL,
            anchor="w",
        )
        self.info_label.pack(anchor="w", pady=(self._scale(4), 0))

        # Thin separator before details
        self.separator = tk.Frame(
            self.frame,
            bg=BORDER_COLOR,
            height=max(1, self._scale(1)),
        )
        # Only packed when there's detail text (see show())

        # Detail / notes label (recycle_for or keep_for)
        self.notes_label = tk.Label(
            self.frame,
            text="",
            font=(self.font_family, detail_size),
            fg=TEXT_DIM,
            bg=BG_PANEL,
            justify=tk.LEFT,
            anchor="w",
        )
        # Only packed when there's detail text (see show())

    def show(
        self,
        item_name: str,
        recommendation: Item | None,
        anchor: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Show the overlay with item info.

        ``anchor`` is the game tooltip's physical-pixel bbox; with
        OVERLAY_ANCHOR=tooltip the popup is placed beside it.
        """
        # Skip redraw if same item already showing
        if self._current_item == item_name and self.is_visible():
            self._position(anchor)
            if self._hide_after_id:
                self.window.after_cancel(self._hide_after_id)
            hide_ms = int(self.settings.overlay.display_time * 1000)
            self._hide_after_id = self.window.after(hide_ms, self.hide)
            return

        self._current_item = item_name

        if self._hide_after_id:
            self.window.after_cancel(self._hide_after_id)

        # Update content: the database's spelling, not the raw OCR text
        # ("CANISTER", "HULLCRACKERIV")
        self.name_label.config(
            text=recommendation.name if recommendation else item_name.title()
        )

        if recommendation:
            action_str = recommendation.action
            action_upper = action_str.upper()

            # Pick color: check for keywords in multi-word actions
            color = "#FFFFFF"
            for keyword, c in ACTION_COLORS.items():
                if keyword in action_upper:
                    color = c
                    break

            self.action_label.config(text=f"\u2192 {action_str}", fg=color)
            self.accent_bar.config(bg=color)

            # Info line
            info_parts: list[str] = []
            if recommendation.sell_price is not None:
                info_parts.append(f"Sell: {recommendation.sell_price:,}\u20a1")
            if recommendation.stack_size is not None:
                info_parts.append(f"Stack: {recommendation.stack_size}")
            self.info_label.config(text="    \u00b7    ".join(info_parts))

            # Detail text
            detail_text = ""
            if "RECYCLE" in action_upper and recommendation.recycle_for:
                detail_text = f"Recycles into: {recommendation.recycle_for}"
            elif recommendation.keep_for:
                detail_text = recommendation.keep_for

            # Show or hide separator and notes based on content
            self.separator.pack_forget()
            self.notes_label.pack_forget()
            if detail_text:
                self.separator.pack(fill="x", pady=(self._scale(8), self._scale(6)))
                self.notes_label.pack(anchor="w")
                self.notes_label.config(text=detail_text)
        else:
            self.action_label.config(text="\u2192 UNKNOWN", fg=ACTION_COLORS["UNKNOWN"])
            self.accent_bar.config(bg=ACTION_COLORS["UNKNOWN"])
            self.info_label.config(text="")
            self.separator.pack_forget()
            self.notes_label.pack_forget()
            self.notes_label.pack(anchor="w", pady=(self._scale(4), 0))
            self.notes_label.config(text="Item not in database")

        # Wrap notes to the widest line, then size and place the popup
        self.window.update_idletasks()
        widths = [
            self.action_label.winfo_reqwidth(),
            self.name_label.winfo_reqwidth(),
            self.info_label.winfo_reqwidth(),
        ]
        max_width = max(*widths, self._scale(280))
        self.notes_label.config(wraplength=max_width)
        self.window.update_idletasks()

        self._position(anchor)
        self.window.deiconify()
        self.window.lift()

        # Auto-hide
        hide_ms = int(self.settings.overlay.display_time * 1000)
        self._hide_after_id = self.window.after(hide_ms, self.hide)

    def _position(self, anchor: tuple[int, int, int, int] | None) -> None:
        """Move the popup beside the tooltip, or to OVERLAY_X/Y."""
        if self.settings.overlay.anchor.lower() == "tooltip" and anchor:
            backend = get_backend()
            scale = backend.tk_scale
            size = (
                round(self.window.winfo_reqwidth() * scale),
                round(self.window.winfo_reqheight() * scale),
            )
            x, y = place_beside(anchor, size, backend.resolution())
        else:
            x, y = self.overlay_x, self.overlay_y
        self.window.geometry("+{}+{}".format(*phys_to_tk_pos(x, y)))

    def hide(self) -> None:
        if self._hide_after_id:
            self.window.after_cancel(self._hide_after_id)
            self._hide_after_id = None
        self._current_item = None
        self.window.withdraw()

    def set_position(self, x: int, y: int) -> None:
        self.overlay_x = x
        self.overlay_y = y

    def is_visible(self) -> bool:
        try:
            return self.window.winfo_viewable()
        except tk.TclError:
            return False


class StatusWindow:
    """Small status indicator showing scanner state."""

    def __init__(self, root: tk.Tk, *, on_quit: Callable[[], None] | None = None):
        self.root = root
        self.on_quit = on_quit
        self.dpi_scale = get_dpi_scale()
        self.ui_scale = get_settings().overlay.scale
        self.font_family = ui_font_family(root)

        self.window = tk.Toplevel(root)
        self.window.title("ARLO Status")
        self.window.attributes("-topmost", True)  # noqa: FBT003
        self.window.attributes("-alpha", 0.8)
        self.window.overrideredirect(boolean=True)
        # Top-left of the captured (game) monitor, not of the X11 desktop,
        # which on multi-monitor setups can be a different screen.
        self.window.geometry("+{}+{}".format(*phys_to_tk_pos(10, 10)))
        if on_quit:
            self.window.bind("<Button-3>", lambda e: on_quit())  # Right-click to quit

        self._setup_ui()

    def _scale(self, value: float) -> int:
        return max(1, round(value * self.ui_scale / self.dpi_scale))

    def _setup_ui(self) -> None:
        padx = self._scale(10)
        pady = self._scale(5)
        font_size = max(7, self._scale(10))

        self.frame = tk.Frame(self.window, bg=BG_DARK, padx=padx, pady=pady)
        self.frame.pack()

        self.status_label = tk.Label(
            self.frame,
            text="\u25cf Scanning...",
            font=(self.font_family, font_size),
            fg="#888888",
            bg=BG_DARK,
        )
        self.status_label.pack()
        if self.on_quit:
            self.status_label.bind("<Button-3>", lambda e: self.on_quit())
            self.frame.bind("<Button-3>", lambda e: self.on_quit())

    def set_scanning(self) -> None:
        self.status_label.config(text="\u25cf Scanning...", fg="#888888")

    def set_active(self) -> None:
        self.status_label.config(text="\u25cf INVENTORY", fg="#32CD32")

    def set_hotkey(self) -> None:
        self.status_label.config(text="\u25cf HOTKEY SCAN", fg="#FFD700")

    def set_error(self, message: str) -> None:
        self.status_label.config(text=f"\u25cf {message}", fg="#FF4444")

    def hide(self) -> None:
        self.window.withdraw()

    def show(self) -> None:
        self.window.deiconify()
