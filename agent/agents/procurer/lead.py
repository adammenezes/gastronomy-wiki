"""
Lead — data class representing a single procurement candidate.
"""

from dataclasses import dataclass, field


@dataclass
class Lead:
    # Core identity
    url: str
    title: str
    source_name: str
    source_type: str        # article | journal | video | reddit | book | rss | podcast
    access: str             # free | paywalled | library | verify
    content_preview: str    # abstract, excerpt, or description — what Gemini scores

    # Flexible per-source metadata
    metadata: dict = field(default_factory=dict)

    # Filled by LeadScorer
    quality_score: float = 0.0
    relevance_score: float = 0.0
    fills_gap: str = ""
    score_justification: str = ""
    combined_score: float = 0.0

    # Content type — classified by Gemini during scoring
    # article   → substantive ingestible text
    # hub_page  → index/nav/chapter-list page; real content is one level deeper
    # book_ref  → landing page for a physical or paywalled book product
    # podcast   → audio/video media; no text to ingest
    # unknown   → Gemini could not determine type from available metadata
    content_type: str = "unknown"

    # Set by user review in leads.md
    status: str = "pending"     # pending | approved | rejected | ingested
