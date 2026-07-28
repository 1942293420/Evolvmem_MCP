#!/bin/bash
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$HOME/.claude/evolvmem"
MODEL_DIR="$DATA_DIR/models"
MODEL_FILE="bge-small-zh-Q5_K_M.gguf"
MODEL_URL="https://huggingface.co/CompendiumLabs/bge-small-zh-Q5_K_M-GGUF/resolve/main/bge-small-zh-Q5_K_M.gguf"

echo "=== EvolvMem Plugin Installation ==="
echo ""

# 1. Create data directories
mkdir -p "$MODEL_DIR"
echo "[1/5] Data directory: $DATA_DIR"

# 2. Install Python dependencies
echo "[2/5] Installing Python dependencies..."
pip install usearch llama-cpp-python

# 3. Download embedding model (if not present)
if [ -f "$MODEL_DIR/$MODEL_FILE" ]; then
    echo "[3/5] Model file already exists, skipping download"
else
    echo "[3/5] Downloading embedding model (~50MB)..."
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
    cat > "$CONFIG_FILE" << 'EOF'
{
    "fts_top_k": 20,
    "vector_top_k": 20,
    "fts_weight": 0.6,
    "vector_weight": 0.4,
    "forget_days_threshold": 90,
    "forget_access_count_threshold": 2,
    "forget_rate_limit_days": 7,
    "embedding_dim": 512,
    "inject_max_count": 50,
    "inject_max_chars": 8000,
    "inject_pinned_max_count": 10,
    "inject_pinned_max_chars": 2000,
    "inject_index_max_chars": 1000,
    "inject_key_prefix_quota": 3,
    "inject_w_importance": 0.5,
    "inject_w_recency": 0.3,
    "inject_w_frequency": 0.2,
    "inject_recency_tau_days": 14.0,
    "inject_freq_norm_cap": 20,
    "forget_auto_run_hours": 24,
    "stop_hook_safe": true,
    "value_max_chars": 500
}
EOF
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
