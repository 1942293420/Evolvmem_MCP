#!/bin/bash
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$HOME/.claude/evolvmem"
MODEL_DIR="$DATA_DIR/models"
MODEL_FILE="$(PYTHONPATH="$PLUGIN_DIR" python3 -c 'from evolvmem.runtime_contract import DEFAULT_EMBEDDING_CONTRACT; print(DEFAULT_EMBEDDING_CONTRACT.filename)')"
MODEL_URL="$(PYTHONPATH="$PLUGIN_DIR" python3 -c 'from evolvmem.runtime_contract import DEFAULT_EMBEDDING_CONTRACT; print(DEFAULT_EMBEDDING_CONTRACT.download_url)')"

echo "=== EvolvMem Plugin Installation ==="
echo ""

# 1. Create data directories
mkdir -p "$MODEL_DIR"
echo "[1/5] Data directory: $DATA_DIR"

# 2. Install Python dependencies
echo "[2/5] Installing Python dependencies..."
pip install usearch llama-cpp-python "tomlkit>=0.13,<1"

# 3. Download embedding model (if not present)
if [ -f "$MODEL_DIR/$MODEL_FILE" ]; then
    echo "[3/5] Model file already exists, skipping download"
else
    echo "[3/5] Downloading embedding model..."
    if command -v wget &>/dev/null; then
        wget -q --show-progress -O "$MODEL_DIR/$MODEL_FILE" "$MODEL_URL"
    elif command -v curl &>/dev/null; then
        curl -L -o "$MODEL_DIR/$MODEL_FILE" "$MODEL_URL"
    else
        echo "Error: wget or curl required to download the model file"
        echo "Please manually download $MODEL_FILE to $MODEL_DIR/"
        exit 1
    fi
fi

# 4. Save default config
CONFIG_FILE="$DATA_DIR/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "[4/5] Creating default config..."
    EVOLVMEM_DATA_DIR="$DATA_DIR" PYTHONPATH="$PLUGIN_DIR" python3 -c \
        'from evolvmem.config import Config; Config().save()'
else
    echo "[4/5] Config file already exists, skipping"
fi

# 5. Verify installation
echo "[5/5] Verifying installation..."
python3 -c "
import sys
sys.path.insert(0, '$PLUGIN_DIR')
from evolvmem.config import Config
c = Config()
c.ensure_dirs()
print('  Config loaded OK')
print(f'  Data directory: {c.data_dir}')
print(f'  Model path:     {c.model_path}')
print(f'  DB path:        {c.db_path}')
"
echo ""
echo "=== Installation Complete ==="
echo ""
echo "Add the following to your Claude Code settings.json:"
echo ""
echo '  "mcpServers": {'
echo '    "evolvmem": {'
echo "      \"command\": \"python3\","
echo "      \"args\": [\"-m\", \"evolvmem.mcp_server\"],"
echo '      "env": {'
echo "        \"PYTHONPATH\": \"$PLUGIN_DIR\""
echo '      }'
echo '    }'
echo '  },'
echo '  "hooks": {'
echo '    "SessionStart": ['
echo '      {'
echo '        "matcher": "",'
echo "        \"hook\": \"python3 -c \\\"from evolvmem.hooks import get_session_start_block; print(get_session_start_block())\\\"\","
echo '        "env": {'
echo "          \"PYTHONPATH\": \"$PLUGIN_DIR\""
echo '        }'
echo '      }'
echo '    ]'
echo '  }'
echo ""
