"""
Logger Sub-Agent
=================
Appends timestamped entries to wiki/log.md — a human-readable changelog
of every ingest, query, and lint run.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

log = logging.getLogger("cooking-brain.logger")

_HEADER = (
    "---\n"
    "title: Cooking Brain — Change Log\n"
    "tags: [log]\n"
    "---\n\n"
    "# Change Log\n\n"
    "Timestamped record of every change to the wiki.\n\n"
)


class LoggerAgent:
    def __init__(self, wiki_root: Path):
        self.log_path = wiki_root / "log.md"
        if not self.log_path.exists():
            self.log_path.write_text(_HEADER, encoding="utf-8")

    # ── Public entry points ───────────────────────────────────────────────────

    def log_ingest(
        self,
        source_file: str,
        category: str,
        title: str,
        wiki_page: str,
        pages_updated: int,
        issues_fixed: list[str] | None = None,
    ):
        ts = self._ts()
        entry = (
            f"## {ts} — Ingest: {title}\n"
            f"- **Source:** `{source_file}`\n"
            f"- **Category:** {category}\n"
            f"- **Wiki page:** `{wiki_page}`\n"
            f"- **Pages cross-linked:** {pages_updated}\n"
            f"- **Standardizer issues fixed:** {len(issues_fixed) if issues_fixed else 0}\n"
        )
        if issues_fixed:
            entry += "".join(f"  - {issue}\n" for issue in issues_fixed)
        entry += "\n"
        self._append(entry)

    def log_query(self, question: str, filed_page: str = ""):
        ts = self._ts()
        entry = f"## {ts} — Query\n- **Question:** {question}\n"
        if filed_page:
            entry += f"- **Answer filed as:** `{filed_page}`\n"
        entry += "\n"
        self._append(entry)

    def log_lint(self, issues: dict):
        ts = self._ts()
        total = (
            len(issues.get("orphans", []))
            + len(issues.get("stubs", []))
            + len(issues.get("missing_links", []))
            + len(issues.get("gaps", []))
            + len(issues.get("contradictions", []))
        )
        entry = (
            f"## {ts} — Lint\n"
            f"- **Issues found:** {total}\n"
            f"  - Orphans: {len(issues.get('orphans', []))}\n"
            f"  - Stubs: {len(issues.get('stubs', []))}\n"
            f"  - Missing links: {len(issues.get('missing_links', []))}\n"
            f"  - Gaps: {len(issues.get('gaps', []))}\n"
            f"  - Contradictions: {len(issues.get('contradictions', []))}\n\n"
        )
        self._append(entry)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ts(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _append(self, text: str):
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(text)
        log.debug("Log entry written.")
