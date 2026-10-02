#!/usr/bin/env python3
"""CLI runner for the EvolvMem trust-acceptance cases.

Runs the real store/service/recall boundaries over synthetic fixtures in a
throwaway data directory: no production database, no conversation logs, no
model call, no network. Prints one JSON object per case (plus a summary), or a
single JSON array with ``--json``.

    python scripts/memory_trust_acceptance.py
    python scripts/memory_trust_acceptance.py --json
    python scripts/memory_trust_acceptance.py --data-dir /tmp/evolvmem-trust

Exit code 0 only when every case passes. Model-level adoption of recalled
context is explicitly UNVERIFIED and never asserted here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evolvmem.trust_acceptance import (  # noqa: E402
    MODEL_ADOPTION,
    results_as_json,
    run_cases,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="print one JSON array instead of JSON lines")
    parser.add_argument("--data-dir", default=None,
                        help="optional parent directory for synthetic stores")
    args = parser.parse_args(argv)

    results = run_cases(args.data_dir)
    if args.json:
        print(results_as_json(results))
    else:
        for result in results:
            print(json.dumps(result.to_json(), ensure_ascii=False))
    passed = sum(1 for result in results if result.passed)
    summary = {
        "cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "model_adoption": MODEL_ADOPTION,
    }
    print(json.dumps(summary, ensure_ascii=False), file=sys.stderr if args.json else sys.stdout)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
