"""In-process Tesseract binding (skipped when libtesseract is absent)."""

import string

import pytest
from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

from arc_helper.tess_api import PSM_SINGLE_LINE
from arc_helper.tess_api import TessApi


@pytest.fixture(scope="module")
def upper_api():
    api = TessApi.create(string.ascii_uppercase)
    if api is None:
        pytest.skip("libtesseract not available")
    yield api
    api.close()


def _text_image(text: str) -> Image.Image:
    image = Image.new("L", (900, 120), 255)
    draw = ImageDraw.Draw(image)
    draw.text((20, 20), text, fill=0, font=ImageFont.load_default(size=64))
    return image


def test_reads_uppercase_line(upper_api):
    text = upper_api.image_to_string(_text_image("CANISTER"), PSM_SINGLE_LINE)
    assert text.strip() == "CANISTER"


def test_reusable_across_calls(upper_api):
    for word in ("RENEGADE", "STITCHER"):
        assert upper_api.image_to_string(_text_image(word), PSM_SINGLE_LINE).strip() == word
