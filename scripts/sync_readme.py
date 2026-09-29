#!/usr/bin/env python3
"""Generate README.txt from the Markdown subset used by this repository.

Code blocks stay literal; headings, emphasis and table borders become plain
text, and every link keeps its destination. No Markdown dependency is needed.
"""

import argparse
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]


def plain_text(markdown: str) -> str:
    output: list[str] = []
    fence = None
    for line in markdown.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)[0]
                continue
            if marker.group(1)[0] == fence:
                fence = None
                continue
        if fence is not None:
            output.append(line)
            continue
        if re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", line):
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^>\s?", "", line)
        line = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"图片：\1 (\2)", line)
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        if line.strip().startswith("|") and line.strip().endswith("|"):
            line = " | ".join(cell.strip() for cell in line.strip()[1:-1].split("|"))
        output.append(line.rstrip())
    return "\n".join(output).strip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "README.md")
    parser.add_argument("--output", type=Path, default=ROOT / "README.txt")
    parser.add_argument("--check", action="store_true", help="exit 1 on drift; write nothing")
    args = parser.parse_args(argv)
    rendered = plain_text(args.source.read_text(encoding="utf-8"))
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            print("README.txt is out of date; run python scripts/sync_readme.py", file=sys.stderr)
            return 1
        print("README.txt is synchronized")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"Generated {args.output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
