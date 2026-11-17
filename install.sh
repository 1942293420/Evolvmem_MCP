#!/usr/bin/env bash
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PLUGIN_DIR/.venv"
PYTHON_BIN="python3"
DATA_DIR="${EVOLVMEM_DATA_DIR:-$HOME/.claude/evolvmem}"
WITH_EMBEDDING=false

usage() {
    cat <<'HELP'
Usage: ./install.sh [--with-embedding] [--python PATH] [--venv DIR]

Install EvolvMem in a project-local .venv. The default installation starts
the web console and MCP with full-text search; it downloads no model.

  --with-embedding  Also install llama-cpp-python and download the Nomic
                    F16 embedding model (about 274 MB; may compile C++).
  --python PATH    Python 3.10+ interpreter used to create the environment.
  --venv DIR       Environment location (default: .venv beside this script).
  -h, --help       Show this help without installing anything.

EVOLVMEM_DATA_DIR selects the data directory (default: ~/.claude/evolvmem).
Existing configuration and model files are preserved. Linux/macOS or WSL.
HELP
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-embedding) WITH_EMBEDDING=true; shift ;;
        --python|--venv)
            [[ $# -ge 2 && -n "$2" ]] || { echo "Missing value for $1" >&2; exit 2; }
            if [[ "$1" == --python ]]; then PYTHON_BIN="$2"; else VENV_DIR="$2"; fi
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

check_runtime() {
    "$1" -I - <<'PY'
import os, sqlite3, sys
if sys.version_info < (3, 10) or os.name != "posix":
    sys.exit("EvolvMem requires Python 3.10+ on Linux/macOS (Windows: use WSL).")
try:
    import fcntl
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
        db.execute("SELECT json_extract('{\"ok\":1}', '$.ok')").fetchone()
        db.execute("SELECT value FROM json_each('[1]')").fetchone()
        db.execute("SELECT count(*) FILTER (WHERE 1)").fetchone()
except (ImportError, sqlite3.Error) as exc:
    sys.exit("Runtime check failed: SQLite needs FTS5, JSON and aggregate FILTER "
             "support; POSIX file locking is required. " + str(exc))
print("Python", sys.version.split()[0], "· SQLite", sqlite3.sqlite_version)
PY
}

check_runtime "$PYTHON_BIN"
VENV_DIR="$("$PYTHON_BIN" -I -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$VENV_DIR")"
DATA_DIR="$("$PYTHON_BIN" -I -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$DATA_DIR")"
export EVOLVMEM_DATA_DIR="$DATA_DIR"

if [[ ! -e "$VENV_DIR" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
elif [[ ! -f "$VENV_DIR/pyvenv.cfg" || ! -x "$VENV_DIR/bin/python" ]]; then
    echo "Not a usable virtual environment: $VENV_DIR. Choose another --venv directory." >&2
    exit 1
fi
VENV_PYTHON="$VENV_DIR/bin/python"
check_runtime "$VENV_PYTHON"
"$VENV_PYTHON" -m pip --version >/dev/null 2>&1 || "$VENV_PYTHON" -m ensurepip --upgrade
INSTALL_TARGET="$PLUGIN_DIR"
if "$WITH_EMBEDDING"; then INSTALL_TARGET="$PLUGIN_DIR[embedding]"; fi
"$VENV_PYTHON" -m pip install "$INSTALL_TARGET"

"$VENV_PYTHON" -I - <<'PY'
from evolvmem.config import Config
config = Config()
if config.config_path.exists():
    print("Keeping existing config:", config.config_path)
else:
    config.save()
    print("Created config:", config.config_path)
PY

if "$WITH_EMBEDDING"; then
    MODEL_DIR="$DATA_DIR/models"
    MODEL_FILE="$("$VENV_PYTHON" -I -c 'from evolvmem.runtime_contract import DEFAULT_EMBEDDING_CONTRACT; print(DEFAULT_EMBEDDING_CONTRACT.filename)')"
    MODEL_URL="$("$VENV_PYTHON" -I -c 'from evolvmem.runtime_contract import DEFAULT_EMBEDDING_CONTRACT; print(DEFAULT_EMBEDDING_CONTRACT.download_url)')"
    mkdir -p "$MODEL_DIR"
    if [[ -s "$MODEL_DIR/$MODEL_FILE" ]]; then
        echo "Keeping existing model: $MODEL_FILE"
    else
        DOWNLOAD_TMP="$(mktemp "$MODEL_DIR/$MODEL_FILE.download.XXXXXX")"
        trap 'rm -f "$DOWNLOAD_TMP"' EXIT
        if command -v curl >/dev/null 2>&1; then
            curl --fail --location --output "$DOWNLOAD_TMP" "$MODEL_URL"
        elif command -v wget >/dev/null 2>&1; then
            wget -O "$DOWNLOAD_TMP" "$MODEL_URL"
        else
            echo "Install curl or wget to download the optional embedding model." >&2
            exit 1
        fi
        [[ -s "$DOWNLOAD_TMP" ]] || { echo "Model download is empty" >&2; exit 1; }
        mv "$DOWNLOAD_TMP" "$MODEL_DIR/$MODEL_FILE"
        trap - EXIT
    fi
else
    echo "Embedding model download skipped. Use --with-embedding to enable semantic search."
fi

"$VENV_PYTHON" -I - <<'PY'
from evolvmem import mcp_server, web_server
assert web_server._STATIC_SIGNAL.is_file(), "Web assets are missing from the installation"
print("Installed MCP and web modules import successfully.")
PY

echo ""
echo "Installation complete. Start the local web console:"
printf '  EVOLVMEM_DATA_DIR=%q %q -m evolvmem.web_server\n' "$DATA_DIR" "$VENV_PYTHON"
echo "MCP command (use this interpreter and data directory in your client config):"
printf '  EVOLVMEM_DATA_DIR=%q %q -m evolvmem.mcp_server\n' "$DATA_DIR" "$VENV_PYTHON"
echo "Existing-data Codex cutover: docs/codex-context-core-runbook.md (dry-run first)."
