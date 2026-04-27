"""
Standardizer Sub-Agent
=======================
Ensures every generated wiki page meets the minimum quality bar before
it is written to disk.

Two-phase approach (cost-efficient):
  Phase 1 — Python-only checks (zero LLM cost):
    - Required frontmatter fields present and non-empty
    - Required section headers present
    - Minimum body word count
    - Minimum [[WikiLink]] density
    - "See Also" section present

  Phase 2 — Gemini enrichment (only triggered when Phase 1 finds issues):
    - Single LLM call with the page + a precise issue list
    - Fills in ONLY what is missing; never rewrites existing content

Returns the (possibly enriched) content and a list of issues found.
"""

import re
import logging
import sys
from pathlib import Path

import yaml

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from llm    import call_llm      # noqa: E402
from utils import load_prompt       # noqa: E402

log = logging.getLogger("cooking-brain.standardizer")

# ── Per-category schemas ──────────────────────────────────────────────────────

REQUIRED_FRONTMATTER: dict[str, list[str]] = {
    "recipe":       ["title", "cuisine", "difficulty", "techniques", "ingredients"],
    "technique":    ["title", "category", "difficulty"],
    "ingredient":   ["title", "category", "flavour_profile", "substitutes"],
    "cuisine":      ["title", "region"],
    "tool":         ["title", "category"],
    "person":       ["title", "nationality", "known_for"],
    "safety":       ["title", "category"],
    "management":   ["title", "category"],
    "science":      ["title", "category"],
    "other":        ["title"],
    "general_note": ["title"],
}

REQUIRED_SECTIONS: dict[str, list[str]] = {
    "recipe":       ["## Overview", "## Ingredients", "## Method", "## Chef's Notes"],
    "technique":    ["## Overview", "## The Science", "## How to Do It", "## Common Mistakes"],
    "ingredient":   ["## Overview", "## Flavour & Texture", "## How to Buy & Store", "## How to Use"],
    "cuisine":      ["## Overview", "## Signature Dishes", "## Key Ingredients"],
    "tool":         ["## Overview", "## How to Use"],
    "person":       ["## Overview", "## Notable Works"],
    "safety":       ["## Overview", "## Key Hazards", "## Prevention & Controls", "## Best Practices"],
    "management":   ["## Overview", "## Key Concepts", "## Calculations & Formulas", "## Implementation"],
    "science":      ["## Overview", "## The Chemistry", "## Practical Applications", "## What Can Go Wrong"],
    "other":        ["## Overview"],
    "general_note": ["## Overview"],
}


# ── Main agent class ──────────────────────────────────────────────────────────

class StandardizerAgent:
    def __init__(
        self,
        client,
        llm_cfg: dict,
        prompts_dir: Path,
        min_body_words: int = 80,
        min_wiki_links: int = 2,
    ):
        self.client         = client
        self.llm_cfg     = llm_cfg
        self.min_body_words = min_body_words
        self.min_wiki_links = min_wiki_links
        self._prompt        = load_prompt(prompts_dir, "standardize")

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self, category: str, content: str) -> tuple[str, list[str]]:
        """
        Check `content` for completeness and enrich if needed.

        Returns:
            (final_content, issues_found)
            - final_content: original or enriched markdown
            - issues_found:  list of issue strings (empty if page was already complete)
        """
        issues = self._check(category, content)

        if not issues:
            log.info("  [standardizer] Page complete — no enrichment needed.")
            return content, []

        log.info(f"  [standardizer] {len(issues)} issue(s) found — enriching…")
        for issue in issues:
            log.info(f"    • {issue}")

        enriched = self._enrich(category, content, issues)
        return enriched, issues

    # ── Phase 1: Python checks (zero LLM cost) ────────────────────────────────

    def _check(self, category: str, content: str) -> list[str]:
        issues: list[str] = []
        fm, body = _split(content)

        # 1. Required frontmatter fields
        req_fm = REQUIRED_FRONTMATTER.get(category, ["title"])
        for field in req_fm:
            val = fm.get(field)
            if val is None:
                issues.append(f"Missing frontmatter field: '{field}'")
            elif isinstance(val, (list, str)) and not val:
                issues.append(f"Empty frontmatter field: '{field}'")

        # 2. Required section headers
        req_sections = REQUIRED_SECTIONS.get(category, ["## Overview"])
        for section in req_sections:
            # Match section header at start of line (case-insensitive prefix match)
            pattern = re.compile(
                r"^" + re.escape(section), re.MULTILINE | re.IGNORECASE
            )
            if not pattern.search(body):
                issues.append(f"Missing section: '{section}'")

        # 3. Minimum body word count
        word_count = len(body.split())
        if word_count < self.min_body_words:
            issues.append(
                f"Insufficient body content: {word_count} words "
                f"(minimum {self.min_body_words})"
            )

        # 4. Minimum [[WikiLink]] count in body
        wiki_links = re.findall(r"\[\[.+?\]\]", body)
        if len(wiki_links) < self.min_wiki_links:
            issues.append(
                f"Too few cross-references: {len(wiki_links)} [[WikiLinks]] "
                f"(minimum {self.min_wiki_links})"
            )

        # 5. See Also section
        if not re.search(r"^## See Also", body, re.MULTILINE | re.IGNORECASE):
            issues.append("Missing '## See Also' section")

        return issues

    # ── Phase 2: Gemini enrichment ────────────────────────────────────────────

    def _enrich(self, category: str, content: str, issues: list[str]) -> str:
        issues_text = "\n".join(f"- {i}" for i in issues)
        user_content = (
            f"CATEGORY: {category}\n\n"
            f"ISSUES TO FIX:\n{issues_text}\n\n"
            f"CURRENT PAGE CONTENT:\n{content}"
        )
        enriched = call_llm(
            self.client, self.llm_cfg, self._prompt, user_content
        )

        # Safety: all original lines must survive — only additions are allowed
        if not _content_preserved(content, enriched):
            log.warning(
                "  [standardizer] Existing content was removed or rewritten — "
                "keeping original."
            )
            return content

        return enriched


# ── Helpers ───────────────────────────────────────────────────────────────────

def _content_preserved(original: str, enriched: str) -> bool:
    """
    Verify that no original content was removed or rewritten.
    Every substantive line from the original (> 20 chars) must still appear
    verbatim in the enriched output.
    """
    orig_lines = [l.strip() for l in original.splitlines() if len(l.strip()) > 20]
    return all(line in enriched for line in orig_lines)


def _split(content: str) -> tuple[dict, str]:
    """Split markdown into (frontmatter_dict, body_text)."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", content, re.DOTALL)
    if match:
        try:
            fm = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        body = match.group(2)
    else:
        fm   = {}
        body = content
    return fm, body
