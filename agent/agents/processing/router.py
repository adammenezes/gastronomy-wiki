"""
Router Sub-Agent
=================
Evaluates raw text for culinary substance, rejects thin content, and determines
whether to ingest it as a single wiki page or split it into multiple pages.
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

log = logging.getLogger("cooking-brain.router")


class RouterAgent:
    def __init__(self, client, gemini_cfg: dict, prompts_dir: Path):
        self.client      = client
        self.gemini_cfg  = gemini_cfg
        self._prompt     = load_prompt(prompts_dir, "route")

    def run(self, raw_text: str) -> dict:
        """
        Returns a routing plan:
        {
            "evaluation": {...},
            "action": "reject" | "ingest_single" | "split",
            "items": [
                {"title_suggestion": str, "category": str, "content_chunk": str}
            ]
        }
        """
        # Ensure we have enough output tokens for potentially splitting large text chunks
        cfg = self.gemini_cfg.copy()
        if "max_output_tokens" not in cfg:
            cfg["max_output_tokens"] = 8192
        cfg["response_mime_type"] = "application/json"

        response = call_gemini(self.client, cfg, self._prompt, raw_text)

        # Strip markdown code fences Gemini sometimes adds
        response = re.sub(r"^```(?:json)?\s*", "", response, flags=re.MULTILINE)
        response = re.sub(r"\s*```$",           "", response, flags=re.MULTILINE)

        try:
            result = json.loads(response)
        except json.JSONDecodeError:
            log.warning(f"Could not parse routing JSON. Defaulting to reject. Snippet: {response[:200]}")
            result = {
                "evaluation": {
                    "reasoning": "JSON Parsing Error from LLM."
                },
                "action": "reject",
                "items": []
            }

        # Failsafe: if not reject but items is empty, force reject
        action = result.get("action", "reject")
        items = result.get("items", [])
        if action in ("ingest_single", "split") and not items:
            log.warning("Action was not 'reject', but items array was empty. Forcing reject.")
            result["action"] = "reject"
            result.setdefault("evaluation", {})["reasoning"] = "Failsafe: No items extracted."

        eval_data = result.get("evaluation", {})
        score = eval_data.get("final_substance_score", 0.0)
        
        log.info(
            f"  [router] Action: {result.get('action')} | "
            f"Score: {score}/10.0 | "
            f"Items: {len(result.get('items', []))}"
        )
        return result
