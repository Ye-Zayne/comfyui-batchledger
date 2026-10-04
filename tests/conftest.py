import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image

PACKAGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("batchledger_testpkg", PACKAGE / "__init__.py", submodule_search_locations=[str(PACKAGE)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

from batchledger_testpkg.core import Ledger, load_records


@pytest.fixture
def workspace(tmp_path):
    inputs, outputs = tmp_path / "input", tmp_path / "output"
    inputs.mkdir()
    outputs.mkdir()
    Image.new("RGB", (8, 6), (20, 30, 40)).save(inputs / "a.png")
    return inputs, outputs


@pytest.fixture
def ledger(workspace):
    return Ledger(*workspace)


@pytest.fixture
def records(workspace):
    return load_records(workspace[0], base_seed=42)


@pytest.fixture
def graph():
    return {
        "1": {"class_type": "BatchLedgerPlan", "inputs": {"output_folder": "batchledger"}},
        "2": {"class_type": "BatchLedgerNext", "inputs": {"plan": ["1", 0]}},
        "3": {"class_type": "ImageScale", "inputs": {"image": ["2", 0], "width": 8, "height": 6, "crop": "disabled", "upscale_method": "nearest-exact"}},
        "4": {"class_type": "BatchLedgerVerifiedSave", "inputs": {"item": ["2", 5], "images": ["3", 0], "expected_width": 8, "expected_height": 6}},
    }

