"""Exercise the shipped wheel and installer without downloading an embedding model."""

import email
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    source = tmp_path_factory.mktemp("distribution")
    for name in ("pyproject.toml", "README.md", "THIRD_PARTY_NOTICES.md"):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, source / name)
    if (ROOT / "LICENSES").is_dir():
        shutil.copytree(ROOT / "LICENSES", source / "LICENSES")
    shutil.copytree(ROOT / "evolvmem", source / "evolvmem",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c",
         "from setuptools.build_meta import build_wheel; build_wheel('dist')"],
        cwd=source, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return next((source / "dist").glob("*.whl"))


def test_wheel_contains_the_complete_browser_assets_without_backups(built_wheel):
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())
    expected = {
        str(path.relative_to(ROOT))
        for path in (ROOT / "evolvmem/web_static").rglob("*")
        if path.is_file() and path.suffix in {".html", ".css", ".js", ".png"}
    }
    assert expected <= names, sorted(expected - names)
    assert not any(".bak" in name for name in names)


def test_wheel_keeps_embedding_backend_out_of_the_base_install(built_wheel):
    with zipfile.ZipFile(built_wheel) as archive:
        metadata_name = next(n for n in archive.namelist() if n.endswith("/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
    requirements = metadata.get_all("Requires-Dist", [])
    embedding = [r for r in requirements if r.startswith("llama-cpp-python")]
    assert embedding and all('extra == "embedding"' in r for r in embedding)


def test_wheel_starts_web_and_mcp_without_embedding_backend(built_wheel, tmp_path):
    installed = tmp_path / "installed"
    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(installed)
    script = '''
import importlib.abc, pathlib, sys, threading, urllib.request
from http.server import HTTPServer
class NoEmbedding(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'llama_cpp':
            raise ModuleNotFoundError('embedding backend intentionally absent')
sys.meta_path.insert(0, NoEmbedding())
sys.path.insert(0, sys.argv[1])
from evolvmem.config import Config
from evolvmem.mcp_server import MemoryMCPServer
from evolvmem.web_server import make_handler
import evolvmem
assert pathlib.Path(evolvmem.__file__).is_relative_to(sys.argv[1])
server = MemoryMCPServer(Config())
server.initialize()
server._init_done.set()
assert server.handle_tool_call('memory_status', {})['embedding_loaded'] is False
http = HTTPServer(('127.0.0.1', 0), make_handler(server.context_service))
worker = threading.Thread(target=http.serve_forever, daemon=True)
worker.start()
try:
    for path in ('/', '/designs/signal.css', '/designs/organizer.js',
                 '/insights.js', '/workflow', '/api/stats', '/api/insights'):
        with urllib.request.urlopen('http://127.0.0.1:'+str(http.server_port)+path,
                                    timeout=3) as response:
            assert response.status == 200, path
            assert len(response.read()) > 0, path
finally:
    http.shutdown()
    worker.join(3)
    http.server_close()
    server.shutdown()
print('wheel web and MCP ready without embedding')
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith("EVOLVMEM_")}
    env["EVOLVMEM_DATA_DIR"] = str(tmp_path / "data")
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(installed)],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def installer_probe(tmp_path):
    """Keep Python/config real; replace only environment creation, pip and HTTP."""
    source = tmp_path / "checkout with spaces"
    source.mkdir()
    shutil.copy2(ROOT / "install.sh", source / "install.sh")
    shutil.copytree(ROOT / "evolvmem", source / "evolvmem",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = f'''#!{sys.executable}
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
with open(os.environ['INSTALL_PROBE_LOG'], 'a') as stream:
    stream.write(json.dumps([sys.argv[0], *args])+'\\n')
if args[:2] == ['-m', 'venv']:
    target = pathlib.Path(args[2])
    (target/'bin').mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, target/'bin/python')
    (target/'pyvenv.cfg').write_text('test environment')
elif args[:2] == ['-m', 'pip'] or name == 'pip':
    pass
elif name in ('wget', 'curl'):
    flag = next(flag for flag in ('--output', '-o', '-O') if flag in args)
    pathlib.Path(args[args.index(flag)+1]).write_bytes(b'GGUF test download')
    sys.exit(int(os.environ.get('INSTALL_PROBE_DOWNLOAD_EXIT', '0')))
elif name == 'mkdir':
    if any(not pathlib.Path(arg).resolve().is_relative_to(os.environ['INSTALL_PROBE_ROOT'])
           for arg in args if not arg.startswith('-')):
        sys.exit('installer attempted to create data outside the test directory')
    os.execv('/usr/bin/mkdir', ['mkdir', *args])
else:
    os.execv({sys.executable!r}, [{sys.executable!r}, *[a for a in args if a != '-I']])
'''
    for name in ("python3", "pip", "curl", "wget", "mkdir"):
        file = bindir / name
        file.write_text(shim)
        file.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("EVOLVMEM_")}
    env.update(PATH=str(bindir) + os.pathsep + env["PATH"],
               PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1",
               EVOLVMEM_DATA_DIR=str(tmp_path / "data"),
               INSTALL_PROBE_ROOT=str(tmp_path),
               INSTALL_PROBE_LOG=str(tmp_path / "calls.jsonl"))

    def run(*args):
        result = subprocess.run(["bash", str(source / "install.sh"), *args],
                                cwd=source, env=env, capture_output=True,
                                text=True, timeout=20)
        log = Path(env["INSTALL_PROBE_LOG"])
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    return source, Path(env["EVOLVMEM_DATA_DIR"]), env, run


def test_installer_help_has_no_install_or_data_side_effects(installer_probe):
    source, data, _, run = installer_probe
    result, calls = run("--help")
    assert result.returncode == 0, result.stderr
    assert "--with-embedding" in result.stdout
    assert not calls and not data.exists() and not (source / ".venv").exists()


def test_default_install_uses_venv_keeps_config_and_skips_model(installer_probe):
    source, data, _, run = installer_probe
    data.mkdir()
    config = data / "config.json"
    original = b'{"embedding_dim": 768, "custom_setting": "keep me"}\n'
    config.write_bytes(original)
    result, calls = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (source / ".venv/bin/python").exists()
    assert config.read_bytes() == original
    assert not list(data.rglob("*.gguf"))
    pip_calls = [call for call in calls if call[1:3] == ["-m", "pip"]]
    assert pip_calls and all(call[0] == str(source / ".venv/bin/python") for call in pip_calls)
    assert any(str(source) in call for call in pip_calls)
    assert not any(Path(call[0]).name in ("curl", "wget", "pip") for call in calls)


@pytest.mark.parametrize("download_exit", [0, 22])
def test_embedding_download_promotes_only_successful_temp_file(installer_probe, download_exit):
    source, data, env, run = installer_probe
    env["INSTALL_PROBE_DOWNLOAD_EXIT"] = str(download_exit)
    result, calls = run("--with-embedding")
    assert (result.returncode == 0) == (download_exit == 0), result.stdout + result.stderr
    target = data / "models/nomic-embed-text-v1.5.f16.gguf"
    if download_exit == 0:
        assert target.read_bytes() == b"GGUF test download"
    else:
        assert not target.exists()
    downloads = [call for call in calls if Path(call[0]).name in ("curl", "wget")]
    assert len(downloads) == 1
    assert str(target) not in downloads[0]
    assert not list((data / "models").glob("*.download.*"))
    assert any(str(source) + "[embedding]" in call for call in calls)
