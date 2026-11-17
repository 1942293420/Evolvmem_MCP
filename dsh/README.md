# DSH 接入 EvolvMem

此目录是 DSH bundle，依赖已安装的 DSH 宿主及其 profile/bundle 机制，不是独立 Web 或 Node 服务。
Python 存储、MCP 与模型配置使用 [主 README](../README.md) 中的同一套 EvolvMem。

## 接入步骤

1. 先完成 Python 安装，并在终端验证 `python -m evolvmem.mcp_server` 能启动。
2. 将本目录作为本地 `dsh-evolvmem` 包安装到目标 DSH profile 的依赖环境。例如在该 profile 的 Node 项目目录执行：

```bash
npm install /absolute/path/evolvmem/dsh
```

3. 按该 DSH 版本的配置机制，将 `dsh-evolvmem` 加入 profile 的 bundles 列表。包中的 `dsh.bundle.patch` 指向 [cordis.patch.yml](cordis.patch.yml)。
4. 将 patch 中 MCP 的 `command`、三个组件的 `python` 改为已安装 EvolvMem 的解释器绝对路径，例如 `/absolute/path/evolvmem/.venv/bin/python`。
5. 在 DSH 启动环境或各组件的 `env` 中设相同的 `EVOLVMEM_DATA_DIR`，并保留 `EVOLVMEM_ADAPTER=dsh`。没有目录覆盖时使用当前用户的 `~/.claude/evolvmem`。

现有 patch 选择 `primary`，适用于已经完成 Core 准备并通过健康检查的数据目录。
仅更改模式不会完成旧库迁移；已有库先读 [切换手册](../docs/codex-context-core-runbook.md) 和 [Core 技术参考](../docs/context-core.md)。
只验收基础 MCP 时，可先在 MCP 条目使用 `legacy`，暂不启用提取/注入组件；这时不会提供完整 Core 经验工具。

6. 重启目标 profile，在新的 DSH 会话中检查 EvolvMem MCP 连接与 `memory_status`；再核对当前模式应提供的工具。

## 四个组件

| patch 条目 | 行为 |
|---|---|
| `mcp-evolvmem` | 启动 Python stdio MCP 服务 |
| `evolvmem-inject` | 注入已有上下文，并按当前实际用户任务查询经验 |
| `evolvmem-extract` | 将有变化的会话交给共享 Python 提取器 |
| `evolvmem-sweep` | 周期检查闲置且尚未提取的会话；每轮数量受配置限制 |

注入和提取失败按现有 fail-open 行为跳过，不能据此声称记忆已保存。
只有设置提取聊天模型后才有自动提取；相同 `llm_credentials.json` 规则见主 README。
启用提取或补扫会把脱敏的会话副本发送给配置的供应商，可能产生 API 费用。
DSH 宿主的聊天登录不会自动成为提取器的 API key。

本子包保留已有的许可证声明；它不代表整个仓库已经指定同一许可证。
