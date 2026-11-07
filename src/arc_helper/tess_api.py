"""In-process Tesseract via its C API (ctypes), avoiding a subprocess per call.

pytesseract launches the tesseract binary for every image, which costs about
75 ms of process start-up and model loading each time - most of a tooltip
read. Keeping one initialised TessBaseAPI per configuration reduces a read
to the actual recognition work. No compiler or extra Python package is
needed: this binds the libtesseract that the tesseract package installs.

If the library cannot be loaded, TessApi.create() returns None and callers
fall back to pytesseract.
"""

import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

from PIL import Image

from .config import logger

PSM_SINGLE_BLOCK = 6
PSM_SINGLE_LINE = 7

# pytesseract hands tesseract PNGs without DPI, which it treats as 70 ppi.
# Matching that keeps recognition identical to the subprocess path.
SOURCE_PPI = 70

_LIB_NAMES = (
    "tesseract",
    "libtesseract.so.5",
    "libtesseract.so.5.5",
    "libtesseract-5",
    "libtesseract-5.dll",
)


def _load_library(tesseract_cmd: str | None) -> ctypes.CDLL | None:
    candidates: list[str] = []
    if tesseract_cmd:
        # Bundled Windows build: the DLL sits next to tesseract.exe
        folder = Path(tesseract_cmd).parent
        candidates += [str(p) for p in folder.glob("libtesseract*.dll")]
    for name in _LIB_NAMES:
        found = ctypes.util.find_library(name)
        candidates.append(found or name)
    for candidate in candidates:
        try:
            return ctypes.CDLL(candidate)
        except OSError:
            continue
    return None


def _bind(lib: ctypes.CDLL) -> None:
    p = ctypes.c_void_p
    lib.TessBaseAPICreate.restype = p
    lib.TessBaseAPICreate.argtypes = []
    lib.TessBaseAPIInit3.restype = ctypes.c_int
    lib.TessBaseAPIInit3.argtypes = [p, ctypes.c_char_p, ctypes.c_char_p]
    lib.TessBaseAPISetVariable.restype = ctypes.c_int
    lib.TessBaseAPISetVariable.argtypes = [p, ctypes.c_char_p, ctypes.c_char_p]
    lib.TessBaseAPISetPageSegMode.restype = None
    lib.TessBaseAPISetPageSegMode.argtypes = [p, ctypes.c_int]
    lib.TessBaseAPISetImage.restype = None
    lib.TessBaseAPISetImage.argtypes = [
        p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    lib.TessBaseAPISetSourceResolution.restype = None
    lib.TessBaseAPISetSourceResolution.argtypes = [p, ctypes.c_int]
    lib.TessBaseAPIGetUTF8Text.restype = p
    lib.TessBaseAPIGetUTF8Text.argtypes = [p]
    lib.TessDeleteText.restype = None
    lib.TessDeleteText.argtypes = [p]
    lib.TessBaseAPIClear.restype = None
    lib.TessBaseAPIClear.argtypes = [p]
    lib.TessBaseAPIEnd.restype = None
    lib.TessBaseAPIEnd.argtypes = [p]
    lib.TessBaseAPIDelete.restype = None
    lib.TessBaseAPIDelete.argtypes = [p]


class TessApi:
    """One initialised Tesseract instance with a fixed whitelist.

    Not thread-safe; ARLO only calls it from the scanner thread.
    """

    def __init__(self, lib: ctypes.CDLL, handle: int, whitelist: str):
        self._lib = lib
        self._handle = handle
        self.whitelist = whitelist

    @classmethod
    def create(
        cls, whitelist: str = "", tesseract_cmd: str | None = None
    ) -> "TessApi | None":
        lib = _load_library(tesseract_cmd)
        if lib is None:
            return None
        try:
            _bind(lib)
        except AttributeError as e:  # not a libtesseract with the C API
            logger.debug(f"libtesseract C API unavailable: {e}")
            return None
        handle = lib.TessBaseAPICreate()
        datapath = os.environ.get("TESSDATA_PREFIX")
        if datapath is None and tesseract_cmd and sys.platform == "win32":
            datapath = str(Path(tesseract_cmd).parent / "tessdata")
        rc = lib.TessBaseAPIInit3(
            handle, datapath.encode() if datapath else None, b"eng"
        )
        if rc != 0:
            lib.TessBaseAPIDelete(handle)
            logger.debug("libtesseract Init3 failed; using the tesseract binary")
            return None
        if whitelist:
            lib.TessBaseAPISetVariable(
                handle, b"tessedit_char_whitelist", whitelist.encode()
            )
        return cls(lib, handle, whitelist)

    def image_to_string(self, image: Image.Image, psm: int = PSM_SINGLE_BLOCK) -> str:
        gray = image.convert("L")
        data = gray.tobytes()
        lib, h = self._lib, self._handle
        lib.TessBaseAPISetPageSegMode(h, psm)
        lib.TessBaseAPISetImage(h, data, gray.width, gray.height, 1, gray.width)
        lib.TessBaseAPISetSourceResolution(h, SOURCE_PPI)
        ptr = lib.TessBaseAPIGetUTF8Text(h)
        try:
            text = ctypes.string_at(ptr).decode("utf-8", "replace") if ptr else ""
        finally:
            if ptr:
                lib.TessDeleteText(ptr)
            lib.TessBaseAPIClear(h)
        return text

    def close(self) -> None:
        if self._handle:
            self._lib.TessBaseAPIEnd(self._handle)
            self._lib.TessBaseAPIDelete(self._handle)
            self._handle = 0
