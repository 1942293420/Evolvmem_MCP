"""Check the public plain-text renderer without depending on README wording."""

from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_readme.py"


def test_sync_keeps_links_tables_and_literal_code_readable(tmp_path):
    source = tmp_path / "README.md"
    output = tmp_path / "README.txt"
    source.write_text(
        "# 项目\n\n**用途**：`memory_search`，见 [配置](examples/config.json)。\n"
        "![界面](screen.png)\n\n| 字段 | 默认值 |\n|---|---|\n| mode | legacy |\n"
        "\n```bash\n# 保留代码注释\nprintf '**literal** `code`'\n```\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(source), "--output", str(output)],
        text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == (
        "项目\n\n用途：memory_search，见 配置 (examples/config.json)。\n"
        "图片：界面 (screen.png)\n\n字段 | 默认值\nmode | legacy\n"
        "\n# 保留代码注释\nprintf '**literal** `code`'\n"
    )


def test_check_detects_drift_without_changing_the_output(tmp_path):
    source = tmp_path / "README.md"
    output = tmp_path / "README.txt"
    source.write_text("# 新内容\n", encoding="utf-8")
    output.write_text("旧内容\n", encoding="utf-8")
    command = [sys.executable, str(SCRIPT), "--source", str(source), "--output", str(output)]

    stale = subprocess.run(command + ["--check"], text=True, capture_output=True)

    assert stale.returncode == 1
    assert output.read_text(encoding="utf-8") == "旧内容\n"
    assert subprocess.run(command, capture_output=True).returncode == 0
    assert subprocess.run(command + ["--check"], capture_output=True).returncode == 0
