"""
Cooking Brain — Lint CLI
=========================
Run a health check on the wiki vault. Identifies orphan pages, stubs,
missing cross-references, gaps, and contradictions.

Usage:
    python agent/lint.py
    python agent/lint.py --json       # output raw JSON report
"""

import sys
import json
import logging
import argparse
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from utils import load_config           # noqa: E402
from orchestrator import Orchestrator   # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cooking-brain.lint_cli")


def _print_report(report: dict):
    sep = "=" * 60

    print(f"\n{sep}")
    print("  COOKING BRAIN — WIKI HEALTH REPORT")
    print(sep)

    if report.get("summary"):
        print(f"\n{report['summary']}\n")

    # Orphans
    if report.get("orphans"):
        print(f"ORPHAN PAGES ({len(report['orphans'])}):")
        for f in report["orphans"]:
            print(f"  - {f}")

    # Stubs
    if report.get("stubs"):
        print(f"\nSTUB PAGES ({len(report['stubs'])}):")
        for f in report["stubs"]:
            print(f"  - {f}")

    # Missing links
    if report.get("missing_links"):
        print(f"\nMISSING LINKS ({len(report['missing_links'])}):")
        for item in report["missing_links"]:
            print(f"  - {item['file']}: should link to '{item['concept']}' — {item['suggestion']}")

    # Gaps
    if report.get("gaps"):
        print(f"\nSUGGESTED NEW PAGES ({len(report['gaps'])}):")
        for gap in report["gaps"]:
            print(f"  - [{gap['category']}] {gap['title']}: {gap['reason']}")

    # Contradictions
    if report.get("contradictions"):
        print(f"\nCONTRADICTIONS ({len(report['contradictions'])}):")
        for c in report["contradictions"]:
            files = ", ".join(c.get("files", []))
            print(f"  - {files}: {c['issue']}")

    total = (
        len(report.get("orphans", []))
        + len(report.get("stubs", []))
        + len(report.get("missing_links", []))
        + len(report.get("gaps", []))
        + len(report.get("contradictions", []))
    )
    print(f"\n{sep}")
    print(f"  Total issues: {total}")
    print(sep + "\n")


def main():
    parser = argparse.ArgumentParser(description="Cooking Brain — Lint")
    parser.add_argument(
        "--json", action="store_true", help="Output raw JSON report"
    )
    args = parser.parse_args()

    cfg  = load_config()
    orch = Orchestrator(cfg, dry_run=True)   # lint never writes

    log.info("Running wiki health check…")
    report = orch.lint()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)


if __name__ == "__main__":
    main()
