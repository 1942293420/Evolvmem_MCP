#!/bin/bash
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$HOME/.claude/hermes-memory"
MODEL_DIR="$DATA_DIR/models"
MODEL_FILE="bge-small-zh-Q5_K_M.gguf"
MODEL_URL="https://huggingface.co/CompendiumLabs/bge-small-zh-Q5_K_M-GGUF/resolve/main/bge-small-zh-Q5_K_M.gguf"

echo "=== Hermes Memory Plugin 安装 ==="
echo ""

# 1. 创建数据目录
mkdir -p "$MODEL_DIR"
echo "[1/5] 数据目录: $DATA_DIR"

# 2. 安装 Python 依赖
echo "[2/5] 安装 Python 依赖..."
pip install usearch llama-cpp-python

# 3. 下载 embedding 模型（如果不存在）
if [ -f "$MODEL_DIR/$MODEL_FILE" ]; then
    echo "[3/5] 模型文件已存在，跳过下载"
else
    echo "[3/5] 下载 embedding 模型 (~50MB)..."
    if command -v wget &>/dev/null; then
        wget -q --show-progress -O "$MODEL_DIR/$MODEL_FILE" "$MODEL_URL"
    elif command -v curl &>/dev/null; then
        curl -L -o "$MODEL_DIR/$MODEL_FILE" "$MODEL_URL"
    else
        echo "错误: 需要 wget 或 curl 下载模型文件"
        echo "请手动下载 $MODEL_FILE 到 $MODEL_DIR/"
        exit 1
    fi
fi

# 4. 保存默认配置
CONFIG_FILE="$DATA_DIR/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "[4/5] 创建默认配置..."
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
    "stop_hook_safe": true
}
EOF
else
    echo "[4/5] 配置文件已存在，跳过"
fi

# 5. 验证安装
echo "[5/5] 验证安装..."
python -c "
import sys
sys.path.insert(0, '$PLUGIN_DIR')
from hermes_memory.config import Config
c = Config()
c.ensure_dirs()
print('  配置加载 OK')
print(f'  数据目录: {c.data_dir}')
print(f'  模型路径: {c.model_path}')
print(f'  DB 路径:  {c.db_path}')
"
echo ""
echo "=== 安装完成 ==="
echo ""
echo "请将以下内容添加到 Claude Code 的 settings.json:"
echo ""
echo '  "mcpServers": {'
echo '    "hermes-memory": {'
echo "      \"command\": \"python\","
echo "      \"args\": [\"-m\", \"hermes_memory.mcp_server\"],"
echo '      "env": {'
echo "        \"PYTHONPATH\": \"$PLUGIN_DIR\""
echo '      }'
echo '    }'
echo '  },'
echo '  "hooks": {'
echo '    "SessionStart": ['
echo '      {'
echo '        "matcher": "",'
echo "        \"hook\": \"python -c \\\"from hermes_memory.hooks import get_session_start_block; print(get_session_start_block())\\\"\","
echo '        "env": {'
echo "          \"PYTHONPATH\": \"$PLUGIN_DIR\""
echo '        }'
echo '      }'
echo '    ]'
echo '  }'
echo ""
