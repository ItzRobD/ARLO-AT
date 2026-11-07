"""Locate the game's item tooltip anywhere on screen by its cream background.

The tooltip anchors to the hovered item, not the cursor, so a fixed
cursor-relative capture box drifts off it. Searching a downsampled copy of
the whole frame for the distinctive cream panel (#f9eedf) is cheap and finds
the tooltip wherever the game drew it - which also tells the overlay where
to sit.
"""

import numpy as np

# Tooltip background is RGB(249, 238, 223); same bounds as preprocess_tooltip.
CREAM_LO = np.array([240, 225, 210], dtype=np.uint8)
CREAM_HI = np.array([255, 250, 240], dtype=np.uint8)

# Smallest plausible tooltip body, as a fraction of the frame height. At
# 1440p the body is ~550x550 px; this keeps out small cream UI fragments.
MIN_SIZE_FRAC = 0.12
# A row is "tooltip" if it is at least this cream relative to the fullest row.
ROW_FRAC = 0.2
# Largest non-cream band bridged inside the tooltip, in pixels at 1440p.
MAX_GAP_PX = 48


def _longest_run(flags: np.ndarray, max_gap: int = 0) -> tuple[int, int] | None:
    """(start, end_exclusive) of the longest run of True values.

    Gaps of up to ``max_gap`` False values inside a run are bridged.
    """
    best: tuple[int, int] | None = None
    start = None
    last_on = -1
    for i, on in enumerate([*flags.tolist(), False]):
        if on:
            if start is None:
                start = i
            last_on = i
        elif start is not None and i - last_on > max_gap:
            if best is None or last_on + 1 - start > best[1] - best[0]:
                best = (start, last_on + 1)
            start = None
    return best


def find_tooltip(
    rgb: np.ndarray, *, step: int = 4
) -> tuple[int, int, int, int] | None:
    """Return the tooltip body's (left, top, right, bottom) in frame pixels.

    Args:
        rgb: HxWx3 uint8 frame.
        step: sampling stride; 4 keeps a 3440x1440 search to ~300k pixels.

    Rows count as tooltip when at least ROW_FRAC of the fullest row is
    cream; the coloured tag row and dense text lines dip well below a full
    row, so short dips (up to MAX_GAP_PX) are bridged rather than ending
    the run.
    """
    sub = rgb[::step, ::step, :3]
    mask = np.all((sub >= CREAM_LO) & (sub <= CREAM_HI), axis=2)
    min_px = max(4, int(rgb.shape[0] * MIN_SIZE_FRAC / step))

    row_counts = mask.sum(axis=1)
    if row_counts.max(initial=0) < min_px:
        return None
    max_gap = max(1, MAX_GAP_PX * rgb.shape[0] // 1440 // step)
    rows = _longest_run(row_counts >= row_counts.max() * ROW_FRAC, max_gap)
    if rows is None or rows[1] - rows[0] < min_px:
        return None

    col_counts = mask[rows[0] : rows[1]].sum(axis=0)
    cols = _longest_run(col_counts >= (rows[1] - rows[0]) * 0.5, max_gap)
    if cols is None or cols[1] - cols[0] < min_px:
        return None

    # The run above can stop early at grey description boxes or stat rows
    # that span nearly the full width. The tooltip's left and right padding
    # stays cream through all of those, so re-derive the vertical extent
    # from the rows whose both edge margins are cream.
    margin = max(2, (cols[1] - cols[0]) // 25)
    left_edge = mask[:, cols[0] : cols[0] + margin].mean(axis=1)
    right_edge = mask[:, cols[1] - margin : cols[1]].mean(axis=1)
    edges = _longest_run((left_edge >= 0.6) & (right_edge >= 0.6), 1)
    if edges is not None and edges[0] <= rows[0] < edges[1]:
        rows = (min(rows[0], edges[0]), max(rows[1], edges[1]))

    return (cols[0] * step, rows[0] * step, cols[1] * step, rows[1] * step)
