"""
OCR module for ARLO.
Screen capture and text extraction using Tesseract.
"""

import atexit
import hashlib
import re
import string
import time
from difflib import SequenceMatcher

import numpy as np
import pytesseract
from PIL import Image
from PIL import ImageOps
from pydantic import BaseModel

from .config import RegionMixin
from .config import get_screen_resolution
from .config import get_settings
from .config import logger
from .screen import Point
from .screen import ScreenCaptureUnavailable
from .screen import get_backend
from .tess_api import PSM_SINGLE_BLOCK
from .tess_api import PSM_SINGLE_LINE
from .tess_api import TessApi
from .tooltip_finder import find_tooltip
from .tooltip_finder import find_tooltips

# A plain-pass token that is only Roman numeral look-alikes ("|", "Il", "lV")
_NUMERAL_TOKEN = re.compile(r"[|l1!IiV]+")


def grab_frame_array(width: int, height: int) -> np.ndarray:
    """Whole captured monitor as an HxWx3 RGB array (a view when possible)."""
    backend = get_backend()
    frame_array = getattr(backend, "frame_array", None)
    if frame_array is not None:
        return frame_array()
    return np.asarray(grab_screen((0, 0, width, height)))


def _bbox_close(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int], tol: int
) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def grab_screen(bbox: tuple[int, int, int, int]) -> Image.Image:
    """Capture a screen region via the active screen backend."""
    return get_backend().grab(bbox)


class OCRResult(BaseModel):
    """Result of an OCR operation."""

    text: str | None
    confidence: float = 0.0
    raw_text: str = ""


def get_cursor_position() -> Point:
    """Get current cursor position on screen in physical pixels."""
    return get_backend().cursor_position()


