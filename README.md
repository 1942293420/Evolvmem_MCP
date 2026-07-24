# Hermes Memory Plugin

Claude Code 完全本地化的三层记忆插件，支持中文 —— FTS5/trigram + HNSW 向量混合检索。

## 功能

- **L0 活跃记忆**：SessionStart 时自动注入 system prompt
- **L1 完整历史**：SQLite + FTS5/trigram 精确检索，支持中文子串匹配
- **L2 语义索引**：USearch HNSW 向量检索，发现换了表达方式的相关记忆
- **自我迭代**：自动提取、冲突检测、访问衰减遗忘

## 快速开始

```bash
./install.sh
```

脚本会自动完成以下步骤：
- 创建 `~/.claude/hermes-memory/` 目录和 `models/` 子目录
- 安装 pip 依赖 usearch 和 llama-cpp-python
- 下载 bge-small-zh-Q5_K_M.gguf (~50MB，如已存在则跳过)
- 生成默认 `config.json` 配置文件
- 验证 Config 模块可正常导入

## 手动配置

在 `~/.claude/settings.json` 中添加 MCP Server 配置：

```json
{
  "mcpServers": {
    "hermes-memory": {
      "command": "python",
      "args": ["-m", "hermes_memory.mcp_server"],
      "env": {
        "PYTHONPATH": "/path/to/hermes-memory-plugin"
      }
    }
  }
}
```

可选：添加 SessionStart hook 用于自动注入活跃记忆：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hook": "python -c \"from hermes_memory.hooks import get_session_start_block; print(get_session_start_block())\"",
        "env": {
          "PYTHONPATH": "/path/to/hermes-memory-plugin"
        }
      }
    ]
  }
}
```

## 工具

| 工具名称 | 说明 |
|---|---|
| `memory_search` | FTS5 + HNSW 混合检索，支持中文 |
| `memory_status` | 查看记忆系统运行状态和统计信息 |
| `memory_add` | 手动写入一条记忆 |
| `memory_replace` | 替换记忆（旧值标记为 superseded） |
| `memory_remove` | 软删除记忆 |

## 数据目录

所有数据存储在 `~/.claude/hermes-memory/` 下：

| 文件/目录 | 说明 |
|---|---|
| `memory.db` | SQLite 数据库，含 FTS5/trigram 索引 |
| `vectors.usearch` | USearch HNSW 向量索引 |
| `models/` | BGE-small-zh Q5_K_M GGUF 模型文件 |
| `config.json` | 检索、遗忘等参数配置 |

## 配置说明

可通过编辑 `~/.claude/hermes-memory/config.json` 调整以下参数：

- `fts_top_k` / `vector_top_k`：FTS5 和向量检索的召回数量，默认各 20
- `fts_weight` / `vector_weight`：混合检索的权重分配，默认 0.6 / 0.4
- `forget_days_threshold`：未访问多少天后可被归档，默认 90 天
- `forget_access_count_threshold`：访问次数低于此值的记忆可被降级，默认 2
- `embedding_dim`：向量维度，需与模型匹配，默认 512

## 依赖

Python 依赖（由 install.sh 自动安装）：

```bash
pip install usearch llama-cpp-python
```

Embedding 模型：BGE-small-zh Q5_K_M GGUF（约 50MB），由 install.sh 自动下载。如需手动下载，请将 `bge-small-zh-Q5_K_M.gguf` 放入 `~/.claude/hermes-memory/models/` 目录。

## 架构

三层记忆结构：活跃记忆（L0，SessionStart 注入 system prompt）-> 精确检索（L1，SQLite + FTS5/trigram）-> 语义检索（L2，USearch HNSW）。记忆通过自动提取、冲突检测和访问衰减实现自我迭代。所有数据本地存储，无需外部服务。
