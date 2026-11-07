"""Make tk overlay windows input-transparent so clicks pass through.

X11/XWayland: empty SHAPE input region. Windows: WS_EX_TRANSPARENT.
Best-effort: failure logs a warning, never crashes (the overlay still
works, it just eats clicks like before).
"""

import sys
import tkinter as tk

from arc_helper.config import logger


def make_click_through(window: tk.Misc) -> None:
    try:
        window.update_idletasks()  # realize the native window before winfo_id()
        if sys.platform == "win32":
            _windows_click_through(window)
        else:
            _x11_click_through(window)
    except Exception as e:  # noqa: BLE001 - cosmetic feature, never fatal
        logger.warning(f"Could not make overlay click-through: {e}")


def make_outline(window: tk.Misc, width: int, height: int, thickness: int = 3) -> None:
    """Cut a window down to a hollow rectangle (X11/XWayland only).

    KWin ignores tk's -alpha on override-redirect windows, so a "semi-
    transparent" box is drawn solid and hides whatever is under it. Shaping
    the window to just its border keeps the inside see-through (and out of
    any screen capture of that area). Best-effort; no-op on Windows.
    """
    if sys.platform == "win32":
        return
    try:
        window.update_idletasks()
        from Xlib import X
        from Xlib import display as xlib_display
        from Xlib.ext import shape

        t = max(1, thickness)
        rects = [
            (0, 0, width, t),
            (0, max(0, height - t), width, t),
            (0, 0, t, height),
            (max(0, width - t), 0, t, height),
        ]
        xdisplay = xlib_display.Display()
        try:
            root_id = xdisplay.screen().root.id
            xwindow = xdisplay.create_resource_object("window", window.winfo_id())
            while xwindow.id != root_id:
                xwindow.shape_rectangles(
                    shape.SO.Set, shape.SK.Bounding, X.Unsorted, 0, 0, rects
                )
                parent = xwindow.query_tree().parent
                if parent is None or parent.id == root_id:
                    break
                xwindow = parent
            xdisplay.sync()
        finally:
            xdisplay.close()
    except Exception as e:  # noqa: BLE001 - cosmetic debug aid, never fatal
        logger.warning(f"Could not shape capture-area outline: {e}")


def _windows_click_through(window: tk.Misc) -> None:
    # NOTE: untested on Windows in this change; verified on X11/XWayland.
    import ctypes

    gwl_exstyle = -20
    ws_ex_layered = 0x00080000
    ws_ex_transparent = 0x00000020
    user32 = ctypes.windll.user32
    hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
    style = user32.GetWindowLongPtrW(hwnd, gwl_exstyle)
    user32.SetWindowLongPtrW(hwnd, gwl_exstyle, style | ws_ex_layered | ws_ex_transparent)


def _x11_click_through(window: tk.Misc) -> None:
    from Xlib import X
    from Xlib import display as xlib_display
    from Xlib.ext import shape

    xdisplay = xlib_display.Display()
    try:
        if not xdisplay.has_extension("SHAPE"):
            logger.warning("X server lacks SHAPE; overlay will not be click-through")
            return
        root_id = xdisplay.screen().root.id
        xwindow = xdisplay.create_resource_object("window", window.winfo_id())
        # Tk reparents Toplevels into a wrapper window that keeps a full
        # input region, which would swallow the clicks anyway. Empty the
        # input region of the whole chain up to (excluding) the root.
        while xwindow.id != root_id:
            xwindow.shape_rectangles(
                shape.SO.Set, shape.SK.Input, X.Unsorted, 0, 0, []
            )
            parent = xwindow.query_tree().parent
            if parent is None or parent.id == root_id:
                break
            xwindow = parent
        xdisplay.sync()  # force errors to surface inside this try
    finally:
        xdisplay.close()
