"""
Cooking Brain — Shared Utilities
=================================
Common helpers used across all sub-agents.
"""

import re
import logging
from datetime import date
from pathlib import Path

import yaml

log = logging.getLogger("cooking-brain.utils")

# ── Category mappings ─────────────────────────────────────────────────────────

CATEGORY_DIR = {
    "recipe":       "recipes",
    "ingredient":   "ingredients",
    "technique":    "techniques",
    "cuisine":      "cuisines",
    "tool":         "tools",
    "person":       "people",
    "safety":       "safety",
    "management":   "management",
    "science":      "science",
    "other":        "other",
    "general_note": ".",
}

CATEGORY_PROMPT = {
    "recipe":       "extract_recipe",
    "ingredient":   "extract_ingredient",
    "technique":    "extract_technique",
    "cuisine":      "extract_recipe",
    "tool":         "extract_ingredient",
    "person":       "extract_ingredient",
    "safety":       "extract_safety",
    "management":   "extract_management",
    "science":      "extract_science",
    "other":        "extract_other",
    "general_note": "extract_recipe",
}

# ── Paths ─────────────────────────────────────────────────────────────────────

_ROOT       = Path(__file__).resolve().parent.parent   # cooking-brain/
_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

# Load .env from project root if present
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key in ("inbox", "processed", "wiki", "templates", "prompts"):
        cfg["paths"][key] = _ROOT / cfg["paths"][key]
    return cfg


# ── Prompt helpers ────────────────────────────────────────────────────────────

def load_prompt(prompts_dir: Path, name: str) -> str:
    return (prompts_dir / f"{name}.txt").read_text(encoding="utf-8").strip()


def inject_date(text: str) -> str:
    return text.replace("<YYYY-MM-DD>", date.today().isoformat())


# ── Slug ──────────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


# ── Wiki page collection ──────────────────────────────────────────────────────

def collect_wiki_pages(wiki_root: Path, include_content: bool = False) -> list[dict]:
    """
    Walk the wiki directory and return metadata (and optionally content) for
    every page except index.md and log.md.
    """
    pages = []
    for md_file in sorted(wiki_root.rglob("*.md")):
        if md_file.name in ("index.md", "log.md"):
            continue
        try:
            text = md_file.read_text(encoding="utf-8")
            fm   = _parse_frontmatter(text)

            rel   = md_file.relative_to(wiki_root)
            parts = rel.parts
            category = parts[0].rstrip("s") if len(parts) > 1 else "general"

            entry = {
                "title":      fm.get("title", md_file.stem),
                "category":   category,
                "tags":       fm.get("tags", []),
                "date_added": fm.get("date_added", ""),
                "file":       str(rel),
                "path":       md_file,
            }
            if include_content:
                entry["content"] = text

            pages.append(entry)
        except Exception as e:
            log.warning(f"Could not parse {md_file}: {e}")
    return pages


def _parse_frontmatter(text: str) -> dict:
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if match:
        return yaml.safe_load(match.group(1)) or {}
    return {}
