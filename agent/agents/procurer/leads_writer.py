"""
Leads Writer — formats and writes inbox/leads.md for human review.

Leads below MIN_SCORE are omitted entirely.
Paywalled leads are EXCLUDED — we only surface free and openly accessible content.

Leads are grouped by content type and access:
  1. Free ingestible articles   (access=free,   content_type=article)
  2. Verify before ingesting    (access=verify, content_type=article)
  3. Hub pages for expansion    (access=free|verify, content_type=hub_page)

Book references and podcasts are EXCLUDED.
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

        # ── Exclude paywalled leads entirely ────────────────────────────────
        paywalled    = [l for l in qualified if l.access in ("paywalled", "library")]
        qualified    = [l for l in qualified if l.access not in ("paywalled", "library")]
        paywall_drop = len(paywalled)

        lines = [
            f"# Procurement Leads — {today}",
            "",
            f"> {len(leads)} candidates found. "
            f"{filtered_out} filtered (score < {self.min_score}). "
            f"{paywall_drop} paywalled (excluded). "
            f"**{len(qualified)} leads** ready for review.",
            "",
            "**To approve a lead:** change `[ ]` to `[x]` next to its Status, then run:",
            "```",
            "python agent/procure.py --approve",
            "```",
            "Free leads are auto-ingested. Verify leads need `python agent/compile.py --url <url>` first.",
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
            # ── Partition by content_type × access ──────────────────────────
            free_articles   = [l for l in qualified
                               if l.access == "free"
                               and l.content_type in ("article", "unknown")]
            verify_articles = [l for l in qualified
                               if l.access == "verify"
                               and l.content_type in ("article", "unknown")]
            hub_pages       = [l for l in qualified
                               if l.content_type == "hub_page"
                               and l.access not in ("paywalled", "library")]

            # ── 1. Free, directly ingestible articles ────────────────────────
            if free_articles:
                lines += ["## Free Leads", ""]
                for lead in free_articles:
                    lines += self._format_lead(lead)

            # ── 2. Verify-before-ingesting articles ─────────────────────────
            if verify_articles:
                lines += [
                    "## Search Discoveries — Verify Before Ingesting",
                    "",
                    "> These URLs were found via web search. **Review each one manually.**",
                    "> To ingest a verified URL: `python agent/compile.py --url <url>`",
                    "",
                ]
                for lead in verify_articles:
                    lines += self._format_lead(lead)

            # ── 3. Hub pages (expandable — HTTP expansion runs on --approve) ─
            if hub_pages:
                lines += [
                    "## Hub Pages — Expandable",
                    "",
                    "> These are index/navigation pages. Approving them triggers",
                    "> automatic link extraction to find the actual articles inside.",
                    "> Mark `[x]` to expand; do NOT run compile.py on these directly.",
                    "",
                ]
                for lead in hub_pages:
                    lines += self._format_lead(lead)

        out_path.write_text("\n".join(lines), encoding="utf-8")
        log.info(f"  [leads] Written {len(qualified)} lead(s) to {out_path}"
                 f" ({paywall_drop} paywalled excluded)")
        return out_path

    def _format_lead(self, lead: Lead, show_approve: bool = True) -> list[str]:
        access_note = {
            "free":    "free — auto-ingestible",
            "verify":  "**verify before ingesting** — `python agent/compile.py --url <url>`",
        }.get(lead.access, lead.access)

        content_tag = {
            "article":  "",
            "hub_page": "  |  **Type:** hub_page (expandable)",
            "book_ref": "  |  **Type:** book reference",
            "podcast":  "  |  **Type:** podcast/media",
            "unknown":  "",
        }.get(lead.content_type, "")

        status_line = (
            f"- **Status:** [ ] pending"
            if show_approve else
            f"- **Status:** [ ] to acquire"
        )

        return [
            f"### {lead.title}",
            status_line,
            f"- **URL:** {lead.url}",
            f"- **Source:** {lead.source_name}  |  **Type:** {lead.source_type}{content_tag}",
            f"- **Access:** {access_note}",
            f"- **Fills gap:** {lead.fills_gap or 'general coverage'}",
            f"- **Scores:** Quality {lead.quality_score:.1f} / Relevance {lead.relevance_score:.1f} / Combined {lead.combined_score:.1f}",
            f"- **Why:** {lead.score_justification}",
            "",
        ]
