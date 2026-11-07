"""Backend-neutral types and selection logic for screen access.

This module must stay free of arc_helper.config imports so unit tests
can import it without triggering settings/logging setup.
"""

from collections.abc import Mapping
from typing import Protocol

from PIL import Image
from pydantic import BaseModel


class Point(BaseModel):
    """Screen coordinates in physical pixels."""

    x: int
    y: int


class ScreenCaptureUnavailable(OSError):  # noqa: N818
    """Screen capture is permanently unavailable (e.g. share revoked).

    Raised instead of plain OSError so callers that substitute dummy
    frames for transient grab errors can let this one propagate.
    """


class ScreenBackend(Protocol):
    """Platform-specific screen access. All coordinates are physical pixels."""

    name: str

    def start(self) -> None:
        """Acquire resources. grab()/cursor_position()/resolution() are only
        valid after this. Must not call back into get_backend() or
        get_screen_resolution() (non-reentrant initialization lock).
        """
        ...

    def stop(self) -> None:
        """Release resources. Safe to call multiple times."""
        ...

    def grab(self, bbox: tuple[int, int, int, int]) -> Image.Image:
        """Capture (left, top, right, bottom) and return an RGB image."""
        ...

    def cursor_position(self) -> Point:
        """Current pointer position in physical pixels."""
        ...

    def resolution(self) -> tuple[int, int]:
        """(width, height) of the captured monitor in physical pixels."""
        ...

    @property
    def tk_scale(self) -> float:
        """Divide physical pixels by this to get tkinter window coordinates.

        A property (not a plain attribute like ``name``) because backends
        compute it at runtime from the compositor's scale factor.
        """
        ...

    @property
    def tk_origin(self) -> tuple[int, int]:
        """tkinter/X11 coordinates of the captured monitor's top-left corner.

        Physical (stream) position p maps to tk position origin + p / tk_scale.
        Non-zero when the captured monitor is not at the X11 root origin
        (multi-monitor Wayland setups).
        """
        ...


def choose_backend_name(
    platform: str, environ: Mapping[str, str], override: str = "auto"
) -> str:
    """Pick the backend: 'windows', 'x11' or 'wayland'."""
    if platform == "win32":
        return "windows"
    override = override.lower()
    if override in {"x11", "wayland"}:
        return override
    if environ.get("WAYLAND_DISPLAY") or environ.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    return "x11"


class MonitorGeometry(BaseModel):
    """An X11/XWayland monitor rectangle, in X11 root-window coordinates."""

    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False


def pick_monitor(
    monitors: list[MonitorGeometry],
    connector: str | None,
    stream_size: tuple[int, int],
) -> MonitorGeometry | None:
    """Find the X11 monitor that the portal stream is capturing.

    Prefers the connector name the portal reports (e.g. "DP-2"); otherwise a
    monitor with the stream's exact size (XWayland in physical pixels), then
    the primary monitor. Returns None if there is nothing to choose from.
    """
    if not monitors:
        return None
    if connector:
        for m in monitors:
            if m.name == connector:
                return m
    same_size = [m for m in monitors if (m.width, m.height) == stream_size]
    if len(same_size) == 1:
        return same_size[0]
    for m in monitors:
        if m.primary:
            return m
    return monitors[0] if len(monitors) == 1 else None


def compute_scale(physical_width: int, logical_width: int) -> float:
    """Scale factor between physical pixels and X11/tk logical pixels.

    Returns physical_width / logical_width, or 1.0 if logical_width <= 0.
    """
    if logical_width <= 0:
        return 1.0
    return physical_width / logical_width
