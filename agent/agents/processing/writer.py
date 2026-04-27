"""
Writer Sub-Agent
=================
Generates a structured wiki page from raw content and writes it to disk.
"""

import logging
import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from llm    import call_llm                              # noqa: E402
from utils import (                                         # noqa: E402
    load_prompt, inject_date, slugify,
    CATEGORY_DIR, CATEGORY_PROMPT,
)

log = logging.getLogger("cooking-brain.writer")


class WriterAgent:
    def __init__(self, client, llm_cfg: dict, prompts_dir: Path, wiki_root: Path):
        self.client      = client
        self.llm_cfg  = llm_cfg
        self.prompts_dir = prompts_dir
        self.wiki_root   = wiki_root

    def generate(self, category: str, raw_text: str, source_url: str | None = None) -> str:
        """Call Gemini to produce the full wiki page markdown."""
        prompt_name = CATEGORY_PROMPT.get(category, "extract_recipe")
        system      = inject_date(load_prompt(self.prompts_dir, prompt_name))
        content     = f"SOURCE URL: {source_url}\n\n{raw_text}" if source_url else raw_text
        return call_llm(self.client, self.llm_cfg, system, content)

    def write(self, category: str, title: str, content: str, dry_run: bool) -> Path:
        """Write (or update) wiki/<subdir>/<slug>.md. Returns the path."""
        subdir   = CATEGORY_DIR.get(category, ".")
        wiki_dir = self.wiki_root / subdir
        wiki_dir.mkdir(parents=True, exist_ok=True)

        output_path = wiki_dir / f"{slugify(title)}.md"

        if dry_run:
            log.info(f"  [writer] [DRY RUN] Would write: {output_path.relative_to(self.wiki_root.parent)}")
            return output_path

        action = "Updating" if output_path.exists() else "Creating"
        log.info(f"  [writer] {action}: {output_path.relative_to(self.wiki_root.parent)}")
        output_path.write_text(content, encoding="utf-8")
        return output_path
