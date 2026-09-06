# 分享到 GitHub

在源码根目录使用 Python 3.10 或更高版本运行：

```bash
python3 scripts/export_source.py --output dist/evolvmem-github
```

脚本仅使用 Python 标准库，生成 `dist/evolvmem-github/` 和
`dist/evolvmem-github.zip`。目录或 ZIP 已存在时会停止；再次导出时换一个输出名称。
它复制当前文件内容，因此会包含尚未提交的前端源码，不读取 Git 历史，也不修改源文件。

## 导出内容

- 根目录：`README.md`、`README.txt`、`pyproject.toml`、`install.sh`、`.gitignore`、
  `LICENSE`（存在时）、`THIRD_PARTY_NOTICES.md`、`migrate_claude_mem.py`。
- 公开目录：`evolvmem/`、`tests/`、`examples/`、`LICENSES/`、`dsh/`、`scripts/`、`.github/`。
- 文档：`docs/context-core.md`、`docs/codex-context-core-runbook.md`、
  `docs/github-sharing.md`、`docs/evolvmem-workflow.json`。

公开目录中的源码、网页和演示图片会保留。脚本排除虚拟环境、缓存、运行数据、模型、
会话、日志、凭据配置、数据库、备份、旧 `uv.lock` 及符号链接。
`docs/superpowers/`、本机工作说明和 `.git/` 不进入导出包。精确名单见
`scripts/export_source.py`。

过滤规则依据路径和文件类型，不对源码中的密钥示例、测试夹具或截图做内容脱敏。
导出前应检查公开文本和图片仅含可分享内容；配置示例使用占位值，截图使用演示数据。
导出目录自带 `.gitignore`，后续仍需检查 Git 暂存内容。

脚本不会选择或生成项目许可证。如果根目录还没有 `LICENSE`，先确认并补充你选择的许可。
`LICENSES/` 和 `THIRD_PARTY_NOTICES.md` 中的第三方声明会原样保留。

## 首次上传

先在你自己的 GitHub 账号下创建一个空仓库。进入导出目录，建立新的 Git 历史：

```bash
cd dist/evolvmem-github
git init -b main
git status --short --untracked-files=all
```

检查文件列表后暂存，再确认暂存的文件与内容：

```bash
git add .
git diff --cached --stat
git diff --cached
git commit -m "Initial public source release"
```

将下面的 `YOUR_ACCOUNT` 和 `YOUR_REPOSITORY` 替换成你自己创建的仓库：

```bash
git remote add origin git@github.com:YOUR_ACCOUNT/YOUR_REPOSITORY.git
git push -u origin main
```

这些命令由你检查后执行；导出脚本不会初始化仓库、提交或推送。
也可将 ZIP 解压后使用其中的文件建立仓库。不要复制原开发目录的 `.git/`。
