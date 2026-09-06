#!/usr/bin/env python3
"""Export the current public source files to a new directory and matching ZIP."""

import argparse
from fnmatch import fnmatch
import os
from pathlib import Path
import shutil
import zipfile


ROOT_FILES = (
    "README.md", "README.txt", "pyproject.toml", "install.sh", ".gitignore",
    "LICENSE", "THIRD_PARTY_NOTICES.md", "migrate_claude_mem.py",
)
PUBLIC_DIRECTORIES = (
    "evolvmem", "tests", "examples", "LICENSES", "dsh", "scripts", ".github",
)
PUBLIC_DOCS = (
    "context-core.md", "codex-context-core-runbook.md", "github-sharing.md",
    "evolvmem-workflow.json",
)
EXCLUDED_DIRECTORIES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".cache", ".superpowers", ".worktrees",
    "build", "dist", "data", "models", "sessions", "session_archives",
    "logs", "backups", "credentials", "secrets", "live", "runtime",
}
EXCLUDED_FILES = {
    "uv.lock", "config.json", "config.yaml", "config.yml", "settings.json",
    "credentials.json", "credentials.yaml", "credentials.yml",
    "llm_credentials.json", "llm_config.json", "secrets.json", "secrets.yaml",
    "secrets.yml", "id_rsa", "id_ed25519", ".ds_store",
}
EXCLUDED_PATTERNS = (
    "*.pyc", "*.pyo", "*.db", "*.db-*", "*.sqlite", "*.sqlite-*",
    "*.sqlite3", "*.sqlite3-*", "*.usearch", "*.gguf", "*.ggml",
    "*.safetensors", "*.onnx", "*.pt", "*.pth", "*.key", "*.pem",
    "*.p12", "*.pfx", "*.log", "*.log.*", "*.jsonl", "*.jsonl.*",
    "*.bak", "*.bak-*", "*.bak.*", "*.backup", "*.orig", "*~",
    "*.part", "*.tmp", "*.deploy-tmp", "*.zip", "*.tar", "*.tar.gz",
    "*.tgz", "*.7z",
)


def _public_file(path: Path) -> bool:
    name = path.name.lower()
    if path.is_symlink() or not path.is_file():
        return False
    if name.startswith(".") and name not in {".gitignore", ".env.example"}:
        return False
    return name not in EXCLUDED_FILES and not any(
        fnmatch(name, pattern) for pattern in EXCLUDED_PATTERNS
    )


def _source_files(source_root: Path) -> list[Path]:
    candidates = [source_root / name for name in ROOT_FILES]
    docs = source_root / "docs"
    if not docs.is_symlink() and docs.is_dir():
        candidates.extend(docs / name for name in PUBLIC_DOCS)
    for name in PUBLIC_DIRECTORIES:
        directory = source_root / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        for current, directories, filenames in os.walk(directory, followlinks=False):
            current = Path(current)
            directories[:] = sorted(
                name for name in directories
                if name.lower() not in EXCLUDED_DIRECTORIES
                and not name.startswith(".")
                and not name.lower().endswith(".egg-info")
                and not (current / name).is_symlink()
            )
            candidates.extend(current / name for name in filenames)
    return sorted(path for path in candidates if _public_file(path))


def export_source(source_root: Path, output_dir: Path) -> tuple[Path, Path]:
    """Copy only public regular files; never overwrite an existing artifact."""
    source_root = Path(source_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError("The source root must be an existing directory, not a symlink")
    source_root = source_root.resolve()
    output = Path(output_dir).absolute()
    archive = output.with_name(output.name + ".zip")
    for target in (output, archive):
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Output already exists: {target}")
    for name in (*PUBLIC_DIRECTORIES, "docs"):
        if output.is_relative_to(source_root / name):
            raise ValueError("Place the export outside public source directories (for example, dist/)")

    files = _source_files(source_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    archive_created = False
    try:
        for source in files:
            destination = output / source.relative_to(source_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        with archive.open("xb") as archive_file:
            archive_created = True
            with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                for source in files:
                    relative = source.relative_to(source_root)
                    zipped.write(output / relative, arcname=(Path(output.name) / relative).as_posix())
    except BaseException:
        shutil.rmtree(output)
        if archive_created:
            archive.unlink(missing_ok=True)
        raise
    return output, archive


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("dist/evolvmem-github"),
        help="New output directory; a sibling .zip is also created (default: dist/evolvmem-github)",
    )
    args = parser.parse_args(argv)
    try:
        output, archive = export_source(Path(__file__).resolve().parents[1], args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Source directory: {output}")
    print(f"ZIP archive: {archive}")
    print("Review the exported files before uploading; no Git history was included.")


if __name__ == "__main__":
    main()
