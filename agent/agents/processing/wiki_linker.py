"""
WikiLinker Sub-Agent
=====================
Dedicated link-annotation pass. Runs on the generated page AFTER the writer
and AFTER the standardizer. Its only job is to aggressively wrap every
meaningful culinary entity in [[WikiLinks]].

Why a separate agent?
  The writer generates prose and links simultaneously, which makes it
  conservative — linking competes with fluency. By separating the jobs,
  the writer focuses on quality prose and the WikiLinker focuses purely
  on coverage. Result: near-Wikipedia link density on every page.

Linking strategy:
  - First mention within each paragraph (not per section, not per article)
  - Every ingredient, technique, tool, dish, cuisine, person, place,
    texture descriptor, doneness cue, science term, cooking verb, failure mode,
    general food category, and physical concept
  - Never touches YAML frontmatter
  - Never changes prose — only wraps existing text in [[ ]]
"""

import re
import logging
import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from gemini import call_gemini   # noqa: E402
from utils import load_prompt    # noqa: E402

log = logging.getLogger("cooking-brain.wiki_linker")


class WikiLinkerAgent:
    def __init__(self, client, gemini_cfg: dict, prompts_dir: Path):
        self.client     = client
        self.gemini_cfg = gemini_cfg
        self._prompt    = load_prompt(prompts_dir, "wiki_link")

    def run(self, content: str) -> tuple[str, int]:
        """
        Annotate content with [[WikiLinks]].

        Returns:
            (annotated_content, links_added)
        """
        before = len(re.findall(r"\[\[.+?\]\]", content))

        linked = call_gemini(self.client, self.gemini_cfg, self._prompt, content)

        # Safety: de-linked prose must be identical — only [[brackets]] may be added
        if _delinked(linked) != _delinked(content):
            log.warning(
                "  [wiki_linker] Prose was altered (not just linked) — keeping original."
            )
            return content, 0

        # Strip accidental code fences
        linked = re.sub(r"^```(?:markdown)?\s*\n?", "", linked, flags=re.MULTILINE)
        linked = re.sub(r"\n?```$", "", linked, flags=re.MULTILINE)

        after      = len(re.findall(r"\[\[.+?\]\]", linked))
        links_added = max(0, after - before)

        log.info(
            f"  [wiki_linker] {before} → {after} links "
            f"(+{links_added} added)."
        )
        return linked, links_added


# ── Helpers ───────────────────────────────────────────────────────────────────

def _delinked(text: str) -> str:
    """Strip [[WikiLink]] brackets, keep inner text, normalise whitespace."""
    plain = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", text)
    return " ".join(plain.split())
