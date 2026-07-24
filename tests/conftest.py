"""共享测试夹具。"""

import pytest
import tempfile
import shutil
from pathlib import Path
from hermes_memory.config import Config


@pytest.fixture
def temp_dir():
    """创建临时目录，测试后自动清理。"""
    d = tempfile.mkdtemp()
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def test_config(temp_dir):
    """返回指向临时目录的测试配置。"""
    return Config(data_dir=temp_dir)
