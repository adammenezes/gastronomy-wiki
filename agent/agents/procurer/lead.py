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
    access: str             # free | paywalled | library
    content_preview: str    # abstract, excerpt, or description — what Gemini scores

    # Flexible per-source metadata
    metadata: dict = field(default_factory=dict)

    # Filled by LeadScorer
    quality_score: float = 0.0
    relevance_score: float = 0.0
    fills_gap: str = ""
    score_justification: str = ""
    combined_score: float = 0.0

    # Set by user review in leads.md
    status: str = "pending"     # pending | approved | rejected | ingested
