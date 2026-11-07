"""
Overlay UI for ARLO (Arc Raiders Loot Overlay).
Displays item recommendations as a styled popup with action colors.
"""

import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from typing import Callable

from .arctracker.verdict import CraftRow
from .arctracker.verdict import Verdict
from .arctracker.verdict import VerdictKind
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

# Personal-verdict popup (ARC Tracker)
VERDICT_COLORS: dict[VerdictKind, str] = {
    VerdictKind.KEEP: "#52cb89",
    VerdictKind.KEEP_BUILDING: "#f5d75e",
    VerdictKind.KEEP_LOW: "#f49b50",
    VerdictKind.SELL: "#FFD700",
    VerdictKind.RECYCLE: "#00CED1",
    VerdictKind.NO_RULE: "#888888",
    VerdictKind.UNKNOWN: "#888888",
}
CATEGORY_COLORS: dict[str, str] = {
    "weapons": "#7dd3fc",
    "consumables": "#60a5fa",
    "medical": "#f9a8d4",
    "crafting": "#e7c66b",
    "base": "#53c8ae",
    "arc_rare": "#bf9af7",
}
PIP_COLORS: dict[str, str] = {
    "low": "#f49b50",
    "building": "#f5d75e",
    "met": "#52cb89",
    "covers": "#52cb89",
    "partial": "#f5d75e",
    "none": "#f49b50",
}
TEXT_LABEL = "#7d7d99"  # small-caps section label, sync age
TEXT_STALE = "#f49b50"
TEXT_COUNTS = "#c8c8dc"
TEXT_ROW = "#e6e6f0"
NO_DATA_AGE = "\u27f3 \u2014"  # "⟳ —": no personal data
VERDICT_WIDTH = 260  # popup width at scale 1

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


