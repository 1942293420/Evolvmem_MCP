"""Shared test fixtures."""

import pytest
import tempfile
import shutil
from pathlib import Path
from evolvmem.config import Config


@pytest.fixture
def temp_dir():
    """Creates a temporary directory, auto-cleaned after test."""
    d = tempfile.mkdtemp()
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def test_config(temp_dir):
    """Returns test configuration pointing to a temporary directory."""
    return Config(data_dir=temp_dir)
