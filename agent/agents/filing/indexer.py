"""
Indexer Sub-Agent
==================
Regenerates wiki/index.md from the current vault state.

Zero Gemini calls. Reads each page's ## Overview to extract a one-sentence
summary, then builds index.md in pure Python grouped by category.

Cost: O(disk reads). Flat regardless of wiki size.
"""

import re
import logging
import sys
from datetime import date
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from utils import collect_wiki_pages   # noqa: E402

log = logging.getLogger("cooking-brain.indexer")

# Display order and labels for categories
_CATEGORY_ORDER = [
    ("recipe",      "Recipes"),
    ("technique",   "Techniques"),
    ("ingredient",  "Ingredients"),
    ("cuisine",     "Cuisines"),
    ("tool",        "Tools"),
    ("person",      "People"),
    ("safety",      "Food Safety"),
    ("management",  "Kitchen Management"),
    ("science",     "Food Science"),
    ("other",       "Other"),
    ("general",     "General Notes"),
]


class IndexerAgent:
    def __init__(self, client, llm_cfg: dict, prompts_dir: Path, wiki_root: Path):
        # client / gemini_cfg / prompts_dir kept for API compatibility but unused
        self.wiki_root = wiki_root

    def run(self, dry_run: bool):
        log.info("Regenerating master index (Python, zero API cost)…")
        pages = collect_wiki_pages(self.wiki_root, include_content=True)

        index_md = _build_index(pages)
        index_path = self.wiki_root / "index.md"

        if dry_run:
            log.info("[indexer] [DRY RUN] Would write: wiki/index.md")
            return

        index_path.write_text(index_md, encoding="utf-8")
        log.info(f"[indexer] Index updated — {len(pages)} page(s) indexed.")


# ── Index builder ─────────────────────────────────────────────────────────────

def _build_index(pages: list[dict]) -> str:
    today = date.today().isoformat()

    # Group by category
    by_cat: dict[str, list[dict]] = {}
    for p in pages:
        cat = p.get("category", "other")
        t = p.get("title", "Untitled")
        p["title"] = t[0] if isinstance(t, list) and t else str(t)
        by_cat.setdefault(cat, []).append(p)

    lines = [
        "---",
        "title: Cooking Brain — Master Index",
        "tags: [index, home]",
        f"date_updated: {today}",
        "---",
        "",
        "# Cooking Brain",
        "",
        f"*{len(pages)} page(s) · last updated {today}*",
        "",
    ]

    for cat_key, cat_label in _CATEGORY_ORDER:
        cat_pages = by_cat.get(cat_key, [])
        if not cat_pages:
            continue

        cat_pages_sorted = sorted(cat_pages, key=lambda p: str(p.get("title", "")).lower())
        lines.append(f"## {cat_label}")
        lines.append("")
        for p in cat_pages_sorted:
            summary = _extract_summary(p.get("content", ""))
            suffix  = f" — {summary}" if summary else ""
            lines.append(f"- [[{p['title']}]]{suffix}")
        lines.append("")

    # Recently added (up to 10, sorted by date_added descending)
    dated = [p for p in pages if p.get("date_added")]
    dated.sort(key=lambda p: str(p["date_added"]), reverse=True)
    recent = dated[:10]
    if recent:
        lines.append("## Recently Added")
        lines.append("")
        for p in recent:
            lines.append(f"- [[{p['title']}]] ({p['date_added']})")
        lines.append("")

    return "\n".join(lines)


def _extract_summary(content: str) -> str:
    """
    Extract the first sentence of the ## Overview section.
    Falls back to first non-empty body line if no Overview found.
    Returns plain text (WikiLink brackets stripped).
    """
    if not content:
        return ""

    # Strip frontmatter
    body = re.sub(r"^---\s*\n.*?\n---\s*\n?", "", content, flags=re.DOTALL)

    # Try ## Overview section
    m = re.search(r"## Overview\s*\n+(.*?)(?:\n##|\Z)", body, re.DOTALL | re.IGNORECASE)
    if m:
        paragraph = m.group(1).strip()
    else:
        # Fall back to first non-empty, non-header line
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("---"):
                paragraph = line
                break
        else:
            return ""

    # Strip [[WikiLink]] brackets
    paragraph = re.sub(r"\[\[([^\]|]+)(?:\|[^\]])?\]\]", r"\1", paragraph)

    # Take first sentence (split on . ! ?)
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    summary = sentences[0].strip() if sentences else paragraph.strip()

    return summary[:200]  # cap length
