import json
from pathlib import Path

import pytest

from arc_helper.arctracker.catalogue import Catalogue

FIXTURE = Path(__file__).parent / "fixtures" / "catalogue_subset.json"


@pytest.fixture(scope="session")
def cat():
    return Catalogue.from_api_json(json.loads(FIXTURE.read_text(encoding="utf-8")))
