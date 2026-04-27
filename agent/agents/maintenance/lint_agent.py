"""
Lint Sub-Agent
===============
Performs a health check on the wiki vault and returns a structured
report of issues: orphans, stubs, missing links, gaps, contradictions.
"""

import re
import json
import logging
import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from llm    import call_llm                   # noqa: E402
from utils import load_prompt, collect_wiki_pages  # noqa: E402

log = logging.getLogger("cooking-brain.lint")

_EMPTY_REPORT = {
    "orphans":        [],
    "stubs":          [],
    "gaps":           [],
    "contradictions": [],
    "summary":        "No issues found.",
}


class LintAgent:
    def __init__(
        self,
        client,
        llm_cfg:  dict,
        prompts_dir: Path,
        wiki_root:   Path,
        lint_cfg:    dict | None = None,
    ):
        self.client     = client
        self.wiki_root  = wiki_root
        self._prompt    = load_prompt(prompts_dir, "lint")

        # Lint produces large structured JSON — apply all lint_cfg overrides to LLM config
        if lint_cfg:
            self.llm_cfg = {**llm_cfg, **lint_cfg}
            for key, val in lint_cfg.items():
                log.info(f"[lint] {key} overridden to {val}")
        else:
            self.llm_cfg = llm_cfg

    def run(self) -> dict:
        """
        Analyse the vault and return an issues dict.
        """
        pages = collect_wiki_pages(self.wiki_root, include_content=True)

        if not pages:
            log.info("[lint] Wiki is empty — nothing to lint.")
            return _EMPTY_REPORT

        log.info(f"[lint] Inspecting {len(pages)} page(s)…")

        # Build compact payload for Gemini (include content for analysis)
        payload = json.dumps(
            [
                {
                    "title":    p["title"],
                    "category": p["category"],
                    "file":     p["file"],
                    "tags":     p.get("tags", []),
                    "content":  p.get("content", ""),
                }
                for p in pages
            ],
            indent=2,
        )

        response = call_llm(self.client, self.llm_cfg, self._prompt, payload)

        # Strip code fences
        response = re.sub(r"^```(?:json)?\s*", "", response, flags=re.MULTILINE)
        response = re.sub(r"\s*```$",           "", response, flags=re.MULTILINE)

        try:
            report = json.loads(response)
        except json.JSONDecodeError:
            log.warning(f"[lint] Could not parse lint response: {response[:300]}")
            return _EMPTY_REPORT

        # Merge with empty template so callers always have all keys
        return {**_EMPTY_REPORT, **report}
