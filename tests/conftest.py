import copy
import json
from pathlib import Path

import pytest

from contracts.policyspec import PolicySpec

SPEC_PATH = Path("data/specs/cafe_demo.json")


@pytest.fixture
def spec_dict():
    return copy.deepcopy(json.loads(SPEC_PATH.read_text()))


@pytest.fixture
def spec(spec_dict):
    return PolicySpec.model_validate(spec_dict)
