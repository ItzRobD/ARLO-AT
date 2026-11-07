"""Tests for item-name line detection used by the two-pass OCR reader."""

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from arc_helper.arctracker.catalogue import Catalogue
from arc_helper.ocr import OCREngine


def test_upper_ratio():
    assert OCREngine._upper_ratio("RASCAL") == 1.0  # noqa: SLF001
    assert OCREngine._upper_ratio("Fires explosive") < 0.3  # noqa: SLF001
    assert OCREngine._upper_ratio("") == 0.0  # noqa: SLF001


def test_single_line_name():
    text = "RASCALII\nFires explosive projectiles that only"
    assert OCREngine._count_name_lines(text) == 1  # noqa: SLF001


def test_tolerates_miscased_numeral():
    # "II" misread as "Il" is still mostly uppercase -> counts as the name line
    text = "BETTINA Il\nHas slow fire rate and high damage"
    assert OCREngine._count_name_lines(text) == 1  # noqa: SLF001


def test_multiword_multiline_name():
    text = "ADVANCED ELECTRICAL\nCOMPONENTS\nA crafting material"
    assert OCREngine._count_name_lines(text) == 2  # noqa: SLF001


def test_zero_when_description_leads():
    text = "the quick brown fox\nmore lowercase text"
    assert OCREngine._count_name_lines(text) == 0  # noqa: SLF001


def test_skips_short_leading_noise():
    # A stray 1-char artifact above the name is ignored
    text = "RASCALII\nFires explosive projectiles"
    assert OCREngine._count_name_lines("x\n" + text) == 1  # noqa: SLF001


# --- Roman numeral recovery (bug A: tier-I weapons read without a tier) ---

FIXTURES = Path(__file__).parent / "fixtures"
CACHED_CATALOGUE = Path(__file__).parents[1] / "cache" / "catalogue.json"


@pytest.mark.parametrize(
    ("plain", "upper", "expected"),
    [
        ("TEMPEST |", "TEMPEST", "TEMPEST I"),
        ("ANVIL l", "ANVIL", "ANVIL I"),
        ("CANTO II", "CANTOII", "CANTOII"),
        ("RENEGADE Il", "RENEGADE", "RENEGADE II"),
        ("RENEGADE Il", "RENEGADEI", "RENEGADE II"),
        ("HULLCRACKER IV", "HULLCRACKERIV", "HULLCRACKERIV"),
        ("HULLCRACKER lV", "HULLCRACKER", "HULLCRACKER IV"),
        ("TEMPEST |", "TEMPESTI", "TEMPESTI"),
        ("BETTINA I", "BETTINAII", "BETTINAII"),  # plain dropped "II" to "I"
        ("METAL PARTS", "METALPARTS", "METALPARTS"),
        ("METAL PARTS", "METAL PARTS", "METAL PARTS"),
        ("TEMPEST", "TEMPEST", "TEMPEST"),  # never invent a numeral
        ("|", "I", "I"),  # a lone token is not a name plus numeral
    ],
)
def test_merge_numeral(plain, upper, expected):
    assert OCREngine._merge_numeral(plain, upper) == expected  # noqa: SLF001


def test_merge_numeral_keeps_word_ending_in_numeral_letters():
    # The upper pass's trailing "I" belongs to the word, not the tier
    assert OCREngine._merge_numeral("TAXI |", "TAXI") == "TAXI I"  # noqa: SLF001
    assert OCREngine._merge_numeral("TAXI |", "TAXII") == "TAXII"  # noqa: SLF001


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs tesseract")
def test_tier_one_tooltip_reads_with_numeral():
    """Live-test capture of the Tempest I tooltip: the uppercase pass drops
    the lone "I", so the name read as plain "TEMPEST" and resolved to
    nothing."""
    engine = OCREngine()
    image = Image.open(FIXTURES / "tooltip_tempest_i.png").convert("RGB")
    name = engine._read_item_name(engine.preprocess_tooltip(image))  # noqa: SLF001
    assert name == "TEMPEST I"
    if CACHED_CATALOGUE.exists():
        data = json.loads(CACHED_CATALOGUE.read_text(encoding="utf-8"))
        assert Catalogue.from_api_json(data).resolve(name) == "tempest_i"
