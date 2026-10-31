"""Build an isolated wheel: served static files must survive installation."""
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


def test_wheel_includes_nested_redesign_page(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / 'source'
    source.mkdir()
    for name in ('pyproject.toml', 'README.md', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(root / name, source / name)
    shutil.copytree(root / 'LICENSES', source / 'LICENSES')
    package = source / 'evolvmem'
    package.mkdir()
    shutil.copy2(root / 'evolvmem' / '__init__.py', package / '__init__.py')
    shutil.copytree(root / 'evolvmem' / 'web_static', package / 'web_static')
    result = subprocess.run([sys.executable, '-c',
                             "from setuptools.build_meta import build_wheel; build_wheel('dist')"],
                            cwd=source, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    wheel = next((source / 'dist').glob('*.whl'))
    asset = 'evolvmem/web_static/redesign/index.html'
    with zipfile.ZipFile(wheel) as built:
        assert asset in built.namelist(), '/redesign must work in installed distributions'
        assert built.read(asset) == (root / asset).read_bytes()
