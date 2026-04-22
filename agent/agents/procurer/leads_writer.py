"""
Leads Writer — formats and writes inbox/leads.md for human review.

Leads below MIN_SCORE are omitted entirely.
Leads are grouped by source type so paywalled items are easy to identify.
"""

import logging
from datetime import date
from pathlib import Path

from .lead import Lead

log = logging.getLogger("cooking-brain.procurer.leads_writer")

MIN_SCORE = 4.0   # combined score threshold — lower leads are not surfaced


class LeadsWriter:
    def __init__(self, inbox_root: Path, min_score: float = MIN_SCORE):
        self.inbox_root = inbox_root
        self.min_score  = min_score

    def write(self, leads: list[Lead], gaps: list[str]) -> Path:
        """Write leads.md and return its path."""
        out_path = self.inbox_root / "leads.md"
        today    = date.today().isoformat()

        qualified    = [l for l in leads if l.combined_score >= self.min_score]
        filtered_out = len(leads) - len(qualified)

        lines = [
            f"# Procurement Leads — {today}",
            "",
            f"> {len(leads)} candidates found. "
            f"{filtered_out} filtered (score < {self.min_score}). "
            f"**{len(qualified)} leads** ready for review.",
            "",
            "**To approve a lead:** change `[ ]` to `[x]` next to its Status, then run:",
            "```",
            "python agent/procure.py --approve",
            "```",
            "Free leads are auto-ingested. Paywalled leads print a download list.",
            "",
        ]

        # Gap summary
        if gaps:
            lines += [
                "## Wiki Gaps Identified",
                "",
                *[f"- {g}" for g in gaps[:30]],
                "",
            ]

        if not qualified:
            lines += ["## No leads met the quality threshold.", ""]
        else:
            # Group by access type so paywall items are visually distinct
            free      = [l for l in qualified if l.access == "free"]
            paywalled = [l for l in qualified if l.access != "free"]

            if free:
                lines += ["## Free Leads", ""]
                for lead in free:
                    lines += self._format_lead(lead)

            if paywalled:
                lines += ["## Paywalled / Library Leads", ""]
                for lead in paywalled:
                    lines += self._format_lead(lead)

        out_path.write_text("\n".join(lines), encoding="utf-8")
        log.info(f"  [leads] Written {len(qualified)} lead(s) to {out_path}")
        return out_path

    def _format_lead(self, lead: Lead) -> list[str]:
        access_note = {
            "free":     "free — auto-ingestible",
            "paywalled":"**paywalled** — download PDF manually, drop in inbox/",
            "library":  "**library access** — obtain via institutional login",
        }.get(lead.access, lead.access)

        return [
            f"### {lead.title}",
            f"- **Status:** [ ] pending",
            f"- **URL:** {lead.url}",
            f"- **Source:** {lead.source_name}  |  **Type:** {lead.source_type}",
            f"- **Access:** {access_note}",
            f"- **Fills gap:** {lead.fills_gap or 'general coverage'}",
            f"- **Scores:** Quality {lead.quality_score:.1f} / Relevance {lead.relevance_score:.1f} / Combined {lead.combined_score:.1f}",
            f"- **Why:** {lead.score_justification}",
            "",
        ]