class OCREngine:
    """OCR engine for extracting text from screen regions."""

    # The trigger word we're looking for
    TRIGGER_WORD = "INVENTORY"

    # Item names render as uppercase letters and Roman numerals. Restricting
    # OCR to this set stops "II" being misread as "Il"/"1l" and the trailing
    # numeral from collapsing to "I" (which silently picks the wrong tier).
    # No space: pytesseract splits the config on spaces, so a space in the
    # whitelist breaks it. Words run together ("HULLCRACKERIV"); the database
    # lookup is space-insensitive, so that still resolves correctly.
    NAME_WHITELIST = string.ascii_uppercase

    def __init__(self):
        """Initialize OCR engine."""
        settings = get_settings()

        # Set Tesseract path if configured
        if settings.tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = settings.tesseract_path

        self.debug_mode = settings.debug_mode
        self.debug_dir = settings.debug_output_dir

        # Tooltip capture settings
        self.tooltip_width = settings.tooltip_capture.width
        self.tooltip_height = settings.tooltip_capture.height
        self.tooltip_offset_x = settings.tooltip_capture.offset_x
        self.tooltip_offset_y = settings.tooltip_capture.offset_y

        self.detection = settings.tooltip_detection.lower()

        # In-process Tesseract (falls back to pytesseract when unavailable)
        self._tess_plain = TessApi.create(tesseract_cmd=settings.tesseract_path)
        self._tess_upper = TessApi.create(
            string.ascii_uppercase, tesseract_cmd=settings.tesseract_path
        )
        if self._tess_plain and self._tess_upper:
            logger.info("OCR: using in-process libtesseract")
            atexit.register(self._tess_plain.close)
            atexit.register(self._tess_upper.close)
        else:
            logger.info("OCR: using the tesseract binary (pytesseract)")

        # Frame mode state: the tooltip must hold still for two scans before
        # it is read (the game animates it in), and an unchanged tooltip is
        # not OCR'd again.
        self._prev_bbox: tuple[int, int, int, int] | None = None
        # Last bbox that passed the stability check; a scan close to it is
        # the same settled tooltip even if it differs from the previous scan
        self._accepted_bbox: tuple[int, int, int, int] | None = None
        self._last_jitter_log = float("-inf")
        self._cache_key: tuple | None = None
        self._cache_name: str | None = None

        # Physical-pixel bbox of the last tooltip found by frame detection;
        # read (atomically) by the GUI thread to anchor the popup.
        self.last_tooltip_bbox: tuple[int, int, int, int] | None = None

        # Crafting screen: panels seen last scan, and names per settled panel
        self._prev_panels: list[tuple[int, int, int, int]] = []
        self._panel_names: dict[tuple, str | None] = {}

        if self.debug_mode:
            self.debug_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def capture_region(region: RegionMixin) -> Image.Image:
        """Capture a screen region."""
        try:
            return grab_screen(region.bbox)
        except ScreenCaptureUnavailable:
            raise  # terminal: must reach the scanner's error handling, not become a black frame
        except OSError as e:
            logger.error(f"Screen grab failed for region {region.bbox}: {e}")
            # Return a dummy image to avoid crashing
            return Image.new("RGB", (region.width, region.height), color="black")

    def capture_around_cursor(self) -> tuple[Image.Image, Point]:
        """
        Capture a region around the current cursor position.
        Returns the captured image and the cursor position.

        If cursor is in the right 30% of the screen, the X offset is flipped
        to capture the tooltip on the left side instead.
        """

        cursor = get_cursor_position()
        screen_width, screen_height = get_screen_resolution()
        if self.debug_mode:
            logger.debug(f"Cursor (physical, game monitor): ({cursor.x}, {cursor.y})")
            self._maybe_save_full_frame(screen_width, screen_height)

        # TODO: flipping the tooltip region on the right side of screen
        # to be implemented properly
        # # Check if cursor is in right 30% of screen
        # right_threshold = screen_width * 0.7

        # if cursor.x > right_threshold:
        #     # Flip X offset (and account for capture width)
        #     offset_x = -self.tooltip_offset_x - self.tooltip_width
        # else:
        #     offset_x = self.tooltip_offset_x
        offset_x = self.tooltip_offset_x

        # Calculate capture region around cursor
        left = cursor.x + offset_x
        top = cursor.y + self.tooltip_offset_y
        right = left + self.tooltip_width
        bottom = top + self.tooltip_height

        # Clamp to valid screen coordinates (handle multi-monitor)
        # Note: We clamp to primary monitor bounds for simplicity
        left = max(0, min(left, screen_width - 1))
        top = max(0, min(top, screen_height - 1))
        right = max(left + 1, min(right, screen_width))
        bottom = max(top + 1, min(bottom, screen_height))

        # Log if we had to clamp significantly
        if (
            right - left < self.tooltip_width // 2
            or bottom - top < self.tooltip_height // 2
        ):
            logger.debug(
                f"Capture region clamped significantly: cursor=({cursor.x}, {cursor.y}), bbox=({left}, {top}, {right}, {bottom})"
            )

        try:
            image = grab_screen((left, top, right, bottom))
        except ScreenCaptureUnavailable:
            raise  # terminal: must reach the scanner's error handling
        except OSError as e:
            logger.error(
                f"Screen grab failed at bbox ({left}, {top}, {right}, {bottom}): {e}"
            )
            # Return a dummy image to avoid crashing
            return Image.new("RGB", (100, 100), color="black"), cursor

        return image, cursor

    def _maybe_save_full_frame(self, width: int, height: int) -> None:
        """Debug aid: save the whole game monitor every few seconds."""
        now = time.monotonic()
        if now - getattr(self, "_last_full_frame", 0.0) < 3.0:
            return
        self._last_full_frame = now
        try:
            grab_screen((0, 0, width, height)).save(self.debug_dir / "full_frame.png")
        except OSError as e:
            logger.debug(f"Full-frame debug capture failed: {e}")

    def check_trigger_any(self, regions: list[RegionMixin]) -> bool:
        """
        Check if the trigger word (INVENTORY) is visible in any of the regions.
        """
        return any(self.check_trigger(region) for region in regions)

    @staticmethod
    def preprocess_for_ocr(
        image: Image.Image, *, invert: bool = True, scale: int = 2
    ) -> Image.Image:
        """
        Preprocess image for better OCR accuracy.

        Args:
            image: Input image
            invert: Whether to invert colors (for white text on dark bg)
            scale: Scale factor for upscaling
        """
        # Convert to grayscale
        gray = image.convert("L")

        # Upscale for better OCR
        if scale > 1:
            new_size = (gray.width * scale, gray.height * scale)
            gray = gray.resize(new_size, Image.Resampling.BILINEAR)

        # Invert if needed (OCR prefers black text on white)
        if invert:
            gray = ImageOps.invert(gray)

        # Apply threshold for cleaner text
        threshold = 128
        gray = gray.point(lambda x: 255 if x > threshold else 0, "1")

        return gray

    def preprocess_tooltip(self, image: Image.Image) -> Image.Image:
        """
        Preprocess tooltip image by isolating the cream-colored tooltip background
        and extracting dark text from it.
        """
        img_array = np.array(image)

        # Tooltip background is #f9eedf = RGB(249, 238, 223)
        lower_bound = np.array([240, 225, 210])
        upper_bound = np.array([255, 250, 240])

        # Create mask for tooltip background
        mask = np.all((img_array >= lower_bound) & (img_array <= upper_bound), axis=2)

        # Find rows and columns that are predominantly cream (>50%)
        row_cream_percentage = np.sum(mask, axis=1) / mask.shape[1]
        col_cream_percentage = np.sum(mask, axis=0) / mask.shape[0]

        # Find first/last row with significant cream
        cream_rows = np.where(row_cream_percentage > 0.5)[0]
        cream_cols = np.where(col_cream_percentage > 0.3)[0]

        if len(cream_rows) == 0 or len(cream_cols) == 0:
            result = Image.new("L", (image.width * 2, image.height * 2), 255)
            return result

        y_min, y_max = cream_rows[0], cream_rows[-1]
        x_min, x_max = cream_cols[0], cream_cols[-1]

        # First crop - to the cream region
        cropped = img_array[y_min:y_max, x_min:x_max]
        cropped_mask = mask[y_min:y_max, x_min:x_max]

        # Find columns that are mostly cream (guard against empty crop)
        n_rows = cropped_mask.shape[0]
        if n_rows == 0:
            result = Image.new("L", (image.width * 2, image.height * 2), 255)
            return result
        col_cream_pct = np.sum(cropped_mask, axis=0) / n_rows
        tight_cols = np.where(col_cream_pct > 0.5)[0]

        if len(tight_cols) == 0:
            result = Image.new("L", (image.width * 2, image.height * 2), 255)
            return result

        tight_x_min, tight_x_max = tight_cols[0], tight_cols[-1]

        # Tight crop - removes the colored borders
        tight_cropped = cropped[:, tight_x_min:tight_x_max]

        # Detect "colored" pixels - specifically looking for saturated colors (tags)
        # Tags are colorful (blue, green, etc.) - they have high saturation
        # Text is nearly black, cream is desaturated
        r, g, b = tight_cropped[:, :, 0], tight_cropped[:, :, 1], tight_cropped[:, :, 2]

        # Calculate a simple "colorfulness" metric
        # Colored pixels have larger differences between RGB channels
        max_rgb = np.maximum(np.maximum(r, g), b)
        min_rgb = np.minimum(np.minimum(r, g), b)
        color_diff = max_rgb - min_rgb

        # Colored pixels: significant difference between channels AND not too dark AND not too bright
        is_colored = (color_diff > 30) & (max_rgb > 80) & (max_rgb < 240)

        # Count colored pixels per row - only exclude rows with SIGNIFICANT color (>5% of row)
        row_color_percentage = np.sum(is_colored, axis=1) / is_colored.shape[1]
        row_has_significant_color = row_color_percentage > 0.05

        # Create output - white background
        result = (
            np.ones((tight_cropped.shape[0], tight_cropped.shape[1]), dtype=np.uint8)
            * 255
        )

        # Only include dark text from rows WITHOUT significant colored pixels
        is_dark = np.all(tight_cropped < 100, axis=2)

        # Vectorized masking: rows without significant color AND dark pixels
        mask = (~row_has_significant_color[:, np.newaxis]) & is_dark
        result[mask] = 0

        # Convert to PIL
        result_image = Image.fromarray(result, mode="L")

        # Upscale for better OCR
        scale = 2
        new_size = (result_image.width * scale, result_image.height * scale)
        result_image = result_image.resize(new_size, Image.Resampling.BILINEAR)

        # Debug: save intermediate images
        if self.debug_mode:
            Image.fromarray(mask.astype(np.uint8) * 255).save(
                self.debug_dir / "tooltip_mask.png"
            )
            Image.fromarray(cropped).save(self.debug_dir / "tooltip_cropped.png")
            Image.fromarray(tight_cropped).save(
                self.debug_dir / "tooltip_tight_cropped.png"
            )
            Image.fromarray(is_colored.astype(np.uint8) * 255).save(
                self.debug_dir / "tooltip_colored.png"
            )
            Image.fromarray(
                row_has_significant_color[:, np.newaxis]
                .repeat(tight_cropped.shape[1], axis=1)
                .astype(np.uint8)
                * 255
            ).save(self.debug_dir / "tooltip_colored_rows.png")
            Image.fromarray(result).save(self.debug_dir / "tooltip_text_mask.png")

        return result_image

    def extract_text(
        self,
        image: Image.Image,
        *,
        single_line: bool = True,
        whitelist: str | None = None,
    ) -> OCRResult:
        """
        Extract text from an image.

        Args:
            image: Preprocessed image
            single_line: Whether to expect a single line of text
            whitelist: Character whitelist for OCR
        """
        # Build Tesseract config
        config_parts = []

        # Page segmentation mode
        if single_line:
            config_parts.append("--psm 7")  # Single line
        else:
            config_parts.append("--psm 6")  # Block of text

        # Character whitelist
        if whitelist:
            config_parts.append(f'-c tessedit_char_whitelist="{whitelist}"')

        config = " ".join(config_parts)

        try:
            # Get text with confidence data
            data = pytesseract.image_to_data(
                image, config=config, output_type=pytesseract.Output.DICT
            )

            # Extract text and calculate average confidence
            texts = []
            confidences = []

            for i, text in enumerate(data["text"]):
                conf = data["conf"][i]
                if text.strip() and conf > 0:
                    texts.append(text.strip())
                    confidences.append(conf)

            raw_text = " ".join(texts)
            avg_confidence = sum(confidences) / len(confidences) if confidences else 0

            # Clean up text
            cleaned = self._clean_text(raw_text)

            return OCRResult(
                text=cleaned or None,
                confidence=avg_confidence,
                raw_text=raw_text,
            )

        except pytesseract.TesseractError as e:
            if self.debug_mode:
                logger.error(f"OCR Error: {e}")
            return OCRResult(text=None, confidence=0, raw_text="")

    @staticmethod
    def _clean_text(text: str) -> str:
        """Clean OCR output text."""
        # Remove common OCR artifacts
        text = re.sub(r"[|\\/_]", "", text)
        # Normalize whitespace
        text = re.sub(r"\s+", " ", text)
        # Strip
        return text.strip()

    def check_trigger(self, region: RegionMixin) -> bool:
        """
        Check if the trigger word (INVENTORY) is visible in the region.

        This is optimized for speed - we just need to detect the word,
        not extract it perfectly.
        """
        # Capture
        image = self.capture_region(region)

        # Preprocess
        processed = self.preprocess_for_ocr(image, invert=True, scale=2)

        # Save debug image if enabled
        if self.debug_mode:
            image.save(self.debug_dir / "trigger_raw.png")
            processed.save(self.debug_dir / "trigger_processed.png")

        # Extract with limited whitelist for speed using image_to_string (faster than image_to_data)
        try:
            text = self.ocr_text(processed, psm=PSM_SINGLE_LINE, upper=True)
            if text:
                # Fuzzy match - allow for some OCR errors
                return self._fuzzy_match(text.strip().upper(), self.TRIGGER_WORD)
        except pytesseract.TesseractError:
            pass

        return False

    # Share of the tooltip body (from the top) that is OCR'd: the name and
    # the first description line, which ends the name block. Skipping the
    # rest cuts Tesseract time by about a quarter.
    TOOLTIP_READ_FRAC = 0.5

    def read_hovered_item_name(self) -> str | None:
        """Read the hovered item's name using the configured detection mode."""
        if self.detection == "cursor":
            self.last_tooltip_bbox = None
            return self.extract_item_name_at_cursor()
        return self.extract_item_name_from_frame()

    # Max movement (physical px) between scans for a tooltip to count as
    # settled rather than still animating in.
    STABLE_TOLERANCE_PX = 6
    # Max movement from the last accepted (settled) bbox that still counts
    # as the same tooltip. find_tooltip samples every 4th pixel, so what is
    # behind the tooltip can flip an edge between two values 8 px apart.
    ACCEPTED_TOLERANCE_PX = 12

    def ocr_text(
        self, image: Image.Image, *, psm: int = PSM_SINGLE_BLOCK, upper: bool = False
    ) -> str:
        """OCR an image, optionally restricted to A-Z."""
        api = self._tess_upper if upper else self._tess_plain
        if api is not None:
            return api.image_to_string(image, psm)
        config = f"--psm {psm}"
        if upper:
            config += f" -c tessedit_char_whitelist={string.ascii_uppercase}"
        return pytesseract.image_to_string(image, config=config)

    def extract_item_name_from_frame(self) -> str | None:
        """Find the tooltip anywhere on the game monitor and read its name.

        Returns None (and clears last_tooltip_bbox) while no tooltip is
        visible or while it is still moving.
        """
        width, height = get_screen_resolution()
        try:
            frame = grab_frame_array(width, height)
        except ScreenCaptureUnavailable:
            raise
        except OSError as e:
            logger.error(f"Full-frame grab failed: {e}")
            return None

        bbox = find_tooltip(frame)
        prev, self._prev_bbox = self._prev_bbox, bbox
        accepted = self._accepted_bbox
        stable = bbox is not None and (
            (prev is not None and _bbox_close(bbox, prev, self.STABLE_TOLERANCE_PX))
            or (
                accepted is not None
                and _bbox_close(bbox, accepted, self.ACCEPTED_TOLERANCE_PX)
            )
        )
        if not stable:
            self._accepted_bbox = None
            self.last_tooltip_bbox = None  # gone, or still animating in
            return None
        self._log_bbox_jitter(accepted, bbox)
        self._accepted_bbox = self.last_tooltip_bbox = bbox

        left, top, right, bottom = bbox
        pad = 8
        read_bottom = top + int((bottom - top) * self.TOOLTIP_READ_FRAC)
        region = frame[
            max(0, top - pad) : min(height, read_bottom),
            max(0, left - pad) : min(width, right + pad),
        ]
        # Same tooltip as last time (position and pixels): reuse the name
        key = (bbox, hashlib.blake2b(region[::4, ::4].tobytes(), digest_size=16).digest())
        if key == self._cache_key:
            return self._cache_name

        image = Image.fromarray(np.ascontiguousarray(region), "RGB")
        if self.debug_mode:
            logger.debug(f"Tooltip found at {bbox}")
            image.save(self.debug_dir / "tooltip_capture_raw.png")
            self._maybe_save_full_frame(width, height)

        processed = self.preprocess_tooltip(image)
        if self.debug_mode:
            processed.save(self.debug_dir / "tooltip_capture_processed.png")
        try:
            item_name = self._read_item_name(processed)
        except pytesseract.TesseractError as e:
            logger.error(f"OCR Error: {e}")
            return None
        self._cache_key, self._cache_name = key, item_name
        if item_name:
            logger.debug(f"Extracted item name: '{item_name}'")
        return item_name

    CRAFTING_WORD = "CRAFTING"
    # Station header strip (left half, top 5%): "<STATION> - LEVEL n
    # CRAFTING LEVEL UP". The station name's length moves the word, so the
    # whole strip is read rather than a fixed trigger box.
    HEADER_STRIP = (0.0, 0.0, 0.5, 0.05)

    def check_crafting(self) -> bool:
        """True while a workshop station's CRAFTING header is on screen."""
        width, height = get_screen_resolution()
        fx0, fy0, fx1, fy1 = self.HEADER_STRIP
        bbox = (
            int(width * fx0),
            int(height * fy0),
            int(width * fx1),
            int(height * fy1),
        )
        image = self.preprocess_for_ocr(grab_screen(bbox), invert=True, scale=2)
        try:
            text = self.ocr_text(image, psm=PSM_SINGLE_LINE, upper=True)
        except pytesseract.TesseractError:
            return False
        return self._contains_word(text, self.CRAFTING_WORD)

    @staticmethod
    def _contains_word(text: str, word: str, threshold: float = 0.85) -> bool:
        """``word`` appears in ``text`` (spaces ignored), allowing an OCR slip."""
        flat = re.sub(r"[^A-Z]", "", text.upper())
        if word in flat:
            return True
        n = len(word)
        return any(
            SequenceMatcher(None, flat[i : i + n], word).ratio() >= threshold
            for i in range(max(0, len(flat) - n + 1))
        )

    PANEL_NAME_CACHE_MAX = 32

    def read_panel_names(self) -> list[tuple[str, tuple[int, int, int, int]]]:
        """Crafting screen: (name, bbox) for every settled cream panel.

        The hovered blueprint's tooltip and the selected blueprint's detail
        panel can both be up. A panel is read once it holds still for two
        scans; an unchanged panel reuses its name.
        """
        width, height = get_screen_resolution()
        try:
            frame = grab_frame_array(width, height)
        except ScreenCaptureUnavailable:
            raise
        except OSError as e:
            logger.error(f"Full-frame grab failed: {e}")
            return []
        panels = find_tooltips(frame)
        prev, self._prev_panels = self._prev_panels, panels
        found: list[tuple[str, tuple[int, int, int, int]]] = []
        for bbox in panels:
            if not any(
                _bbox_close(bbox, p, self.ACCEPTED_TOLERANCE_PX) for p in prev
            ):
                continue  # new or still animating in
            left, top, right, bottom = bbox
            pad = 8
            read_bottom = top + int((bottom - top) * self.TOOLTIP_READ_FRAC)
            region = frame[
                max(0, top - pad) : min(height, read_bottom),
                max(0, left - pad) : min(width, right + pad),
            ]
            key = (
                bbox,
                hashlib.blake2b(region[::4, ::4].tobytes(), digest_size=16).digest(),
            )
            if key not in self._panel_names:
                if len(self._panel_names) >= self.PANEL_NAME_CACHE_MAX:
                    self._panel_names.clear()
                image = Image.fromarray(np.ascontiguousarray(region), "RGB")
                try:
                    name = self._read_item_name(self.preprocess_tooltip(image))
                except pytesseract.TesseractError as e:
                    logger.error(f"OCR Error: {e}")
                    continue
                self._panel_names[key] = name
            name = self._panel_names[key]
            if name:
                found.append((name, bbox))
        return found

    def _log_bbox_jitter(
        self,
        old: tuple[int, int, int, int] | None,
        new: tuple[int, int, int, int],
    ) -> None:
        """Debug aid: note a settled tooltip's bbox moving within tolerance
        (at most once a second), so jitter reports come with data."""
        if not self.debug_mode or old is None or old == new:
            return
        now = time.monotonic()
        if now - self._last_jitter_log < 1.0:
            return
        self._last_jitter_log = now
        delta = tuple(n - o for n, o in zip(new, old, strict=True))
        logger.debug(f"Tooltip bbox jitter: {old} -> {new} (delta {delta})")

    def extract_item_name_at_cursor(self) -> str | None:
        """
        Extract item name from tooltip near the cursor.

        Captures area around cursor, finds the tooltip, and extracts the item name.
        Returns the cleaned item name or None if extraction fails.
        """
        # Capture around cursor
        image, _ = self.capture_around_cursor()

        # Save debug image if enabled
        if self.debug_mode:
            image.save(self.debug_dir / "tooltip_capture_raw.png")

        # The tooltip has a cream/beige background with dark text
        # We need to find the item name which is the large bold text
        # It appears after "ACTIONS" header and category tags

        # Strategy: Look for the item name area
        # The item name is typically in the upper portion of the tooltip
        # in large dark text on the cream background

        # First, let's try to find and extract from the whole capture
        # using appropriate preprocessing for dark text on light background

        processed = self.preprocess_tooltip(image)

        if self.debug_mode:
            processed.save(self.debug_dir / "tooltip_capture_processed.png")

        # Extract the item name from the tooltip area
        try:
            item_name = self._read_item_name(processed)
        except pytesseract.TesseractError as e:
            logger.error(f"OCR Error: {e}")
            return None

        if item_name:
            logger.debug(f"Extracted item name: '{item_name}'")
        return item_name

    def _read_item_name(self, processed: Image.Image) -> str | None:
        """Read the item name from a preprocessed tooltip via two OCR passes.

        The plain pass locates the name block (the leading mostly-uppercase
        lines, set off from the lower-case description by case). A second
        uppercase-whitelisted pass supplies the text with Roman numerals
        intact - a plain pass reads "II" as "Il" or drops it to "I", which
        silently selects the wrong item tier.
        """
        plain = self.ocr_text(processed)
        logger.debug(f"Raw tooltip OCR:\n{plain}")

        plain_names = self._name_lines(plain)
        if not plain_names:
            return None

        whitelisted = self.ocr_text(processed, upper=True)
        name_lines = [ln.strip() for ln in whitelisted.split("\n") if ln.strip()]
        name = self._clean_text(
            " ".join(self._matching_lines(name_lines, plain_names))
        )
        return self._clean_text(self._merge_numeral(plain_names[-1], name)) or None

    @staticmethod
    def _matching_lines(lines: list[str], plain_names: list[str]) -> list[str]:
        """The run of ``lines`` that best matches the plain pass's name lines.

        Icon noise can add a junk line before the name in either pass, so the
        two passes' lines do not always line up by position.
        """
        n = len(plain_names)
        if len(lines) <= n:
            return lines

        def flat(parts: list[str]) -> str:
            return re.sub(r"[^A-Z]", "", " ".join(parts).upper())

        want = flat(plain_names)
        return max(
            (lines[i : i + n] for i in range(len(lines) - n + 1)),
            key=lambda run: SequenceMatcher(None, flat(run), want).ratio(),
        )

    @staticmethod
    def _merge_numeral(plain_name_line: str, upper_text: str) -> str:
        """Restore a tier numeral the uppercase pass dropped.

        A lone "I" in a bold title often vanishes from the whitelisted pass
        ("TEMPEST I" reads as "TEMPEST"), while the plain pass keeps it as a
        look-alike ("TEMPEST |"). When the plain line ends in a separate
        numeral-like token and the uppercase text ends in a shorter numeral
        run, replace that run with the normalised numeral. A longer or equal
        run is kept: the plain pass is the one that drops "II" to "I".
        """
        tokens = plain_name_line.split()
        if len(tokens) < 2 or not _NUMERAL_TOKEN.fullmatch(tokens[-1]):
            return upper_text
        numeral = "".join("V" if c == "V" else "I" for c in tokens[-1])

        def tail(text: str) -> int:
            return len(text) - len(text.rstrip("IV"))

        upper = upper_text.rstrip()
        # Numeral-like letters that end the name word itself ("TAXI")
        word_tail = tail(re.sub(r"[^A-Z]", "", tokens[-2].upper()))
        run = max(0, tail(upper) - word_tail)
        if run >= len(numeral):
            return upper_text
        return f"{upper[: len(upper) - run].rstrip()} {numeral}"

    @staticmethod
    def _upper_ratio(text: str) -> float:
        """Fraction of alphabetic characters that are uppercase."""
        alpha = [c for c in text if c.isalpha()]
        return sum(c.isupper() for c in alpha) / len(alpha) if alpha else 0.0

    # Lines shorter than this before the name are icon noise, not description
    MAX_LEADING_JUNK = 8

    @classmethod
    def _name_lines(cls, text: str) -> list[str]:
        """The leading mostly-uppercase lines that form the item name, raw.

        Stops at the first lower-case line (the description), tolerating a
        stray mis-cased character (e.g. "II" read as "Il") via a ratio.
        Short junk before the name (a tag icon read as "arn") is skipped;
        the description's first line is far longer.
        Lines are returned uncleaned so a numeral read as "|" survives.
        """
        lines: list[str] = []
        for raw_line in text.split("\n"):
            line = cls._clean_text(raw_line)
            if len(line) >= 2 and cls._upper_ratio(line) >= 0.6:
                lines.append(raw_line.strip())
            elif lines or len(line) >= cls.MAX_LEADING_JUNK:
                break
        return lines

    @classmethod
    def _count_name_lines(cls, text: str) -> int:
        """Count the leading mostly-uppercase lines that form the item name."""
        return len(cls._name_lines(text))

    def parse_item_name_from_tooltip(self, text: str) -> str | None:
        """
        Parse item name from tooltip OCR text.

        Item names are in ALL CAPS (like "ADVANCED ELECTRICAL COMPONENTS")
        and may span multiple lines.
        """
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        if not lines:
            return None

        # Find consecutive ALL CAPS lines (the item name)
        name_lines = []
        found_name = False

        for line in lines:
            # Clean the line
            cleaned = self._clean_text(line)
            if not cleaned or len(cleaned) < 2:
                continue

            # Check if line is ALL CAPS (item name style)
            # Allow spaces, numbers, and punctuation
            alpha_chars = [c for c in cleaned if c.isalpha()]
            if not alpha_chars:
                continue

            is_all_caps = all(c.isupper() for c in alpha_chars)

            if is_all_caps and len(cleaned) >= 3:
                # This looks like an item name line
                name_lines.append(cleaned)
                found_name = True
            elif found_name:
                # We were collecting name lines but hit a non-caps line
                # This means the name is complete
                break

        if name_lines:
            # Join multi-line names with a space
            return " ".join(name_lines)

        return None

    def extract_item_name(self, region: RegionMixin) -> str | None:
        """
        Legacy method - extract item name from a fixed region.
        Kept for calibration tool compatibility.
        """
        image = self.capture_region(region)
        processed = self.preprocess_tooltip(image)

        if self.debug_mode:
            image.save(self.debug_dir / "tooltip_raw.png")
            processed.save(self.debug_dir / "tooltip_processed.png")

        try:
            text = pytesseract.image_to_string(processed, config="--psm 6")
            return self.parse_item_name_from_tooltip(text)
        except pytesseract.TesseractError as e:
            logger.error(f"OCR Error: {e}")
            return None

    @staticmethod
    def _fuzzy_match(text: str, target: str, threshold: float = 0.7) -> bool:
        """
        Check if text approximately matches target.
        Uses simple character overlap ratio.
        """
        if not text:
            return False

        # Check if target appears as substring
        if target in text:
            return True

        # Check character overlap
        text_chars = set(text)
        target_chars = set(target)
        overlap = len(text_chars & target_chars) / len(target_chars)

        return overlap >= threshold


class OCREngineManager:
    """Manages the singleton OCREngine instance."""

    _instance: OCREngine | None = None

    @classmethod
    def get(cls) -> OCREngine:
        """Get the OCR engine instance, creating it if needed."""
        if cls._instance is None:
            cls._instance = OCREngine()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (mainly for testing or reloading settings)."""
        cls._instance = None


def get_ocr_engine() -> OCREngine:
    """Get the singleton OCR engine instance."""
    return OCREngineManager.get()