@dataclass
class _RowWidgets:
    """One craft / made-from row: pip + label left, right text right."""

    frame: tk.Frame
    pip: tk.Frame
    label: tk.Label
    right: tk.Label


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
        # What is on screen: ("overlay", name) or ("verdict", name, verdict,
        # age, stale). An unchanged key only repositions and extends the timer.
        self._current_key: tuple | None = None
        self._mode: str | None = None  # "generic" | "verdict"

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

        # Inner content area; holds one of two content frames at a time
        # (generic database popup or personal verdict), swapped in show*().
        self.frame = tk.Frame(self.outer_frame, bg=BG_PANEL)
        self.frame.pack()

        self.generic_frame = tk.Frame(
            self.frame,
            bg=BG_PANEL,
            padx=padx,
            pady=pady,
        )
        gf = self.generic_frame

        # Colored accent bar at the top (changes color with action)
        self.accent_bar = tk.Frame(
            gf,
            bg="#32CD32",
            height=max(2, self._scale(4)),
        )
        self.accent_bar.pack(fill="x", pady=(0, self._scale(8)))

        # Item name, with the (absent) sync age in the top-right corner
        name_row = tk.Frame(gf, bg=BG_PANEL)
        name_row.pack(fill="x")
        self.fallback_age_label = tk.Label(
            name_row,
            text=NO_DATA_AGE,
            font=(self.font_family, max(7, self._scale(10))),
            fg=TEXT_LABEL,
            bg=BG_PANEL,
        )
        self.fallback_age_label.pack(side="right", anchor="n")
        self.name_label = tk.Label(
            name_row,
            text="Item Name",
            font=(self.font_family, name_size, "bold"),
            fg="white",
            bg=BG_PANEL,
            anchor="w",
        )
        self.name_label.pack(side="left", anchor="w")

        # Action label (large, colored)
        self.action_label = tk.Label(
            gf,
            text="ACTION",
            font=(self.font_family, action_size, "bold"),
            fg="#FFD700",
            bg=BG_PANEL,
            anchor="w",
        )
        self.action_label.pack(anchor="w", pady=(self._scale(4), 0))

        # Sell price / stack size info line
        self.info_label = tk.Label(
            gf,
            text="",
            font=(self.font_family, info_size),
            fg=TEXT_INFO,
            bg=BG_PANEL,
            anchor="w",
        )
        self.info_label.pack(anchor="w", pady=(self._scale(4), 0))

        # Thin separator before details
        self.separator = tk.Frame(
            gf,
            bg=BORDER_COLOR,
            height=max(1, self._scale(1)),
        )
        # Only packed when there's detail text (see show())

        # Detail / notes label (recycle_for or keep_for)
        self.notes_label = tk.Label(
            gf,
            text="",
            font=(self.font_family, detail_size),
            fg=TEXT_DIM,
            bg=BG_PANEL,
            justify=tk.LEFT,
            anchor="w",
        )
        # Only packed when there's detail text (see show())

        self._setup_verdict_ui(action_size, info_size, detail_size)

    def _setup_verdict_ui(
        self, action_size: int, info_size: int, detail_size: int
    ) -> None:
        """Build the personal-verdict layout (approved mockup v5)."""
        fam = self.font_family
        self._row_font = (fam, detail_size)
        self._pip_size = (max(2, self._scale(4)), max(6, self._scale(12)))
        self._row_pady = (self._scale(2), 0)

        self.verdict_frame = tk.Frame(self.frame, bg=BG_PANEL)
        vf = self.verdict_frame
        self.verdict_bar = tk.Frame(vf, bg="#888888", height=max(2, self._scale(5)))
        self.verdict_bar.pack(fill="x")

        inner = tk.Frame(vf, bg=BG_PANEL, padx=self._scale(12))
        inner.pack(fill="x", pady=(self._scale(7), self._scale(9)))
        # Fixes the popup width; text rows wrap inside it.
        self._verdict_wrap = self._scale(VERDICT_WIDTH - 24)
        tk.Frame(inner, bg=BG_PANEL, width=self._verdict_wrap, height=1).pack()

        top = tk.Frame(inner, bg=BG_PANEL)
        top.pack(fill="x")
        self.age_label = tk.Label(
            top, text="", font=(fam, info_size), fg=TEXT_LABEL, bg=BG_PANEL
        )
        self.age_label.pack(side="right", anchor="n")
        self.title_label = tk.Label(
            top,
            text="",
            font=(fam, action_size, "bold"),
            fg="#888888",
            bg=BG_PANEL,
            anchor="w",
            justify=tk.LEFT,
        )
        self.title_label.pack(side="left", anchor="w")

        self.reason_label = tk.Label(
            inner,
            text="",
            font=(fam, info_size, "bold"),
            fg=TEXT_DIM,
            bg=BG_PANEL,
            anchor="w",
            justify=tk.LEFT,
            wraplength=self._verdict_wrap,
        )
        self.reason_label.pack(anchor="w", pady=(self._scale(2), 0))
        self.counts_label = tk.Label(
            inner,
            text="",
            font=(fam, info_size),
            fg=TEXT_COUNTS,
            bg=BG_PANEL,
            anchor="w",
            justify=tk.LEFT,
            wraplength=self._verdict_wrap,
        )
        self.counts_label.pack(anchor="w", pady=(self._scale(3), 0))

        # Optional sections; packed only when the verdict has rows
        sec_pady = (self._scale(6), 0)
        self._section_pady = sec_pady
        self.crafts_frame = tk.Frame(inner, bg=BG_PANEL)
        self._section_separator(self.crafts_frame)
        self.crafts_rows_frame = tk.Frame(self.crafts_frame, bg=BG_PANEL)
        self.crafts_rows_frame.pack(fill="x")

        self.made_from_frame = tk.Frame(inner, bg=BG_PANEL)
        self._section_separator(self.made_from_frame)
        self.made_from_label = tk.Label(
            self.made_from_frame,
            text="MADE FROM",
            font=(fam, max(7, self._scale(10))),
            fg=TEXT_LABEL,
            bg=BG_PANEL,
            anchor="w",
        )
        self.made_from_label.pack(anchor="w")
        self.made_from_rows_frame = tk.Frame(self.made_from_frame, bg=BG_PANEL)
        self.made_from_rows_frame.pack(fill="x")
        self.hint_label = tk.Label(
            self.made_from_frame,
            text="",
            font=(fam, detail_size),
            fg=TEXT_DIM,
            bg=BG_PANEL,
            anchor="w",
            justify=tk.LEFT,
            wraplength=self._verdict_wrap,
        )

        self.craft_rows: list[_RowWidgets] = []
        self.made_from_rows: list[_RowWidgets] = []

    def _section_separator(self, parent: tk.Frame) -> None:
        tk.Frame(parent, bg=BORDER_COLOR, height=max(1, self._scale(1))).pack(
            fill="x", pady=(0, self._scale(5))
        )

    def _build_rows(
        self, parent: tk.Frame, rows: tuple[CraftRow, ...]
    ) -> list[_RowWidgets]:
        for child in parent.winfo_children():
            child.destroy()
        built: list[_RowWidgets] = []
        pip_w, pip_h = self._pip_size
        for row in rows:
            frame = tk.Frame(parent, bg=BG_PANEL)
            frame.pack(fill="x", pady=self._row_pady)
            pip = tk.Frame(
                frame,
                bg=PIP_COLORS.get(row.state, TEXT_DIM),
                width=pip_w,
                height=pip_h,
            )
            pip.pack(side="left", padx=(0, self._scale(6)))
            right = tk.Label(
                frame, text=row.right, font=self._row_font, fg=TEXT_DIM, bg=BG_PANEL
            )
            right.pack(side="right")
            label = tk.Label(
                frame,
                text=row.label,
                font=self._row_font,
                fg=TEXT_ROW,
                bg=BG_PANEL,
                anchor="w",
            )
            label.pack(side="left")
            built.append(_RowWidgets(frame, pip, label, right))
        return built

    def _use_content(self, mode: str) -> None:
        """Swap the generic / verdict content frame inside the one window."""
        if self._mode == mode:
            return
        self.generic_frame.pack_forget()
        self.verdict_frame.pack_forget()
        target = self.generic_frame if mode == "generic" else self.verdict_frame
        target.pack(fill="x")
        self._mode = mode

    def _refresh_shown(self, anchor: tuple[int, int, int, int] | None) -> None:
        """Same content already up: follow the tooltip, extend the timer."""
        self._position(anchor)
        self._restart_hide_timer()

    def _restart_hide_timer(self) -> None:
        if self._hide_after_id:
            self.window.after_cancel(self._hide_after_id)
        hide_ms = int(self.settings.overlay.display_time * 1000)
        self._hide_after_id = self.window.after(hide_ms, self.hide)

    def _present(self, anchor: tuple[int, int, int, int] | None) -> None:
        self.window.update_idletasks()
        self._position(anchor)
        self.window.deiconify()
        self.window.lift()
        self._restart_hide_timer()

    def show_verdict(
        self,
        item_name: str,
        verdict: Verdict,
        age: str,
        stale: bool,  # noqa: FBT001 - positional per the scanner message
        anchor: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Show the personal verdict for the hovered item.

        Same visibility, timer and anchoring behaviour as ``show()``; the
        item name is not shown (the game tooltip already has it).
        """
        key = ("verdict", item_name, verdict, age, stale)
        if self._current_key == key and self.is_visible():
            self._refresh_shown(anchor)
            return
        same_verdict = (
            self._current_key is not None
            and self._current_key[:3] == key[:3]
            and self._mode == "verdict"
        )
        self._current_key = key

        if self._hide_after_id:
            self.window.after_cancel(self._hide_after_id)
            self._hide_after_id = None
        self._use_content("verdict")

        self.age_label.config(text=age, fg=TEXT_STALE if stale else TEXT_LABEL)
        if not same_verdict:  # only the age changed: keep the widgets
            color = VERDICT_COLORS.get(verdict.kind, "#888888")
            self.verdict_bar.config(bg=color)
            self.title_label.config(text=verdict.title, fg=color)
            self.reason_label.config(
                text=verdict.reason,
                fg=CATEGORY_COLORS.get(verdict.category, TEXT_DIM),
            )
            self.counts_label.config(text=verdict.counts)
            if verdict.counts:
                self.counts_label.pack(anchor="w", pady=(self._scale(3), 0))
            else:
                self.counts_label.pack_forget()

            self.crafts_frame.pack_forget()
            self.made_from_frame.pack_forget()
            self.craft_rows = self._build_rows(self.crafts_rows_frame, verdict.crafts)
            if verdict.crafts:
                self.crafts_frame.pack(fill="x", pady=self._section_pady)

            self.made_from_rows = self._build_rows(
                self.made_from_rows_frame, verdict.made_from
            )
            self.hint_label.pack_forget()
            if verdict.hint:
                self.hint_label.config(
                    text=verdict.hint,
                    fg=PIP_COLORS.get(verdict.hint_state or "", TEXT_DIM),
                )
                self.hint_label.pack(anchor="w", pady=(self._scale(4), 0))
            if verdict.made_from or verdict.hint:
                self.made_from_frame.pack(fill="x", pady=self._section_pady)

        self._present(anchor)

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
        key = ("overlay", item_name)
        if self._current_key == key and self.is_visible():
            self._refresh_shown(anchor)
            return

        self._current_key = key

        if self._hide_after_id:
            self.window.after_cancel(self._hide_after_id)
            self._hide_after_id = None
        self._use_content("generic")

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
        self._present(anchor)  # sizes, places, shows and arms auto-hide

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
        self._current_key = None
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
