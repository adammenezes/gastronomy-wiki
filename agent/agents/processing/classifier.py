"""
Classifier Sub-Agent
=====================
Determines the content type (recipe, technique, ingredient, …) and
suggests a wiki page title for a raw text input.
"""

import re
import json
import logging
import sys
from pathlib import Path

# Allow importing sibling modules from agent/
_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from gemini import call_gemini   # noqa: E402
from utils import load_prompt    # noqa: E402

log = logging.getLogger("cooking-brain.classifier")


class ClassifierAgent:
    def __init__(self, client, gemini_cfg: dict, prompts_dir: Path):
        self.client      = client
        self.gemini_cfg  = gemini_cfg
        self._prompt     = load_prompt(prompts_dir, "classify")

    def run(self, raw_text: str) -> dict:
        """
        Returns:
            {"category": str, "confidence": float, "title_suggestion": str}
        """
        response = call_gemini(self.client, self.gemini_cfg, self._prompt, raw_text)

        # Strip markdown code fences Gemini sometimes adds
        response = re.sub(r"^```(?:json)?\s*", "", response, flags=re.MULTILINE)
        response = re.sub(r"\s*```$",           "", response, flags=re.MULTILINE)

        try:
            result = json.loads(response)
        except json.JSONDecodeError:
            log.warning(f"Could not parse classification JSON: {response[:200]}")
            result = {
                "category":         "general_note",
                "confidence":       0.0,
                "title_suggestion": "Untitled",
            }

        log.info(
            f"  [classifier] {result.get('category')} "
            f"({result.get('confidence', 0):.2f}) — {result.get('title_suggestion')}"
        )
        return result
